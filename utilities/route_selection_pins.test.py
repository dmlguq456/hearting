#!/usr/bin/env python3
"""Route-sealed selection pins: `compose --pin`, frame policy sealing, wrapper use.

Every test is isolated: a temp AGENT_HOME/registry/XDG tree, a temp
DISPATCH_DEFAULTS_CONFIG and fixture dispatch evidence. No adapter runtime is
started and no user configuration is read or written.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = _load("route_pins_route", ROOT / "utilities" / "capability-route.py")
MP = _load("route_pins_model_profile", ROOT / "utilities" / "model_profile.py")
D = R.DEFAULTS

ALL_ENABLED = """schema_version: 4
harnesses:
  enabled: [claude, codex, opencode]
profiles:
  deep:
    primary: [claude, codex]
    relief: []
    last_resort: []
    promote_relief_below: 0
  balanced-deep:
    primary: [claude, codex]
    relief: []
    last_resort: [opencode]
    promote_relief_below: 0
  balanced:
    primary: [claude, codex]
    relief: []
    last_resort: [opencode]
    promote_relief_below: 0
  light:
    primary: [claude, codex, opencode]
    relief: []
    last_resort: []
    promote_relief_below: 0
  mini:
    primary: [claude, codex, opencode]
    relief: []
    last_resort: []
    promote_relief_below: 0
allocation:
  strategy: balanced
  window: 30
  harness_weights:
    opencode: 0.3
capabilities:
"""
# Breaks every recommendation at once: OpenCode only, and it is the deep primary.
OPENCODE_ONLY = "".join([
    "schema_version: 4\nharnesses:\n  enabled: [opencode]\nprofiles:\n",
    *[
        f"  {name}:\n    primary: [opencode]\n    relief: []\n    last_resort: []\n"
        "    promote_relief_below: 0\n"
        for name in ("deep", "balanced-deep", "balanced", "light", "mini")
    ],
    "allocation:\n  strategy: balanced\n  window: 30\ncapabilities:\n",
])


def nested(parent="claude", child="claude"):
    sandbox = R.WRAPPER_PARENT_SANDBOXES[parent][0] if parent in R.WRAPPER_PARENT_SANDBOXES else "workspace-write"
    return {
        "parent_harness": parent, "parent_transport": "headless", "parent_sandbox": sandbox,
        "child_harness": child, "launch_authority": "conductor", "status": "supported",
        "probe_source": "fixture-probe", "probe_time": "2026-07-16T00:00:00Z",
        "failure_class": "", "checked_worktree": str(R.ROOT.resolve()), "failure_scope": "none",
        "codex_command": "ok" if child == "codex" else "not-applicable",
        "retry_on_isolated_worktree": 0,
    }


def evidence(parent="claude", children=("claude", "codex", "opencode")):
    return {
        "tuples": [nested(parent, child) for child in children],
        "native_subagent": [{
            "harness": "codex", "transport": "headless",
            "execution_surface": "codex-native-subagent", "registered_worker": False,
            "status": "supported", "check_source": "fixture-native-check",
        }],
    }


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        (home / "core").mkdir()
        (home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        jobs = home / "state" / "jobs.log"
        jobs.parent.mkdir()
        jobs.write_text("", encoding="utf-8")
        env = mock.patch.dict(os.environ, {
            "AGENT_HOME": str(home), "AGENT_DISPATCH_JOBS": str(jobs),
            "XDG_STATE_HOME": str(home / "state"), "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"), "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(home / "claude"), "CODEX_HOME": str(home / "codex"),
        })
        env.start()
        self.addCleanup(env.stop)
        for key in ("AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_REGISTERED_WORKER", "AGENT_DISPATCH_DEPTH",
                    "AGENT_OWNER_ROUTE_FILE", "AGENT_OWNER_ROUTE_ID", "AGENT_OWNER_ROUTE_HASH",
                    "AGENT_WORKFLOW_ROOT"):
            os.environ.pop(key, None)
        self.home = home

    def config(self, text):
        path = self.home / "dispatch-defaults.yaml"
        path.write_text(text, encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"DISPATCH_DEFAULTS_CONFIG": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def compose(self, pins=None, graph="frame,frame-alternative,execute,test,report", **kw):
        arguments = dict(
            capability="autopilot-code", capability_mode="dev", shape="staged", graph=graph,
            slug="pins-fixture", cwd=R.ROOT, artifact_root=R.ROOT, spec_read="fixture",
            drift_verdict="fixture", unassigned=True,
            dispatch_evidence=evidence(parent=(pins or {}).get("owner", {}).get("harness", "claude")),
            selection_pins=pins,
        )
        arguments.update(kw)
        R.DISPATCH_DEFAULTS_WARNINGS.clear()
        return R.compose_route(**arguments)


class PinParserTest(unittest.TestCase):
    def parse(self, values, owner=None):
        return R._parse_selection_pins(values, owner)

    def test_full_forms(self):
        pins = self.parse([
            "owner=opencode:opencode-go/muse-spark-1.3-contributor@xhigh",
            "worker=opencode",
            "frame=claude:fable",
        ])
        self.assertEqual(pins["owner"], {
            "harness": "opencode", "model": "opencode-go/muse-spark-1.3-contributor", "effort": "xhigh"})
        self.assertEqual(pins["worker"], {"harness": "opencode", "model": None, "effort": None})
        self.assertEqual(pins["frame"], {"harness": "claude", "model": "fable", "effort": None})

    def test_model_may_contain_a_colon_and_the_effort_is_the_last_at(self):
        pin = self.parse(["worker=opencode:provider/model:tag@max"])["worker"]
        self.assertEqual((pin["model"], pin["effort"]), ("provider/model:tag", "max"))
        self.assertEqual(self.parse(["worker=opencode:provider/model:tag"])["worker"]["model"],
                         "provider/model:tag")

    def test_owner_flag_is_the_same_statement_as_an_owner_pin(self):
        self.assertEqual(self.parse([], "codex")["owner"], {"harness": "codex", "model": None, "effort": None})
        self.assertEqual(self.parse(["owner=codex:gpt-x@high"], "codex")["owner"]["model"], "gpt-x")
        with self.assertRaisesRegex(ValueError, "compose-pin-owner-conflict"):
            self.parse(["owner=claude"], "codex")

    def test_unreadable_values_are_refused(self):
        for bad in ("nope", "boss=claude", "owner=gemini", "owner=claude:", "owner=claude:@high",
                    "owner=claude:m@", "owner=claude:has space", "owner=claude:a|b", "owner=claude:m@High",
                    "owner=claude:a,b", "worker=claude:--foo@high", "owner=claude:-x", "owner=claude:.hidden"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "compose-pin-invalid"):
                self.parse([bad])
        with self.assertRaisesRegex(ValueError, "compose-pin-invalid:owner given twice"):
            self.parse(["owner=claude", "owner=codex"])

    def test_no_pin_and_no_owner_is_an_empty_input(self):
        self.assertEqual(self.parse([]), {})
        self.assertEqual(self.parse(None), {})


class TopPinTest(unittest.TestCase):
    def test_owner_and_worker_lose_a_main_session_only_model_but_keep_the_tool(self):
        pins = R._parse_selection_pins(
            ["owner=claude:fable@xhigh", "worker=claude:claude-fable-5-1", "frame=claude:fable"])
        with mock.patch.object(R, "_main_session_only_models", return_value="fable"):
            kept, warnings = R._filter_top_pins(pins)
        self.assertEqual(kept["owner"], {"harness": "claude", "model": None, "effort": None})
        self.assertEqual(kept["worker"], {"harness": "claude", "model": None, "effort": None})
        self.assertEqual(kept["frame"]["model"], "fable")
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all(w.startswith("warning=pin-dropped:") and "main-session-only" in w for w in warnings))

    def test_an_ordinary_model_is_untouched_and_a_harness_only_pin_needs_no_lookup(self):
        pins = R._parse_selection_pins(["owner=claude:sonnet", "worker=opencode"])
        with mock.patch.object(R, "_main_session_only_models", return_value="fable"):
            kept, warnings = R._filter_top_pins(pins)
        self.assertEqual(kept, pins)
        self.assertEqual(warnings, [])


def reseal(route):
    route["route_hash"] = R.route_hash(route)
    route["route_id"] = "rt-" + route["route_hash"].split(":", 1)[1][:16]
    return route


class ComposeSealTest(IsolatedCase):
    def test_pinned_owner_is_probed_as_conductor_for_new_and_frame_inherited_routes(self):
        self.config(ALL_ENABLED)
        for harness in ("claude", "codex", "opencode"):
            for inherited in (None, evidence(parent="claude")):
                with self.subTest(harness=harness, inherited=inherited is not None):
                    pins = R._parse_selection_pins([f"owner={harness}"])
                    with mock.patch.object(R, "_compose_readiness", return_value={
                            "tuples": evidence(parent=harness)["tuples"]}) as probe:
                        route = self.compose(pins=pins, dispatch_evidence=inherited)
                    if inherited is None:
                        self.assertEqual(probe.call_args.kwargs["owner_harnesses"],
                                         ("claude", "codex", "opencode"))
                    elif harness != "claude":
                        self.assertEqual(probe.call_args.args[2], harness)  # exact pinned-owner top-up
                    else:
                        probe.assert_not_called()
                    self.assertIn(harness, {row["parent_harness"] for row in route["dispatch_evidence"]["tuples"]})
                    R.verify_route(route, R.ROOT)

    def test_no_pin_means_no_key_and_identical_bytes(self):
        self.config(ALL_ENABLED)
        first = self.compose()
        second = self.compose(pins=None)
        third = self.compose(pins={})
        self.assertNotIn("selection_pins", first)
        self.assertEqual(first["route_hash"], second["route_hash"])
        self.assertEqual(first["route_hash"], third["route_hash"])
        R.verify_route(first, R.ROOT)

    def test_pins_are_sealed_rehashed_and_verified(self):
        self.config(ALL_ENABLED)
        plain = self.compose()
        pins = R._parse_selection_pins([
            "owner=opencode:opencode-go/muse-spark-1.3-contributor@xhigh",
            "frame=opencode", "worker=opencode:opencode-go/deepseek-v4.1-flash@max"])
        route = self.compose(pins=pins)
        self.assertEqual(route["selection_pins"], {"contract_version": 1, **pins})
        self.assertNotEqual(route["route_hash"], plain["route_hash"])
        self.assertEqual(route["route_hash"], R.route_hash(route))
        self.assertEqual(route["route_hash"], self.compose(pins=pins)["route_hash"])
        R.verify_route(route, R.ROOT)
        depth2 = [n for n in route["nodes"] if n.get("dispatch_depth") == 2]
        self.assertTrue(depth2)
        for node in depth2:
            self.assertEqual(node["harness_affinity"], "opencode")
            policy = node["harness_policy"]
            self.assertEqual(policy["primary"][0], "opencode")
            everything = policy["primary"] + policy["relief"] + policy["last_resort"]
            self.assertEqual(everything.count("opencode"), 1)
        for node in route["nodes"]:
            if node.get("dispatch_depth") == 1:
                self.assertNotEqual(node.get("harness_affinity"), "opencode")

    def test_a_harness_only_owner_pin_is_sealed_with_null_model_and_effort(self):
        self.config(ALL_ENABLED)
        pins = R._parse_selection_pins([], "codex")
        route = self.compose(pins=pins, parent_harness="codex", dispatch_evidence=evidence("codex"))
        self.assertEqual(route["selection_pins"]["owner"], {"harness": "codex", "model": None, "effort": None})
        self.assertNotIn("worker", route["selection_pins"])
        R.verify_route(route, R.ROOT)

    def test_a_frame_pin_is_sealed_beside_two_top_legs_and_changes_neither_profile(self):
        self.config(ALL_ENABLED)
        pins = R._parse_selection_pins(["frame=claude:fable@xhigh"], None)
        route = self.compose(pins=pins)
        self.assertEqual(route["selection_pins"]["frame"],
                         {"harness": "claude", "model": "fable", "effort": "xhigh"})
        frames = [n for n in route["nodes"] if R._frame_node(n)]
        self.assertEqual([(n["id"], n["model_profile"]) for n in frames],
                         [("frame", "top"), ("frame-alternative", "top")])
        # the pin is the user's explicit choice for the launch; the route's own profile stays `top`
        # and the wrapper resolves the pinned harness/model/effort (RouteSelectionPinHelperTest)
        R.verify_route(route, R.ROOT)

    def test_frame_legs_carry_their_profile_policy_and_old_routes_still_verify(self):
        self.config(ALL_ENABLED)
        route = self.compose()
        frames = [n for n in route["nodes"] if R._frame_node(n)]
        self.assertEqual([n["id"] for n in frames], ["frame", "frame-alternative"])
        for node in frames:
            policy = node["harness_policy"]
            self.assertIsInstance(policy, dict)
            self.assertEqual(set(policy), {"primary", "relief", "last_resort", "promote_relief_below"})
        # A route sealed before frame legs carried a policy has no such field:
        # it must keep verifying (the launch then reads the live policy).
        for node in route["nodes"]:
            if R._frame_node(node):
                node.pop("harness_policy")
        reseal(route)
        R.verify_route(route, R.ROOT)

    def test_without_a_config_file_frames_seal_a_null_policy(self):
        with mock.patch.object(R.DEFAULTS, "default_config_path", return_value="/nonexistent-defaults"):
            route = self.compose()
        for node in route["nodes"]:
            if R._frame_node(node):
                self.assertIsNone(node["harness_policy"])

    def test_a_policy_that_breaks_recommendations_composes_with_warnings(self):
        self.config(OPENCODE_ONLY.replace("enabled: [opencode]", "enabled: [claude, codex, opencode]")
                    .replace("    primary: [opencode]\n", "    primary: [opencode, claude]\n", 1))
        route = self.compose()
        self.assertIn("dispatch_allocation", route)
        warnings = "\n".join(R.DISPATCH_DEFAULTS_WARNINGS)
        self.assertIn("warning=dispatch-defaults:profiles.deep.primary includes opencode", warnings)
        R.verify_route(route, R.ROOT)

    def test_an_opencode_only_policy_seals_and_verifies_without_warnings(self):
        self.config(OPENCODE_ONLY)
        route = self.compose(parent_harness="opencode", dispatch_evidence=evidence("opencode", ("opencode",)))
        self.assertEqual(R.DISPATCH_DEFAULTS_WARNINGS, [])
        self.assertEqual(route["owner_harness_policy"]["primary"], ["opencode"])
        R.verify_route(route, R.ROOT)

    def test_a_structurally_corrupt_policy_still_fails_compose(self):
        self.config("schema_version: 4\nmystery: 1\n")
        with self.assertRaisesRegex(ValueError, "corrupt dispatch-defaults config"):
            self.compose()

    def test_owner_policy_may_omit_a_pool_harness_but_never_add_one(self):
        self.config(ALL_ENABLED)
        route = self.compose()
        R.verify_route(route, R.ROOT)
        pool = set(route["dispatch_allocation"]["harness_order"])
        self.assertEqual(pool, {"claude", "codex", "opencode"})
        self.assertNotIn("opencode", route["owner_harness_policy"]["primary"])
        route["dispatch_allocation"]["harness_order"] = ["claude"]
        reseal(route)
        with self.assertRaisesRegex(ValueError, "owner_harness_policy differs from dispatch allocation pool"):
            R.verify_route(route, R.ROOT)

    def test_harness_weights_are_sealed_in_the_allocation_and_checked(self):
        self.config(ALL_ENABLED)
        route = self.compose()
        self.assertEqual(route["dispatch_allocation"]["harness_weights"], {"opencode": 0.3})
        R.verify_route(route, R.ROOT)
        route["dispatch_allocation"]["harness_weights"] = {"opencode": 3}
        reseal(route)
        with self.assertRaisesRegex(ValueError, "invalid dispatch_allocation harness weights"):
            R.verify_route(route, R.ROOT)

    def test_a_config_without_weights_seals_no_weights_key(self):
        self.config(ALL_ENABLED.replace("  harness_weights:\n    opencode: 0.3\n", ""))
        self.assertNotIn("harness_weights", self.compose()["dispatch_allocation"])


class SelectionPinVerifyTest(unittest.TestCase):
    def check(self, pins):
        R._verify_selection_pins({"selection_pins": pins})

    def test_absent_is_fine_and_bad_shapes_are_refused(self):
        R._verify_selection_pins({})
        self.check({"contract_version": 1, "owner": {"harness": "codex", "model": None, "effort": None}})
        for bad, message in (
            ({"contract_version": 2}, "contract"),
            ({"contract_version": 1, "boss": {}}, "key"),
            ({"contract_version": 1, "owner": {"harness": "codex"}}, "shape"),
            ({"contract_version": 1, "owner": {"harness": "gemini", "model": None, "effort": None}}, "harness"),
            ({"contract_version": 1, "owner": {"harness": "codex", "model": "a b", "effort": None}}, "model"),
            ({"contract_version": 1, "owner": {"harness": "codex", "model": "--foo", "effort": None}}, "model"),
            ({"contract_version": 1, "owner": {"harness": "codex", "model": "m", "effort": "High"}}, "effort"),
            ("owner=codex", "contract"),
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, message):
                self.check(bad)


class DefaultChildrenTest(IsolatedCase):
    def test_pins_cannot_add_disabled_harnesses_to_compose_candidates(self):
        self.config(ALL_ENABLED)
        self.assertEqual(R._compose_default_children(), ("claude", "codex", "opencode"))
        self.config(OPENCODE_ONLY)
        self.assertEqual(R._compose_default_children(), ("opencode",))
        pins = R._parse_selection_pins(["worker=claude"])
        with self.assertRaisesRegex(ValueError, "disabled-by-user-policy"):
            R._compose_default_children(pins)

    def test_selected_unreadable_or_malformed_policy_is_not_an_all_enabled_fallback(self):
        with mock.patch.object(R.DEFAULTS, "default_config_path", return_value="/nonexistent-defaults"):
            with self.assertRaises((R.DEFAULTS.DefaultsConfigError, OSError)):
                R._compose_default_children()
        self.config("schema_version: 4\nmystery: 1\n")
        with self.assertRaises((R.DEFAULTS.DefaultsConfigError, OSError)):
            R._compose_default_children()


    def test_disabled_pin_cannot_expand_bands_or_trigger_a_new_probe(self):
        self.config(OPENCODE_ONLY)
        pins=R._parse_selection_pins(["owner=codex", "worker=codex", "frame=codex"])
        route={"nodes":[{"dispatch_depth":2,"harness_policy":{"primary":["opencode"],"relief":[],"last_resort":[]}}]}
        before=json.dumps(route)
        with self.assertRaisesRegex(ValueError,"disabled-by-user-policy"):
            R._apply_selection_pins(route,pins)
        self.assertEqual(json.dumps(route),before)
        with mock.patch.object(R,"_compose_readiness") as probe:
            for target in ("owner","worker","frame"):
                with self.assertRaisesRegex(ValueError,"disabled-by-user-policy"):
                    R._pin_change_probe(route,self.home/"state/jobs.log",target,"codex","claude")
            probe.assert_not_called()

    def test_formal_enabled_pin_change_adds_only_its_checked_target(self):
        self.config(ALL_ENABLED)
        import route_authority as RA
        route={"route_id":"rt-d5-pin","route_hash":"sha256:original","artifact_root":str(self.home),"cwd":str(self.home),
               "dispatch_allocation":{"harness_order":["claude"]},
               "dispatch_evidence":{"tuples":[nested("claude","claude")]},
               "nodes":[{"id":"execute","dispatch_depth":2,"fallback_hops":[{"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[nested("claude","claude")]},{"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[]}]}]}
        before=json.dumps(route,sort_keys=True)
        with mock.patch.object(R,"_compose_readiness",return_value={"tuples":[nested("claude","opencode")]}) as probe:
            tuples,candidates=R._pin_change_probe(route,self.home/"state/jobs.log","worker","opencode","claude")
        self.assertEqual(probe.call_count,1)
        RA.record_pin_change(route,target="worker",pin={"harness":"opencode","model":None,"effort":None},
                             by={"harness":"claude","session_id":"parent"},source="unattributed",tuples=tuples,candidates=candidates)
        view=RA.route_in_force(route)
        self.assertEqual(view["selection_pins"]["worker"]["harness"],"opencode")
        self.assertEqual(view["dispatch_allocation"],route["dispatch_allocation"])
        self.assertIn("opencode",[r["child_harness"] for r in view["dispatch_evidence"]["tuples"]])
        self.assertEqual(json.dumps(route,sort_keys=True),before)
        spec=importlib.util.spec_from_file_location("d5_pin_node",R.ROOT/"utilities/dispatch-node.py")
        node_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(node_module)
        parent={key:nested("claude","opencode")[key] for key in ("parent_harness","parent_transport","parent_sandbox","launch_authority")}
        with mock.patch.object(node_module,"current_parent_identity",return_value=parent), mock.patch("dispatch_capacity_evidence.active_limits",return_value={}):
            self.assertFalse(node_module.pin_harness_available(route,route["nodes"][0],"opencode",self.home/"state/jobs.log"))
            self.assertTrue(node_module.pin_harness_available(view,view["nodes"][0],"opencode",self.home/"state/jobs.log"))
            self.config(OPENCODE_ONLY.replace("enabled: [opencode]","enabled: [claude]"))
            self.assertFalse(node_module.pin_harness_available(view,view["nodes"][0],"opencode",self.home/"state/jobs.log"))


class RouteSelectionPinHelperTest(unittest.TestCase):
    def write(self, tmp, pins):
        path = Path(tmp) / "route.json"
        path.write_text(json.dumps({"selection_pins": pins} if pins is not None else {}), encoding="utf-8")
        return path

    def test_statuses(self):
        pins = {"contract_version": 1,
                "owner": {"harness": "opencode", "model": "m/x", "effort": "xhigh"},
                "frame": {"harness": "opencode", "model": None, "effort": None},
                "worker": {"harness": "codex", "model": "gpt-x", "effort": None}}
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, pins)
            self.assertEqual(MP.route_selection_pin(path, worker_type="owner", adapter="opencode"),
                             {"status": "applied", "model": "m/x", "effort": "xhigh"})
            self.assertEqual(MP.route_selection_pin(path, worker_type="frame", adapter="opencode"),
                             {"status": "harness-only"})
            self.assertEqual(MP.route_selection_pin(path, worker_type="stage", adapter="codex"),
                             {"status": "applied", "model": "gpt-x", "effort": None})
            self.assertEqual(MP.route_selection_pin(path, worker_type="review", adapter="claude"),
                             {"status": "harness-mismatch", "pinned_harness": "codex"})
            self.assertEqual(MP.route_selection_pin(self.write(tmp, None), worker_type="owner", adapter="claude"),
                             {"status": "none"})
        self.assertEqual(MP.route_selection_pin(None, worker_type="owner", adapter="claude"), {"status": "none"})
        with self.assertRaises(MP.ModelProfileError):
            MP.route_selection_pin("/nonexistent/route.json", worker_type="owner", adapter="claude")

    def test_targets(self):
        self.assertEqual([MP.pin_target(t) for t in ("frame", "owner", "stage", "review", None)],
                         ["frame", "owner", "worker", "worker", "worker"])


def _wrapper(adapter):
    return _load(f"route_pins_{adapter}_wrapper", ROOT / "adapters" / adapter / "bin" / "dispatch-headless.py")


class WrapperPinTest(IsolatedCase):
    """The same pin behaviour through each adapter's own model resolver."""

    EFFORT_FIELD = {"claude": "effort", "codex": "reasoning", "opencode": "variant"}
    PIN_MODEL = {"claude": "sonnet", "codex": "gpt-5.4", "opencode": "opencode-go/deepseek-v4.1-flash"}

    def args(self, adapter, route_file, *, worker_type="stage", depth=2, retry=False):
        field = self.EFFORT_FIELD[adapter]
        values = dict(
            model_profile="light", registered_worker=True, dispatch_depth=depth, worker_type=worker_type,
            route_file=str(route_file), route_node="execute", inherit_model_settings=False,
            model_role="fast implementer", model=None, effort=None, reasoning=None, variant=None,
            capacity_retry=0, owner_route_binding=None,
        )
        if retry:
            values.update(model="retry-model", capacity_retry=1, **{field: "low"})
        return argparse.Namespace(**values)

    def route(self, adapter, **pin):
        path = self.home / f"route-{adapter}.json"
        path.write_text(json.dumps({"selection_pins": {"contract_version": 1, **pin}}), encoding="utf-8")
        return path

    def test_each_wrapper_applies_reports_and_survives_a_capacity_retry(self):
        for adapter in ("claude", "codex", "opencode"):
            wrapper = _wrapper(adapter)
            field = self.EFFORT_FIELD[adapter]
            model = self.PIN_MODEL[adapter]
            with self.subTest(adapter=adapter):
                base = wrapper.resolve_model_settings(self.args(adapter, self.route(adapter)))
                self.assertEqual(base["source"], "profile")
                self.assertEqual(base["pin_status"], "none")
                # applied: the pinned model and effort win over the profile.
                route = self.route(adapter, worker={"harness": adapter, "model": model, "effort": "high"})
                applied = wrapper.resolve_model_settings(self.args(adapter, route))
                self.assertEqual((applied["source"], applied["model"], applied[field]), ("pin", model, "high"))
                self.assertEqual((applied["pin_status"], applied["pin_model"]), ("applied", model))
                # no effort in the pin: the profile's own budget applies.
                route = self.route(adapter, worker={"harness": adapter, "model": model, "effort": None})
                self.assertEqual(wrapper.resolve_model_settings(self.args(adapter, route))[field], base[field])
                # harness only: profile model, status says so.
                route = self.route(adapter, worker={"harness": adapter, "model": None, "effort": None})
                only = wrapper.resolve_model_settings(self.args(adapter, route))
                self.assertEqual((only["source"], only["model"], only["pin_status"]),
                                 ("profile", base["model"], "harness-only"))
                # another tool was pinned: this launch keeps its profile default.
                other = "codex" if adapter != "codex" else "claude"
                route = self.route(adapter, worker={"harness": other, "model": model, "effort": None})
                mismatch = wrapper.resolve_model_settings(self.args(adapter, route))
                self.assertEqual((mismatch["source"], mismatch["model"], mismatch["pin_status"]),
                                 ("profile", base["model"], "harness-mismatch"))
                # a checked capacity retry replaces the pin visibly, not silently.
                route = self.route(adapter, worker={"harness": adapter, "model": model, "effort": "high"})
                retried = wrapper.resolve_model_settings(self.args(adapter, route, retry=True))
                self.assertEqual((retried["source"], retried["model"], retried["pin_model"]),
                                 ("pin+capacity", "retry-model", model))

    def test_a_pin_targets_only_its_own_worker_type(self):
        for adapter in ("claude", "codex", "opencode"):
            wrapper = _wrapper(adapter)
            model = self.PIN_MODEL[adapter]
            with self.subTest(adapter=adapter):
                route = self.route(adapter, owner={"harness": adapter, "model": model, "effort": None})
                stage = wrapper.resolve_model_settings(self.args(adapter, route))
                self.assertEqual(stage["pin_status"], "none")
                owner = wrapper.resolve_model_settings(
                    self.args(adapter, route, worker_type="owner", depth=1))
                self.assertEqual((owner["source"], owner["model"]), ("pin", model))

    def test_main_session_only_model_is_refused_for_a_worker_but_not_for_frame(self):
        for adapter in ("claude", "codex"):
            wrapper = _wrapper(adapter)
            with self.subTest(adapter=adapter):
                pins = {"worker": {"harness": adapter, "model": "fable", "effort": None},
                        "frame": {"harness": adapter, "model": "fable", "effort": None}}
                if adapter == "codex":
                    top = wrapper.resolve_config("codex", source_root=ROOT)[0]["CFG_MAIN_SESSION_ONLY_MODELS"].split()[0]
                    pins = {"worker": {"harness": adapter, "model": top, "effort": None},
                            "frame": {"harness": adapter, "model": top, "effort": None}}
                route = self.route(adapter, **pins)
                with self.assertRaises(wrapper.ModelSelectionError) as ctx:
                    wrapper.resolve_model_settings(self.args(adapter, route))
                self.assertEqual(ctx.exception.reason, "headless-main-session-only-model")
                frame = wrapper.resolve_model_settings(
                    self.args(adapter, route, worker_type="frame", depth=1))
                self.assertEqual((frame["source"], frame["pin_status"]), ("pin", "applied"))

    def test_a_capacity_retry_model_that_is_main_session_only_is_refused_for_a_worker(self):
        for adapter in ("claude", "codex"):
            wrapper = _wrapper(adapter)
            with self.subTest(adapter=adapter):
                top = "fable"
                if adapter == "codex":
                    top = wrapper.resolve_config("codex", source_root=ROOT)[0]["CFG_MAIN_SESSION_ONLY_MODELS"].split()[0]
                ordinary = self.PIN_MODEL[adapter]
                pins = {"worker": {"harness": adapter, "model": ordinary, "effort": None},
                        "frame": {"harness": adapter, "model": ordinary, "effort": None}}
                route = self.route(adapter, **pins)
                args = self.args(adapter, route, retry=True)
                args.model = top
                with self.assertRaises(wrapper.ModelSelectionError) as ctx:
                    wrapper.resolve_model_settings(args)
                self.assertEqual(ctx.exception.reason, "headless-main-session-only-model")
                frame_args = self.args(adapter, route, worker_type="frame", depth=1, retry=True)
                frame_args.model = top
                frame = wrapper.resolve_model_settings(frame_args)
                self.assertEqual((frame["source"], frame["model"]), ("pin+capacity", top))

    def test_the_override_refusal_points_at_compose_pin(self):
        wrapper = _wrapper("claude")
        args = self.args("claude", self.route("claude"))
        args.model, args.effort = "sonnet", "high"
        with self.assertRaises(wrapper.ModelSelectionError) as ctx:
            wrapper.resolve_model_settings(args)
        self.assertEqual(ctx.exception.reason, "model-profile-override-forbidden")
        self.assertIn("compose --pin", str(ctx.exception))


class CliComposeTest(IsolatedCase):
    """The real `compose` command line: parsing, exit status, stderr lines."""

    def run_cli(self, *extra):
        import subprocess
        import sys
        prompt = self.home / "task.md"
        prompt.write_text("do the small thing\n", encoding="utf-8")
        evidence_path = self.home / "evidence.json"
        evidence_path.write_text(json.dumps(evidence()), encoding="utf-8")
        with tempfile.TemporaryDirectory() as artifacts:
            command = [
                sys.executable, str(ROOT / "utilities" / "capability-route.py"), "compose",
                "--slug", "cli-pins", "--unassigned", "--shape", "staged",
                "--graph", "execute,test,report", "--cwd", str(ROOT), "--artifact-root", artifacts,
                "--spec-read", "fixture", "--drift-verdict", "fixture",
                "--dispatch-evidence", str(evidence_path), "--prompt-file", str(prompt), *extra,
            ]
            env = {**os.environ, "AGENT_HOME": str(ROOT)}
            result = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
            route_file = next(iter(Path(artifacts).glob(".runtime/routes/rt-*.json")), None)
            route = json.loads(route_file.read_text()) if route_file else None
            return result, route

    def test_pins_are_sealed_and_bad_pins_exit_64(self):
        self.config(ALL_ENABLED)
        result, route = self.run_cli(
            "--pin", "owner=opencode:opencode-go/muse-spark-1.3-contributor@xhigh",
            "--pin", "worker=opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(route["selection_pins"]["owner"]["effort"], "xhigh")
        self.assertEqual(route["work_request"]["owner_harness"], "opencode")
        self.assertEqual(route["selection_pins"]["worker"]["harness"], "opencode")
        bad, none = self.run_cli("--pin", "owner=nowhere")
        self.assertEqual(bad.returncode, 64)
        self.assertIn("compose-pin-invalid", bad.stderr)
        self.assertIsNone(none)
        conflict, _ = self.run_cli("--owner", "codex", "--pin", "owner=claude")
        self.assertEqual(conflict.returncode, 64)
        self.assertIn("compose-pin-owner-conflict", conflict.stderr)

    def test_owner_flag_alone_seals_a_harness_only_owner_pin(self):
        self.config(ALL_ENABLED)
        result, route = self.run_cli("--owner", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(route["selection_pins"], {
            "contract_version": 1, "owner": {"harness": "codex", "model": None, "effort": None}})

    def test_no_pin_flags_leave_the_route_without_the_key(self):
        self.config(ALL_ENABLED)
        result, route = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("selection_pins", route)

    def test_recommendation_warnings_reach_stderr_and_compose_still_succeeds(self):
        self.config(ALL_ENABLED.replace("  deep:\n    primary: [claude, codex]",
                                        "  deep:\n    primary: [claude, codex, opencode]", 1))
        result, route = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("warning=dispatch-defaults:profiles.deep.primary includes opencode", result.stderr)
        self.assertIsNotNone(route)

    def test_a_top_model_pin_on_the_owner_warns_and_keeps_the_tool(self):
        self.config(ALL_ENABLED)
        import model_config
        codex_top = model_config.resolve_config("codex", source_root=ROOT)[0].get(
            "CFG_MAIN_SESSION_ONLY_MODELS", "").split()
        model = codex_top[0] if codex_top else "gpt-6-astra"
        result, route = self.run_cli("--pin", f"owner=codex:{model}@xhigh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(route["selection_pins"]["owner"], {"harness": "codex", "model": None, "effort": None})
        self.assertIn("warning=pin-dropped:owner:", result.stderr)


class DispatchNodePinRowTest(unittest.TestCase):
    """The real `compose --pin worker=claude` then the real `dispatch-node --action register`.

    register spawns nothing, so no runtime projection is needed. The row the registry ends up with
    is what the e2e Claude run showed: a pinned route whose worker row says `harness=codex`."""

    def setUp(self):
        import subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        for command in (["init", "-q", str(self.repo)], ["-C", str(self.repo), "config", "user.email", "f@example.com"],
                        ["-C", str(self.repo), "config", "user.name", "Fixture"]):
            subprocess.run(["git", *command], check=True)
        (self.repo / "x").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "x"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        self.artifacts = self.repo / ".agent_reports"  # the canonical root a wrapper resolves for this repo
        self.artifacts.mkdir()
        self.state = base / "state"
        self.state.mkdir()
        self.jobs = self.state / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.home = base / "home"
        self.home.mkdir()
        # A runtime wrapper may create these empty ignored folders under the source tree it runs from.
        for name in (".core-grounding", ".spec-grounding"):
            if not (ROOT / name).exists():
                self.addCleanup(lambda path=ROOT / name: path.is_dir() and not any(path.iterdir()) and path.rmdir())
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("AGENT_", "CODEX_", "CLAUDE_"))}
        self.env.update(AGENT_HOME=str(ROOT), HOME=str(self.home), XDG_STATE_HOME=str(self.state),
                        XDG_CONFIG_HOME=str(self.home / "config"), XDG_DATA_HOME=str(self.home / "data"),
                        CLAUDE_CONFIG_DIR=str(self.home / "claude"), CODEX_HOME=str(self.home / "codex"),
                        AGENT_DISPATCH_JOBS=str(self.jobs), DISPATCH_DEFAULTS_CONFIG=str(self.home / "defaults.yaml"))
        (self.home / "defaults.yaml").write_text(ALL_ENABLED, encoding="utf-8")

    def compose(self, *pins):
        import subprocess
        import sys
        tuples = evidence("claude")
        for row in tuples["tuples"]:
            row["checked_worktree"] = str(self.repo.resolve())
        (self.home / "evidence.json").write_text(json.dumps(tuples), encoding="utf-8")
        (self.home / "task.md").write_text("do the small thing\n", encoding="utf-8")
        command = [sys.executable, str(ROOT / "utilities" / "capability-route.py"), "compose",
                   "--slug", "pin-row", "--unassigned", "--shape", "staged", "--graph", "execute,test,report",
                   "--cwd", str(self.repo), "--artifact-root", str(self.artifacts), "--spec-read", "fixture",
                   "--drift-verdict", "fixture", "--dispatch-evidence", str(self.home / "evidence.json"),
                   "--prompt-file", str(self.home / "task.md"), "--jobs", str(self.jobs)]
        for pin in pins:
            command += ["--pin", pin]
        result = subprocess.run(command, text=True, capture_output=True, env=self.env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return max(self.artifacts.glob(".runtime/routes/rt-*.json"), key=lambda path: path.stat().st_mtime_ns)

    def register(self, route_file, adapter, node="execute"):
        import subprocess
        import sys
        owner = subprocess.Popen(["sleep", "120"])
        self.addCleanup(lambda: (owner.kill(), owner.wait()))
        started = (Path("/proc") / str(owner.pid) / "stat").read_text().split()[21]
        self.jobs.write_text(
            f"2026-07-23T00:00:00Z\topen\t{self.repo}\t{self.repo}\towner\t"
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,harness=claude,"
            "runtime_sandbox=adapter-default,execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=owner,attempt_id=att-pin-owner,"
            f"pid={owner.pid},pid_start={started}\n", encoding="utf-8")
        env = dict(self.env, AGENT_DISPATCH_SELF_SLUG="owner", AGENT_DISPATCH_ATTEMPT_ID="att-pin-owner",
                   AGENT_DISPATCH_CURRENT_HARNESS="claude", AGENT_DISPATCH_CURRENT_TRANSPORT="headless",
                   AGENT_DISPATCH_CURRENT_SANDBOX="adapter-default")
        result = subprocess.run(
            [sys.executable, str(ROOT / "utilities" / "dispatch-node.py"), "--route", str(route_file), "--node", node,
             "--adapter", adapter, "--action", "register", "--slug", "pin-exec", "--parent", "owner",
             "--jobs", str(self.jobs)], text=True, capture_output=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = next(line for line in self.jobs.read_text().splitlines() if "\tpin-exec\t" in line)
        return dict(item.split("=", 1) for item in row.split("\t")[5].split(",") if "=" in item)

    def test_a_pinned_worker_row_is_the_pinned_harness_and_records_the_request(self):
        row = self.register(self.compose("worker=claude"), "codex")
        self.assertEqual(row["harness"], "claude")
        self.assertEqual(row["explicit_adapter"], "codex")
        self.assertEqual(row["model_pin_status"], "harness-only")
        self.assertEqual(row["fallback_hop"], "same-harness-headless")
        self.assertEqual(row["harness_affinity"], "claude")

    def test_requesting_the_pinned_harness_or_having_no_worker_pin_adds_no_override_field(self):
        pinned = self.register(self.compose("worker=claude"), "claude")
        self.assertEqual((pinned["harness"], pinned["model_pin_status"]), ("claude", "harness-only"))
        self.assertNotIn("explicit_adapter", pinned)
        # an owner-only pin does not steer a worker
        other = self.register(self.compose("owner=claude"), "codex")
        self.assertEqual(other["harness"], "codex")
        self.assertNotIn("explicit_adapter", other)


if __name__ == "__main__":
    unittest.main()
