"""Keep an approved, runtime-created repository bound across first-leg handoffs."""
from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import os
import subprocess
from pathlib import Path

_CURRENT = contextvars.ContextVar("approved_repository", default=None)
_HANDOFF = "HEARTING_REPOSITORY_HANDOFF"


class RepositoryBindingError(ValueError):
    reason = "repository-preparation-pending"


class RepositoryBinding:
    def __init__(self, record, index=0):
        self.record = record or {}
        self.setup = ((self.record.get("decision") or {}).get("first_leg_compose") or {}).get("repository_setup")
        self.active = bool(self.setup and (
            self.setup.get("state") == "create-after-approval"
            or (self.record.get("first_leg") or {}).get("repository_created")))
        self.opened = contextlib.ExitStack()
        self.fds = {}

    def changed(self):
        return RepositoryBindingError(
            "repository-preparation-manual: approved parent, destination or Git directory was replaced; "
            "first-leg handoff stopped")

    @staticmethod
    def same(stamp, evidence):
        return (stamp.st_dev, stamp.st_ino) == (evidence.get("device"), evidence.get("inode"))

    def open_directory(self, key, path, *, dir_fd=None):
        if key not in self.fds:
            fd = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            self.opened.callback(os.close, fd)
            self.fds[key] = fd
        return self.fds[key]

    def __enter__(self):
        try:
            if self.active:
                self.path = Path(self.setup["cwd"])
                self.parent = self.setup.get("parent") or {}
                self.open_directory("parent", self.path.parent)
                self.bind(self.record)
            self.token = _CURRENT.set(self if self.active else None)
            return self
        except OSError as exc:
            self.opened.close()
            raise self.changed() from exc
        except BaseException:
            self.opened.close()
            raise

    def __exit__(self, *exc):
        _CURRENT.reset(self.token)
        return self.opened.__exit__(*exc)

    def bind(self, record):
        """Retain the created leaf/Git descriptors as preparation adds its evidence."""
        self.record = record
        if not self.active:
            return
        first = record.get("first_leg") or {}
        try:
            self.verify()
            if first.get("repository_created"):
                leaf = self.open_directory("leaf", self.path.name, dir_fd=self.fds["parent"])
                if first.get("repository_initialized"):
                    self.open_directory("git", self.setup.get("git_path", ".git"), dir_fd=leaf)
            self.verify()
        except OSError as exc:
            raise self.changed() from exc

    def verify(self, cwd=None):
        if not self.active:
            return
        first = self.record.get("first_leg") or {}
        try:
            if (str(self.path.parent.resolve()) != self.parent.get("path")
                    or not self.same(os.fstat(self.fds["parent"]), self.parent)
                    or not self.same(self.path.parent.lstat(), self.parent)):
                raise self.changed()
            for key, evidence, name, parent in (
                    ("leaf", first.get("repository_created"), self.path.name, "parent"),
                    ("git", first.get("repository_initialized"), self.setup.get("git_path", ".git"), "leaf")):
                if key not in self.fds:
                    continue
                visible = os.stat(name, dir_fd=self.fds[parent], follow_symlinks=False)
                if (not evidence or not self.same(os.fstat(self.fds[key]), evidence)
                        or not self.same(visible, evidence)):
                    raise self.changed()
            if "leaf" in self.fds:
                if (self.path.is_symlink() or str(self.path.resolve()) != str(self.path)
                        or not self.same(self.path.lstat(), first["repository_created"])):
                    raise self.changed()
                if cwd is not None and (str(Path(cwd).resolve()) != str(self.path)
                                        or not self.same(Path(cwd).stat(), first["repository_created"])):
                    raise self.changed()
        except OSError as exc:
            raise self.changed() from exc


def capture_source(path):
    """Optional compose provenance; no initialization, new input or launch gate."""
    root = Path(path)
    active = current()
    if active and "leaf" in active.fds and root == active.path:
        def held(key, label):
            stamp = os.fstat(active.fds[key])
            return {"path": str(label), "device": stamp.st_dev, "inode": stamp.st_ino}
        result = {"worktree": held("leaf", root), "parent": held("parent", root.parent)}
        if "git" in active.fds:
            result["git_common_dir"] = held("git", active.setup.get("git_path", root / ".git"))
        return result
    def identity(value):
        stamp = value.stat()
        return {"path": str(value), "device": stamp.st_dev, "inode": stamp.st_ino}
    try:
        result = {"worktree": identity(root), "parent": identity(root.parent)}
    except OSError as exc:
        raise RepositoryBindingError("source-directory-changed-during-compose") from exc
    try:
        probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-common-dir"],
                               text=True, capture_output=True, timeout=10)
        if probe.returncode == 0 and probe.stdout.strip():
            result["git_common_dir"] = identity((root / probe.stdout.strip()).resolve())
    except (OSError, subprocess.SubprocessError):
        pass  # Git provenance is optional; source-directory identity is known.
    return result


def _selected_repository(route, route_file=None):
    source = route.get("source_repository")
    if not source:
        return RepositoryBinding(None)  # old explicit recovery keeps its prior policy
    root = source["worktree"]
    if root["path"] != route["cwd"]:
        raise RepositoryBindingError("repository-preparation-pending: source identity differs from route cwd")
    git = source.get("git_common_dir")
    setup = {"cwd": root["path"], "parent": source["parent"], "state": "bound-route"}
    if git:
        setup["git_path"] = git["path"]
    # Internal transport representation only: existing-directory provenance
    # never emits repository-created facts in the route or owner task.
    record = {"decision": {"first_leg_compose": {"repository_setup": setup,
              "context": {"artifact_root": route["artifact_root"]}}}, "first_leg": {
              "repository_created": root, "repository_baseline": {"commit": route.get("source_commit")},
              "route": {"route_file": str(route_file or (Path(route["artifact_root"]) /
                        ".runtime/routes" / (route["route_id"] + ".json")))}}}
    if git:
        record["first_leg"]["repository_initialized"] = git
    return RepositoryBinding(record)


def for_route(route, route_file=None):
    """Reopen the same evidence for an independent/resumed first-leg launch."""
    plan = route.get("route_plan") or {}
    if not isinstance(plan.get("index"), int) or plan["index"] < 0:
        return _selected_repository(route, route_file)
    import route_plan
    try:
        binding = route_plan.read_route_plan(f"{plan['decision']}#{plan['index']}", route["artifact_root"])
    except (OSError, ValueError) as exc:
        raise RepositoryBindingError("route-decision-conflict: first-leg repository decision unavailable") from exc
    if binding["digest"] != plan["digest"]:
        raise RepositoryBindingError("route-decision-conflict: first-leg repository decision changed")
    record = binding["record"]
    first = record.get("first_leg") or {}
    created = first.get("repository_created")
    bound = first.get("route") or first.get("snapshot") or {}
    # The existing explicit --route-plan --cwd recovery may choose another
    # repository. It does not adopt the runtime-created destination's evidence.
    if not created or (route.get("cwd") != created["path"] and bound.get("route_id") != route.get("route_id")):
        return _selected_repository(route, route_file)
    result = RepositoryBinding(record)
    result.authority_file = str(route_file or (Path(route["artifact_root"]) /
                               ".runtime/routes" / (route["route_id"] + ".json")))
    return result


def _handoff(env=None):
    raw = (os.environ if env is None else env).get(_HANDOFF)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("record"), dict):
            raise ValueError("invalid handoff")
        setup = data["record"]["decision"]["first_leg_compose"]["repository_setup"]
        if (not isinstance(setup, dict) or not Path(setup["cwd"]).is_absolute()
                or not isinstance(data["record"]["first_leg"], dict)
                or not isinstance(data["fds"], dict) or not isinstance(data["route_file"], str)):
            raise ValueError("invalid handoff")
        return data
    except (ValueError, TypeError, KeyError) as exc:
        raise RepositoryBindingError("repository-preparation-pending: invalid directory handoff") from exc


def current():
    return _CURRENT.get()


def execution_path(path):
    """An IO alias to the live directory; canonical labels stay in saved records."""
    binding = current()
    if binding is None or "leaf" not in binding.fds:
        return str(path)
    try:
        relative = Path(os.path.abspath(path)).relative_to(binding.path)
    except ValueError:
        return str(path)
    # Other synchronous helpers can use this alias without inheriting our fd.
    return str(Path(f"/proc/{os.getpid()}/fd/{binding.fds['leaf']}") / relative)


def native_worktree(path):
    """Native exec starts in the bound cwd; do not cd back through its label."""
    return "." if execution_path(path) != str(path) else str(path)


def canonical_worktree(path):
    binding = current()
    if binding and (str(path) == str(binding.path) or str(path).startswith("/proc/")):
        if RepositoryBinding.same(Path(path).stat(), binding.record["first_leg"]["repository_created"]):
            return str(binding.path)
        raise binding.changed()
    return str(Path(path).resolve())


def child_argv(argv):
    values = list(argv)
    binding = current()
    if binding and "leaf" in binding.fds and "--worktree" in values:
        index = values.index("--worktree") + 1
        if values[index] == str(binding.path):
            values[index] = f"/proc/self/fd/{binding.fds['leaf']}"
    return values


def durable_argv(argv):
    values = list(argv)
    if current() and "--worktree" in values:
        index = values.index("--worktree") + 1
        values[index] = canonical_worktree(values[index])
    return values


def child_options(*, env=None, cwd=None, pass_fds=()):
    """Actual Popen inputs, shared by every execution hop (no user arguments)."""
    binding = current()
    options = {}
    if env is not None:
        options["env"] = dict(env)
    if cwd is not None:
        options["cwd"] = cwd
    if pass_fds:
        options["pass_fds"] = tuple(pass_fds)
    if binding is None or "leaf" not in binding.fds:
        return options
    binding.verify()
    child_env = dict(os.environ if env is None else env)
    route_file = getattr(binding, "authority_file", None)
    if route_file is None:
        context = binding.record["decision"]["first_leg_compose"].get("context") or {}
        published = (binding.record.get("first_leg") or {}).get("route") or {}
        if not context.get("artifact_root") or not published.get("route_file"):
            raise RepositoryBindingError("repository-preparation-pending: route not published")
        route_file = str(Path(context["artifact_root"]) / published["route_file"])
    child_env[_HANDOFF] = json.dumps({"version": 1, "record": {
        "decision": {"first_leg_compose": {"repository_setup": binding.setup}},
        "first_leg": {key: value for key, value in binding.record["first_leg"].items()
                      if key in ("repository_created", "repository_initialized", "repository_baseline")}},
        "fds": {"leaf": binding.fds["leaf"]}, "route_file": route_file}, separators=(",", ":"))
    options["env"] = child_env
    options["pass_fds"] = tuple(dict.fromkeys((*pass_fds, binding.fds["leaf"])))
    options["cwd"] = (f"/proc/self/fd/{binding.fds['leaf']}" if cwd is None
                      or execution_path(cwd) != str(cwd) else cwd)
    return options


def handoff_environment():
    options = child_options()
    return {_HANDOFF: options["env"][_HANDOFF]} if _HANDOFF in options.get("env", {}) else {}


def child_environment(env):
    return {**env, **handoff_environment()}


def child_process_options(*, cwd=True, pass_fds=()):
    options = child_options(pass_fds=pass_fds)
    options.pop("env", None)
    if not cwd:
        options.pop("cwd", None)
    return options


def native_environment(env):
    result = dict(env)
    result.pop(_HANDOFF, None)
    return result


def native_process_options(path=None):
    """Pin cwd before exec without leaking directory capabilities to the model."""
    binding = current()
    if binding is None:
        return {"cwd": path} if path is not None else {}
    binding.verify()
    return {"cwd": execution_path(binding.path if path is None else path)}


def rpc_worktree(path, server):
    binding = current()
    if binding is None or execution_path(path) == str(path):
        return str(path)
    relative = Path(os.path.abspath(path)).relative_to(binding.path)
    process = getattr(server, "process", None)
    pid = getattr(process, "pid", None)
    if binding and type(pid) is int:
        if process.poll() is not None:
            raise binding.changed()
        candidate = Path(f"/proc/{pid}/cwd")
        if not binding.same(candidate.stat(), binding.record["first_leg"]["repository_created"]):
            raise binding.changed()
        return str(candidate / relative)
    return execution_path(path)


def native_path(path):
    """A shell/payload alias that survives the short-lived adapter's exit."""
    binding = current()
    if binding and execution_path(path) != str(path):
        relative = Path(os.path.abspath(path)).relative_to(binding.path)
        return str(Path("/proc/self/cwd") / relative)
    return str(path)


def runtime_paths(value, server=None):
    if isinstance(value, dict):
        return {runtime_paths(key, server): runtime_paths(item, server) for key, item in value.items()}
    if isinstance(value, list):
        return [runtime_paths(item, server) for item in value]
    if isinstance(value, str) and Path(value).is_absolute() and execution_path(value) != value:
        return rpc_worktree(value, server) if server is not None else native_path(value)
    return value


def runtime_request(params, server):
    result = dict(params)
    # Prompt bytes and logical session identifiers are never filesystem paths.
    for key in ("cwd", "sandboxPolicy", "permissions"):
        if key in result:
            result[key] = runtime_paths(result[key], server)
    return result


@contextlib.contextmanager
def process_binding(argv=(), env=None):
    """Consume inherited fds, or reopen their recorded identity on a fresh resume."""
    environment = os.environ if env is None else env
    argv = list(argv)
    if "--" in argv:
        argv = argv[:argv.index("--")]
    data = _handoff(environment)
    if data and "--worktree" in argv:
        given = argv[argv.index("--worktree") + 1]
        origin = data["record"]["decision"]["first_leg_compose"]["repository_setup"]["cwd"]
        if given not in (origin, ".") and not str(given).startswith("/proc/"):
            # An independently selected repository keeps its existing flow.
            with RepositoryBinding(None) as inactive:
                yield inactive
            return
    active = current()
    if data and active and all(active.fds.get(key) == fd for key, fd in data.get("fds", {}).items()):
        active.verify()
        yield active
        return
    if data is None:
        # Direct wrapper/resume entry also uses the existing sealed route.
        route_file = (argv[argv.index("--route-file") + 1] if "--route-file" in argv
                      else environment.get("AGENT_OWNER_ROUTE_FILE"))
        try:
            route = json.loads(Path(route_file).read_text()) if route_file else {}
        except (OSError, ValueError):
            # The normal route validator owns missing/invalid route receipts.
            # Prepared launches carry their bound descriptors above this path.
            route = {}
        with for_route(route, route_file) as binding:
            yield binding
        return
    # The environment is transport, not approval authority. Re-read the sealed
    # route's creation evidence before adopting or reopening its descriptors.
    try:
        route = json.loads(Path(data["route_file"]).read_text())
        binding = for_route(route, data["route_file"])
        expected = {key: value for key, value in binding.record.get("first_leg", {}).items()
                    if key in ("repository_created", "repository_initialized", "repository_baseline")}
        if (not binding.active or binding.setup != data["record"]["decision"]["first_leg_compose"]["repository_setup"]
                or expected != data["record"]["first_leg"]):
            raise ValueError("binding evidence mismatch")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RepositoryBindingError("repository-preparation-pending: directory handoff differs from approved record") from exc
    path = binding.setup["cwd"]
    given = argv[argv.index("--worktree") + 1] if "--worktree" in argv else path
    if given not in (path, ".") and not str(given).startswith("/proc/"):
        with RepositoryBinding(None) as inactive:
            yield inactive
        return
    first = binding.record["first_leg"]
    evidence = {"parent": binding.setup["parent"], "leaf": first["repository_created"]}
    if first.get("repository_initialized"):
        evidence["git"] = first["repository_initialized"]
    fds = data.get("fds") or {}
    try:
        valid = ("leaf" in fds and set(fds) <= set(evidence) and all(type(fd) is int and fd >= 3
                 and binding.same(os.fstat(fd), evidence[key]) for key, fd in fds.items()))
    except OSError:
        valid = False
    if valid:
        # This process owns the inherited descriptors; no pathname is reopened.
        binding.fds = dict(fds)
        for fd in fds.values():
            binding.opened.callback(os.close, fd)
    with binding:
        yield binding


def process_entry(function):
    @functools.wraps(function)
    def bound(*args, **kwargs):
        argv = args[0] if args and args[0] is not None else __import__("sys").argv[1:]
        try:
            with process_binding(argv):
                return function(*args, **kwargs)
        except RepositoryBindingError as exc:
            print(f"check=failed\nreason={exc}\nchild_spawned=0")
            return 65
    return bound


def forwarded_entry(function):
    @functools.wraps(function)
    def bound(command, **kwargs):
        with process_binding(command, kwargs.get("env")):
            options = child_options(env=kwargs.get("env"), cwd=kwargs.get("cwd"),
                                    pass_fds=kwargs.get("pass_fds", ()))
            return function(child_argv(command), **{**kwargs, **options})
    return bound
