"""F-40-adjacent spec-grounding marker attribution matrix (SD-82 fleet-grounding-projection).

Every cell is hermetic: `.spec-grounding` is never scanned from disk here —
`spec_markers` is injected directly as a `{name: mtime}` dict (mirrors the
existing `spec_marker_home`/`spec_markers` injection points added to
`attach_projections`/`resolve_work_projection`). Only `pipeline_state.yaml`
content lives on a tmp filesystem tree, since `_spec_stage_parts` is a real
file reader by design (zero-dep line parser, not injectable).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import projection, render, route  # noqa: E402
from fleet.model import DispatchJob, Session  # noqa: E402


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "route")
REAL = os.path.join(FIXTURES, "real_claude_staged.json")

NOW = 10000.0


def _key(root):
    # Mirrors spec-read-marker.sh: key=$(printf '%s' "$root" | sed 's#[/ ]#_#g')
    return os.path.realpath(root).replace("/", "_").replace(" ", "_")


def _marker_name(sid, root, slug=None):
    name = "%s__%s" % (sid, _key(root))
    return "%s__%s" % (name, slug) if slug else name


def _write_pipeline_state(root, slug, text):
    base = os.path.join(root, ".agent_reports", "spec")
    target_dir = os.path.join(base, slug) if slug else base
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, "pipeline_state.yaml"), "w", encoding="utf-8") as f:
        f.write(text)


def _make_plan_dir(root, name, subdir, mtime):
    """Create <root>/plans/<name>/<subdir>/marker.txt and pin every file's AND
    every directory's mtime to `mtime` -- `os.makedirs`/file-write otherwise
    stamp real wall-clock time, which would swamp the synthetic NOW used across
    this module's freshness comparisons."""
    plan_root = os.path.join(root, "plans", name)
    os.makedirs(os.path.join(plan_root, subdir), exist_ok=True)
    with open(os.path.join(plan_root, subdir, "marker.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    for dirpath, _dirnames, filenames in os.walk(plan_root):
        for fname in filenames:
            os.utime(os.path.join(dirpath, fname), (mtime, mtime))
        os.utime(dirpath, (mtime, mtime))
    return plan_root


def _session(sid="sid-a", cwd=None, elapsed_min=1, slug=None, **kw):
    return Session(harness="claude", pid=1, cwd=cwd, session_id=sid,
                   elapsed_min=elapsed_min, liveness="working", slug=slug, **kw)


def _attach_projections(sessions, jobs, **kwargs):
    """Most legacy cells exercise spec projection itself, so explicitly model
    an active autopilot-spec entry.  F-43 negative cells call the production
    function directly with no capability evidence."""
    home = kwargs.get("spec_marker_home")
    cap_index = projection._capability_grounding_index(home) if home else {}
    if kwargs.get("spec_markers") is not None:
        for entity in sessions:
            if entity.session_id:
                cap_index.setdefault(
                    entity.session_id,
                    (NOW, {"capability": "autopilot-spec"}),
                )
    kwargs["capability_groundings"] = cap_index
    return projection.attach_projections(sessions, jobs, **kwargs)


class SpecMarkerAttributionTest(unittest.TestCase):
    # (a) exact sid + fresh marker + standard pipeline_state -> "spec <topic> ·<phase>"
    def test_a_exact_sid_fresh_marker_standard_pipeline_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a", "phases:\n  design: done\n  dev: in_progress\n")
            markers = {_marker_name("sid-a", tmp, "topic-a"): NOW}
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "artifact-inferred")
            self.assertEqual(entity.work_projection.stage_label, "spec topic-a ·dev")

    # (b) sid mismatch -> unadopted (source "none")
    def test_b_sid_mismatch_not_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a", "phases:\n  dev: in_progress\n")
            markers = {_marker_name("sid-a", tmp, "topic-a"): NOW}
            entity = _session(sid="sid-other", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "none")

    # (c) direction-fixed cell: a fixed/slug sid marker ("codex") is never adopted,
    # even for a real session on the same root -- exact-sid-only structurally
    # excludes it, no cardinality reasoning is (re)introduced here (D2).
    def test_c_fixed_sid_marker_never_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a", "phases:\n  dev: in_progress\n")
            markers = {_marker_name("codex", tmp, "topic-a"): NOW}
            entity = _session(sid="019f1bdf-c9d8-72f0-b529-d8793305210e", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "none")

    # (d) multiple topics: strictly-freshest wins; exact tie -> unadopted +
    # MULTIPLE_SPEC_MARKERS diagnostic when no plans candidate exists either.
    def test_d_multiple_topics_freshest_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-old", "phases:\n  dev: in_progress\n")
            _write_pipeline_state(tmp, "topic-new", "phases:\n  test: in_progress\n")
            markers = {
                _marker_name("sid-a", tmp, "topic-old"): NOW - 500,
                _marker_name("sid-a", tmp, "topic-new"): NOW - 10,
            }
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.stage_label, "spec topic-new ·test")

    def test_d_multiple_topics_exact_tie_rejected_with_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-x", "phases:\n  dev: in_progress\n")
            _write_pipeline_state(tmp, "topic-y", "phases:\n  test: in_progress\n")
            markers = {
                _marker_name("sid-a", tmp, "topic-x"): NOW - 10,
                _marker_name("sid-a", tmp, "topic-y"): NOW - 10,
            }
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "none")
            self.assertEqual(entity.work_projection.ambiguity, projection.MULTIPLE_SPEC_MARKERS)

    # (e) minimal falsification cell from the original brief: route evidence
    # present + a valid exact-sid marker -> route label wins, spec label absent
    # (handoff rule 2, guaranteed structurally by control-flow order).
    def test_e_route_evidence_wins_over_valid_marker(self):
        rid = route.load(REAL)["route_id"]
        with tempfile.TemporaryDirectory() as tmp:
            markers = {_marker_name("sid-e", tmp): NOW}
            entity = _session(sid="sid-e", cwd=tmp, route_id=rid, route_file=REAL, route_node="execute")
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "route-exact")
            self.assertEqual(entity.work_projection.stage_label, "execute")

    # (f) freshness boundary matrix: slack = 120s past estimated session start
    # (now - elapsed_min*60).
    def test_f_freshness_boundary_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a", "phases:\n  dev: in_progress\n")
            elapsed_min = 5
            start = NOW - elapsed_min * 60

            def resolve_at(mtime):
                markers = {_marker_name("sid-a", tmp, "topic-a"): mtime}
                entity = _session(sid="sid-a", cwd=tmp, elapsed_min=elapsed_min)
                _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
                return entity.work_projection

            with self.subTest("beyond slack before start -> reject"):
                self.assertEqual(resolve_at(start - 130).source, "none")
            with self.subTest("within slack before start -> accept"):
                self.assertEqual(resolve_at(start - 100).source, "artifact-inferred")
            with self.subTest("after start -> accept"):
                self.assertEqual(resolve_at(start + 50).source, "artifact-inferred")
            with self.subTest("exactly at slack boundary -> accept (>=)"):
                self.assertEqual(resolve_at(start - 120).source, "artifact-inferred")

    # (g) pipeline_state absent -> topic-only
    def test_g_pipeline_state_absent_topic_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(tmp, exist_ok=True)
            markers = {_marker_name("sid-a", tmp, "topic-a"): NOW}
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.stage_label, "spec topic-a")

    # (h) phases: absent, top-level status: present -> status fallback (value taken verbatim)
    def test_h_terminal_status_fallback_is_not_live_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a", "mode: library\nphase: complete\nstatus: complete\n")
            markers = {_marker_name("sid-a", tmp, "topic-a"): NOW}
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "none")

    # (i) all three keys absent -> topic-only
    def test_i_no_recognized_keys_topic_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a", "mode: library\nupdated: 2026-07-14\n")
            markers = {_marker_name("sid-a", tmp, "topic-a"): NOW}
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.stage_label, "spec topic-a")

    # (i2) non-UTF-8 pipeline_state.yaml -> degrades to topic-only, never raises
    def test_i2_non_utf8_pipeline_state_topic_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, ".agent_reports", "spec", "topic-a")
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, "pipeline_state.yaml"), "wb") as f:
                f.write(b"phases:\n  dev: in_progress\xff\xfe\n")
            markers = {_marker_name("sid-a", tmp, "topic-a"): NOW}
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.stage_label, "spec topic-a")

    # (j) root marker: project_name: present -> topic reflected; absent (context-recovery-like) -> bare "spec" label
    def test_j_root_marker_with_project_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, None, "project_name: my-proj\nphases:\n  dev: in_progress\n")
            markers = {_marker_name("sid-a", tmp): NOW}
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.stage_label, "spec my-proj ·dev")

    def test_j_root_terminal_marker_is_not_live_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, None, "mode: library,cli\nphase: complete\nstatus: complete\n")
            markers = {_marker_name("sid-a", tmp): NOW}
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "none")

    def test_terminal_done_and_skipped_sequence_is_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(
                tmp, "topic-a", "phases:\n  design: done\n  dev: deferred\n"
            )
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections(
                [entity], [], artifact_root=tmp, now=NOW,
                spec_markers={_marker_name("sid-a", tmp, "topic-a"): NOW},
            )
            self.assertEqual(entity.work_projection.source, "none")

    def test_terminal_sequence_with_active_phase_remains_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(
                tmp, "topic-a", "phases:\n  design: done\n  dev: in_progress\n"
            )
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections(
                [entity], [], artifact_root=tmp, now=NOW,
                spec_markers={_marker_name("sid-a", tmp, "topic-a"): NOW},
            )
            self.assertEqual(entity.work_projection.source, "artifact-inferred")

    # (k) marker vs plans-glob freshness mediation
    def test_k_plans_dir_newer_keeps_code_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_plan_dir(tmp, "2026-07-24_proj-a", "test", NOW - 5)
            markers = {_marker_name("sid-a", tmp): NOW - 50}
            entity = _session(sid="sid-a", cwd=tmp, slug="proj-a")
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "artifact-inferred")
            self.assertEqual(entity.work_projection.stage_label, "test")

    def test_k_marker_newer_wins_spec_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_plan_dir(tmp, "2026-07-24_proj-a", "test", NOW - 50)
            markers = {_marker_name("sid-a", tmp): NOW - 5}
            entity = _session(sid="sid-a", cwd=tmp, slug="proj-a")
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "artifact-inferred")
            self.assertEqual(entity.work_projection.stage_label, "spec")

    def test_k_tie_is_unadopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_plan_dir(tmp, "2026-07-24_proj-a", "test", NOW - 5)
            markers = {_marker_name("sid-a", tmp): NOW - 5}
            entity = _session(sid="sid-a", cwd=tmp, slug="proj-a")
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "none")
            self.assertEqual(entity.work_projection.ambiguity, projection.MARKER_ARTIFACT_TIE)

    def test_k_multiple_plans_candidates_marker_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "plans", "2026-07-24_proj-a", "test"))
            os.makedirs(os.path.join(tmp, "plans", "2026-07-20_proj-a", "plan"))
            markers = {_marker_name("sid-a", tmp): NOW}
            entity = _session(sid="sid-a", cwd=tmp, slug="proj-a")
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "artifact-inferred")
            self.assertEqual(entity.work_projection.stage_label, "spec")

    # (l) DispatchJob never adopts a marker (rule 1); other-root marker excluded;
    # session_id-less session never adopts.
    def test_l_dispatch_job_never_adopts_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a", "phases:\n  dev: in_progress\n")
            markers = {_marker_name("sid-a", tmp, "topic-a"): NOW}
            job = DispatchJob(key="code", slug="topic-a", cwd=tmp, liveness="working")
            _attach_projections([], [job], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(job.work_projection.source, "none")

    def test_l_other_root_marker_excluded(self):
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            _write_pipeline_state(tmp_b, "topic-a", "phases:\n  dev: in_progress\n")
            markers = {_marker_name("sid-a", tmp_b, "topic-a"): NOW}
            entity = _session(sid="sid-a", cwd=tmp_a)
            _attach_projections([entity], [], artifact_root=tmp_a, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "none")

    def test_l_session_without_session_id_never_adopts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a", "phases:\n  dev: in_progress\n")
            markers = {"nosession__%s__topic-a" % _key(tmp): NOW}
            entity = Session(harness="claude", pid=1, cwd=tmp, session_id=None,
                             elapsed_min=1, liveness="working")
            _attach_projections([entity], [], artifact_root=tmp, now=NOW, spec_markers=markers)
            self.assertEqual(entity.work_projection.source, "none")


class SpecGroundingRenderTest(unittest.TestCase):
    def test_spec_label_shows_no_fake_code_track(self):
        entity = _session(sid="sid-a", cwd="/tmp/whatever")
        entity.liveness = "working"
        entity.work_projection = projection.WorkProjection(
            source="artifact-inferred", stage_label="spec topic-a ·dev")
        self.assertEqual(render._projection_stage_detail_rows(entity), [])

    def test_code_label_no_longer_renders_a_detail_track(self):
        # 2026-07-24: an INFERRED inline code stage carries no dedicated `plan › exec › test`
        # detail row — the stage rides the row's own column only (user "main 세션에 여전히 stage
        # 뜨는 케이스"). Parity with the spec-grounding label, which was already row-less.
        entity = _session(sid="sid-a", cwd="/tmp/whatever")
        entity.liveness = "working"
        entity.work_projection = projection.WorkProjection(
            source="artifact-inferred", stage_label="exec")
        self.assertEqual(render._projection_stage_detail_rows(entity), [])

    def test_long_topic_label_clips_safely_within_available_width(self):
        entity = _session(sid="sid-a", cwd="/tmp/whatever")
        entity.work_projection = projection.WorkProjection(
            source="artifact-inferred", stage_label="spec agent-fleet-dashboard ·dev")
        text = render._projection_stage_text(entity, max_width=24)
        self.assertLessEqual(render._dw(text), 24)
        self.assertTrue(text.startswith("stage "))
        self.assertTrue(text.endswith("…"))


class SpecPhaseSequenceTest(unittest.TestCase):
    """2026-07-24: pipeline_state.yaml phases -> ordered [(display, state)] for a lit breadcrumb."""

    def test_standard_phases_verbatim_with_state_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a",
                "phases:\n  spec: done\n  scaffolding: deferred\n"
                "  design: n/a\n  dev: in_progress\nlast_updated: 2026-07-24\n")
            seq = projection._spec_phase_sequence(tmp, "topic-a")
        self.assertEqual(seq, [("spec", "done"), ("scaffolding", "skipped"),
                               ("design", "skipped"), ("dev", "active")])

    def test_long_custom_names_collapse_to_phase_ordinal(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a",
                "phases:\n  phase_1_cross_runtime_source_activation: completed\n"
                "  phase_2_manifest_projections_profiles: in_progress\n"
                "  some_other_really_long_custom_phase_name: pending\n")
            seq = projection._spec_phase_sequence(tmp, "topic-a")
        # explicit phase_<N>_ prefix -> Phase<N>; a long non-standard name -> Phase<position>.
        self.assertEqual(seq, [("Phase1", "done"), ("Phase2", "active"), ("Phase3", "pending")])

    def test_missing_or_malformed_state_file_yields_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(projection._spec_phase_sequence(tmp, "absent"), [])
            _write_pipeline_state(tmp, "topic-a", "status: completed\n")  # no phases block
            self.assertEqual(projection._spec_phase_sequence(tmp, "topic-a"), [])

    def test_projection_attaches_sequence_and_row_renders_lit_breadcrumb(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a",
                "mode: [cli]\nphases:\n  spec: done\n  scaffolding: deferred\n  dev: in_progress\n")
            markers = {_marker_name("sid-a", tmp, "topic-a"): NOW}
            entity = _session(sid="sid-a", cwd=tmp)
            _attach_projections([entity], [], artifact_root=tmp, now=NOW,
                                          spec_markers=markers)
            seq = render._spec_phase_seq(entity)
            self.assertEqual(seq, [("spec", "done"), ("scaffolding", "skipped"),
                                   ("dev", "active")])
            segs = render._session_stage_segs(entity, working=True, max_width=80)
            text = "".join(t for t, _k in segs)
            # Code-parity paren syntax: `spec(cli) : …` — parens (not brackets), dim colon.
            self.assertIn("spec(cli) : ", text)
            self.assertNotIn("[cli]", text)               # square brackets never used
            self.assertNotIn("spec-cli", text)            # mode is a paren knob, not a name suffix
            self.assertNotIn("topic-a", text)             # topic dropped
            self.assertIn("spec ✓", text)                  # done glyph
            self.assertNotIn("scaffolding", text)         # deferred phase filtered out (no ⊘)
            self.assertNotIn("⊘", text)                   # skip glyph never rendered
            self.assertIn("dev", text)                    # active phase present
            self.assertIn("›", text)                      # dispatch-syntax separator
            # entry `spec` rides name_dim, the paren group is dim — exactly _opts_segs.
            keyed = dict((t, k) for t, k in segs)
            self.assertEqual(keyed.get("spec"), "name_dim")
            self.assertEqual(keyed.get("("), "dim")

    def test_mode_rides_the_paren_group_like_code(self):
        cases = {"cli": "spec(cli) : ", "library": "spec(library) : ",
                 "app": "spec(app) : ", "library,cli": "spec(library·cli) : "}
        for mode, expected in cases.items():
            with tempfile.TemporaryDirectory() as tmp:
                _write_pipeline_state(tmp, "t",
                    "mode: [%s]\nphases:\n  dev: in_progress\n" % mode)
                markers = {_marker_name("sid-a", tmp, "t"): NOW}
                entity = _session(sid="sid-a", cwd=tmp)
                _attach_projections([entity], [], artifact_root=tmp, now=NOW,
                                              spec_markers=markers)
                segs = render._session_stage_segs(entity, working=True, max_width=80)
                text = "".join(t for t, _k in segs)
                with self.subTest(mode=mode):
                    self.assertIn(expected, text)
            # active phase carries a lit (non-dim) color key so it stands out / blinks.
            active_keys = [k for t, k in segs if t == "dev"]
            self.assertTrue(any(k and k.startswith("stg") and k.endswith("_on")
                                for k in active_keys))

    def test_mode_parsing_flow_and_block_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "flow", "mode: [library, cli]\nphases:\n  dev: in_progress\n")
            self.assertEqual(projection._spec_mode(tmp, "flow"), "library,cli")
            _write_pipeline_state(tmp, "block", "mode:\n  - library\n  - cli\nphases:\n  dev: done\n")
            self.assertEqual(projection._spec_mode(tmp, "block"), "library,cli")
            _write_pipeline_state(tmp, "none", "phases:\n  dev: done\n")
            self.assertIsNone(projection._spec_mode(tmp, "none"))

    def test_wide_row_does_not_truncate_breadcrumb_on_wide_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a",
                "phases:\n  spec: done\n  scaffolding: done\n  skeleton: done\n"
                "  design: done\n  dev: in_progress\n  ship_setup: pending\n")
            markers = {_marker_name("sid-a", tmp, "topic-a"): NOW}
            entity = _session(sid="sid-a", cwd=tmp, slug="topic-a")
            _attach_projections([entity], [], artifact_root=tmp, now=NOW,
                                          spec_markers=markers)
            lines = render._build_lines([entity], [], "fleet", False, 0,
                                        layout="wide", term_width=220)
            text = "\n".join("".join(t for t, _k in ln) for ln in lines if ln)
            # The full 6-phase breadcrumb survives on a wide terminal (no early `…` clip).
            for phase in ("spec ✓", "scaffolding ✓", "skeleton ✓", "design ✓", "ship_setup"):
                self.assertIn(phase, text)


class RouteStageSkippedGlyphTest(unittest.TestCase):
    def test_skipped_residual_renders_glyph_free(self):
        # spec skipped phases are filtered before rendering; a residual one must not emit a
        # font-fragile skip glyph (⊘ is missing from many terminal fonts) — it falls through to
        # the plain dim name instead.
        segs = render._route_stage_segs([("a", "done"), ("b", "skipped"), ("c", "active")],
                                        working=False, max_width=60)
        rendered = "".join(t for t, _k in segs)
        self.assertIn("a ✓", rendered)
        self.assertIn(" b ", " %s " % rendered)   # plain name, no glyph
        self.assertNotIn("⊘", rendered)


class CapabilityGroundingTest(unittest.TestCase):
    """2026-07-24: inline entry work (no route/dispatch row) shows `capability(mode·intensity)`
    on the main session row from a `.capability-grounding/<sid>` marker."""

    def _write(self, home, sid, body):
        d = os.path.join(home, ".capability-grounding")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid), "w", encoding="utf-8") as handle:
            handle.write(body)

    def test_inline_code_session_shows_capability_mode_intensity(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "sid-c", "capability=autopilot-code\nmode=dev\nintensity=standard\n")
            s = _session(sid="sid-c", cwd="/x")
            _attach_projections([s], [], now=NOW, spec_marker_home=tmp)
            self.assertEqual(s.cap_grounding,
                             {"capability": "autopilot-code", "mode": "dev", "intensity": "standard"})
            text = "".join(t for t, _k in render._session_stage_segs(s, True, 80))
            self.assertIn("code(dev·std)", text)   # short intensity, dispatch-options syntax

    def test_spec_session_intensity_comes_from_grounding_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(tmp, "topic-a", "mode: [cli]\nphases:\n  spec: done\n  dev: in_progress\n")
            markers = {_marker_name("sid-s", tmp, "topic-a"): NOW}
            self._write(tmp, "sid-s", "capability=autopilot-spec\nintensity=strong\n")
            s = _session(sid="sid-s", cwd=tmp)
            _attach_projections([s], [], artifact_root=tmp, now=NOW,
                                          spec_markers=markers, spec_marker_home=tmp)
            text = "".join(t for t, _k in render._session_stage_segs(s, True, 80))
            self.assertIn("spec(cli·strong) : ", text)   # intensity from the grounding marker

    def test_spec_read_marker_alone_is_not_active_inline_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(
                tmp, "topic-a", "mode: [cli]\nphases:\n  dev: in_progress\n"
            )
            s = _session(sid="sid-read-only", cwd=tmp)
            projection.attach_projections(
                [s], [], artifact_root=tmp, now=NOW,
                spec_markers={_marker_name("sid-read-only", tmp, "topic-a"): NOW},
                capability_groundings={},
            )
            self.assertEqual(s.work_projection.source, "none")
            self.assertEqual(
                "".join(t for t, _k in render._session_stage_segs(s, True, 80)), "-"
            )

    def test_non_spec_capability_hides_a_fresher_spec_read_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_pipeline_state(
                tmp, "topic-a", "mode: [cli]\nphases:\n  dev: in_progress\n"
            )
            self._write(
                tmp, "sid-code",
                "capability=autopilot-code\nmode=debug\nintensity=direct\n",
            )
            s = _session(sid="sid-code", cwd=tmp)
            _attach_projections(
                [s], [], artifact_root=tmp, now=NOW,
                spec_markers={_marker_name("sid-code", tmp, "topic-a"): NOW},
                spec_marker_home=tmp,
            )
            text = "".join(t for t, _k in render._session_stage_segs(s, True, 80))
            self.assertEqual(s.work_projection.source, "none")
            self.assertIn("code(debug·direct)", text)
            self.assertNotIn("spec", text)

    def test_non_entry_and_stale_markers_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            # a non-entry capability is never written by the util, but if present the index drops it
            self._write(tmp, "sid-x", "capability=dataviz\nintensity=standard\n")
            index = projection._capability_grounding_index(tmp)
            self.assertNotIn("sid-x", index)
            # a marker predating the session start (sid reuse) is rejected
            self._write(tmp, "sid-old", "capability=autopilot-code\n")
            os.utime(os.path.join(tmp, ".capability-grounding", "sid-old"), (NOW - 99999, NOW - 99999))
            s = _session(sid="sid-old", cwd="/x", elapsed_min=1)
            self.assertIsNone(projection._capability_grounding_for(
                s, projection._capability_grounding_index(tmp), now=NOW))


if __name__ == "__main__":
    unittest.main()
