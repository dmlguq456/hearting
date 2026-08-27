#!/usr/bin/env python3
"""C-20 (plan.md §6): SD-110's three sealed node fields (`advance_class`,
`model_required_reason`, `commit_expected`) are `_compile_from_recipe`
verbatim copies (checklist 1.7) -- no adapter wrapper reads, filters, or
special-cases them. Isomorphism here means all three
`adapters/{claude,codex,opencode}/bin/dispatch-headless.py` wrappers stay
identically blind to the new fields: none references their names, and the
pre-existing `validate_route_record` required-field tuple is unchanged and
identical across all three. A regression would be exactly one wrapper
growing bespoke SD-110 handling (or requiring the new fields as CLI-time
metadata) while its siblings do not -- silently breaking the "verbatim
copy, no adapter divergence" invariant blocks ①/④ rely on.

Also pins the two named pre-SD-110 route fixtures byte-frozen (rt-9fa0fed86699b8f5 /
rt-f942824768304759) -- neither is a compiled recipe route (no `advance_class`
et al in their node shape) and nothing in this cycle should have touched them."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

WRAPPERS = {
    harness: ROOT / "adapters" / harness / "bin" / "dispatch-headless.py"
    for harness in ("claude", "codex", "opencode")
}

SD110_FIELD_NAMES = {"advance_class", "model_required_reason", "commit_expected"}

FROZEN_FIXTURES = {
    "tools/fleet/tests/fixtures/route/real_claude_staged.json": (
        "rt-9fa0fed86699b8f5",
        "dceaf51e7aad87764eb9ccee65ce44f73e3a521aa915d9d035e5109551ad03dd",
    ),
    "tools/fleet/tests/fixtures/route/real_codex_staged.json": (
        "rt-f942824768304759",
        "f829667b64d2bee622dc7cbbfed7ae28dff0b823e3678994b82228bc438b515b",
    ),
}


def _module_strings(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _required_field_tuple(path: Path) -> tuple[str, ...]:
    """The `required = (...)` literal inside `validate_route_record`."""

    scope = _function(path, "validate_route_record")
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Tuple)
            and any(isinstance(target, ast.Name) and target.id == "required" for target in node.targets)
        ):
            return tuple(
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    raise AssertionError(f"no literal `required` tuple found in {path}")


class DispatchV45ProjectionTest(unittest.TestCase):
    def test_no_wrapper_special_cases_the_three_sealed_node_fields(self):
        observed = {
            harness: _module_strings(path) & SD110_FIELD_NAMES
            for harness, path in WRAPPERS.items()
        }
        self.assertEqual(
            observed, {harness: set() for harness in WRAPPERS},
            "an adapter wrapper referenced an SD-110 sealed field name -- "
            "these are compile-time-only, verbatim-copied node fields "
            "(checklist 1.7); no wrapper should read them directly",
        )

    def test_validate_route_record_required_tuple_is_isomorphic_and_unchanged(self):
        expected = ("route_id", "route_hash", "route_node", "registry_digest", "write_scope")
        observed = {
            harness: _required_field_tuple(path) for harness, path in WRAPPERS.items()
        }
        self.assertEqual(observed, {harness: expected for harness in WRAPPERS})

    def test_named_pre_sd110_route_fixtures_stay_byte_frozen(self):
        for relative, (route_id, digest) in FROZEN_FIXTURES.items():
            path = ROOT / relative
            data = path.read_bytes()
            self.assertEqual(
                hashlib.sha256(data).hexdigest(), digest,
                f"{relative} content drifted -- it must stay byte-frozen",
            )
            self.assertIn(route_id.encode("utf-8"), data)
            for field in SD110_FIELD_NAMES:
                self.assertNotIn(
                    field.encode("utf-8"), data,
                    f"{relative} is pre-SD-110 and must not gain sealed node fields",
                )


if __name__ == "__main__":
    unittest.main()
