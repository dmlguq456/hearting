"""Retryable direct inline route finish transaction."""
from __future__ import annotations
import errno, fcntl, hashlib, json, os, re, stat, subprocess, sys, tempfile, time
import fnmatch, urllib.parse
from pathlib import Path
from typing import Any, Mapping, Sequence
import artifact_producer, artifact_lifecycle, dispatch_terminal_commit

class InlineFinishError(ValueError):
    pass


_SYNTHETIC_WRITE_SCOPES = frozenset({
    "source-scoped", "source/**", "source-alternative/**", "target-artifact",
})


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _scope_matches(path: str, item: str) -> bool:
    """Whether a tracked path intersects one concrete declared source scope."""
    if not item or item in _SYNTHETIC_WRITE_SCOPES:
        return False
    if item.endswith("/**"):
        base = item[:-3].rstrip("/")
        if any(char in base for char in "*?["):
            return fnmatch.fnmatchcase(path, base + "/*")
        return path == base or path.startswith(base + "/")
    base = item.rstrip("/")
    if path == base or path.startswith(base + "/"):
        return True
    return fnmatch.fnmatchcase(path, item)


def _unscoped_tracked_dirt(cwd: str, scope: Sequence[str]) -> list[str]:
    """Fresh tracked paths outside concrete scope; synthetic authority is not a path."""
    status = subprocess.run(
        ["git", "-C", cwd, "status", "--porcelain=v1", "-z", "--untracked-files=no"],
        capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if status.returncode:
        raise InlineFinishError("finish-git-status-unavailable")
    records = status.stdout.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise InlineFinishError("finish-git-status-unavailable")
        paths.append(os.fsdecode(record[3:]))
        if b"R" in record[:2] or b"C" in record[:2]:
            if index >= len(records) or not records[index]:
                raise InlineFinishError("finish-git-status-unavailable")
            paths.append(os.fsdecode(records[index]))
            index += 1
    paths = sorted(set(paths))
    concrete_scope = [item for item in scope
                      if isinstance(item, str) and item not in _SYNTHETIC_WRITE_SCOPES]
    if any(_scope_matches(path, item) for path in paths for item in concrete_scope):
        raise InlineFinishError("finish-scoped-tracked-dirt")
    return paths


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".finish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def _atomic(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_bytes(path, json.dumps(value, sort_keys=True, indent=2).encode() + b"\n")

def _read(path: Path):
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise InlineFinishError("finish-intent-unsafe")
        row = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as exc:
        raise InlineFinishError("finish-intent-corrupt") from exc
    if not isinstance(row, dict) or row.get("schema") != "inline_finish_v1":
        raise InlineFinishError("finish-intent-corrupt")
    return row

def _fault(point: str) -> None:
    if os.environ.get("HEARTING_INLINE_FINISH_CRASH_AT") == point:
        raise InlineFinishError("fault-injected-" + point)

def _registry_lines(jobs: Path, inherited: bool) -> list[str]:
    try:
        return jobs.read_text().splitlines()
    except FileNotFoundError:
        if inherited:
            raise
        return []


def _on_a_branch(cwd, commit) -> bool:
    """True when ``commit`` is on a local or remote-tracking branch, not only the checkout's HEAD.

    Work done in a linked worktree and merged through a PR lands on the remote main before
    anyone moves the shared checkout, and moving it under live routes is not the finisher's job.
    """
    found = subprocess.run(["git", "-C", cwd, "for-each-ref", "--count=1", "--contains", commit,
                            "--format=%(refname)", "refs/heads", "refs/remotes"],
                           capture_output=True, text=True)
    return found.returncode == 0 and bool(found.stdout.strip())


def _shares_source_history(cwd, source, commit) -> bool:
    """True when ``commit`` and the route's sealed ``source`` have a common ancestor.

    The source is where the route started, not a commit the result has to keep: review often
    rebases the branch (its base moved, or a stacked parent was rewritten), which drops the
    sealed commit from the new history while the work is the same. A commit from unrelated
    history, or one this repository does not hold, is still not this route's result.
    """
    return subprocess.run(["git", "-C", cwd, "merge-base", source, commit],
                          capture_output=True).returncode == 0


def select_result_commit(route, supplied, api):
    """Choose observable Git evidence without moving either checkout or changing route identity."""
    source, route_cwd = str(route.get("source_commit") or ""), str(route["cwd"])
    if not api._COMMIT_SHA.fullmatch(source):
        return None, route_cwd, {}  # Non-Git sources never acquire a result commit later.
    caller_cwd = os.getcwd()
    route_head, caller_head = api._head_commit(route_cwd), api._head_commit(caller_cwd)

    def changed(cwd, head):
        if not api._COMMIT_SHA.fullmatch(str(head or "")):
            return False
        done = subprocess.run(["git", "-C", cwd, "rev-parse", source + "^{tree}", head + "^{tree}"],
                              capture_output=True, text=True)
        trees = done.stdout.splitlines()
        return done.returncode == 0 and len(trees) == 2 and trees[0] != trees[1]

    def common(cwd):
        done = subprocess.run(["git", "-C", cwd, "rev-parse", "--git-common-dir"],
                              capture_output=True, text=True)
        if done.returncode or not done.stdout.strip():
            return None
        try:
            path = (Path(cwd) / done.stdout.strip()).resolve()
            stamp = path.stat()
            return {"path": str(path), "device": stamp.st_dev, "inode": stamp.st_ino}
        except OSError:
            return None

    def same(first, second):
        return bool(first and second and (first.get("device"), first.get("inode"))
                    == (second.get("device"), second.get("inode")))

    provenance = route.get("source_repository") or {}
    recorded_common = provenance.get("git_common_dir")
    caller_common, route_common = common(caller_cwd), common(route_cwd)
    caller_repo = same(caller_common, recorded_common or route_common)
    execution_available = Path(route_cwd).is_dir()
    recorded_worktree = provenance.get("worktree")
    if execution_available and recorded_worktree:
        try:
            stamp = Path(route_cwd).stat()
            execution_available = ((stamp.st_dev, stamp.st_ino) ==
                                   (recorded_worktree["device"], recorded_worktree["inode"]))
        except OSError:
            execution_available = False
    if route_common and recorded_common and not same(route_common, recorded_common):
        execution_available = False
    if execution_available:
        status = subprocess.run(["git", "-C", route_cwd, "status", "--porcelain=v1", "-z",
                                 "--untracked-files=normal"], capture_output=True,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        execution_status = ("unavailable" if status.returncode else
                            "dirty" if status.stdout else "clean")
    else:
        execution_status = "missing-or-replaced"
    route_candidate = route_head if execution_available else None
    caller_valid = bool(caller_repo and caller_head
                        and _shares_source_history(caller_cwd, source, caller_head)
                        and _on_a_branch(caller_cwd, caller_head))
    git_cwd, selected_from = route_cwd, "execution-checkout"
    commit = str(supplied or route_candidate or "").lower()
    if supplied:
        selected_from = "explicit"
        if not route_candidate and caller_valid:
            git_cwd = caller_cwd
    elif caller_valid and (
            (not execution_available and recorded_common)
            or (execution_status == "clean" and (not route_candidate or (
                not changed(route_cwd, route_candidate) and changed(caller_cwd, caller_head))))):
        commit, git_cwd, selected_from = caller_head, caller_cwd, "caller-working-directory"
    warnings, messages = [], []
    if execution_status != "clean":
        warnings.append("execution-uncommitted-changes" if execution_status == "dirty"
                        else "execution-checkout-unavailable")
        messages.append("The execution checkout has uncommitted changes; its dirt and scope checks remain in force."
                        if execution_status == "dirty" else
                        "The execution checkout state could not be established; it is not reported as clean.")
    if caller_valid and route_head and caller_head != route_head and commit != route_head:
        warnings.append("checkout-heads-differ")
        messages.append("The execution checkout and caller have different Git heads; the receipt names "
                        "which checkout supplied the selected commit.")
    if not changed(git_cwd, commit):
        warnings.append("unchanged-starting-tree")
        messages.append("The selected commit has no source tree changes from the route start; "
                        "a new source result commit is not established.")
    observation = {"selected_from": selected_from, "git_cwd": git_cwd, "source_commit": source,
                   "execution_cwd": route_cwd, "execution_head": route_head,
                   "execution_available": execution_available, "execution_status": execution_status,
                   "caller_cwd": caller_cwd, "caller_head": caller_head,
                   "caller_repository_proven": caller_repo,
                   "warnings": warnings, "warning_messages": messages}
    return commit, git_cwd, observation


def _finish_tracked_dirt(route, git_cwd, scope, observation):
    # Commit-object access and the original execution directory are distinct.
    # Preserve the old concrete-scope rule on every observed execution edit.
    cwds = [git_cwd]
    if observation.get("execution_available") and route["cwd"] not in cwds:
        cwds.append(route["cwd"])
    return sorted({path for cwd in cwds for path in _unscoped_tracked_dirt(cwd, scope)})


def recorded_head(state: Mapping[str, Any] | None) -> tuple[Any, Any]:
    """The commit and summary digest a finished route records now: its latest update, else its finish."""
    state = state or {}
    intent = state.get("intent") or {}
    updates = state.get("updates") or []
    last = updates[-1] if updates and isinstance(updates[-1], dict) else {}
    return (last.get("commit") or intent.get("commit"),
            last.get("summary_sha256") or intent.get("summary_sha256"))


def _follow_up(cwd, state, commit, sid) -> bool:
    """True when ``commit`` is review fixes on top of this session's finished result.

    It has to descend from the commit the route records now (a fast-forward of the same
    branch) and come from the session that finished it. A rewound, rebased or unrelated
    head, or another session, stays a conflict.
    """
    head, _summary = recorded_head(state)
    return bool(commit and head and commit != head and sid
                and state.get("claimant_session") == sid
                and subprocess.run(["git", "-C", cwd, "merge-base", "--is-ancestor", head, commit],
                                   capture_output=True).returncode == 0)


def registered_route(route, jobs, inherited):
    from dispatch_contract import parse_registry_metadata
    try:
        return any(route["route_id"] in {meta.get("route_id"), meta.get("owner_route_id")}
            for fields in (line.split("\t") for line in _registry_lines(jobs, inherited))
            for meta in [parse_registry_metadata(fields[5]) if len(fields) > 5 else {}])
    except Exception as exc:
        raise InlineFinishError("finish-registry-unreadable") from exc


def admit(route, *, allow_legacy=False):
    """Collect the current caller and registry; the common policy selects finish."""
    from dispatch_contract import DispatchContractError, resolve_global_registry
    from dispatch_parent_completion import interactive_parent_identity
    from route_authority import decide_continuation
    facts = {"allow_legacy": allow_legacy, "registered_caller": bool(
        os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or os.environ.get("AGENT_DISPATCH_DEPTH", "0") != "0")}
    try:
        _harness, facts["sid"] = interactive_parent_identity(os.environ)
    except DispatchContractError as exc:
        facts["identity_error"] = ("finish-caller-harness-ambiguous" if str(exc) == "caller-harness-ambiguous"
                                   else "finish-caller-harness-invalid")
    inherited = bool(os.environ.get("AGENT_DISPATCH_JOBS"))
    jobs = resolve_global_registry(Path(__file__).resolve().parents[1], None, 0, "read").path
    if inherited and not jobs.is_file():
        raise InlineFinishError("finish-registry-unavailable")
    facts["registered_route"] = registered_route(route, jobs, inherited)
    choice = decide_continuation(route, action="finish", facts=facts)
    if not choice.allowed:
        raise InlineFinishError(choice.reason)
    return choice, jobs, inherited


def _evidence_url(raw: str) -> bool:
    """An http(s) locator, not a filesystem spelling that happens to contain `://`."""
    if not raw or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7f for char in raw):
        return False
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def _evidence_locator(kind: str, identity: str, original: Path | None) -> Path:
    """A locator-safe runtime name for one imported evidence identity."""
    suffix = ".url" if kind == "url" else ""
    if kind != "url" and original is not None:
        candidate = original.suffix
        if (re.fullmatch(r"\.[A-Za-z0-9]{1,10}", candidate)
                and candidate not in {".tmp", ".part", ".swp", ".pyc"}):
            suffix = candidate
    return Path("finish-evidence") / (_digest((kind + "\0" + identity).encode())[:16] + suffix)


def _read_evidence_file(path: Path) -> bytes:
    """Read another cycle's regular file through a non-following descriptor."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise InlineFinishError("finish-evidence-not-regular" if exc.errno == errno.ELOOP
                                else "finish-evidence-outside-cycle-or-unreadable") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise InlineFinishError("finish-evidence-not-regular")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        raise InlineFinishError("finish-evidence-outside-cycle-or-unreadable") from exc
    finally:
        os.close(fd)
    if not data:
        raise InlineFinishError("finish-evidence-empty")
    return data


def _cycle_evidence(raw: str, output: Path, root: Path, record: Mapping[str, Any], *,
                    finished: bool, state_path: Path, lock_path: Path,
                    recorded_path: Path | None = None) -> Path:
    """One `--evidence` value as the cycle-local path the transaction records.

    This cycle's regular file is used as it is, another regular file is copied
    byte for byte, and an http(s) URL becomes one deterministic line; both
    imports land under a runtime locator name inside this cycle.  A finished
    finish replays what it recorded and never imports again.
    """
    url = _evidence_url(raw)
    evidence = None if url else Path(raw).absolute()
    rel = inside_rel = resolved = None
    if evidence is not None:
        try:
            rel = evidence.relative_to(output).as_posix()
        except ValueError:
            rel = None
        if rel is not None:
            placed = artifact_producer._placed_locator(
                rel, artifact_producer._output_placements(root, record))
            if placed != rel:
                evidence = output.parent / placed
                rel = placed[len("artifacts/"):] if placed.startswith("artifacts/") else placed
        regular = False
        try:
            regular = not evidence.is_symlink() and stat.S_ISREG(evidence.lstat().st_mode)
        except OSError:
            regular = False
        if regular:
            try:
                resolved = evidence.resolve(strict=True)
            except OSError:
                resolved = None
        if resolved is not None:
            try:
                inside_rel = resolved.relative_to(output.resolve(strict=True)).as_posix()
            except ValueError:
                inside_rel = None
    if finished:
        recorded_rel = ((_read(state_path) or {}).get("intent") or {}).get("evidence_path")
        recorded = output / recorded_rel if isinstance(recorded_rel, str) and recorded_rel else None
        if url:
            candidate = _evidence_locator("url", raw, None).as_posix()
        elif inside_rel is not None:
            candidate = inside_rel
        elif resolved is not None:
            candidate = _evidence_locator("file", str(resolved), evidence).as_posix()
        elif rel is not None and rel != ".." and not rel.startswith("../"):
            candidate = rel
        else:
            candidate = None
        if candidate is None or (recorded is not None and candidate == recorded_rel):
            return recorded_path or recorded or evidence or Path(raw).absolute()
        return output / candidate
    if url:
        data = (raw + "\n").encode("utf-8")
        locator = _evidence_locator("url", raw, None)
    elif inside_rel is not None:
        return evidence
    elif resolved is not None:
        data = _read_evidence_file(evidence)
        locator = _evidence_locator("file", str(resolved), evidence)
    else:
        # Missing, symbolic, or not a regular file: the existing checks below
        # name the refusal exactly as they did before this import existed.
        return evidence
    copy = output / locator
    copy_rel = locator.as_posix()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = _read(state_path)
            if state is None:
                _atomic_bytes(copy, data)
                return copy
            if copy.is_symlink():
                raise InlineFinishError("finish-intent-unsafe")
            if not copy.exists():
                intent = state.get("intent") or {}
                if intent.get("evidence_path") != copy_rel:
                    raise InlineFinishError("finish-intent-conflict")
                if intent.get("evidence_sha256") != _digest(data):
                    raise InlineFinishError("finish-evidence-drift")
                _atomic_bytes(copy, data)
                return copy
            try:
                held = copy.read_bytes()
            except OSError as exc:
                raise InlineFinishError("finish-evidence-drift") from exc
            if held != data:
                raise InlineFinishError("finish-evidence-drift")
            return copy
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def finish(args, route: Mapping[str, Any], route_file: Path, api, *, _admission=None) -> dict[str, Any]:
    root = Path(route["artifact_root"]).resolve(strict=True)
    route_file = Path(route_file)
    if route_file.is_symlink():
        raise InlineFinishError("finish-route-unsafe")
    route_file = route_file.resolve(strict=True)
    _choice, jobs, inherited_jobs = _admission or admit(route)
    sid = _choice.census
    nodes = route.get("nodes") or []
    if dispatch_terminal_commit.require_current_cleanup("inline-finish", target=route_file) is not None:
        raise InlineFinishError("finish-foreign-cleanup-scope")
    node = nodes[-1]
    if not isinstance(node, dict) or not node.get("id"):
        raise InlineFinishError("finish-terminal-node-missing")
    base = root / ".runtime" / "inline-finish" / "v1" / route["route_id"]
    state_path, lock_path = base / "finish.json", base / "finish.lock"
    prior_state = _read(state_path)
    from route_authority import decide_continuation
    historical_false = None
    if not prior_state and api.outcome_path(route_file).exists():
        # Autoclose remains terminal. A historical false is consumable only
        # after this exact route now has a current, fully proven terminal gate.
        try:
            closure = json.loads(api.outcome_path(route_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            closure = {}
        choice = decide_continuation(route, action="finish-outcome", facts={"closure": closure})
        if choice.state == "autoclosed":
            print("capability-route: route already closed automatically; nothing left to finish", file=sys.stderr)
            return {"schema": "finish_receipt_v1", "route_id": route["route_id"], "route_hash": route["route_hash"],
                    "state": "already-closed-automatically", "autoclose": closure["autoclose"]}
        if (not choice.allowed
                or closure.get("route_id") != route["route_id"]
                or closure.get("route_hash") != route["route_hash"]
                or closure.get("route_file") != str(Path(route_file).resolve())
                or closure.get("cwd") != route.get("cwd")):
            raise InlineFinishError("finish-route-already-closed")
        # A registered owner's tuple is that owner's: an inline caller (never a registered one) cannot
        # take it over. An inline tuple is compared below with the intent, marker and binding this
        # finish builds now, before anything is claimed.
        if (any(closure.get(key) is not None for key in ("terminal_commit_id", "terminal_owner_attempt_id"))
                or (closure.get("producer_binding_digest") is not None and closure.get("inline_finish_id") is None)):
            raise InlineFinishError("finish-route-outcome-conflict")
        historical_false = closure
        # `finish` itself is the normal completion consumer: it still validates
        # the evidence, caller, route binding, and cleanup before it publishes
        # the terminal marker and current outcome.
    # A finish that already completed is replayed from what it recorded (§45 D-127):
    # the evidence file, the summary, or a manifest revision may have changed since.
    finished = bool(prior_state and prior_state.get("state") == "finished")
    record = artifact_producer.route_cycle_for(root, route)
    if record is None and prior_state:
        record = artifact_producer.read_cycle_record(root, prior_state.get("intent", {}).get("cycle_id", ""))
    if record is None and not prior_state and historical_false is None:
        # A direct start that never reached its cycle leaves the route without
        # one.  Finish prepares the same cycle with the same call direct start
        # uses, and never when a prior finish or a legacy root already owns the
        # decision (those two keep their existing paths).
        try:
            inactive_legacy = artifact_producer.classify_root(root)["state"] == "inactive-with-legacy"
            if not inactive_legacy:
                artifact_producer.prepare_route_artifact_env(
                    route_file, start=True, jobs=jobs, require_cycle=False)
                record = artifact_producer.route_cycle_for(root, route)
        except artifact_producer.ProducerError as exc:
            raise InlineFinishError("finish-route-cycle-" + exc.code) from exc
    if not record:
        raise InlineFinishError("finish-route-cycle-missing")
    admitted = artifact_producer.cycle_route_admission(root, record, route, finalize=True)
    if not admitted.allow:
        raise InlineFinishError("finish-route-cycle-" + admitted.reason)
    observed_lease = artifact_producer._review_lease_observation(root, record["cycle_id"])
    choice = decide_continuation(route, action="finish-cycle", facts={"deleted": bool(record.get("deleted_at")),
        "finished": finished, "review_live": observed_lease == "live",
        "review_unobservable": observed_lease == "unobservable"})
    if choice.state == "deleted":
        # The cycle was deleted while its route ran (§45 D-126): the route ends as a route with no output.
        return _finish_deleted_cycle(root, route, route_file, record, args, api)
    # A campaign deleted since the finish is the campaign it was; a cycle moved to another campaign is
    # still this route's cycle (§45 D-126), so its campaign's key is not the route's to compare.
    campaign = artifact_producer.campaign_or_tombstone(root, record["campaign_id"])
    expected_key = _route_campaign_key(root, route)
    if not campaign or not expected_key or (
            campaign.get("key") != expected_key and not record.get("moved_at") and not finished):
        raise InlineFinishError("finish-campaign-key-mismatch")
    if not choice.allowed:
        error = InlineFinishError(choice.reason)
        error.retry_when = choice.retry_when
        raise error
    output = artifact_producer.cycle_dir(root, record["campaign_id"], record["cycle_id"], record) / "artifacts"
    # A scoped refine preview belongs to the cycle this finish seals, never to
    # wherever the caller happened to put its summary file.
    api._require_entry_scope_review_preview(route, {"id": "one-shot"}, output)
    recorded_path = None
    if finished:
        try:
            recorded_path = (json.loads(
                (api.completion_dir(route["route_id"]) / (node["id"] + ".json")).read_text(encoding="utf-8")
            ).get("evidence") or {}).get("path")
        except (OSError, ValueError, UnicodeError):
            recorded_path = None
        recorded_path = Path(recorded_path) if isinstance(recorded_path, str) and recorded_path else None
    evidence = _cycle_evidence(
        str(args.evidence), output, root, record, finished=finished,
        state_path=state_path, lock_path=lock_path, recorded_path=recorded_path)
    recorded_intent = (prior_state.get("intent") or {}) if finished else {}
    try:
        if evidence.is_symlink() or not stat.S_ISREG(evidence.lstat().st_mode):
            raise InlineFinishError("finish-evidence-not-regular")
        evidence.resolve(strict=True).relative_to(output.resolve(strict=True))
        evidence_raw = evidence.read_bytes()
    except InlineFinishError:
        if not finished: raise
        evidence_raw = None
    except (OSError, ValueError) as exc:
        if not finished: raise InlineFinishError("finish-evidence-outside-cycle-or-unreadable") from exc
        evidence_raw = None
    if evidence_raw is not None and not evidence_raw: raise InlineFinishError("finish-evidence-empty")
    try: summary_raw = Path(args.summary_file).read_bytes()
    except OSError as exc:
        if not finished: raise InlineFinishError("finish-summary-unreadable") from exc
        summary_raw = None
    if summary_raw is not None and (not summary_raw or b"\0" in summary_raw): raise InlineFinishError("finish-summary-invalid")
    summary_text = ""
    if summary_raw is not None:
        try: summary_text = summary_raw.decode("utf-8").strip()
        except UnicodeError as exc: raise InlineFinishError("finish-summary-invalid") from exc
        if not summary_text: raise InlineFinishError("finish-summary-invalid")
    source = str(route.get("source_commit") or "")
    source_is_sha = bool(api._COMMIT_SHA.fullmatch(source))
    commit, git_cwd, commit_observation = select_result_commit(route, args.commit, api)
    if source_is_sha:
        if not api._COMMIT_SHA.fullmatch(commit):
            raise InlineFinishError("finish-commit-invalid")
        if not _shares_source_history(git_cwd, source, commit) or not _on_a_branch(git_cwd, commit):
            raise InlineFinishError("finish-commit-not-source-descendant")
    else:
        # The route's sealed source kind is authoritative, even if this folder
        # later acquires Git metadata or the caller supplies --commit.
        commit = None
    # Tracked edits inside this direct node's declared scope are not admissible.
    scope = (node.get("write_scope") or [])
    if source_is_sha:
        _finish_tracked_dirt(route, git_cwd, scope, commit_observation)
    else:
        unscoped_tracked_dirt = []
    identity = artifact_lifecycle.read_root_identity(root)
    if not identity: raise InlineFinishError("finish-artifact-root-identity-missing")
    try:
        evidence_rel = evidence.resolve().relative_to(output.resolve()).as_posix()
    except (OSError, ValueError):
        if not finished: raise InlineFinishError("finish-evidence-outside-cycle-or-unreadable")
        evidence_rel = recorded_intent.get("evidence_path")
    intent = {"route_id":route["route_id"], "route_hash":route["route_hash"],
              "artifact_root_id":identity.artifact_root_id, "campaign_key":campaign.get("key"),
              "campaign_id":record["campaign_id"], "cycle_id":record["cycle_id"], "producer_id":record["producer_id"],
              "terminal_node":node["id"], "evidence_path":evidence_rel,
              "evidence_sha256":_digest(evidence_raw) if evidence_raw is not None else recorded_intent.get("evidence_sha256"),
              "summary_sha256":_digest(summary_raw) if summary_raw is not None else recorded_intent.get("summary_sha256"),
              "commit":commit, "cycle_record_digest":dispatch_terminal_commit.cycle_identity_digest(record)}
    # The commit this call names. A finished route may record a later one than its finish
    # (review fixes pushed to the same branch); its sealed record keeps the finish's own.
    result_commit = commit
    if finished and all(recorded_intent.get(key) == value for key, value in intent.items()
                        if key not in {"evidence_sha256", "summary_sha256", "campaign_id", "cycle_record_digest",
                                       "campaign_key", "commit"}):
        # The same finish, asked again after its files or its cycle's campaign moved on:
        # what it recorded stands (the receipt is not rewritten, nor is it cancelled).
        # A different commit is decided under the lock: the head it records now, or a
        # fast-forward of it, and nothing else.
        intent = dict(recorded_intent)
        commit = recorded_intent.get("commit")
    intent_id = _digest(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode())

    def make_binding(marker_digest):
        return {"kind":"inline_producer_binding_v1", "artifact_root_id":identity.artifact_root_id,
                "campaign_key":campaign.get("key"), "campaign_id":record["campaign_id"],
                "cycle_id":record["cycle_id"], "producer_id":record["producer_id"],
                "route_id":route["route_id"], "route_hash":route["route_hash"],
                "cycle_record_digest":dispatch_terminal_commit.cycle_identity_digest(record),
                "terminal_marker_digest":marker_digest, "evidence_sha256":intent["evidence_sha256"],
                "inline_finish_id":intent_id}

    base.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # Re-prove every mutable admission fact while holding the route
            # finish fence.  In particular lstat before reading so a raced
            # symlink replacement cannot redirect evidence bytes.
            try:
                evidence_mode = evidence.lstat().st_mode
                if not stat.S_ISREG(evidence_mode) or evidence.is_symlink():
                    raise InlineFinishError("finish-evidence-not-regular")
                locked_evidence = evidence.read_bytes()
                locked_summary = Path(args.summary_file).read_bytes()
            except OSError as exc:
                if not finished: raise InlineFinishError("finish-evidence-drift") from exc
                locked_evidence, locked_summary = evidence_raw, summary_raw
            if not finished and (locked_evidence != evidence_raw or locked_summary != summary_raw):
                raise InlineFinishError("finish-evidence-drift")
            from route_authority import decide_continuation
            choice = decide_continuation(route, action="finish-registry",
                facts={"registered_route": registered_route(route, jobs, bool(inherited_jobs))})
            if not choice.allowed:
                raise InlineFinishError(choice.reason)
            unscoped_tracked_dirt = (_finish_tracked_dirt(route, git_cwd, scope, commit_observation)
                                     if source_is_sha else [])
            latest = artifact_producer.read_cycle_record(root, record["cycle_id"])
            if (not latest or latest.get("state") not in {"open", "sealed"}
                    or not dispatch_terminal_commit.cycle_identity_matches(
                        latest, intent["cycle_record_digest"], campaign_id=intent.get("campaign_id"))):
                raise InlineFinishError("finish-cycle-drift")
            current_route = json.loads(route_file.read_text(encoding="utf-8"))
            if (current_route.get("route_id") != intent["route_id"]
                    or current_route.get("route_hash") != intent["route_hash"]
                    or api.route_hash(current_route) != intent["route_hash"]):
                raise InlineFinishError("finish-route-drift")
            # The commit this call names; a later one of a finished route descends from the
            # finish's own, so it being on a branch keeps that one on a branch too.
            if source_is_sha and not _on_a_branch(git_cwd, result_commit):
                raise InlineFinishError("finish-commit-drift")
            observed_lease = artifact_producer._review_lease_observation(root, record["cycle_id"])
            choice = decide_continuation(route, action="finish-cycle",
                facts={"review_live": observed_lease == "live",
                       "review_unobservable": observed_lease == "unobservable"})
            if not choice.allowed:
                error = InlineFinishError(choice.reason)
                error.retry_when = choice.retry_when
                raise error
            state = _read(state_path)
            if state and state.get("inline_finish_id") != intent_id:
                prior_intent = state.get("intent") or {}
                if (prior_intent.get("evidence_path") == evidence_rel
                        and prior_intent.get("evidence_sha256") != intent["evidence_sha256"]):
                    raise InlineFinishError("finish-evidence-drift")
                raise InlineFinishError("finish-intent-conflict")
            update = None
            if state and state.get("state") == "finished" and result_commit != recorded_head(state)[0]:
                # Review asked for fixes after the finish and they were pushed to the same PR
                # branch: the same evidence names a later head of this session's result. It
                # is recorded as an update beside the finish, which stays as it was.
                if not (source_is_sha and _follow_up(git_cwd, state, result_commit, sid)):
                    raise InlineFinishError("finish-intent-conflict")
                previous_commit, previous_summary = recorded_head(state)
                update = {"commit": result_commit, "previous_commit": previous_commit,
                          "summary_sha256": (_digest(summary_raw) if summary_raw is not None
                                             else previous_summary),
                          "previous_summary_sha256": previous_summary, "session": sid,
                          "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            replayed_at_entry = state is not None
            marker_file = api.completion_dir(route["route_id"]) / (node["id"] + ".json")
            if not state:
                if historical_false is not None and any(historical_false.get(key) is not None for key in (
                        "inline_finish_id", "terminal_marker_digest")):
                    # The recorded tuple is compared with what this finish holds now, before a claim: for an
                    # inline record the same intent, summary, commit and binding, and for either the marker
                    # already written (a marker this finish would write now has bytes no record could name).
                    held_marker = marker_file.read_bytes() if marker_file.is_file() else None
                    held_digest = _digest(held_marker) if held_marker is not None else None
                    inline = historical_false.get("inline_finish_id") is not None
                    if (held_digest is None or historical_false.get("terminal_marker_digest") != held_digest
                            or (inline and (
                                historical_false.get("inline_finish_id") != intent_id
                                or historical_false.get("summary_digest") != intent["summary_sha256"]
                                or historical_false.get("head_commit") != commit
                                or historical_false.get("producer_binding_digest") != _digest(json.dumps(
                                    make_binding(held_digest), sort_keys=True, separators=(",", ":")).encode())))):
                        raise InlineFinishError("finish-route-outcome-conflict")
                _fault("before-claim")
                state = {"schema":"inline_finish_v1", "inline_finish_id":intent_id, "intent":intent,
                         "claimant_session":sid, "state":"claimed"}
                _atomic(state_path, state)
                _fault("after-claim")
            if state["state"] == "claimed":
                _fault("before-marker")
                if not marker_file.is_file():
                    cmd = [sys.executable, str(api.ROOT/"utilities/capability-route.py"), "complete",
                           "--route", str(route_file), "--node", node["id"], "--evidence", str(evidence)]
                    child_env=os.environ.copy(); child_env["AGENT_INLINE_FINISH_ID"]=intent_id
                    subprocess.run(cmd, cwd=git_cwd, env=child_env, check=True, capture_output=True, text=True)
                marker_raw = marker_file.read_bytes()
                marker = json.loads(marker_raw)
                if (marker.get("route_id") != route["route_id"]
                        or marker.get("route_hash") != route["route_hash"]
                        or marker.get("node_id") != node["id"]
                        or marker.get("completion_gate") != node["completion_gate"]
                        or (marker.get("evidence") or {}).get("path") != str(evidence)
                        or (marker.get("evidence") or {}).get("sha256") != intent["evidence_sha256"]):
                    raise InlineFinishError("finish-marker-evidence-mismatch")
                marker_digest = _digest(marker_raw)
                _fault("after-marker-write")
                state.update(state="node-completed", terminal_marker_digest=marker_digest); _atomic(state_path, state)
                _fault("after-marker")
            else:
                marker_raw = marker_file.read_bytes()
                try:
                    marker = json.loads(marker_raw)
                except (ValueError, UnicodeError) as exc:
                    raise InlineFinishError("finish-marker-corrupt") from exc
                if (marker.get("route_id") != route["route_id"]
                        or marker.get("route_hash") != route["route_hash"]
                        or marker.get("node_id") != node["id"]
                        or marker.get("completion_gate") != node["completion_gate"]
                        or (marker.get("evidence") or {}).get("path") != str(evidence)
                        or (marker.get("evidence") or {}).get("sha256") != intent["evidence_sha256"]):
                    raise InlineFinishError("finish-marker-evidence-mismatch")
                marker_digest = _digest(marker_raw)
                if marker_digest != state.get("terminal_marker_digest"): raise InlineFinishError("finish-marker-drift")
            outcome_path = api.outcome_path(route_file)
            binding = make_binding(marker_digest)
            if state["state"] == "route-closed":
                try:
                    recorded_outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise InlineFinishError("finish-outcome-missing-or-corrupt") from exc
                if _digest(json.dumps(recorded_outcome, sort_keys=True).encode()) != state.get("outcome_digest"):
                    raise InlineFinishError("finish-outcome-drift")
                if recorded_outcome.get("terminal_gate_proven") is False:
                    if not api._outcome_replay_matches(
                            recorded_outcome, route_id=route["route_id"], route_hash=route["route_hash"],
                            terminal_marker_digest=marker_digest, inline_finish_id=intent_id,
                            summary_digest=intent["summary_sha256"], inline_commit=commit,
                            producer_binding_digest=_digest(json.dumps(
                                binding, sort_keys=True, separators=(",", ":")).encode())):
                        raise InlineFinishError("finish-outcome-conflict")
                    # Re-enter the existing close step durably before consuming the false record.
                    # If close publishes and then stops, its normal replay updates this same intent.
                    state.update(state="node-completed")
                    _atomic(state_path, state)
            if state["state"] == "node-completed":
                _fault("before-close")
                if not outcome_path.is_file():
                    outcome, _ = api.close_route(route, route_file, commit, summary_text,
                        allow_unproven=False, expected_terminal_marker_digest=marker_digest,
                        inline_finish_id=intent_id, inline_commit=commit,
                        expected_summary_digest=intent["summary_sha256"],
                        expected_producer_binding_digest=_digest(json.dumps(binding,sort_keys=True,separators=(",", ":")).encode()))
                else:
                    outcome = json.loads(outcome_path.read_text())
                    if outcome.get("terminal_gate_proven") is False:
                        outcome, _ = api.close_route(
                            route, route_file, commit, summary_text, allow_unproven=False,
                            expected_terminal_marker_digest=marker_digest,
                            inline_finish_id=intent_id, inline_commit=commit,
                            expected_summary_digest=intent["summary_sha256"],
                            expected_producer_binding_digest=_digest(
                                json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()))
                    elif (outcome.get("route_hash") != route["route_hash"]
                            or outcome.get("terminal_marker_digest") != marker_digest
                            or outcome.get("inline_finish_id") != intent_id
                            or outcome.get("summary_digest") != intent["summary_sha256"]
                            or outcome.get("producer_binding_digest") != _digest(json.dumps(binding,sort_keys=True,separators=(",", ":")).encode())
                            or outcome.get("head_commit") != commit):
                        raise InlineFinishError("finish-outcome-conflict")
                _fault("after-close-write")
                state.update(state="route-closed", outcome_digest=_digest(json.dumps(outcome,sort_keys=True).encode()))
                _atomic(state_path, state)
                _fault("after-close")
            if binding["terminal_marker_digest"] != marker_digest:
                raise InlineFinishError("finish-marker-drift")
            if state["state"] in {"route-closed", "producer-sealed", "finished"}:
                try:
                    applied_outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise InlineFinishError("finish-outcome-missing-or-corrupt") from exc
                if _digest(json.dumps(applied_outcome, sort_keys=True).encode()) != state.get("outcome_digest"):
                    raise InlineFinishError("finish-outcome-drift")
            if state["state"] == "route-closed":
                _fault("before-finalize")
                crash_manifest = os.environ.get("HEARTING_INLINE_FINISH_CRASH_AT") == "after-manifest"
                artifact_producer.finalize_exact_cycle(root, cycle_id=record["cycle_id"],
                    expected_binding=binding, crash_after_manifest=crash_manifest)
                _fault("after-index")
                verified = artifact_producer.verify_finalized_cycle(root, cycle_id=record["cycle_id"], expected_binding=binding)
                state.update(state="producer-sealed", manifest_digest=verified["manifest_digest"]); _atomic(state_path, state)
                _fault("after-finalize")
            if state["state"] == "producer-sealed":
                _fault("before-receipt")
                # The close this finish made is judged by the revision it recorded; a later
                # document of the cycle (§45 D-124) does not undo it.
                verified = artifact_producer.verify_finalized_cycle(
                    root, cycle_id=record["cycle_id"], expected_binding=binding,
                    expected_manifest_digest=state.get("manifest_digest"))
                if verified.get("manifest_digest") != state.get("manifest_digest"): raise InlineFinishError("finish-seal-drift")
                receipt = {"schema":"finish_receipt_v1", "inline_finish_id":intent_id, "route_id":route["route_id"],
                           "route_hash":route["route_hash"], "cycle_id":record["cycle_id"],
                           "terminal_marker_digest":marker_digest, "manifest_digest":verified["manifest_digest"],
                           "commit":commit, "replay":replayed_at_entry}
                state.update(state="finished", receipt=receipt); _atomic(state_path, state)
                _fault("after-receipt")
            if state.get("state") != "finished": raise InlineFinishError("finish-pending")
            # A durable finished label is only a replay hint. Re-prove the
            # exact public marker, close outcome, and producer seal before
            # returning a successful receipt to any caller.
            if state.get("state") == "finished":
                try:
                    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise InlineFinishError("finish-outcome-missing-or-corrupt") from exc
                expected_binding_digest = _digest(json.dumps(binding, sort_keys=True,
                    separators=(",", ":")).encode())
                if (outcome.get("route_id") != route["route_id"]
                        or outcome.get("route_hash") != route["route_hash"]
                        or outcome.get("inline_finish_id") != intent_id
                        or outcome.get("terminal_marker_digest") != marker_digest
                        or outcome.get("summary_digest") != intent["summary_sha256"]
                        or outcome.get("producer_binding_digest") != expected_binding_digest
                        or outcome.get("head_commit") != commit
                        or outcome.get("terminal_gate_proven") is not True):
                    raise InlineFinishError("finish-outcome-conflict")
                verified = artifact_producer.verify_finalized_cycle(
                    root, cycle_id=record["cycle_id"], expected_binding=binding,
                    expected_manifest_digest=state.get("manifest_digest"))
                if (verified.get("manifest_digest") != state.get("manifest_digest")
                        or state.get("receipt", {}).get("terminal_marker_digest") != marker_digest):
                    raise InlineFinishError("finish-seal-drift")
            if update is not None:
                # Only after the finish it updates is proven again. Its receipt, close and seal keep
                # the finish's own commit (the sealed manifest binds the close's bytes); the update
                # and the summary it names are kept beside them, oldest first.
                if summary_raw is not None:
                    update["summary_file"] = "summary-" + update["summary_sha256"][:16] + ".md"
                    _atomic_bytes(base / update["summary_file"], summary_raw)
                updated = dict(state, updates=[*(state.get("updates") or []), update])
                if len(json.dumps(updated, sort_keys=True, indent=2).encode()) + 1 > 65536:
                    raise InlineFinishError("finish-intent-conflict")  # never past what `_read` accepts
                _atomic(state_path, updated)
                state = updated
            result = dict(state["receipt"]); result["replay"] = replayed_at_entry
            if state.get("updates"):
                result["commit"] = recorded_head(state)[0]
                result["updates"] = list(state["updates"])
            result["unscoped_tracked_dirt"] = unscoped_tracked_dirt
            if commit_observation:
                result["commit_observation"] = {**commit_observation, "recorded_commit": result.get("commit")}
            if finished and verified.get("deleted"):
                # Information only: the cycle (or its campaign) was deleted after the finish.
                result["deleted_since"] = True
            if finished and verified.get("updated_since"):
                # Information only: the stored receipt keeps the revision it finished at.
                result["manifest_updated_since"] = verified["current_manifest_digest"]
            # Route/cycle/receipt are already committed. Producer eligibility
            # decides whether this completed cycle has a spec publication to
            # settle; unrelated code work remains a no-op.
            publication = artifact_producer.completed_spec_publication(
                root, cycle_id=record["cycle_id"], settle=True)
            if publication.get("status") != "not-applicable":
                result["shared_publication"] = publication
                _fault("after-admission")
            # Information only, projected fresh on the first finish and on every replay; the stored
            # receipt and its identity are untouched, and nothing here starts the next leg.
            import route_plan
            next_leg = route_plan.next_leg_for_route(route)
            if next_leg is not None:
                result["next_leg"] = next_leg
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

def _route_campaign_key(root: Path, route: Mapping[str, Any]) -> str | None:
    """The campaign key the route's own selection names: its key, the reserved `_unassigned`
    container for `--unassigned`, or the parent cycle's campaign for `--parent-cycle`."""
    from route_autoclose import campaign_key_of
    key = campaign_key_of(route)
    if key is None and route.get("parent_cycle_id"):
        parent = artifact_producer.read_cycle_record(root, str(route["parent_cycle_id"]))
        if parent:
            key = (artifact_producer.campaign_or_tombstone(root, parent["campaign_id"]) or {}).get("key")
    return key

def _finish_deleted_cycle(root: Path, route: dict, route_file: Path, record: dict, args, api) -> dict:
    """Close the route of a deleted cycle the way the runtime closes one nobody works on: no proof is
    claimed, and the cycle ends with the existing no-output record (the folder is not made again)."""
    try:
        summary = Path(args.summary_file).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        summary = ""
    commit = (str(args.commit or api._head_commit(route["cwd"]) or "").lower() or None
              if api._COMMIT_SHA.fullmatch(str(route.get("source_commit") or "")) else None)
    api.close_route(route, route_file, commit, summary or f"{route.get('capability')} {route.get('slug') or route['route_id']}",
                    allow_unproven=True,
                    autoclose={"reason": "cycle-deleted", "trigger": "finish", "closed_by": "finish",
                               "proof": "not-claimed",
                               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    artifact_producer.finalize(root, cycle_id=record["cycle_id"], state="abandoned",
                               abandon_reason="route-unrecoverable")
    return {"schema": "finish_receipt_v1", "route_id": route["route_id"], "route_hash": route["route_hash"],
            "cycle_id": record["cycle_id"], "state": "cycle-deleted", "terminal_gate_proven": False}


def pending_state(root: Path, route_id: str):
    return _read(Path(root)/".runtime"/"inline-finish"/"v1"/route_id/"finish.json")

def pending_for_cycle(root: Path, cycle_id: str):
    """Find a cycle's unfinished exact finish slot, including a continuation route slot.

    The cycle record retains its begin route, so looking up only that route
    would miss a finish claimed by a verified continuation.
    """
    home = Path(root)/".runtime"/"inline-finish"/"v1"
    if not home.is_dir():
        return None
    matches = []
    for route_home in home.iterdir():
        if route_home.is_symlink() or not route_home.is_dir():
            raise InlineFinishError("finish-intent-unsafe")
        state = _read(route_home/"finish.json")
        if (state and (state.get("intent") or {}).get("cycle_id") == cycle_id
                and state.get("state") != "finished"):
            matches.append(state)
    if len(matches) > 1:
        raise InlineFinishError("finish-cycle-ambiguous")
    return matches[0] if matches else None

def evidence_matches(path: Path, digest: str) -> bool:
    from dispatch_contract import evidence_digest
    return evidence_digest(path) == digest

def assert_not_pending(root: Path, route_id: str) -> None:
    state = pending_state(root, route_id)
    if state and state.get("state") != "finished": raise InlineFinishError("finish-in-progress")
