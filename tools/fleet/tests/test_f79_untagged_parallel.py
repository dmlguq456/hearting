"""F-79 — parallel legs still fold when the route record forgot to tag them.

User 2026-08-14: "fleet에 review가 3-way로 안뜨고 전부 직렬로 뜨는데". Measured on the live
board: a `thorough` route whose three `impl-review*` nodes were ALL `active` at once drew as
three serial stages, while a neighbouring route folded its groups correctly.

The compiler is not at fault — `_expand_parallel_groups` writes `parallel_group` on every leg,
verified here against the real recipe. What reached the board was a hand-built partial route
(a recovery route naming only the nodes left to run) carrying `parallel_groups: []` and no
per-node tag. Fleet was drawing the record faithfully; the record was short an attribute.

So the tag stays authoritative and a shape-based recovery fills in only where there is none.
The shape is the compiler's own: legs share one `depends_on` set, and every non-anchor leg is
`<anchor-id>-<suffix>`. Checked against every tagged group on the live board, this reproduced
the tag exactly with no group it disagreed with.
"""
import json
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                      # noqa: E402


REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _nodes(*specs):
    return [{"id": nid, "state": state, "depends_on": list(deps)}
            for nid, state, deps in specs]


def _ids(nodes):
    return [n["id"] for n in nodes]


class UntaggedRecoveryTest(unittest.TestCase):

    def setUp(self):
        self.route = _nodes(
            ("execute-fix", "done", []),
            ("impl-review", "active", ["execute-fix"]),
            ("impl-review-alternative", "active", ["execute-fix"]),
            ("impl-review-failure-mode", "active", ["execute-fix"]),
            ("test", "pending", ["impl-review"]),
            ("report", "pending", ["test"]),
        )

    def test_untagged_legs_fold_into_one_group(self):
        self.assertEqual(_ids(render._collapse_parallel_nodes(self.route)),
                         ["execute-fix", "impl-review(3-way)", "test", "report"])

    def test_tagged_and_untagged_reach_the_same_shape(self):
        """The recovery must reproduce the tag, not invent a different grouping."""
        tagged = [dict(n, parallel_group=("impl-review" if n["id"].startswith("impl-review")
                                          else None))
                  for n in self.route]
        self.assertEqual(_ids(render._collapse_parallel_nodes(tagged)),
                         _ids(render._collapse_parallel_nodes(self.route)))

    def test_downstream_dependency_is_rewritten_to_the_merged_id(self):
        merged = render._collapse_parallel_nodes(self.route)
        test_node = next(n for n in merged if n["id"] == "test")
        self.assertEqual(test_node["depends_on"], ["impl-review(3-way)"])

    def test_state_follows_the_same_precedence(self):
        route = list(self.route)
        route[2] = dict(route[2], state="failed")
        merged = render._collapse_parallel_nodes(route)
        group = next(n for n in merged if n["id"].startswith("impl-review"))
        self.assertEqual(group["state"], "failed")


class RecoveryBoundaryTest(unittest.TestCase):
    """What must NOT be folded — the recovery reads one convention, it does not guess."""

    def test_siblings_without_a_common_anchor_stay_serial(self):
        route = _nodes(("plan", "done", []),
                       ("execute", "active", ["plan"]),
                       ("docs", "active", ["plan"]))
        self.assertEqual(_ids(render._collapse_parallel_nodes(route)),
                         ["plan", "execute", "docs"])

    def test_similar_names_on_different_dependencies_stay_serial(self):
        """Sharing a prefix is not enough: real legs branch from the SAME node."""
        route = _nodes(("plan", "done", []),
                       ("review", "active", ["plan"]),
                       ("review-late", "pending", ["review"]))
        self.assertEqual(_ids(render._collapse_parallel_nodes(route)),
                         ["plan", "review", "review-late"])

    def test_a_lone_node_is_never_a_group(self):
        route = _nodes(("plan", "done", []), ("execute", "active", ["plan"]))
        self.assertEqual(_ids(render._collapse_parallel_nodes(route)), ["plan", "execute"])

    def test_multi_hyphen_suffixes_are_recognized(self):
        """Real suffixes contain hyphens (`failure-mode`, `implementation-risk`), so a leg
        id may hold several — only the ANCHOR prefix decides membership."""
        route = _nodes(("a", "active", ["root"]),
                       ("a-b", "active", ["root"]),
                       ("a-b-c", "active", ["root"]))
        self.assertEqual(list(render._untagged_parallel_groups(route)), ["a"])

    def test_legs_without_their_anchor_are_refused(self):
        """`<x>-one` and `<x>-two` with no `<x>` node is not the compiler's shape — the
        anchor always keeps the bare id — so nothing is folded."""
        route = _nodes(("root", "done", []),
                       ("review-one", "active", ["root"]),
                       ("review-two", "active", ["root"]))
        self.assertEqual(render._untagged_parallel_groups(route), {})
        self.assertEqual(_ids(render._collapse_parallel_nodes(route)),
                         ["root", "review-one", "review-two"])

    def test_an_existing_tag_is_never_overridden(self):
        route = [dict(n, parallel_group="explicit") if n["id"].startswith("impl-review") else n
                 for n in _nodes(("execute", "done", []),
                                 ("impl-review", "active", ["execute"]),
                                 ("impl-review-alternative", "active", ["execute"]))]
        merged = render._collapse_parallel_nodes(route)
        self.assertIn("explicit(2-way)", _ids(merged))


class CompilerStillTagsTest(unittest.TestCase):
    """The recovery is a safety net, not a replacement: the compiler's own expansion must
    keep writing the tag, or every future route would quietly rely on shape alone."""

    def _expand(self, intensity):
        path = os.path.join(REPO, "hearting", "utilities", "capability-route.py")
        if not os.path.isfile(path):
            path = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                "utilities", "capability-route.py")
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            self.skipTest("capability-route.py not reachable from this checkout")
        spec = importlib.util.spec_from_file_location("cr_f79", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        registry_path = os.path.join(os.path.dirname(path), "..", "capabilities",
                                     "topologies.json")
        with open(os.path.abspath(registry_path), encoding="utf-8") as handle:
            registry = json.load(handle)
        recipe = next(r for r in registry["recipes"]
                      if r.get("capability") == "autopilot-code")["standard_plus"]
        nodes = json.loads(json.dumps(recipe["nodes"]))
        # `capability` became required (a caller that omitted it silently rejected the
        # shipped groups), so pass it positionally and let a future signature change fail
        # loudly here rather than quietly stop verifying anything.
        return module._expand_parallel_groups(nodes, recipe.get("parallel_groups"),
                                              intensity, "autopilot-code")

    def test_thorough_expansion_tags_every_review_leg(self):
        legs = [n for n in self._expand("thorough") if n["id"].startswith("impl-review")]
        self.assertEqual(len(legs), 3)
        self.assertTrue(all(n.get("parallel_group") == "impl-review" for n in legs), legs)

    def test_compiled_shape_matches_what_the_recovery_looks_for(self):
        """If these two ever diverge, the recovery stops recognizing real legs."""
        legs = [n for n in self._expand("thorough") if n["id"].startswith("impl-review")]
        bare = [{"id": n["id"], "state": "active", "depends_on": list(n.get("depends_on") or ())}
                for n in legs]
        self.assertEqual(list(render._untagged_parallel_groups(bare)), ["impl-review"])


if __name__ == "__main__":
    unittest.main()
