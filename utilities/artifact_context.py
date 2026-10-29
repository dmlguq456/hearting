#!/usr/bin/env python3
"""Resolve route-bound outputs, terminal artifacts and source worktrees.

This module contains no persistent state and adds no command-line interface.  It
joins the existing route, producer, parent-attempt, and review bindings so
callers do not reconstruct cycle paths from inherited environment strings.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


SEMANTIC_OUTPUTS = frozenset({
    "applied-artifact", "apply-log", "components", "config",
    "diff-preview", "evidence-map", "experiment-artifact",
    "experiment-scaffold", "final-artifact", "render",
    "research-artifact", "revised-artifact", "ship-checklist",
    "snapshot", "source-diff", "strategy", "summary-stats",
    "tokens", "version-snapshot",
})

_ROLE_OUTPUTS = {
    "test-report": ("test_logs/**",),
    "test-diagnostics": ("test_logs/**",),
    "research-summary": ("analysis_summary.md",),
    "research-cards": ("cards/**",),
    "research-report": ("report/**",),
    "report-source": ("report/**",),
    "report-manifest": ("report/report_manifest.json", "report/**"),
    "ship-config": ("release-config/**",),
    "ship-review": ("reviews/**",),
    "ship-deploy": ("deploy-record.json",),
    "smoke-attestation": ("reviews/smoke-attestation.json",),
    "config-seal": ("reviews/smoke/**", "config/**"),
    "resource-log": ("logs/**",),
    "run-record": ("run.json",),
    "spec-output": ("spec/**",),
}


def _readonly_git_env() -> dict[str, str]:
    """Disable optional index refreshes for a read-only Git observation."""
    env = os.environ.copy()
    contract = sys.modules.get("dispatch_contract")
    helper = getattr(contract, "git_optional_locks_env", None) if contract else None
    env.update(helper() if callable(helper) else {"GIT_OPTIONAL_LOCKS": "0"})
    return env

_ROLE_DEFAULT_LEAF = {
    "test-report": "test_logs/run-tests-report.tsv",
    "test-diagnostics": "test_logs/run-tests-diagnostics",
    "smoke-attestation": "reviews/smoke-attestation.json",
    "report-manifest": "report/report_manifest.json",
    "run-record": "run.json",
}


class ArtifactContextError(ValueError):
    """A route context was missing, contradictory, or outside its declaration."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class OutputBinding:
    role: str
    kind: str
    declared: str
    path: Path | None = None


@dataclass(frozen=True)
class ArtifactContext:
    artifact_root: Path | None
    route: Mapping[str, Any] | None
    node: Mapping[str, Any] | None
    node_id: str | None
    cycle_record: Mapping[str, Any] | None
    cycle_dir: Path | None
    output_dir: Path | None
    source_worktree: Path | None
    binding_source: str
    review_output: Path | None = None
    inherited_cycle_mismatch: bool = False

    @property
    def outputs(self) -> tuple[str, ...]:
        values = (self.node or {}).get("outputs") or ()
        return tuple(value for value in values if isinstance(value, str))

    @property
    def write_scope(self) -> tuple[str, ...]:
        values = (self.node or {}).get("write_scope") or ()
        return tuple(value for value in values if isinstance(value, str))

    def resolve_output(
        self, role: str, *, leaf: str | None = None, explicit: str | Path | None = None,
    ) -> OutputBinding | None:
        """Select a declared output by role, then validate the resulting path.

        Explicit paths are returned as supplied so each CLI can retain its
        historical relative-path base.  Route scope validation still applies
        when the explicit path is cycle-relative and the caller requests it.
        """
        if explicit is not None:
            path = Path(explicit).expanduser()
            if self.output_dir and role not in {"target", "review-output", "report-source"}:
                candidate = path if path.is_absolute() else (Path.cwd() / path)
                try:
                    relative = candidate.resolve(strict=False).relative_to(self.output_dir.resolve(strict=False))
                except ValueError as exc:
                    raise ArtifactContextError("artifact-output-outside-node-scope", str(path)) from exc
                if not scope_matches(relative.as_posix(), self.write_scope):
                    raise ArtifactContextError("artifact-output-outside-node-scope", str(path))
            return OutputBinding(role, "explicit", str(explicit), path)
        if role == "review-output" and self.review_output is not None:
            return OutputBinding(role, "target", str(self.review_output), self.review_output)
        if not self.route or not self.node or not self.output_dir:
            return None
        options = _ROLE_OUTPUTS.get(role)
        if options is None:
            raise ArtifactContextError("artifact-output-role-unknown", role)
        borrowed_part_prefix = None
        if role == "config-seal" and (self.node or {}).get("part"):
            borrowed_part_prefix = _borrowed_part_prefix(self.node)
            if borrowed_part_prefix is None:
                raise ArtifactContextError("artifact-output-role-missing", f"{role}: {list(options)}")
            options = tuple(f"{borrowed_part_prefix}/{expected}" for expected in options)
        if role == "run-record":
            part_io = (self.node or {}).get("part_io") or {}
            mapped = part_io.get("run.json") if isinstance(part_io, Mapping) else None
            declared = [value for value in self.outputs if not is_semantic_output(value)]
            if mapped is not None:
                matches = [value for value in declared if value == mapped]
            else:
                matches = [value for value in declared
                           if value == "run.json" or value.endswith("/run.json")]
            unique = list(dict.fromkeys(matches))
            if len(unique) != 1:
                code = "artifact-output-role-missing" if not unique else "artifact-output-role-ambiguous"
                raise ArtifactContextError(code, f"{role}: {unique or declared}")
            relative = _safe_relative(unique[0])
            if not scope_matches(relative, self.write_scope):
                raise ArtifactContextError("artifact-output-outside-node-scope", relative)
            if leaf is not None and leaf not in (relative, "run.json"):
                raise ArtifactContextError("artifact-output-leaf-not-allowed", role)
            return OutputBinding(role, "artifact", unique[0], self.output_dir / PurePosixPath(relative))
        candidates = []
        declarations = [value for value in self.outputs if not is_semantic_output(value)]
        if role in {"config-seal", "spec-output"}:
            declarations.extend(self.write_scope)
        for expected in options:
            matches = [value for value in declarations if _declaration_matches(value, expected)]
            if matches:
                candidates.extend((expected, value) for value in matches)
        # A literal report manifest declaration is more specific than report/**.
        exact_declarations = [(expected, value) for expected, value in candidates if value == expected]
        if exact_declarations:
            candidates = exact_declarations
        unique = list(dict.fromkeys(value for _, value in candidates))
        if role == "report-source" and len(unique) > 1 and all(value.startswith("report/") for value in unique):
            # This is the route's domain report subtree, selected by the
            # report-source role rather than inferred from unrelated outputs.
            relative = "report"
            return OutputBinding(role, "artifact-subtree", "report/*", self.output_dir / relative)
        if len(unique) != 1:
            code = "artifact-output-role-missing" if not unique else "artifact-output-role-ambiguous"
            raise ArtifactContextError(code, f"{role}: {unique or list(options)}")
        declaration = unique[0]
        relative = leaf if leaf is not None else _ROLE_DEFAULT_LEAF.get(role)
        if _has_pattern(declaration):
            prefix = _static_pattern_prefix(declaration)
            if relative is None:
                if not prefix:
                    raise ArtifactContextError("artifact-output-leaf-required", role)
                relative = prefix
            elif relative == declaration:
                relative = prefix
            elif prefix and relative != prefix and not relative.startswith(prefix + "/"):
                relative = f"{prefix}/{relative}"
        elif relative is None:
            relative = declaration
        elif relative == declaration:
            pass
        elif role not in ("config-seal", "ship-config", "ship-review", "resource-log",
                          "research-cards", "research-report", "report-source", "spec-output"):
            raise ArtifactContextError("artifact-output-leaf-not-allowed", role)
        relative = _safe_relative(relative)
        if re.search(r"<[a-zA-Z0-9_-]+>", relative):
            raise ArtifactContextError("artifact-output-placeholder-unresolved", relative)
        if not scope_matches(relative, self.write_scope):
            raise ArtifactContextError("artifact-output-outside-node-scope", relative)
        path = self.output_dir / PurePosixPath(relative)
        if borrowed_part_prefix is not None:
            try:
                resolved_relative = path.resolve(strict=False).relative_to(
                    self.output_dir.resolve(strict=False))
            except ValueError as exc:
                raise ArtifactContextError("artifact-output-outside-node-scope", relative) from exc
            if not scope_matches(resolved_relative.as_posix(), self.write_scope):
                raise ArtifactContextError("artifact-output-outside-node-scope", relative)
        return OutputBinding(role, "artifact", declaration, path)

    def resolve_bucket(self, bucket: str) -> OutputBinding:
        """Resolve a producer query's exact declared top-level bucket."""
        if not self.route or not self.node or not self.output_dir:
            raise ArtifactContextError("artifact-cycle-unbound", bucket)
        try:
            bucket = _safe_relative(bucket)
        except ArtifactContextError:
            raise ArtifactContextError("artifact-output-role-missing", bucket)
        declarations = [*self.outputs, *self.write_scope]
        wanted = {bucket, bucket + "/**"}
        matches = list(dict.fromkeys(
            value for value in declarations
            if value in wanted and not is_semantic_output(value)
        ))
        if not matches:
            raise ArtifactContextError("artifact-output-role-missing", bucket)
        if len(matches) > 1:
            raise ArtifactContextError("artifact-output-role-ambiguous", bucket)
        if not scope_matches(bucket, self.write_scope):
            raise ArtifactContextError("artifact-output-outside-node-scope", bucket)
        return OutputBinding("bucket:" + bucket, "artifact", matches[0], self.output_dir / bucket)

    def resolve_terminal_artifact(self, relative: str) -> Path | None:
        """Resolve a terminal handoff only when it names this node's declared output.

        A terminal `artifact:` value may be cycle-relative. That interpretation is
        deliberately narrower than general artifact selection: both the node's
        output declaration and write scope must authorize the relative path.
        Logical outputs such as `revised-artifact` remain targets and are handled
        by the caller's existing root/worktree compatibility rules.
        """
        if not self.route or not self.node or not self.output_dir or not self.artifact_root:
            return None
        relative = _safe_relative(relative)
        parts = PurePosixPath(relative).parts
        declarations = [value for value in self.outputs if not is_semantic_output(value)]
        if (not scope_matches(relative, self.write_scope)
                or not any(_path_pattern_matches(parts, _scope_parts(value)) for value in declarations)):
            return None

        root = self.artifact_root.resolve(strict=False)
        output_dir = self.output_dir
        if not output_dir.is_absolute():
            raise ArtifactContextError("artifact-output-not-absolute", str(output_dir))
        lexical_output = Path(os.path.abspath(output_dir))
        try:
            output_relative = lexical_output.relative_to(root)
        except ValueError as exc:
            raise ArtifactContextError("artifact-output-outside-root", str(output_dir)) from exc
        cursor = root
        for component in output_relative.parts:
            cursor = cursor / component
            if cursor.is_symlink():
                raise ArtifactContextError("artifact-terminal-output-symlink", str(cursor))
        resolved_output = output_dir.resolve(strict=False)
        if resolved_output != lexical_output or resolved_output != root / output_relative:
            raise ArtifactContextError("artifact-terminal-output-symlink", str(output_dir))

        candidate = resolved_output.joinpath(*parts)
        cursor = resolved_output
        for component in parts:
            cursor = cursor / component
            if cursor.is_symlink():
                raise ArtifactContextError("artifact-terminal-output-symlink", str(cursor))
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(resolved_output)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ArtifactContextError("artifact-output-outside-node-scope", str(candidate)) from exc
        return resolved


def is_semantic_output(value: str) -> bool:
    """Use topology's existing semantic vocabulary without treating it as a path."""
    return value in SEMANTIC_OUTPUTS or any(
        re.fullmatch(re.escape(token) + r"-[a-z0-9]+(?:-[a-z0-9]+)*", value)
        for token in SEMANTIC_OUTPUTS
    )


def classify_scope(value: str) -> str:
    """Classify existing scope vocabulary by its owner."""
    if value == "source-scoped" or value in ("source/**", "source-alternative/**"):
        return "source"
    if value.startswith("tests/"):
        return "source"
    if value == "target-artifact":
        return "target"
    return "artifact"


def scope_values(value) -> list[str]:
    """Normalize the existing semicolon-delimited worker scope argument."""
    values = value.split(";") if isinstance(value, str) else value or ()
    return sorted(part for part in values if isinstance(part, str) and part)


def scope_matches(relative: str, scopes) -> bool:
    """Component-aware match for a route scope; `**` crosses components."""
    try:
        path = _safe_relative(relative)
    except ArtifactContextError:
        return False
    parts = PurePosixPath(path).parts
    for scope in scopes or ():
        if not isinstance(scope, str) or not scope or classify_scope(scope) != "artifact":
            continue
        if _path_pattern_matches(parts, _scope_parts(scope)):
            return True
    return False


def render_outputs(output_dir: str | Path | None, outputs) -> list[str]:
    """Render declared outputs without turning semantic tokens into filenames."""
    rendered = []
    for value in outputs or ():
        if not isinstance(value, str):
            continue
        if is_semantic_output(value):
            rendered.append(value)
        elif output_dir:
            rendered.append(str(Path(output_dir) / PurePosixPath(value)))
        else:
            rendered.append(value)
    return rendered


def resolve_context(
    *, environ: Mapping[str, str] | None = None, route=None,
    node_id: str | None = None, parent_attempt_id: str | None = None,
    explicit: Mapping[str, Any] | None = None, require_producer_binding: bool = False,
    route_file: str | Path | None = None,
) -> ArtifactContext:
    """Resolve the exact route cycle and source worktree from existing bindings.

    Precedence is review binding, exact owner producer binding, then the
    producer's route-lineage lookup. Inherited cycle variables are consistency
    evidence only; they never select a cycle. A verified binding therefore
    survives a stale inherited cycle tuple, while an unbound stale tuple is
    refused.
    """
    env = os.environ if environ is None else environ
    explicit = explicit or {}
    producer = _producer_module()
    route_ref = route if route is not None else explicit.get("route")
    if route_ref is None:
        route_ref = env.get("AGENT_ROUTE_FILE") or env.get("AGENT_ROUTE_ID") or None
    raw_route = route_ref if isinstance(route_ref, Mapping) else None
    artifact_root_value = (explicit.get("artifact_root") or env.get("AGENT_ARTIFACT_ROOT")
                           or (raw_route or {}).get("artifact_root"))
    route_path = Path(route_file) if route_file is not None else None
    if route_ref is not None and not raw_route and route_path is None:
        route_path = Path(str(route_ref))
        if not route_path.is_absolute() and artifact_root_value:
            route_path = Path(artifact_root_value) / ".runtime" / "routes" / f"{route_path.name}.json"
        if not artifact_root_value:
            try:
                preview = json.loads(route_path.read_text(encoding="utf-8"))
                artifact_root_value = preview.get("artifact_root")
            except (OSError, ValueError, AttributeError):
                return _empty_context()
    if not artifact_root_value or route_ref is None:
        return _empty_context()
    root = Path(artifact_root_value).expanduser().resolve(strict=False)
    if not root.is_absolute():
        raise ArtifactContextError("artifact-root-not-absolute", str(root))
    try:
        if raw_route:
            if route_path is not None:
                route_data = producer.load_route(root, route_path, expected_identity=raw_route)
            else:
                route_data = producer.load_route(root, Path(str(raw_route.get("route_id", ""))))
                if route_data.get("route_hash") != raw_route.get("route_hash"):
                    raise ArtifactContextError("route-binding-mismatch", "route hash changed")
        else:
            route_data = producer.load_route(root, route_path)
    except ArtifactContextError:
        raise
    except Exception as exc:
        raise ArtifactContextError(getattr(exc, "code", "route-context-invalid"), str(exc)) from exc
    env_route_id = env.get("AGENT_ROUTE_ID")
    if env_route_id and env_route_id != route_data.get("route_id"):
        raise ArtifactContextError("route-binding-mismatch", "AGENT_ROUTE_ID")
    requested_node = node_id or explicit.get("node_id") or None
    env_node = env.get("AGENT_ROUTE_NODE") or None
    if requested_node and env_node and requested_node != env_node:
        raise ArtifactContextError("route-node-mismatch", f"expected={env_node} observed={requested_node}")
    selected_node = requested_node or env_node
    node = next((item for item in route_data.get("nodes", [])
                 if item.get("id") == selected_node), None) if selected_node else None
    if selected_node and node is None:
        raise ArtifactContextError("route-node-mismatch", str(selected_node))

    source = _verified_worktree(route_data, env, explicit)
    cycle_record = None
    review_output = None
    binding_source = "route-only"
    owner_attempt = parent_attempt_id or explicit.get("parent_attempt_id") or env.get("AGENT_DISPATCH_PARENT_ATTEMPT_ID")
    review_values = tuple(env.get(key, "") for key in (
        "AGENT_REVIEW_OUTPUT", "AGENT_REVIEW_CYCLE_ID", "AGENT_REVIEW_PRODUCER_ID"))
    if any(review_values):
        if not all(review_values):
            raise ArtifactContextError("review-binding-incomplete")
        jobs_value = env.get("AGENT_DISPATCH_JOBS")
        attempt_id = env.get("AGENT_DISPATCH_ATTEMPT_ID")
        unit = env.get("AGENT_DISPATCH_UNIT") or "qa/code-review"
        if not jobs_value or not attempt_id or not source:
            raise ArtifactContextError("review-binding-context-missing")
        try:
            import dispatch_contract
            checked = dispatch_contract.validate_review_output_binding(
                jobs_value, attempt_id=attempt_id, output_path=review_values[0],
                cycle_id=review_values[1], producer_id=review_values[2],
                capability=str(route_data.get("capability", "")), unit=unit,
                worktree=source, artifact_root=root,
            )
            cycle_record = producer.read_cycle_record(root, str(checked["cycle_id"]))
            if not cycle_record or cycle_record.get("producer_id") != checked["producer_id"]:
                raise ArtifactContextError("review-binding-cycle-mismatch")
            review_output = Path(str(checked["output_path"]))
            binding_source = "review-binding"
        except ArtifactContextError:
            raise
        except Exception as exc:
            raise ArtifactContextError(getattr(exc, "code", "review-binding-invalid"), str(exc)) from exc
    elif owner_attempt:
        import dispatch_terminal_commit
        try:
            loaded = dispatch_terminal_commit.load_producer_binding(
                artifact_root=root, route_id=str(route_data["route_id"]),
                owner_attempt_id=str(owner_attempt),
            )
            bound = loaded.binding or {}
            if (require_producer_binding
                    and (bound.get("route_id") != route_data.get("route_id")
                         or bound.get("route_hash") != route_data.get("route_hash"))):
                raise ArtifactContextError("producer-binding-route-mismatch", "route identity")
            if require_producer_binding and route_path is not None and bound.get("route_file"):
                if Path(str(bound["route_file"])).resolve(strict=False) != route_path.resolve(strict=False):
                    raise ArtifactContextError("producer-binding-route-mismatch", "route file")
            campaign_id, cycle_id = bound.get("campaign_id"), bound.get("cycle_id")
            candidate = producer.read_cycle_record(root, str(cycle_id or ""))
            if not candidate or candidate.get("campaign_id") != campaign_id:
                raise ArtifactContextError("producer-binding-cycle-mismatch")
            admission = producer.cycle_route_admission(root, candidate, route_data)
            if not admission.allow:
                raise ArtifactContextError(admission.reason or "producer-binding-route-mismatch", admission.detail)
            cycle_record = candidate
            binding_source = "producer-binding"
        except ArtifactContextError:
            raise
        except Exception as exc:
            if getattr(exc, "code", "") == "producer-binding-required" and require_producer_binding:
                raise ArtifactContextError("producer-binding-required", str(exc)) from exc
            if getattr(exc, "code", "") not in {"producer-binding-required"}:
                raise ArtifactContextError(getattr(exc, "code", "producer-binding-invalid"), str(exc)) from exc

    if require_producer_binding and binding_source != "producer-binding":
        raise ArtifactContextError("producer-binding-required", str(owner_attempt or "owner-attempt"))

    if cycle_record is None and binding_source != "review-binding":
        try:
            cycle_record = producer.route_cycle_for(root, route_data)
        except Exception as exc:
            raise ArtifactContextError(getattr(exc, "code", "route-cycle-binding-invalid"), str(exc)) from exc
        if cycle_record is not None:
            binding_source = "route-lineage"

    env_has_cycle = any(env.get(name) for name in (
        "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID",
        "AGENT_ARTIFACT_PRODUCER_ID", "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR"))
    mismatch = False
    if cycle_record is not None:
        cycle_directory = producer.cycle_dir(
            root, str(cycle_record["campaign_id"]), str(cycle_record["cycle_id"]), cycle_record)
        expected = {
            "AGENT_ARTIFACT_CAMPAIGN_ID": cycle_record.get("campaign_id", ""),
            "AGENT_ARTIFACT_CYCLE_ID": cycle_record.get("cycle_id", ""),
            "AGENT_ARTIFACT_PRODUCER_ID": cycle_record.get("producer_id", ""),
            "AGENT_ARTIFACT_CYCLE_DIR": str(cycle_directory),
            "AGENT_ARTIFACT_OUTPUT_DIR": str(cycle_directory / "artifacts"),
        }
        for key, wanted in expected.items():
            supplied = env.get(key, "")
            if not supplied:
                continue
            if key.endswith(("DIR", "OUTPUT_DIR")):
                differs = str(Path(supplied).resolve(strict=False)) != str(Path(wanted).resolve(strict=False))
            else:
                differs = supplied != wanted
            if differs:
                mismatch = True
        output_dir = cycle_directory / "artifacts"
        if not output_dir.is_absolute():
            raise ArtifactContextError("artifact-output-not-absolute", str(output_dir))
    else:
        if env_has_cycle:
            raise ArtifactContextError("artifact-cycle-unbound", "inherited producer context has no route binding")
        cycle_directory = output_dir = None
    return ArtifactContext(
        root, route_data, node, selected_node, cycle_record, cycle_directory,
        output_dir, source, binding_source, review_output, mismatch,
    )


def source_git_head(worktree: str | Path | None) -> str | None:
    if not worktree:
        return None
    result = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def source_git_state(worktree: str | Path | None) -> tuple[str, str | None]:
    """Summarize the verified execution worktree using the existing state enum."""
    head = source_git_head(worktree)
    if not head or not worktree:
        return "unknown-no-git", head
    result = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z",
         "--untracked-files=all", "--ignored=matching"],
        capture_output=True,
        env=_readonly_git_env(),
    )
    if result.returncode != 0:
        return "unknown-no-git", head
    records = [part for part in result.stdout.split(b"\0") if part]
    staged = unstaged = untracked = ignored = False
    for record in records:
        if record.startswith(b"??"):
            untracked = True
            continue
        status = record[:2].decode("ascii", "replace")
        if status == "!!":
            ignored = True
            continue
        staged |= status[0] not in (" ", "?")
        unstaged |= status[1] not in " "
    if staged and unstaged:
        return "staged+unstaged", head
    if staged:
        return "staged", head
    if unstaged:
        return "unstaged", head
    if untracked:
        return "untracked", head
    if ignored:
        return "ignored", head
    return "clean", head


def _verified_worktree(route: Mapping[str, Any], env, explicit) -> Path | None:
    sealed = route.get("cwd")
    source_override = explicit.get("source_worktree")
    candidate = source_override or env.get("AGENT_DISPATCH_WORKTREE") or sealed
    if not candidate:
        return None
    path = Path(str(candidate)).expanduser().resolve(strict=False)
    if not path.is_absolute():
        raise ArtifactContextError("source-worktree-not-absolute", str(path))
    if sealed and path != Path(str(sealed)).resolve(strict=False):
        if source_override is None or explicit.get("source_worktree_verified") is not True:
            raise ArtifactContextError("source-worktree-route-mismatch", str(path))
        import dispatch_contract
        sealed_path = Path(str(sealed)).expanduser().resolve(strict=False)
        if (dispatch_contract.canonical_repository_identity(path)
                != dispatch_contract.canonical_repository_identity(sealed_path)):
            raise ArtifactContextError("source-worktree-route-mismatch", str(path))
    return path


def _producer_module():
    try:
        import artifact_producer
        return artifact_producer
    except ImportError:
        import sys
        utilities = Path(__file__).resolve().parent
        if str(utilities) not in sys.path:
            sys.path.insert(0, str(utilities))
        import artifact_producer
        return artifact_producer


def _empty_context() -> ArtifactContext:
    return ArtifactContext(None, None, None, None, None, None, None, None, "unbound")


def _safe_relative(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value
            or value.startswith("/") or value.endswith("/") or "//" in value):
        raise ArtifactContextError("artifact-output-path-invalid", str(value))
    raw_parts = value.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ArtifactContextError("artifact-output-path-invalid", value)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ArtifactContextError("artifact-output-path-invalid", value)
    if re.match(r"^[A-Za-z]:", value):
        raise ArtifactContextError("artifact-output-path-invalid", value)
    return path.as_posix()


def _scope_parts(scope: str) -> tuple[str, ...]:
    value = re.sub(r"<[a-zA-Z0-9_-]+>", "*", scope)
    if value.endswith("/**"):
        value = value[:-3] + "/**"
    return tuple(PurePosixPath(value).parts)


def _path_pattern_matches(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return False
    if pattern[-1] == "**":
        fixed = pattern[:-1]
        return len(parts) >= len(fixed) and all(
            _component_matches(actual, expected) for actual, expected in zip(parts, fixed)
        )
    return len(parts) == len(pattern) and all(
        _component_matches(actual, expected) for actual, expected in zip(parts, pattern)
    )


def _component_matches(actual: str, expected: str) -> bool:
    if re.fullmatch(r"<[a-zA-Z0-9_-]+>", expected):
        return bool(actual)
    return fnmatch.fnmatchcase(actual, expected)


def _declaration_matches(declaration: str, expected: str) -> bool:
    if declaration == expected:
        return True
    if declaration in ("snapshot", "revised-artifact", "source-diff", "experiment-artifact"):
        return False
    expected_head = expected[:-3] if expected.endswith("/**") else expected
    declaration_head = declaration.rstrip("/*")
    return (declaration_head == expected_head
            or expected.endswith("/**") and declaration.startswith(expected_head + "/"))


def _has_pattern(value: str) -> bool:
    return any(char in value for char in "*?[]") or bool(re.search(r"<[a-zA-Z0-9_-]+>", value))


def _static_pattern_prefix(value: str) -> str:
    parts = []
    for part in PurePosixPath(value).parts:
        if part == "**" or any(char in part for char in "*?[]") or re.fullmatch(r"<[a-zA-Z0-9_-]+>", part):
            break
        parts.append(part)
    return "/".join(parts)


def _borrowed_part_prefix(node: Mapping[str, Any]) -> str | None:
    """Derive the relocated root of a borrowed lab smoke part from its sealed IO map."""
    part = node.get("part")
    if not isinstance(part, str) or part.count(":") != 1:
        return None
    capability, stage = part.split(":", 1)
    try:
        identity = _safe_relative(f"{capability}/{stage}")
    except ArtifactContextError:
        return None
    identity_parts = PurePosixPath(identity).parts
    if len(identity_parts) != 2:
        return None

    part_io = node.get("part_io")
    if not isinstance(part_io, Mapping):
        return None
    origin = "reviews/smoke-attestation.json"
    mapped = part_io.get(origin)
    if not isinstance(mapped, str):
        return None
    try:
        mapped = _safe_relative(mapped)
    except ArtifactContextError:
        return None

    mapped_parts = PurePosixPath(mapped).parts
    origin_parts = PurePosixPath(origin).parts
    if len(mapped_parts) <= len(origin_parts) or mapped_parts[-len(origin_parts):] != origin_parts:
        return None
    root_parts = mapped_parts[:-len(origin_parts)]
    identity_path = ("parts", *identity_parts)
    positions = [index for index in range(len(root_parts) - len(identity_path) + 1)
                 if root_parts[index:index + len(identity_path)] == identity_path]
    if len(positions) != 1 or positions[0] + len(identity_path) != len(root_parts):
        return None
    return PurePosixPath(*root_parts).as_posix()
