"""Keep an approved, runtime-created repository bound across first-leg handoffs."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path


class RepositoryBindingError(ValueError):
    reason = "repository-preparation-pending"


class RepositoryBinding:
    def __init__(self, record, index=0):
        self.record = record or {}
        self.setup = ((self.record.get("decision") or {}).get("first_leg_compose") or {}).get("repository_setup")
        self.active = bool(index == 0 and self.setup and (
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
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
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
            return self
        except OSError as exc:
            self.opened.close()
            raise self.changed() from exc
        except BaseException:
            self.opened.close()
            raise

    def __exit__(self, *exc):
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
                    self.open_directory("git", ".git", dir_fd=leaf)
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
                    ("git", first.get("repository_initialized"), ".git", "leaf")):
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


def for_route(route):
    """Reopen the same evidence for an independent/resumed first-leg launch."""
    plan = route.get("route_plan") or {}
    if plan.get("index") != 0:
        return RepositoryBinding(None)
    import route_plan
    try:
        binding = route_plan.read_route_plan(f"{plan['decision']}#0", route["artifact_root"])
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
        return RepositoryBinding(None)
    return RepositoryBinding(record)
