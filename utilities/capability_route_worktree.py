"""Capability route: worktrees, attempts and compose receipts.

Stage blocks, isolated worktrees and their preparation, git diagnosis for
compose, route attempts, the caller's open route, route chains, compose plans,
receipts and snapshots, and publishing a composed route.

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import hashlib
import json
import locale
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from hearting_gates import note_same_work_drift
import git_index_lock
import route_authority as ROUTE_AUTHORITY
import owner_write_advisory as OWNER_WRITE_ADVISORY
from dispatch_contract import (
    diagnose_repository_start, DispatchContractError, parse_registry_metadata,
)

from capability_route_base import (
    _git, _route_chain_identity, _route_chain_module, atomic_write, canonical_route_path,
    classify_route_location, ROOT, route_path_is_exact, TOPO, UI_TEXT, write_once,
)

from capability_route_continuation import (
    _node_mutates_worktree,
)

from capability_route_compose import (
    _compose_default_jobs, _record_access_change, ROUTE_FRAME_CAPABILITY,
)

from capability_route_scope import (
    compose_campaign_selection,
)

from capability_route_verify import (
    verify_route,
)

from capability_route_markers import (
    _find_attempt_row_metadata,
)

from capability_route_revision import (
    _require_entry_scope_review_preview,
)

from capability_route_close import (
    close_route, outcome_path, retire_stale_closure, route_status,
)

from capability_route_completion import (
    complete_node,
)


def publish_composed_route(route, artifact_root, *, plan=None):
    """The compose CLI's write tail for an already compiled route: canonical write-once,
    owner binding and one route-chain line. Returns the canonical route path."""
    shim = argparse.Namespace(command="compose", start=True, output=None, owner=None, full_record=False)
    shim._route_chain_plan = (plan, "explicit" if plan else None)
    return _emit_compiled_route(shim, route, artifact_root)

def stages_block(registry, recipe):
    """One recipe's parts for `stages` and the frame catalog (SD-165, 13.64.5).

    Recipe stages in recipe order, then the recipe's optional catalog parts,
    then the parts of other recipes this host can borrow. The catalog is the
    only source: a frame assembles a graph from this output alone.
    """
    capability=recipe["capability"]
    group_by_node={g.get("node"):g.get("id") for g in (recipe["standard_plus"].get("parallel_groups") or [])}
    gate_by_node={}
    for row in recipe.get("human_gate_bindings") or []:
        gate_by_node.setdefault(row.get("node"), []).append(row.get("gate"))
    aliases=(TOPO.part_catalog(registry).get("frame") or {}).get("aliases") or []
    def part_view(part_id,node,origin,*,optional=False,local=True):
        catalog_row=TOPO.part_row(registry,part_id)
        placement=catalog_row.get("optional") or {}
        produced={out for other in origin["standard_plus"]["nodes"] for out in other.get("outputs") or []}
        return {
            "id":node["id"] if local else part_id,"unit":node.get("unit"),
            "unit_choices":node.get("unit_choices") or catalog_row.get("unit_choices") or [],
            "parallel_group":group_by_node.get(node["id"]) if local else None,
            "human_gates":gate_by_node.get(node["id"], []) if local else [],
            "terminal":local and node.get("terminal") is True,
            "part":part_id,"summary":catalog_row.get("summary",""),"kind":node.get("kind"),
            "inputs":list(node.get("inputs") or []),
            "external_inputs":[name for name in node.get("inputs") or []
                               if name not in produced and not TOPO._is_semantic_output(name)],
            "optional_inputs":list(catalog_row.get("optional_inputs") or []),
            "outputs":list(node.get("outputs") or []),
            "shareable":catalog_row.get("shareable") is True,
            "start_approval":catalog_row.get("start_approval"),
            "optional":optional,
            "after":list(placement.get("after") or []),"before":list(placement.get("before") or []),
            "frame_alias":local and node["id"] in aliases and node.get("unit")=="plan/frame",
        }
    nodes=[part_view(f"{capability}:{node['id']}",node,recipe) for node in recipe["standard_plus"]["nodes"]]
    nodes+=[part_view(f"{capability}:{stage}",row["optional"]["node"],recipe,optional=True)
            for stage,row in TOPO.recipe_optional_parts(registry,recipe)]
    borrowable=[]
    for part_id in TOPO.borrowable_parts(registry,recipe):
        origin,node=TOPO.part_recipe(registry,part_id)
        borrowable.append(part_view(part_id,node,origin,optional="optional" in TOPO.part_row(registry,part_id),
                                    local=False))
    return {"capability":capability,"modes":list(recipe["modes"]),
            "topology_class":recipe["topology_class"],"nodes":nodes,"borrowable":borrowable,
            "frame_brief_inputs":TOPO.frame_brief_inputs(registry,capability)}


def _isolates_worktree(route):
    """Whether the selected route changes source, independent of artifact-root location."""
    return any(_node_mutates_worktree(node) for node in route.get("nodes") or [])


def _compose_git_diagnosis(cwd):
    """Keep an explicit start-time distinction between a non-repository and Git failure."""
    return diagnose_repository_start(cwd)


def _decide_non_git_compose(cwd_diagnosis, requested_shape,
                            dispatch_depth, registered_attempt, capability_hint):
    """The single compose-start decision for Git, non-Git direct, and non-Git refusal.

    `requested_shape` is the shape the start will run: a readable route-plan leg's own
    shape, else `--shape`. Only an explicit direct shape runs without a worker; an omitted
    shape follows the Git default (framed), so the frame decides as in a Git folder."""
    common = {"cwd": str(cwd_diagnosis.get("cwd") or ""),
              "capability_hint": capability_hint}
    state = cwd_diagnosis.get("state")
    if state == "git":
        return {**common, "kind": "git"}
    if state != "non-git":
        detail = cwd_diagnosis.get("detail") or "git diagnosis unavailable"
        return {**common, "kind": "error", "detail": f"{common['cwd']}: {detail}"}
    t = UI_TEXT.translator()
    cause = t("route.nongit.cause", cwd=common['cwd'])
    if requested_shape != "direct":
        return {**common, "kind": "worker-candidate", "cause": cause,
                "next": t("route.nongit.next_worker")}
    if dispatch_depth > 0 or registered_attempt:
        return {**common, "kind": "refuse", "cause": cause,
                "next": t("route.nongit.next_refuse")}
    return {**common, "kind": "direct", "cause": cause}


def _classify_non_git_task(text):
    """Conservative pure task-purpose classifier; called only for non-Git starts."""
    if not isinstance(text, str) or not text.strip():
        return None
    if re.search(r"(?:\b(?:not|don't|do not|without|instead of)\b|아니|않|말고|제외)", text, re.I):
        return None
    if any(mark in text for mark in ('"', "'", "“", "”", "‘", "’", "`")):
        return None
    if re.search(r"\b(?:research)\b|연구|조사", text, re.I):
        if not re.search(r"문헌 검토|논문 조사|literature review|paper research|academic research|시장 조사|시장 분석|market research|market survey|기술 동향|기술 조사|technology research|technical research|technology survey", text, re.I):
            return None
    patterns = {
        ("autopilot-research", "academic"): r"문헌 검토|논문 조사|literature review|paper research|academic research",
        ("autopilot-research", "market"): r"시장 조사|시장 분석|market research|market survey",
        ("autopilot-research", "technology"): r"기술 동향|기술 조사|technology research|technical research|technology survey",
        ("autopilot-lab", "eval"): r"고정 (?:metric|metrics|결과)|모델 비교|결과 비교|재평가|compare (?:the )?(?:model|results)|fixed (?:metric|results)|reevaluate|re-evaluate|experiment conclusions",
        ("autopilot-code", "dev"): r"재사용 (?:프로그램|도구|driver)|구현(?:해|한다|하기|$)|함수 버그 수정|implement a reusable|fix (?:the )?(?:function|path) bug",
        ("autopilot-refine", "default"): r"오탈자|문장만 수정|fix only typos?|typos? only|fix only the wording|correct only",
        ("autopilot-draft", "presentation"): r"독립 (?:새 )?발표|새 발표문|new independent presentation|write an independent presentation",
        ("autopilot-draft", "paper"): r"독립 (?:새 )?논문|새 논문 작성|new independent paper|write an independent paper",
        ("analyze-project", "code"): r"기존 코드.*(?:구조|분석)|inspect existing (?:project )?code|analyze (?:the )?existing codebase",
        ("analyze-project", "doc"): r"기존 문서.*(?:구조|분석)|inspect existing documents|analyze existing documentation",
        ("analyze-project", "paper"): r"기존 논문.*(?:구조|분석)|inspect existing papers?|analyze existing paper structure",
        ("autopilot-ship", "package"): r"주어진 결과.*zip|결과.*패키징|package existing results|make (?:a )?zip from (?:the )?existing",
    }
    matches = [(capability, mode) for (capability, mode), pattern in patterns.items()
               if re.search(pattern, text, re.I)]
    return matches[0] if len(matches) == 1 else None


def _git_common_dir(cwd):
    """Absolute common git dir; git < 2.31 lacks --path-format, so absolutize a relative answer."""
    value = _git(cwd, "rev-parse", "--git-common-dir")
    if not value:
        return value
    return value if os.path.isabs(value) else os.path.normpath(os.path.join(str(cwd), value))


def _worktree_git(cwd, *args):
    """Run one preparation command, retaining stderr for a fail-closed receipt."""
    try:
        argv = ["git", "-C", str(cwd), *args]
        raw = subprocess.run(argv, text=False, capture_output=True, timeout=120, check=False)
        encoding = locale.getpreferredencoding(False) or "utf-8"
        result = subprocess.CompletedProcess(
            raw.args, raw.returncode,
            git_index_lock.compatibility_text(raw.stdout, encoding, failed=raw.returncode != 0),
            git_index_lock.compatibility_text(raw.stderr, encoding, failed=raw.returncode != 0))
        result.raw_stdout, result.raw_stderr = raw.stdout, raw.stderr
        if raw.returncode:
            result.git_failure = git_index_lock.failure_record(
                raw.returncode, raw.stdout, raw.stderr, encoding)
            diagnosis = git_index_lock.diagnose_failure(
                argv, str(cwd), raw.returncode, raw.stdout, raw.stderr)
            result.git_diagnosis = diagnosis if diagnosis and diagnosis.get("state") != "unbound" else None
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        result = subprocess.CompletedProcess(["git", *args], 127, "", str(exc))
        result.git_uncertain = True
        return result


def _primary_worktree(cwd, common):
    """Find the primary worktree belonging to this common Git directory."""
    # The ordinary common directory identifies the primary directly. Do not
    # inspect every unrelated linked checkout just to rediscover that identity.
    common = Path(os.path.realpath(common))
    if common.name == ".git":
        configured = _worktree_git(cwd, "config", "--get", "core.worktree")
        bare = _worktree_git(cwd, "config", "--bool", "--get", "core.bare")
        if configured.returncode == 1 and bare.returncode == 0 and bare.stdout.strip() == "false":
            candidate = str(common.parent)
            actual = _git_common_dir(candidate)
            if actual and os.path.realpath(actual) == str(common):
                return candidate
    current = _git(cwd, "rev-parse", "--show-toplevel")
    if not current:
        return None
    if OWNER_WRITE_ADVISORY.git_topology(cwd) == "primary":
        return os.path.realpath(current)
    result = _worktree_git(cwd, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return None
    candidates = [line.partition(" ")[2] for line in result.stdout.splitlines()
                  if line.startswith("worktree ")]
    for candidate in candidates:
        root = _git(candidate, "rev-parse", "--show-toplevel")
        candidate_common = _git_common_dir(candidate)
        if root and candidate_common and os.path.realpath(candidate_common) == os.path.realpath(common):
            if OWNER_WRITE_ADVISORY.git_topology(root) == "primary":
                return os.path.realpath(root)
    return None


def _moved_preparation(route, top):
    """The preparation record that moved this route out of the person's folder, or None.

    Only a record this route's own start wrote qualifies: its worktree is `top`, its base is the
    route's sealed `source_commit` (compose runs in the prepared worktree), and its origin lies
    outside it. An approved linked task, a retained linked worktree and a start that prepared
    nothing write no such record, so their own folder is never reported as somewhere else."""
    folder = Path(route.get("artifact_root") or "") / ".runtime" / "worktree-preparation"
    base = route.get("source_commit")
    needle = json.dumps(str(top), ensure_ascii=False).encode("utf-8")   # as `atomic_write` writes it
    try:
        entries = list(os.scandir(folder)) if route.get("artifact_root") else []
    except OSError:
        return None
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry.path, "rb") as handle:
                data = handle.read()
            if needle not in data:  # cheap filter before parsing every record
                continue
            record = json.loads(data)
        except (OSError, ValueError):
            continue
        origin = record.get("origin_cwd") if isinstance(record, dict) else None
        if (isinstance(origin, str) and origin and record.get("remote_default") != "approved-linked-task"
                and os.path.realpath(record.get("path") or "") == os.path.realpath(top)
                and record.get("base_sha") == base
                and os.path.commonpath([os.path.realpath(origin), os.path.realpath(top)])
                != os.path.realpath(top)):
            return record
    return None


def _result_location_line(route):
    """One plain closing line naming where a route's source change is when its start moved the
    work out of the person's folder, or "".

    A source start from a primary checkout runs in a prepared `<repo>-wt/<slug>` worktree from
    the fetched remote default, and leaves the person's folder untouched, so a person who reads
    only the agent's last message looks for the change in the folder they started from.
    Read-only, no new record. "" for an unversioned route (no git at all), a route that ran in
    the person's own folder, a worktree with nothing changed since its base, a change already in
    that folder, or any git failure. It names the place, branch and commit state only: no merge
    command, since a worktree rebased onto or merged with upstream would carry those commits too."""
    cwd, base = route.get("cwd"), route.get("source_commit")
    if base == "unversioned" or not cwd or not os.path.isdir(cwd):
        return ""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    head = _git(top, "rev-parse", "HEAD") if top else None
    pending = _git(top, "status", "--porcelain") if head else None
    if pending is None or (head == base and not pending):
        return ""   # nothing this work changed (or unreadable)
    git_dir, common = _git(top, "rev-parse", "--git-dir"), _git_common_dir(top)
    if not git_dir or not common or os.path.realpath(os.path.join(top, git_dir)) == os.path.realpath(common):
        return ""   # the primary checkout is the person's own folder; no record scan
    record = _moved_preparation(route, os.path.realpath(top))
    origin = _git(record["origin_cwd"], "rev-parse", "--show-toplevel") if record and os.path.isdir(
        record["origin_cwd"]) else None
    if not origin or os.path.realpath(origin) == os.path.realpath(top):
        return ""
    origin_head = _git(origin, "rev-parse", "HEAD")
    if not pending and origin_head and _worktree_git(
            top, "merge-base", "--is-ancestor", "HEAD", origin_head).returncode == 0:
        return ""   # already in the person's folder
    branch = _git(top, "symbolic-ref", "-q", "--short", "HEAD")
    label = (f"branch {branch}" if branch else "detached HEAD") + (", not committed yet" if pending else "")
    return f"result: the change is in {top} ({label}), not in {origin}"


def _preparation_record_path(artifact_root, key):
    return Path(artifact_root).resolve() / ".runtime" / "worktree-preparation" / f"{key}.json"


def _recorded_preparation_roots(artifact_root, common_dir):
    directory = Path(artifact_root).resolve() / ".runtime" / "worktree-preparation"
    roots = []
    try:
        records = directory.glob("*.json")
        for path in records:
            record = _read_preparation_record(path)
            if (record and record.get("common_dir") == os.path.realpath(common_dir)
                    and record.get("path")):
                roots.append(os.path.realpath(record["path"]))
    except OSError:
        return []
    return roots


def _read_preparation_record(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and value.get("schema_version") == 1 else None


def _worktree_cwd(path, relative):
    target = Path(path) if relative in ("", ".") else Path(path) / relative
    target.mkdir(parents=True, exist_ok=True)
    return str(target.resolve())


def _prepared_record_worktree(record, common, relative):
    """Return a recorded allocation only when path, common dir, and branch prove its identity."""
    if not isinstance(record, dict) or record.get("common_dir") != os.path.realpath(common):
        return None
    path = Path(record.get("path") or "")
    if not path.is_dir():
        return None
    if record.get("state") == "reserved":
        gitdir = _git(path, "rev-parse", "--git-dir")
        if not gitdir:
            return None
        gitdir = Path(gitdir) if os.path.isabs(gitdir) else path / gitdir
        # A partial add can already have HEAD and a branch but is not a ready
        # checkout. In particular Git's initializing lock must survive resume.
        if os.path.lexists(gitdir / "locked") or not (gitdir / "index").is_file():
            return None
    actual_common = _git_common_dir(path)
    symbolic = _git(path, "symbolic-ref", "-q", "HEAD")
    head = _git(path, "rev-parse", "HEAD")
    branch = str(record.get("branch") or "")
    detached = symbolic is None
    if (not actual_common or os.path.realpath(actual_common) != os.path.realpath(common)
            or (not detached and symbolic != f"refs/heads/{branch}")):
        return None
    if detached and (record.get("state") != "reserved" or head != record.get("base_sha")
                     or _git(path, "rev-parse", "--verify", f"refs/heads/{branch}") != head):
        return None
    if record.get("state") == "reserved" and head != record.get("base_sha"):
        return None
    return {"path": str(path.resolve()), "cwd": _worktree_cwd(path, relative),
            "branch": branch, "base": record.get("base_sha"), "base_sha": record.get("base_sha"),
            **({"detached": True} if detached else {})}


def _attach_prepared_branch(record, prepared):
    """Attach a proven reserved detached worktree to its exact existing branch."""
    if not prepared or not prepared.get("detached"):
        return prepared
    path = Path(prepared["path"])
    branch = prepared["branch"]
    expected = record.get("base_sha")
    branch_tip = _git(path, "rev-parse", "--verify", f"refs/heads/{branch}")
    current = _git(path, "rev-parse", "HEAD")
    if not expected or branch_tip != expected or current != expected:
        return None
    attached = _worktree_git(path, "checkout", "-B", branch, f"refs/heads/{branch}")
    if attached.returncode != 0:
        return None
    symbolic = _git(path, "symbolic-ref", "-q", "HEAD")
    head = _git(path, "rev-parse", "HEAD")
    if symbolic != f"refs/heads/{branch}" or head != expected:
        return None
    return {**prepared, "detached": False}


def _remote_default_base(top):
    """Fetch origin's current default (origin/HEAD, then main fallback) and pin the fetched SHA."""
    remotes = _worktree_git(top, "remote")
    if remotes.returncode == 0 and not remotes.stdout.strip():
        head = _worktree_git(top, "rev-parse", "--verify", "HEAD^{commit}")
        if head.returncode != 0 or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head.stdout.strip()):
            return None, "local-default-unresolved", (head.stderr or "HEAD did not resolve to a commit").strip()
        return {"branch": None, "sha": head.stdout.strip()}, None, None
    advertised = _worktree_git(top, "ls-remote", "--symref", "origin", "HEAD")
    if advertised.returncode != 0:
        return None, "remote-default-fetch-failed", (advertised.stderr or "git ls-remote failed").strip()
    match = re.search(r"^ref: refs/heads/(.+)\s+HEAD$", advertised.stdout, re.MULTILINE)
    if match:
        default = match.group(1).strip()
    else:
        symbolic = _git(top, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
        default = symbolic[len("origin/"):] if symbolic and symbolic.startswith("origin/") else "main"
    valid = _worktree_git(top, "check-ref-format", "--branch", default)
    if valid.returncode != 0:
        return None, "remote-default-ref-invalid", (valid.stderr or default).strip()
    tip = re.search(r"^([0-9a-f]{40}|[0-9a-f]{64})\s+HEAD$", advertised.stdout, re.MULTILINE)
    tracking = f"refs/remotes/origin/{default}"
    if match and tip and _git(top, "rev-parse", "--verify", f"{tracking}^{{commit}}") == tip.group(1):
        # --missing=print reports absent objects rather than lazily fetching
        # promised objects. Traverse this exact tip, not every worktree/ref as
        # fetch's connectivity census can do. A ref alone proves no availability.
        objects = _worktree_git(top, "rev-list", "--objects", "--no-object-names",
                                "--missing=print", tip.group(1))
        if objects.returncode == 0 and objects.stdout.strip() and not any(
                line.startswith("?") for line in objects.stdout.splitlines()):
            return {"branch": default, "sha": tip.group(1)}, None, None
    fetched = _worktree_git(top, "fetch", "-q", "origin",
                            f"+refs/heads/{default}:refs/remotes/origin/{default}")
    if fetched.returncode != 0:
        return None, "remote-default-fetch-failed", (fetched.stderr or "git fetch failed").strip()
    base = _git(top, "rev-parse", "--verify", f"refs/remotes/origin/{default}^{{commit}}")
    if not base or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base):
        return None, "remote-default-ref-unresolved", f"refs/remotes/origin/{default} did not resolve to a commit"
    return {"branch": default, "sha": base}, None, None


def _existing_local_branch(top, branch):
    result = _worktree_git(top, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    return result.returncode == 0


def _branch_ref_oid(top, branch):
    result = _worktree_git(top, "show-ref", "--verify", "--hash", f"refs/heads/{branch}")
    return result.stdout.strip() if result.returncode == 0 else None


def _branch_namespaces(top):
    result = _worktree_git(top, "for-each-ref", "--format=%(refname)")
    if result.returncode != 0:
        return None
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _branch_candidate_available(top, branch, protected, refs):
    if branch in protected:
        return False
    checked = _worktree_git(top, "check-ref-format", "--branch", branch)
    if checked.returncode != 0 or refs is None:
        return False
    heads = [ref[len("refs/heads/"):] for ref in refs if ref.startswith("refs/heads/")]
    other_names = [ref[len("refs/tags/"):]
                   for ref in refs if ref.startswith("refs/tags/")]
    for ref in refs:
        if ref.startswith("refs/remotes/"):
            remote_branch = ref[len("refs/remotes/"):].partition("/")[2]
            if remote_branch and remote_branch != "HEAD":
                other_names.append(remote_branch)
    if any(name == branch or name.startswith(branch + "/") or branch.startswith(name + "/")
           for name in heads):
        return False
    if any(name == branch for name in other_names):
        return False
    return True


def _branch_checked_out(top, branch):
    result = _worktree_git(top, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return None
    return any(line.strip() == f"branch refs/heads/{branch}" for line in result.stdout.splitlines())




def _ref_namespace_conflict(top, branch, *, allow_local_branch=False, observation=None):
    """Fail closed on every ref spelling that can make Git's short name ambiguous."""
    valid = _worktree_git(top, "check-ref-format", "--branch", branch)
    if valid.returncode != 0:
        return True
    common = observation["common"] if observation is not None else _git_common_dir(top)
    if not common:
        return True
    # Git's revision DWIM checks these namespaces, while pseudo-ref files live
    # directly in the common directory and are not returned by for-each-ref.
    if os.path.lexists(Path(common) / branch):
        return True
    refs = observation["refs"] if observation is not None else _branch_namespaces(top)
    if refs is None:
        return True
    names = set(refs)
    aliases = {
        f"refs/{branch}", f"refs/tags/{branch}", f"refs/heads/{branch}",
        f"refs/remotes/{branch}", f"refs/remotes/{branch}/HEAD",
    }
    target = f"refs/heads/{branch}"
    for ref in names:
        if ref in aliases and not (allow_local_branch and ref == target):
            return True
        # A ref cannot be both a file and a directory. Check D/F collisions in
        # each DWIM namespace and in the actual branch namespace.
        if any(ref.startswith(alias + "/") or alias.startswith(ref + "/")
               for alias in aliases):
            if allow_local_branch and ref == target:
                continue
            return True
        if ref.startswith(target + "/") or target.startswith(ref + "/"):
            return True
    worktrees = (observation["worktrees"] if observation is not None else
                 _worktree_git(top, "worktree", "list", "--porcelain"))
    if worktrees.returncode != 0:
        return True
    if any(line == f"branch {target}" for line in worktrees.stdout.splitlines()):
        # A reserved branch is allowed only when it is not already checked out.
        return True
    return False


def _allocate_branch_ref(top, branch, base_sha):
    """Create a branch ref we own with a CAS against the all-zero absent value."""
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(base_sha or "")):
        return False, "invalid-base-object-id"
    created = _worktree_git(top, "update-ref", f"refs/heads/{branch}", base_sha,
                            "0" * len(base_sha))
    if created.returncode == 0:
        return True, ""
    return False, (created.stderr or created.stdout or "branch-ref-allocation-failed").strip()


def _release_owned_branch(top, branch, expected_sha):
    """CAS-delete only this call's unlinked branch; uncertainty always keeps it."""
    worktrees = _worktree_git(top, "worktree", "list", "--porcelain")
    if worktrees.returncode != 0 or any(
            line == f"branch refs/heads/{branch}" for line in worktrees.stdout.splitlines()):
        return False
    current = _worktree_git(top, "rev-parse", "--verify", f"refs/heads/{branch}")
    if current.returncode != 0 or current.stdout.strip() != expected_sha:
        return False
    removed = _worktree_git(top, "update-ref", "-d", f"refs/heads/{branch}", expected_sha)
    if removed.returncode != 0:
        return False
    # Recheck after the CAS delete: a user or another tool may have attached
    # the branch between the first worktree scan and ref removal. Restore only
    # the exact old SHA with an absent-ref CAS; never overwrite a concurrent
    # move or a newly allocated branch.
    attached = _worktree_git(top, "worktree", "list", "--porcelain")
    if attached.returncode == 0 and not any(
            line == f"branch refs/heads/{branch}" for line in attached.stdout.splitlines()):
        return True
    current = _worktree_git(top, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    if current.returncode == 1:
        _worktree_git(top, "update-ref", f"refs/heads/{branch}", expected_sha,
                      "0" * len(expected_sha))
    return False


def _cleanup_failed_worktree_branch(top, branch, base_sha):
    """Report the outcome of releasing a branch this attempt created and left unused."""
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(base_sha or "")):
        return {"state": "preserved", "reason": "base-sha-unverified"}
    current = _branch_ref_oid(top, branch)
    if current != base_sha:
        return {"state": "preserved", "reason": "branch-moved-or-missing"}
    checked_out = _branch_checked_out(top, branch)
    if checked_out is not False:
        return {"state": "preserved", "reason": "branch-worktree-used-or-unobservable"}
    if _release_owned_branch(top, branch, base_sha):
        return {"state": "removed", "reason": "owned-unchanged-branch"}
    return {"state": "preserved", "reason": "compare-and-swap-refused"}


def _worktree_collision(result):
    detail = ((result.stderr or "") + "\n" + (result.stdout or "")).lower()
    return any(token in detail for token in ("already exists", "already checked out", "already in use",
                                               "not an empty directory", "is locked"))


def prepare_isolated_worktree(cwd, slug, *, artifact_root=None, preparation_key=None,
                              explicitly_approved=False, framed_start=False):
    """Prepare or resume a source-start allocation without changing the primary checkout.

    New work starts from a fetched remote-default SHA. Explicit approved linked tasks and a
    recorded allocation keep their existing contents. A failure is returned to the caller so it
    can stop before publishing a route or starting an owner.
    """
    original = str(Path(cwd).resolve())
    topology = OWNER_WRITE_ADVISORY.git_topology(original)
    source_top = _git(original, "rev-parse", "--show-toplevel")
    if not source_top:
        return {"state": "skipped", "reason": "non-git-cwd"}
    if topology not in ("primary", "linked"):
        return {"state": "failed", "reason": "git-topology-unresolved", "cwd": original}
    common = _git_common_dir(source_top)
    if not common:
        return {"state": "failed", "reason": "git-common-dir-unresolved", "cwd": original}
    common = os.path.realpath(common)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(slug or "")):
        return {"state": "failed", "reason": "worktree-slug-invalid", "slug": str(slug or "")}
    try:
        root = artifact_root or os.environ.get("AGENT_ARTIFACT_ROOT") or _compose_artifact_root(original)
        root = str(Path(root).resolve())
    except (OSError, ValueError) as exc:
        return {"state": "failed", "reason": "canonical-artifact-root-unresolved", "detail": str(exc)}
    if preparation_key is None:
        preparation_key = {"kind": "compat", "common_dir": common, "cwd": original, "slug": slug}
    key_payload = json.dumps({"common_dir": common, "key": preparation_key}, sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False)
    key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
    record_path = _preparation_record_path(root, key)
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(record_path.with_suffix(".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        return {"state": "failed", "reason": "preparation-state-unavailable", "detail": str(exc)}
    with os.fdopen(fd, "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        record = _read_preparation_record(record_path)
        relative = os.path.relpath(original, os.path.realpath(source_top))
        if topology == "linked" and explicitly_approved:
            branch = _git(original, "symbolic-ref", "-q", "--short", "HEAD")
            if not branch:
                return {"state": "failed", "reason": "approved-linked-branch-unresolved", "cwd": original}
            approved_path = str(Path(source_top).resolve())
            if record:
                reused = _prepared_record_worktree(record, common, relative)
                if reused:
                    if reused["path"] != approved_path:
                        return {"state": "failed", "reason": "preparation-identity-conflict",
                                "path": reused["path"], "requested_path": approved_path}
                    return {"state": "reused", **reused, "preparation_key": key,
                            "reason": "approved-linked-task"}
            primary = _primary_worktree(original, common)
            primary = os.path.realpath(primary) if primary else None
            record = {"schema_version": 1, "state": "ready", "key": key,
                      "common_dir": common, "primary_top": primary, "origin_cwd": original,
                      "relative_cwd": relative, "path": approved_path, "branch": branch,
                      "base_sha": _git(source_top, "rev-parse", "HEAD"),
                      "remote_default": "approved-linked-task"}
            try:
                atomic_write(record_path, record)
            except OSError as exc:
                return {"state": "failed", "reason": "preparation-state-write-failed", "detail": str(exc)}
            return {"state": "reused", "path": approved_path, "cwd": original,
                    "branch": branch, "base": None, "preparation_key": key,
                    "reason": "approved-linked-task"}
        if topology == "linked" and record:
            reused = _prepared_record_worktree(record, common, os.path.relpath(original, source_top))
            if reused and os.path.commonpath([original, reused["path"]]) == reused["path"]:
                attached = _attach_prepared_branch(record, reused)
                if attached is None:
                    return {"state": "failed", "reason": "prepared-branch-attach-unverified",
                            "branch": reused["branch"], "path": reused["path"]}
                return {"state": "reused", **attached, "preparation_key": key}
        primary = _primary_worktree(original, common)
        if not primary:
            return {"state": "failed", "reason": "primary-worktree-unresolved", "cwd": original}
        primary = os.path.realpath(primary)
        if record and record.get("primary_top") == primary:
            reused = _prepared_record_worktree(record, common, relative)
            if reused:
                reused = _attach_prepared_branch(record, reused)
                if reused is None:
                    return {"state": "failed", "reason": "prepared-branch-attach-unverified",
                            "branch": record.get("branch"), "path": record.get("path")}
            if reused and record.get("state") == "ready":
                return {"state": "reused", **reused, "preparation_key": key}
            if reused and record.get("state") == "reserved":
                record["state"] = "ready"
                try:
                    atomic_write(record_path, record)
                except OSError as exc:
                    return {"state": "failed", "reason": "preparation-state-write-failed", "detail": str(exc)}
                return {"state": "reused", **reused, "preparation_key": key}

        framed_base = None
        if framed_start and topology == "linked":
            framed_base, reason, detail = _remote_default_base(primary)
            if reason:
                return {"state": "failed", "reason": reason, "detail": detail, "cwd": original}
            status = _worktree_git(source_top, "status", "--porcelain", "--untracked-files=all")
            if status.returncode != 0:
                return {"state": "failed", "reason": "linked-worktree-status-unresolved",
                        "detail": (status.stderr or status.stdout).strip(), "cwd": original}
            if status.stdout.strip():
                return {"state": "retained-linked", "reason": "linked-worktree-dirty",
                        "cwd": original, "path": str(Path(source_top).resolve()),
                        "branch": _git(source_top, "symbolic-ref", "-q", "--short", "HEAD"),
                        "base": framed_base["sha"], "base_sha": framed_base["sha"],
                        "preparation_key": key}
            ancestry = _worktree_git(source_top, "merge-base", "--is-ancestor", "HEAD", framed_base["sha"])
            if ancestry.returncode == 1:
                return {"state": "retained-linked", "reason": "linked-worktree-unmerged",
                        "cwd": original, "path": str(Path(source_top).resolve()),
                        "branch": _git(source_top, "symbolic-ref", "-q", "--short", "HEAD"),
                        "base": framed_base["sha"], "base_sha": framed_base["sha"],
                        "preparation_key": key}
            if ancestry.returncode != 0:
                return {"state": "failed", "reason": "linked-worktree-ancestry-unresolved",
                        "detail": (ancestry.stderr or ancestry.stdout).strip(), "cwd": original}

        worktree_root = Path(primary).parent / f"{Path(primary).name}-wt"
        try:
            worktree_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"state": "failed", "reason": "worktree-parent-create-failed", "detail": str(exc)}
        # A reserved record pins the fetched SHA even after a failed add safely
        # removes the branch it created. Recreate that exact absent ref on
        # retry without another network fetch. A moved or otherwise occupied
        # reserved ref is never adopted or overwritten.
        reserved = None
        retry_base = None
        if record and record.get("primary_top") == primary and record.get("state") == "reserved":
            branch = record.get("branch")
            path = Path(record.get("path") or "")
            base_sha = record.get("base_sha")
            valid_anchor = (
                isinstance(branch, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
                and path.parent.resolve() == worktree_root.resolve()
                and path.name == branch
                and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(base_sha or ""))
                and not os.path.lexists(path)
            )
            if valid_anchor:
                ref = f"refs/heads/{branch}"
                existing = _worktree_git(primary, "show-ref", "--verify", "--quiet", ref)
                if existing.returncode == 0:
                    current = _worktree_git(primary, "rev-parse", "--verify", ref)
                    if current.returncode == 0 and current.stdout.strip() == base_sha:
                        reserved = (branch, path, base_sha)
                    else:
                        return {"state": "failed", "reason": "preparation-identity-conflict",
                                "path": str(path), "branch": branch, "base": base_sha}
                elif existing.returncode == 1:
                    retry_base = {"branch": record.get("remote_default"), "sha": base_sha}
                else:
                    return {"state": "failed", "reason": "preparation-identity-conflict",
                            "path": str(path), "branch": branch, "base": base_sha}
            elif branch or os.path.lexists(path):
                return {"state": "failed", "reason": "preparation-identity-conflict",
                        "path": str(path), "branch": str(branch or "")}
        base = retry_base or ({"branch": record.get("remote_default"), "sha": reserved[2]}
                              if reserved is not None else {"branch": None, "sha": None})
        if reserved is None and retry_base is None:
            base = framed_base
            if base is None:
                base, reason, detail = _remote_default_base(primary)
            else:
                reason = detail = None
            if reason:
                return {"state": "failed", "reason": reason, "detail": detail, "cwd": original}
        observed_branch = _git(primary, "symbolic-ref", "-q", "--short", "HEAD")
        protected = {"HEAD", "main", "master", "develop", "trunk", "origin"}
        protected.update(value for value in (observed_branch, base.get("branch"),
                           (record or {}).get("remote_default")) if value)
        refs = _branch_namespaces(primary)
        observation = {"common": common, "refs": refs,
                       "worktrees": _worktree_git(primary, "worktree", "list", "--porcelain")}

        def candidates():
            seen = set()
            if reserved:
                seen.add(reserved[0])
                yield reserved[0], reserved[1], reserved[2], True
            if record and record.get("primary_top") == primary and not reserved:
                branch = record.get("branch")
                path = Path(record.get("path") or "")
                if (branch and path.parent.resolve() == worktree_root.resolve()
                        and branch == path.name):
                    seen.add(branch)
                    yield branch, path, base["sha"], _existing_local_branch(primary, branch)
            for suffix in range(1, 65):
                branch = slug if suffix == 1 else f"{slug}-{suffix}"
                if branch not in seen and _branch_candidate_available(primary, branch, protected, refs):
                    yield branch, worktree_root / branch, base["sha"], False

        for branch, path, base_sha, resume_branch in candidates():
            if _worktree_git(primary, "check-ref-format", "--branch", branch).returncode != 0:
                continue
            if os.path.lexists(path):
                if (record and str(path.resolve()) == str(Path(record.get("path") or "").resolve())
                        and record.get("state") == "reserved"):
                    reused = _prepared_record_worktree(record, common, relative)
                    if reused:
                        reused = _attach_prepared_branch(record, reused)
                    if reused:
                        record["state"] = "ready"
                        try:
                            atomic_write(record_path, record)
                        except OSError as exc:
                            return {"state": "failed", "reason": "preparation-state-write-failed",
                                    "detail": str(exc)}
                        return {"state": "reused", **reused, "preparation_key": key}
                continue
            if _ref_namespace_conflict(primary, branch, allow_local_branch=resume_branch,
                                       observation=observation):
                continue
            if not resume_branch and _existing_local_branch(primary, branch):
                continue
            record = {"schema_version": 1, "state": "reserved", "key": key,
                      "common_dir": common, "primary_top": primary, "origin_cwd": original,
                      "relative_cwd": relative, "path": str(path), "branch": branch,
                      "base_sha": base_sha,
                      "remote_default": base.get("branch") or (record or {}).get("remote_default")}
            try:
                atomic_write(record_path, record)
            except OSError as exc:
                return {"state": "failed", "reason": "preparation-state-write-failed", "detail": str(exc)}
            branch_created = False
            if not resume_branch:
                if not _branch_candidate_available(primary, branch, protected, refs):
                    continue
                branch_created, ref_error = _allocate_branch_ref(primary, branch, base_sha)
                if not branch_created:
                    if (_ref_namespace_conflict(primary, branch)
                            or _branch_ref_oid(primary, branch) is not None):
                        continue
                    return {"state": "failed", "reason": "branch-ref-allocation-failed",
                            "branch": branch, "base": base_sha, "detail": ref_error}
            command = ("worktree", "add", str(path), f"refs/heads/{branch}")
            added = _worktree_git(primary, *command)
            if added.returncode != 0:
                if branch_created and not getattr(added, "git_uncertain", False):
                    cleanup = _cleanup_failed_worktree_branch(primary, branch, base_sha)
                else:
                    cleanup = {"state": "preserved",
                               "reason": ("preparation-outcome-uncertain" if branch_created
                                          else "branch-not-created-by-attempt")}
                released = cleanup["state"] == "removed"
                if _worktree_collision(added):
                    if branch_created and not released:
                        return {"state": "failed", "reason": "worktree-add-failed", "path": str(path),
                                "branch": branch, "base": base_sha,
                                "detail": (added.stderr or added.stdout).strip()}
                    if resume_branch:
                        return {"state": "failed", "reason": "reserved-worktree-resume-failed",
                                "path": str(path), "branch": branch, "base": base_sha,
                                "detail": (added.stderr or added.stdout).strip()}
                    continue
                failed = {"state": "failed", "reason": "worktree-add-failed", "path": str(path),
                          "branch": branch, "base": base_sha,
                          "detail": (added.stderr or added.stdout).strip(),
                          "branch_cleanup": cleanup}
                if hasattr(added, "git_failure"):
                    failed["git_failure"] = added.git_failure
                if getattr(added, "git_diagnosis", None):
                    failed["git_diagnosis"] = added.git_diagnosis
                return failed
            prepared = _prepared_record_worktree(record, common, relative)
            if not prepared:
                return {"state": "failed", "reason": "worktree-branch-identity-unverified",
                        "path": str(path), "branch": branch, "base": base_sha}
            if _attach_prepared_branch(record, prepared) is None:
                return {"state": "failed", "reason": "worktree-branch-attach-unverified",
                        "path": str(path), "branch": branch, "base": base_sha}
            record["state"] = "ready"
            try:
                atomic_write(record_path, record)
                final_cwd = _worktree_cwd(path, relative)
            except OSError as exc:
                return {"state": "failed", "reason": "preparation-state-write-failed", "detail": str(exc)}
            return {"state": "created", "path": str(path.resolve()), "cwd": final_cwd,
                    "branch": branch, "base": base_sha, "base_sha": base_sha,
                    "preparation_key": key}
        return {"state": "failed", "reason": "worktree-collision-budget-exhausted",
                "cwd": original, "slug": slug}

ROUTE_ID_FORM = re.compile(r"rt-[0-9a-f]{16}")


def _registry_rows(jobs=None, needle=None):
    """`{attempt_id: (status, metadata)}` from the jobs registry, latest row per attempt last.

    Only lines containing `needle` are parsed (the registry is large). Read-only; an
    unreadable registry is an empty one."""
    rows = {}
    try:
        text = Path(jobs or _compose_default_jobs()).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        if needle and needle not in line:
            continue
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = parse_registry_metadata(fields[5])
        attempt = meta.get("attempt_id")
        if attempt:
            rows.pop(attempt, None)
            rows[attempt] = (fields[1], meta)
    return rows


def attempt_route_file(value, jobs=None):
    """The route file one registry attempt runs for (an owner's route, else its stage route), or None.

    Lets every command that takes a route also take the attempt id a receipt printed."""
    text = str(value)
    if not text or any(ch in text for ch in "\t\n,=/"):
        return None
    row = _registry_rows(jobs, needle="attempt_id=" + text).get(text)
    metas = [row[1]] if row is not None else _archived_metadata(jobs, "attempt_id=" + text)
    for meta in metas:
        if meta.get("attempt_id") != text:
            continue
        for key in ("owner_route_file", "route_file"):
            if meta.get(key) and Path(meta[key]).is_file():
                return Path(meta[key])
    return None


def _archived_metadata(jobs, needle):
    """Metadata of the settled rows the dispatch registry archive holds that contain `needle`."""
    try:
        from dispatch_registry_archive import archived_rows
        lines = archived_rows(jobs or _compose_default_jobs(), needle)
    except (OSError, ValueError):
        return []
    return [parse_registry_metadata(fields[5]) for fields in (line.split("\t") for line in lines)
            if len(fields) == 6]


ROUTE_ATTEMPT_FIELDS = ("worker_type", "route_node", "harness", "failure_class", "note", "log_file")


def route_attempts(route_id, jobs=None):
    """The registry attempts of one route, most recently updated last: id, registry status and a
    few row fields.

    Read-only: the same row a registry reconcile dry run starts from, without classifying it."""
    return [{"attempt_id": aid, "status": status,
             **{key: meta[key] for key in ROUTE_ATTEMPT_FIELDS if meta.get(key)}}
            for aid, (status, meta) in _registry_rows(jobs, needle=route_id).items()
            if route_id in {meta.get("owner_route_id"), meta.get("route_id")}]


def route_owner_attempt(route_id, jobs=None):
    """The newest registered owner attempt of one route, or None."""
    owners = [aid for aid, (_status, meta) in _registry_rows(jobs, needle=route_id).items()
              if meta.get("worker_type") == "owner"
              and route_id in {meta.get("owner_route_id"), meta.get("route_id")}]
    return owners[-1] if owners else None


def resolve_route_argument(value, jobs=None):
    """`--route` as given: a route file, or a route ID (`rt-<16 hex>`, the form `resume_command`
    prints) looked up as the canonical record under the cwd's artifact root, then under
    `AGENT_ARTIFACT_ROOT`, then by the file the jobs registry or a route-chain ledger names for it
    (this session's, then the newest ledgers of every session). An ID that names no route file is
    refused (`route-id-unresolved:<id>`). An attempt id from the registry names its route."""
    text = str(value)
    if Path(text).is_file():
        return Path(text)
    if not ROUTE_ID_FORM.fullmatch(text):
        return attempt_route_file(text, jobs) or Path(text)
    roots = [os.environ.get("AGENT_ARTIFACT_ROOT")]
    try:
        roots.insert(0, _compose_artifact_root(os.getcwd()))
    except ValueError:
        pass
    named = [canonical_route_path(root, text) for root in roots if root]
    try:
        for line in Path(jobs or _compose_default_jobs()).read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) == 6:
                meta = parse_registry_metadata(fields[5])
                for key in ("owner_route", "route"):
                    if meta.get(f"{key}_id") == text and meta.get(f"{key}_file"):
                        named.append(meta[f"{key}_file"])
    except OSError:
        pass
    if not any(Path(path).is_file() for path in named):
        # A route closed long ago may have its settled rows in the dispatch registry archive.
        for meta in _archived_metadata(jobs, text):
            for key in ("owner_route", "route"):
                if meta.get(f"{key}_id") == text and meta.get(f"{key}_file"):
                    named.append(meta[f"{key}_file"])
    rc = _route_chain_module()
    if rc is not None:
        # This session's ledger first, then the newest ledgers of every session (any event).
        own = rc.writer_identity()
        ledgers = [own] if own else []
        for harness in rc.HARNESSES:
            try:
                directory = Path(rc.state_root()) / harness
                ledgers += [(harness, path.stem) for path in sorted(
                    directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:rc.ANCHOR_SCAN_FILES]]
            except OSError:
                continue
        for anchor in ledgers:
            named += [line["route_file"] for line in rc.read_tail(*anchor) if line.get("route_id") == text]
            if any(Path(path).is_file() for path in named):
                break
    for path in named:
        if Path(path).is_file():
            return Path(path)
    raise ValueError(f"route-id-unresolved:{text} (pass the route file, or run from the route's checkout)")


def caller_open_route(cwd):
    """`(route_file | None, source, rows)`: the open route a bare `start` continues.

    This session's own newest open route first (its route-chain ledger, which every
    compose and start writes); otherwise the one open route sealed for this cwd under
    the cwd's artifact root, or, with none there, for the worktree compose made for it in the cwd's
    repository (`<repo>-wt/<route slug>`). `rows` are the open routes that were looked at, each with
    its `resume_command`, so a caller that finds none or several sees what is there."""
    from parent_next_directive import resume_command
    rc = _route_chain_module()
    anchor = rc.writer_identity() if rc is not None else None
    for line in reversed(rc.read_tail(*anchor) if anchor else []):
        path = Path(line["route_file"])
        if path.is_file() and not outcome_path(path).is_file():
            return str(path), "this-session", []
    try:
        artifact_root = _compose_artifact_root(cwd)
    except ValueError:
        return None, "none", []
    here = os.path.realpath(cwd)
    rows, same_repository = [], []
    for row in route_status(artifact_root):
        if row["closed"] or row.get("read_only"):
            continue
        try:
            sealed = json.loads(Path(row["route_file"]).read_text(encoding="utf-8"))
            sealed_cwd = sealed.get("cwd")
        except (OSError, ValueError):
            continue
        if not isinstance(sealed_cwd, str):
            continue
        found = {"route_id": row["route_id"], "capability": row["capability"], "route_file": row["route_file"],
                 "cwd": sealed_cwd, "slug": sealed.get("slug"),
                 "resume_command": resume_command(row["route_file"], agent_home=ROOT)}
        if os.path.realpath(sealed_cwd) == here:
            rows.append(found)
        else:
            same_repository.append(found)
    if not rows and same_repository:
        # Prefer the exact persisted allocation, including an automatically selected collision
        # suffix. Retain the old slug path only as a read-only compatibility lookup for legacy routes.
        top = _git(here, "rev-parse", "--show-toplevel")
        common = _git_common_dir(here)
        allocated = _recorded_preparation_roots(artifact_root, common) if common else []
        def moved(row):
            if not top:
                return False
            sealed = os.path.realpath(row["cwd"])
            for home in allocated:
                try:
                    if os.path.commonpath([sealed, home]) == home:
                        return True
                except ValueError:
                    continue
            if not row.get("slug"):
                return False
            legacy = os.path.realpath(Path(top).parent / f"{Path(top).name}-wt" / row["slug"])
            return sealed == legacy or sealed.startswith(legacy + os.sep)
        rows = [row for row in same_repository if moved(row)]
    if len(rows) == 1:
        return rows[0]["route_file"], "cwd", rows
    return None, "ambiguous" if rows else "none", rows


def _compose_artifact_root(cwd):
    script=ROOT/"utilities"/"artifact-root.sh"
    result=subprocess.run(["sh",str(script),str(cwd)],text=True,capture_output=True,check=False)
    root=(result.stdout or "").strip().splitlines()[-1] if (result.stdout or "").strip() else ""
    if result.returncode!=0 or not root:
        raise ValueError("compose-artifact-root-unresolved:"+(result.stderr or "").strip()[:200])
    return root


def compose_receipt(route, path, *, owner_harness=None):
    """The ordinary caller needs its choices and handle, not all sealed evidence."""
    return {
        "route_file": str(Path(path).resolve()), "route_id": route["route_id"],
        "selection": route.get("selection", {}),
        "cwd": route["cwd"], "artifact_root": route["artifact_root"],
        "effective_intensity": route["effective_intensity"],
        "owner_model_profile": route.get("owner_model_profile"),
        "nodes": [{key: node[key] for key in
                   ("id", "unit", "dispatch_depth", "model_profile", "depends_on", "terminal") if key in node}
                  for node in route["nodes"]],
        "human_gates": route.get("human_gates", []),
        "campaign": compose_campaign_selection(route),
        "advisories": OWNER_WRITE_ADVISORY.advisories(route, owner_harness=owner_harness),
    }


def _correct_attempt_argument(value, jobs):
    """`correct` takes an owner attempt id as given; a route id or route file names that route's
    newest registered owner (`route-owner-unregistered:<id>` when it has none)."""
    text = str(value)
    if not (ROUTE_ID_FORM.fullmatch(text) or Path(text).is_file()):
        return text
    route_file = resolve_route_argument(text, jobs)
    try:
        route_id = json.loads(Path(route_file).read_text(encoding="utf-8")).get("route_id") or text
    except (OSError, ValueError):
        route_id = text
    owner = route_owner_attempt(route_id, jobs)
    if owner is None:
        raise ValueError(f"route-owner-unregistered:{route_id}; this route has no registered owner to correct")
    return owner


def _stage_route_owner(jobs, attempt_id):
    """The owner a depth-2 stage attempt of the same route reports to, else `attempt_id` itself.

    A correction addressed to a stage is a correction for the work its route owner is doing; only a
    parent owner of the very same route counts, so another route's attempt stays refused."""
    row = _find_attempt_row_metadata(Path(jobs), attempt_id)
    if not row or row.get("worker_type") == "owner" or row.get("dispatch_depth") != "2":
        return attempt_id
    parent = _find_attempt_row_metadata(Path(jobs), row.get("parent_attempt_id") or "")
    if (parent and parent.get("worker_type") == "owner" and parent.get("dispatch_depth") == "1"
            and (parent.get("owner_route_id") or parent.get("route_id")) == (row.get("route_id") or row.get("route"))):
        return parent["attempt_id"]
    return attempt_id


def _current_owner_attempt(jobs, attempt_id):
    """The newest launched owner in this attempt's replacement lineage (itself when none).

    An answer sent with an older owner's id (or a stage attempt of the same route) still reaches the
    owner doing the work now. When the lineage of an attempt that was replaced cannot be read, the
    old owner is not used in its place: the correction stays retained until it can be observed."""
    start = _stage_route_owner(jobs, attempt_id)
    try:
        from dispatch_replacement import effective_attempts
        effective, _mapping = effective_attempts(jobs, {start})
        return next(iter(effective)) if len(effective) == 1 else start
    except (DispatchContractError, OSError, ValueError, StopIteration):
        row = _find_attempt_row_metadata(Path(jobs), start) or {}
        if any(row.get(key) for key in ("replacement_family_id", "replacement_attempt_id",
                                        "replacement_claim_digest")):
            raise ValueError("owner-input-unavailable-retain-correction")
        return start


def _continue_after_answer(jobs, attempt_id, correction):
    """Continue a route whose owner ended BLOCKED, now that its answer is kept.

    The shared `start` does the work: its replacement owner receives the answer
    first (`dispatch_replacement` 'corrected'). Its receipt, `parent_next`
    included, is this command's receipt, so nothing else has to be run."""
    import dispatch_replacement
    from work_start import start_work, _rows as start_rows
    try:
        _, meta = start_rows(jobs)[attempt_id]
        route_path, _ = dispatch_replacement._route(jobs, attempt_id, meta)
        route = verify_route(json.loads(route_path.read_text()))
    except (DispatchContractError, OSError, ValueError, KeyError) as exc:
        return {**correction, "state": "retained", "reason": getattr(exc, "reason", None) or str(exc)[:200],
                "next_step": "The answer is kept, but this owner's route cannot continue (it is closed or "
                    "unreadable). Report that; compose the remaining work as a new route."}
    _record_route_chain(route, str(route_path), "start")
    access_change = _record_access_change(route, jobs)
    receipt = start_work(route, route_path, jobs)
    if access_change is not None:
        receipt = {**receipt, "access_change": access_change}
    return {**receipt, "correction": correction}


def _record_route_chain(route, route_file, event, *, plan=None, plan_source=None):
    """Append one route-chain ledger line. Failures report their cause — route creation/start
    must never fail because of this sidecar (plan §3 B-1.3). Success/failure is only
    ever observable on stderr."""
    try:
        rc = _route_chain_module()
        if rc is None:
            return
        identity = _route_chain_identity(event, route)
        if identity is None:
            print("route_chain_written=0 reason=no-identity", file=sys.stderr)
            return
        harness, sid, depth, by_attempt = identity
        if not os.environ.get("FLEET_ROUTE_CHAIN_DIR"):
            artifact_root = route.get("artifact_root")
            if isinstance(artifact_root, str) and artifact_root:
                real_root = os.path.realpath(artifact_root)
                real_tmp = os.path.realpath(tempfile.gettempdir())
                if real_root == real_tmp or real_root.startswith(real_tmp + os.sep):
                    print("route_chain_written=0 reason=tmp-artifact-root", file=sys.stderr)
                    return
        if event == "start" and any(
            line.get("route_id") == route.get("route_id")
            for line in rc.read_tail(harness, sid)
        ):
            # A repeated start is a normal continuation of the same route, not a refusal:
            # the ledger already holds it, and the start receipt below is the answer.
            print("route_chain_already_recorded=1", file=sys.stderr)
            return
        line = rc.build_line(
            route, event=event, harness=harness, session_id=sid, route_file=route_file,
            plan=plan, plan_source=plan_source, dispatch_depth=depth, by_attempt=by_attempt,
        )
        append_errors = []
        if rc.append(harness, sid, line, on_error=append_errors.append):
            print(f"route_chain_written=1 harness={harness}", file=sys.stderr)
        else:
            detail = append_errors[0] if append_errors else "unknown"
            print(f"route_chain_written=0 reason=append-failed detail={detail}", file=sys.stderr)
    except Exception as exc:
        try:
            print(f"route_chain_written=0 reason={exc}", file=sys.stderr)
        except Exception:
            pass


def _resolve_compose_plan(a, route_plan=None):
    """`(plan, plan_source)` for the compose CLI's `--plan` input (plan §3 B-1.5). An explicit
    `--plan` is validated eagerly — before any route work — and its failure is a normal
    `ValueError("compose-plan-invalid:...")`, never swallowed. With no explicit plan, an
    inherited value is read from this session's own ledger, but ONLY when the new route's
    own chain key still matches the ledger's current segment (D-9: a fresh campaign starts
    a fresh chain, so it must not silently inherit a different campaign's declared plan).
    Never raises for the inherited path — a ledger read failure just means no inheritance."""
    rc = _route_chain_module()
    if a.plan:
        if rc is None:
            raise ValueError("compose-plan-invalid:route-chain-unavailable")
        known = {r["capability"] for r in TOPO.load_registry()["recipes"]
                 if r["capability"] != ROUTE_FRAME_CAPABILITY}
        return rc.parse_plan(a.plan, known), "explicit"
    if route_plan is not None:
        # Display only: the approved legs' capability order, never a permission or a hash input.
        import route_plan as RP
        return RP.display_plan(route_plan["legs"]), "explicit"
    if rc is None:
        return None, None
    try:
        identity = _route_chain_identity("compose", None)
        if identity is None:
            return None, None
        harness, sid, _depth, _by = identity
        lines = rc.read_tail(harness, sid)
        segment = rc.current_segment(lines)
        if not segment:
            return None, None
        new_key = rc.chain_key({"campaign_key": a.campaign_key, "parent_cycle_id": a.parent_cycle})
        if rc.chain_key(segment[-1]) != new_key:
            return None, None
        plan, source = rc.inherited_plan(segment)
        return (plan, source) if plan else (None, None)
    except Exception:
        return None, None


def _session_latest_route(artifact_root, event="compose"):
    """This session's latest route-chain line in the same artifact root, or None."""
    rc = _route_chain_module()
    if rc is None:
        return None
    try:
        identity = _route_chain_identity(event, None)
        if identity is None:
            return None
        harness, sid, _depth, _by = identity
        root = os.path.realpath(artifact_root)
        for line in reversed(rc.read_tail(harness, sid)):
            if os.path.realpath(str(line.get("artifact_root") or "")) == root:
                return line
    except Exception:
        return None
    return None


def _session_campaign_key(artifact_root):
    """The campaign key of this session's latest route in the same artifact root, or None.

    Only that latest route counts: when it named no campaign (a parent cycle or an
    explicit `--unassigned`), nothing older is reached for."""
    line = _session_latest_route(artifact_root) or {}
    key = line.get("campaign_key")
    return key if isinstance(key, str) and key else None


def _emit_compiled_route(a,route,artifact_root,output=None):
    """Shared tail of compile/compose: runtime-root check, canonical write-once, owner binding, prints."""
    output=output if output is not None else getattr(a,"output",None)
    vbasis=route.get("validation_basis") or {}
    if vbasis.get("runtime_root_match") is False:
        note_same_work_drift("launch-runtime-root-mismatch")
    expected_output=canonical_route_path(artifact_root,route["route_id"])
    if output:
        output_path=Path(output)
        if classify_route_location(output_path,artifact_root) != "canonical":
            raise ValueError("route-output-outside-canonical")
        if not route_path_is_exact(output_path,artifact_root,route["route_id"]):
            raise ValueError("route-output-alias-basename")
    else:
        output_path=expected_output
    write_once(output_path,route)
    try:  # the person's words, once and beside the route: never part of its identity (WORKFLOW §0.4)
        import work_intent
        work_intent.seal_original(route)
    except Exception:  # noqa: BLE001 -- optional record; the route stands without it
        pass
    retired=retire_stale_closure(output_path)
    if retired is not None:
        print(f"route_closure_retired={retired}",file=sys.stderr)
    # A registered depth-1 owner can compile its first route only after it
    # has started.  Attach those immutable bytes to the exact active owner
    # attempt; non-owner and ordinary interactive compiles remain no-ops.
    try:
        from owner_route_binding import (
            OwnerRouteBindingError,
            publish_owner_route_attachment_from_environment,
        )
        route_for_binding = dict(route)
        route_for_binding["route_file"] = str(output_path.resolve())
        attachment = publish_owner_route_attachment_from_environment(
            os.environ.get("AGENT_DISPATCH_JOBS", ""),
            target_route=route_for_binding,
            environ=os.environ,
        ) if os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") else None
    except OwnerRouteBindingError as exc:
        print(
            f"route_file_written=1 owner_route_binding_written=0 reason={exc}",
            file=sys.stderr,
        )
        raise ValueError(str(exc)) from exc
    if attachment is not None:
        print("owner_route_binding_written=1", file=sys.stderr)
    plan, plan_source = getattr(a, "_route_chain_plan", (None, None))
    _record_route_chain(route, str(output_path.resolve()), a.command,
                        plan=plan, plan_source=plan_source)
    print(f"route_file={output_path.resolve()}",file=sys.stderr)
    result = (compose_receipt(route, output_path, owner_harness=getattr(a, "owner", None))
              if a.command == "compose" and not getattr(a, "full_record", False) else route)
    if not getattr(a, "start", False):
        print(json.dumps(result,sort_keys=True))
    return output_path.resolve()

def _close_route_argument(a):
    """`close`/`finish --route` take a route file, an `rt-...` id or a registry attempt id; an id is
    looked up under the artifact root, an attempt by its registry row. `close` remembers a named
    attempt: one that is not its route's owner is stopped alone and the route stays open. Without
    one, `finish` takes this session's latest route there."""
    import artifact_producer
    route=a.route
    if route and not artifact_producer._ROUTE_ID_RE.fullmatch(str(route)) and not Path(route).exists():
        found=attempt_route_file(route,getattr(a,"jobs",None))
        if found is not None:
            a.named_attempt=str(route)
            return str(found)
    if route and (not artifact_producer._ROUTE_ID_RE.fullmatch(str(route)) or Path(route).exists()):
        return route
    root=getattr(a,"artifact_root",None) or os.environ.get("AGENT_ARTIFACT_ROOT") or _compose_artifact_root(os.getcwd())
    if not route:
        latest=(_session_latest_route(root,a.command) or {}).get("route_file")
        if not latest:
            raise ValueError(f"route-required: this session has no route under {root}; pass --route")
        return latest
    path=artifact_producer.resolve_route_argument(Path(root),route)
    if not path.is_file():
        raise ValueError(f"route-not-found: {route} under {root}/.runtime/routes")
    return str(path)


def _legacy_inline_finish(a, route, route_file, api, entry_error=None, *, _admission=None):
    """Consume the common inline finish decision with the legacy writer."""
    import artifact_producer
    import inline_finish
    _admission = _admission or inline_finish.admit(route, allow_legacy=True)
    if route.get("capability") == "autopilot-refine" and route.get("entry_scope_contract_version") == 1:
        # The preview is checked in the cycle this route's lineage owns, never
        # in whichever cycle the caller's summary file happens to name.
        try:
            record = artifact_producer.route_cycle_for(Path(route["artifact_root"]), route)
        except artifact_producer.ProducerError:
            record = None
        if record is not None:
            output = artifact_producer.cycle_dir(
                Path(route["artifact_root"]), record["campaign_id"], record["cycle_id"], record) / "artifacts"
            _require_entry_scope_review_preview(route, {"id": "one-shot"}, output)
    node = route["nodes"][0]
    node_id = node["id"]
    choice = ROUTE_AUTHORITY.decide_continuation(route, action="finish-outcome",
        facts={"legacy": True, "closure": {} if api.outcome_path(Path(route_file)).exists() else None})
    if not choice.allowed:
        raise inline_finish.InlineFinishError(choice.reason)
    evidence = Path(a.evidence).resolve()
    if not (evidence.is_file() or evidence.is_dir()):
        raise SystemExit("completion evidence missing")
    try:
        artifact_producer.require_cycle_output(Path(route["artifact_root"]), evidence,
                                               route_id=route["route_id"])
    except artifact_producer.ProducerError as exc:
        raise ValueError(f"{exc.code}: {exc.detail}") from exc
    try:
        summary_text = Path(a.summary_file).read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        raise SystemExit("completion summary missing")
    summary = (summary_text[0].strip() if summary_text and summary_text[0].strip() else f"legacy inline {node_id}")[:200]
    if not summary:
        raise SystemExit("completion summary missing")
    attempt_id = None
    explicit = None
    marker, _row = complete_node(route, node, node_id, evidence, jobs=None,
                                 attempt_id=attempt_id, explicit_attempt_metadata=explicit)
    outcome, _created = close_route(route, str(route_file), a.commit if getattr(a, "commit", None) else None,
                                    summary, allow_unproven=True)
    root = Path(route.get("artifact_root") or _compose_artifact_root(os.getcwd()))
    cycle = None
    try:
        cycle = artifact_producer.route_cycle_for(root, route)
    except Exception:
        cycle = None
    cycle_id = (cycle or {}).get("cycle_id") if isinstance(cycle, dict) else None
    finalized = None
    if cycle_id:
        finalized = artifact_producer.finalize(root, cycle_id=cycle_id, state="completed",
                                               allow_open_route=True)
    print(f"legacy-inline-finish node={node_id} outcome={bool(outcome)} finalized={bool(finalized)}", file=sys.stderr)
    return {"schema": "finish_receipt_v1", "route_id": route.get("route_id"),
            "route_hash": route.get("route_hash"), "state": "legacy-finished", "legacy": True,
            "node": node_id, "marker": marker, "outcome": outcome, "finalized": finalized}

@contextlib.contextmanager
def compose_campaign_snapshot():
    import artifact_producer
    with artifact_producer.campaign_summary_scope():
        yield


def compose_command_snapshot(function):
    @functools.wraps(function)
    def invoke(*args, **kwargs):
        if sys.argv[1:2] == ["compose"]:
            with compose_campaign_snapshot():
                return function(*args, **kwargs)
        return function(*args, **kwargs)
    return invoke
