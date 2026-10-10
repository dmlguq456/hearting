#!/usr/bin/env python3
"""Queue PR merges; validate waiting PRs together without changing their heads."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from urllib.parse import quote, urlparse
import uuid


class MergeError(RuntimeError):
    pass


class CIFailure(MergeError):
    """A completed integration workflow failed, rather than an API request."""


class Changed(MergeError):
    """The validation subject changed; rebuild the remaining integration."""


class Withdrawn(MergeError):
    """No live caller still requests this PR."""


def one_line(value):
    return " ".join(str(value).split())


def pid_start(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return None if fields[0] in ("Z", "X") else fields[19]


def state_directory(repository):
    state = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not state.is_absolute():
        state = Path.home() / ".local/state"
    key = hashlib.sha256(repository.lower().encode()).hexdigest()[:24]
    return state / "hearting/merge-line" / key


class MergeTurn:
    """The short bookkeeping lock gives the long-lived flock FIFO order."""

    def __init__(self, directory, pr, emit=print, interval=1):
        self.directory, self.pr = Path(directory), pr
        self.emit, self.interval = emit, interval
        self.token = uuid.uuid4().hex
        self.guard_fd = self.turn_fd = None
        self.result = None

    @contextlib.contextmanager
    def guard(self):
        fcntl.flock(self.guard_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(self.guard_fd, fcntl.LOCK_UN)

    def read(self):
        try:
            fd = os.open(self.directory / "line.json", os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return []
        with os.fdopen(fd) as source:
            rows = json.load(source)
        return [row for row in rows if pid_start(row["pid"]) == row["start"]]

    def save(self, rows):
        fd, name = tempfile.mkstemp(prefix="line-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(rows, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.directory / "line.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
            self.guard_fd = os.open(self.directory / "bookkeeping.lock", flags, 0o600)
            self.turn_fd = os.open(self.directory / "merge.lock", flags, 0o600)
            with self.guard():
                rows = self.read()
                rows.append({"token": self.token, "pr": self.pr, "pid": os.getpid(),
                             "start": pid_start(os.getpid()), "holding": False})
                self.save(rows)
            last = None
            while True:
                with self.guard():
                    rows = self.read()
                    position = next(i for i, row in enumerate(rows) if row["token"] == self.token)
                    if "result" in rows[position]:
                        self.result = rows[position]["result"]
                        return self
                    if position == 0:
                        try:
                            fcntl.flock(self.turn_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            pass
                        else:
                            rows[0]["holding"] = True
                            self.save(rows)
                            self.emit(f"merge-line: turn PR=#{self.pr}")
                            return self
                    ahead = rows[position - 1]["pr"] if position else "unknown"
                    waiting_position = position + (0 if rows[0]["holding"] else 1)
                    status = (waiting_position, ahead)
                    if status != last:
                        self.emit(f"merge-line: waiting PR=#{self.pr} position={waiting_position} ahead=#{ahead}")
                        last = status
                time.sleep(self.interval)
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def collect(self):
        """Snapshot admitted callers; later arrivals keep their FIFO place."""
        with self.guard():
            return list(dict.fromkeys(row["pr"] for row in self.read() if "result" not in row))

    def settle(self, pr, result):
        with self.guard():
            rows = self.read()
            for row in rows:
                if row["pr"] == pr and "result" not in row:
                    row["result"] = result
            self.save(rows)
            if pr == self.pr:
                self.result = result

    def __exit__(self, *_):
        try:
            if self.guard_fd is not None:
                with self.guard():
                    self.save([row for row in self.read() if row["token"] != self.token])
        finally:
            for fd in (self.turn_fd, self.guard_fd):
                if fd is not None:
                    os.close(fd)
            self.turn_fd = self.guard_fd = None


class GitHub:
    def __init__(self):
        repo = self.json("repo", "view", "--json", "nameWithOwner,url,defaultBranchRef")
        self.repo = repo["nameWithOwner"]
        self.host = urlparse(repo["url"]).hostname
        self.base = repo["defaultBranchRef"]["name"]
        self.identity = f"{self.host}/{self.repo}"
        self.url = repo["url"] + ".git"

    def json(self, *args):
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise MergeError(one_line(result.stderr or result.stdout or "GitHub request failed"))
        return json.loads(result.stdout) if result.stdout.strip() else None

    def api(self, endpoint, *args):
        return self.json("api", f"repos/{self.repo}/{endpoint}", "--hostname", self.host, *args)

    def snapshot(self, pr):
        return self.json("pr", "view", str(pr), "--repo", self.identity, "--json",
                         "state,isDraft,baseRefName,headRefOid,mergeable,statusCheckRollup,title")

    def base_head(self):
        return self.api(f"commits/{quote(self.base, safe='')}")["sha"]

    def contains_base(self, base, head):
        return self.api(f"compare/{base}...{head}")["merge_base_commit"]["sha"] == base

    def update(self, pr, head):
        self.api(f"pulls/{pr}/update-branch", "--method", "PUT", "-f", f"expected_head_sha={head}")

    def merge(self, pr, head):
        result = self.api(f"pulls/{pr}/merge", "--method", "PUT", "-f", f"sha={head}",
                          "-f", "merge_method=merge")
        if not result.get("merged"):
            raise MergeError(result.get("message", "GitHub did not merge the PR"))
        return result["sha"]

    def tree(self, commit):
        return self.api(f"git/commits/{commit}")["tree"]["sha"]

    def integration(self, base, entries):
        return Integration(self, base, entries)

    def batch_allowed(self):
        # Indirect merges must never bypass required reviews/checks, even if
        # the operator's credentials happen to have administrator privileges.
        branch = quote(self.base, safe="")
        return (self.api(f"branches/{branch}")["protected"] is False
                and not self.api(f"rules/branches/{branch}"))

    def delete_branch(self, branch, head):
        result = subprocess.run(["git", "push", f"--force-with-lease=refs/heads/{branch}:{head}",
                                 self.url, f":refs/heads/{branch}"],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise MergeError(one_line(result.stderr or "integration branch deletion refused; preserved"))


class Integration:
    """Disposable source-only clone, branch and PR; never mutate caller worktrees."""

    def __init__(self, client, base, entries):
        self.client, self.base, self.requested = client, base, entries
        self.entries, self.conflicts = [], []
        self.branch = "merge-line/" + uuid.uuid4().hex
        self.record = state_directory(client.identity) / "batches" / (self.branch.split("/")[-1] + ".json")
        self.data = {"branch": self.branch, "pid": os.getpid(), "start": pid_start(os.getpid())}
        self.temp = None

    def git(self, *args, check=True):
        result = subprocess.run(["git", "-C", self.temp.name,
                                 "-c", "user.name=Hearting merge-line",
                                 "-c", "user.email=merge-line@hearting.local", *args],
                                capture_output=True, text=True, timeout=300)
        if check and result.returncode:
            raise MergeError(one_line(result.stderr or result.stdout))
        return result

    def save(self):
        self.record.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.record.parent)
        with os.fdopen(fd, "w") as out:
            json.dump(self.data, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, self.record)

    def __enter__(self):
        try:
            self.temp = tempfile.TemporaryDirectory(prefix="hearting-merge-line-")
            self.git("init", "-q")
            self.git("remote", "add", "origin", self.client.url)
            heads = [row["head"] for row in self.requested]
            self.git("fetch", "--no-tags", "origin", self.base, *heads)
            self.git("checkout", "-q", "-b", self.branch, self.base)
            for row in self.requested:
                before = self.git("rev-parse", "HEAD").stdout.strip()
                result = self.git("merge", "--no-ff", "-m", f"Merge pull request #{row['pr']}\n\n{row.get('title', '')}",
                                  row["head"], check=False)
                if result.returncode:
                    # A real content conflict alone goes to the single-PR path.
                    conflicts = self.git("diff", "--name-only", "--diff-filter=U").stdout
                    if not conflicts:
                        raise MergeError(one_line(result.stderr or result.stdout))
                    self.git("merge", "--abort")
                    if self.git("rev-parse", "HEAD").stdout.strip() != before:
                        raise MergeError("integration conflict changed the saved prefix")
                    self.conflicts.append(row["pr"])
                    continue
                self.entries.append(dict(row, tree=self.git("rev-parse", "HEAD^{tree}").stdout.strip(),
                                         commit=self.git("rev-parse", "HEAD").stdout.strip()))
            if not self.entries:
                return self
            self.data["head"] = self.git("rev-parse", "HEAD").stdout.strip()
            self.save()  # Recovery covers death during push or PR creation too.
            self.git("push", "origin", f"HEAD:refs/heads/{self.branch}")
            prs = ", ".join(f"#{row['pr']}" for row in self.entries)
            pr = self.client.api("pulls", "--method", "POST", "-f", f"head={self.branch}",
                                 "-f", f"base={self.client.base}", "-f", f"title=묶음 병합 검증: {prs}",
                                 "-f", "body=merge-line의 임시 통합 검사입니다. 원본 PR의 이력과 리뷰를 보존해 각각 병합합니다.")
            self.data["pr"] = pr["number"]
            self.save()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            if self.record.exists():
                cleanup_integration(self.client, self.record, self.data)
        except (MergeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            # Preserve the exact record. The next holder retries cleanup.
            print(f"merge-line: integration cleanup deferred: {one_line(exc)}", flush=True)
        finally:
            if self.temp is not None:
                self.temp.cleanup()

    def validate(self, emit=print, sleep=time.sleep, interval=10):
        head, pr = self.data["head"], self.data["pr"]
        emit(f"merge-line: waiting batch CI PR=#{pr} head={head}")
        deadline = time.monotonic() + 5400
        while True:
            if self.client.base_head() != self.base:
                raise Changed("default branch moved during integration CI")
            pull = self.client.api(f"pulls/{pr}")
            if pull["state"] != "open" or pull["head"]["sha"] != head:
                raise Changed("integration PR changed")
            runs = self.client.api(f"actions/runs?event=pull_request&head_sha={head}&per_page=100")["workflow_runs"]
            # Choose the latest attempt for every workflow. A green job from a
            # failed workflow never authorizes a merge (the PR302 regression).
            latest = {}
            for run in sorted(runs, key=lambda row: row["id"], reverse=True):
                if run["head_sha"] == head and run["event"] == "pull_request":
                    latest.setdefault(run["workflow_id"], run)
            if latest and all(run["status"] == "completed" for run in latest.values()):
                failures = [run["name"] for run in latest.values() if run["conclusion"] != "success"]
                if failures:
                    raise CIFailure("CI failed: " + ", ".join(failures))
                # GitHub executes the PR merge result, not necessarily its head.
                merge_sha = pull.get("merge_commit_sha")
                if merge_sha and self.client.tree(merge_sha) == self.entries[-1]["tree"]:
                    try:
                        ready = check_state(self.client.snapshot(pr))
                    except MergeError as exc:
                        raise CIFailure(str(exc)) from exc
                    if ready:
                        return
                elif merge_sha:
                    raise Changed("tested integration tree changed")
            if time.monotonic() >= deadline:
                raise MergeError("integration CI did not finish within 90 minutes")
            sleep(interval)

    def publish(self):
        # The checked base is an ancestor, so this lease can only fast-forward
        # that exact base. One push avoids untested intermediate main CI runs.
        self.git("merge-base", "--is-ancestor", self.base, self.data["head"])
        if not self.client.batch_allowed():
            raise MergeError("repository merge policy changed; use original PR path")
        try:
            self.git("push", f"--force-with-lease=refs/heads/{self.client.base}:{self.base}",
                     "origin", f"{self.data['head']}:refs/heads/{self.client.base}")
        except (MergeError, subprocess.TimeoutExpired):
            current = self.client.base_head()
            if self.client.contains_base(self.data["head"], current):
                return  # Lost response after a successful push; never replay it.
            if current != self.base:
                raise Changed("default branch moved; lease preserved its successor")
            raise MergeError("integration publication refused; use original PR path")


def cleanup_integration(client, record, data):
    """Only our recorded branch at its exact head may be deleted."""
    branch, head = data["branch"], data["head"]
    if not branch.startswith("merge-line/") or branch.split("/")[-1] != record.stem:
        raise MergeError("integration cleanup identity mismatch")
    prs = client.api(f"pulls?state=open&head={quote(client.repo.split('/')[0] + ':' + branch, safe='')}")
    for pr in prs:
        if pr["head"]["sha"] != head:
            raise MergeError("integration branch changed; preserved")
    refs = client.api(f"git/matching-refs/heads/{quote(branch, safe='/')}")
    exact = [row for row in refs if row["ref"] == f"refs/heads/{branch}"]
    if exact:
        if exact[0]["object"]["sha"] != head:
            raise MergeError("integration branch changed; preserved")
        # Deleting the exact head closes its open temporary PR on GitHub. Do
        # not PATCH it first: a successor push must preserve both branch/PR.
        client.delete_branch(branch, head)
    record.unlink()


def recover_integrations(client, emit=print):
    for record in sorted((state_directory(client.identity) / "batches").glob("*.json")):
        try:
            data = json.loads(record.read_text())
            if pid_start(data["pid"]) != data["start"]:
                cleanup_integration(client, record, data)
        except (MergeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            emit(f"merge-line: integration cleanup deferred: {one_line(exc)}")


def check_state(snapshot):
    checks = snapshot.get("statusCheckRollup") or []
    pending, success, failed = not checks, False, []
    for check in checks:
        if check.get("__typename") == "StatusContext":
            state, name = check.get("state"), check.get("context", "status")
        else:
            state = check.get("conclusion") if check.get("status") == "COMPLETED" else "PENDING"
            name = check.get("name", "check")
        if state == "SUCCESS":
            success = True
        elif state in ("SKIPPED", "NEUTRAL"):
            continue
        elif state in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
                       "STARTUP_FAILURE", "STALE"):
            failed.append(name)
        else:
            pending = True
    if failed:
        raise MergeError("CI failed: " + ", ".join(failed))
    if checks and not pending and not success:
        raise MergeError("CI finished without a successful check")
    return not pending and success


def merge_pr(client, pr, emit=print, sleep=time.sleep, interval=10, admitted=lambda: True):
    last = None
    while True:
        if not admitted():
            raise Withdrawn("caller stopped")
        snapshot = client.snapshot(pr)
        if snapshot["state"] == "MERGED":
            emit(f"merge-line: already merged PR=#{pr}")
            return None
        if snapshot["state"] != "OPEN" or snapshot["isDraft"]:
            raise MergeError("PR is closed or draft")
        if snapshot["baseRefName"] != client.base:
            raise MergeError(f"PR base must be {client.base}")
        if snapshot["mergeable"] == "CONFLICTING":
            raise MergeError("PR has merge conflicts")
        head, base = snapshot["headRefOid"], client.base_head()
        if not client.contains_base(base, head):
            if not admitted():
                raise Withdrawn("caller stopped")
            emit(f"merge-line: updating PR=#{pr} head={head} base={base}")
            client.update(pr, head)
            deadline = time.monotonic() + 120
            while True:
                updated = client.snapshot(pr)
                if updated["headRefOid"] != head or updated["state"] != "OPEN" or updated["isDraft"]:
                    break
                if time.monotonic() >= deadline:
                    raise MergeError("branch update was accepted but the new head is not visible")
                sleep(interval)
            last = None
            continue
        if not check_state(snapshot) or snapshot["mergeable"] != "MERGEABLE":
            status = (head, tuple((c.get("name", c.get("context")), c.get("status"),
                                  c.get("conclusion", c.get("state")))
                                 for c in snapshot.get("statusCheckRollup") or []))
            if status != last:
                emit(f"merge-line: waiting CI PR=#{pr} head={head}")
                last = status
            sleep(interval)
            continue
        # No other participant can merge during this turn. Catch outside pushes
        # or raw merges too; the server then checks the exact head atomically.
        current = client.snapshot(pr)
        if current["headRefOid"] != head or client.base_head() != base:
            last = None
            continue
        if current["state"] != "OPEN":
            continue
        if current["isDraft"] or current["baseRefName"] != client.base:
            raise MergeError("PR became draft or changed its base")
        if not check_state(current) or current["mergeable"] != "MERGEABLE":
            sleep(interval)
            continue
        if not admitted():
            raise Withdrawn("caller stopped")
        commit = client.merge(pr, head)
        emit(f"merge-line: merged PR=#{pr} head={head} commit={commit}")
        return commit


def outcome(pr, ok, detail):
    return {"ok": ok, "message": f"merge-line: {'merged' if ok else 'failed'} PR=#{pr}: {one_line(detail)}"}


def merge_batch(client, prs, settle, emit=print, sleep=time.sleep, admitted=lambda _: True):
    """Bisect failed CI, consume each exact PR once, continue eligible siblings."""
    pending = list(dict.fromkeys(prs))

    def finish(pr, ok, detail):
        result = outcome(pr, ok, detail)
        emit(result["message"])
        settle(pr, result)
        if pr in pending:
            pending.remove(pr)

    def single(pr):
        if pr not in pending:
            return
        try:
            if not admitted(pr):
                raise Withdrawn("caller stopped")
            commit = merge_pr(client, pr, emit=emit, sleep=sleep, admitted=lambda: admitted(pr))
            finish(pr, True, commit or "already merged")
        except Withdrawn:
            pending.remove(pr)
        except MergeError as exc:
            finish(pr, False, exc)

    def process(group):
        group = [pr for pr in group if pr in pending]
        while group:
            entries = []
            for pr in group:
                if not admitted(pr):
                    pending.remove(pr)
                    continue
                current = client.snapshot(pr)
                if current["state"] == "MERGED":
                    finish(pr, True, "already merged")
                elif current["state"] != "OPEN" or current["isDraft"]:
                    finish(pr, False, "PR is closed or draft")
                elif current["baseRefName"] != client.base:
                    finish(pr, False, f"PR base must be {client.base}")
                else:
                    entries.append({"pr": pr, "head": current["headRefOid"], "title": current.get("title", "")})
            group = [row["pr"] for row in entries]
            if not group:
                return
            base = client.base_head()
            conflicts = []
            try:
                with client.integration(base, entries) as batch:
                    conflicts = batch.conflicts
                    if batch.entries:
                        batch.validate(emit=emit, sleep=sleep)
                        if client.base_head() != base:
                            raise Changed("default branch moved after CI")
                        for row in batch.entries:
                            pr, head = row["pr"], row["head"]
                            current = client.snapshot(pr)
                            if (current["state"] != "OPEN" or current["isDraft"]
                                    or current["headRefOid"] != head
                                    or current["baseRefName"] != client.base
                                    or not admitted(pr)):
                                raise Changed(f"PR #{pr} or default branch changed after CI")
                        if client.base_head() != base:
                            raise Changed("default branch changed before publication")
                        try:
                            batch.publish()
                        except Changed:
                            raise
                        except MergeError as exc:
                            emit(f"merge-line: {one_line(exc)}")
                            for row in batch.entries:
                                single(row["pr"])
                        else:
                            changed = []
                            for row in batch.entries:
                                try:
                                    current = client.snapshot(row["pr"])
                                except (MergeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                                    emit(f"merge-line: published PR=#{row['pr']}; observation unavailable: {one_line(exc)}")
                                    current = {"headRefOid": row["head"], "state": "UNKNOWN"}
                                if current["headRefOid"] != row["head"] and current["state"] == "OPEN":
                                    changed.append(row["pr"])
                                    continue
                                finish(row["pr"], True, f"head={row['head']} commit={row['commit']}")
                            if changed:
                                raise Changed(f"PRs {changed} received new heads during publication")
                for pr in conflicts:
                    emit(f"merge-line: integration conflict PR=#{pr}; using original path")
                    single(pr)
                return
            except Changed as exc:
                emit(f"merge-line: rebuilding remaining batch: {one_line(exc)}")
                group = [pr for pr in group if pr in pending]
                continue
            except CIFailure as exc:
                # A failed old head is no more authoritative than a passed old
                # head. Recheck before assigning blame or splitting the group.
                if client.base_head() != base:
                    emit("merge-line: failed batch base changed; rebuilding")
                    continue
                stale = False
                for row in entries:
                    current = client.snapshot(row["pr"])
                    if (current["headRefOid"] != row["head"] or current["state"] != "OPEN"
                            or current["isDraft"] or current["baseRefName"] != client.base):
                        stale = True
                        break
                if stale:
                    emit("merge-line: failed batch subject changed; rebuilding")
                    continue
                tested = [pr for pr in group if pr not in conflicts]
                if len(tested) == 1:
                    finish(tested[0], False, exc)
                elif tested:
                    emit(f"merge-line: splitting failed batch PRs={','.join(map(str, tested))}")
                    mid = len(tested) // 2
                    process(tested[:mid])
                    process(tested[mid:])
                for pr in conflicts:
                    single(pr)
                return

    try:
        process(pending[:])
    except (MergeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        for pr in pending[:]:
            finish(pr, False, exc)


def run_turn(client, turn, emit=print, sleep=time.sleep):
    if turn.result is not None:
        emit(turn.result["message"])
        return turn.result["ok"]
    recover_integrations(client, emit=emit)
    # One brief admission window coalesces simultaneous callers. A backlog
    # accumulated during the preceding CI is already available immediately.
    prs = turn.collect()
    if len(prs) == 1:
        sleep(1)
        prs = turn.collect()
    allowed = False
    if len(prs) > 1:
        try:
            allowed = client.batch_allowed()
        except (MergeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            emit(f"merge-line: batch policy unavailable; using original path: {one_line(exc)}")
    if allowed:
        emit(f"merge-line: batch PRs={','.join(map(str, prs))}")
        merge_batch(client, prs, turn.settle, emit=emit, sleep=sleep,
                    admitted=lambda pr: pr in turn.collect())
    else:
        try:
            commit = merge_pr(client, turn.pr, emit=emit, sleep=sleep)
            turn.settle(turn.pr, outcome(turn.pr, True, commit or "already merged"))
        except MergeError as exc:
            turn.settle(turn.pr, outcome(turn.pr, False, exc))
            emit(turn.result["message"])
    return turn.result["ok"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", type=int, help="PR number in the current GitHub repository")
    args = parser.parse_args()
    if args.pr < 1:
        parser.error("PR number must be positive")
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        client = GitHub()
        emit = lambda line: print(one_line(line), flush=True)
        with MergeTurn(state_directory(client.identity), args.pr, emit=emit) as turn:
            return 0 if run_turn(client, turn, emit=emit) else 1
    except KeyboardInterrupt:
        print(f"merge-line: stopped PR=#{args.pr}; turn released", flush=True)
        return 130
    except (MergeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"merge-line: failed PR=#{args.pr}: {one_line(exc)}; turn released", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
