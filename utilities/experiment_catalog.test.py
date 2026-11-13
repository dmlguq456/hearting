#!/usr/bin/env python3
"""Tests for `experiment_catalog.py`: the project experiment catalog and the read-only status.

Every fixture lives in a `TemporaryDirectory` with its own HOME and without `AGENT_ARTIFACT_ROOT`;
no real artifact root is read.  Public run records are produced by the real `resource_progress`
writer so a change to their shape breaks these tests rather than the status.  The model, tmux, and
the network are never used.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as admission  # noqa: E402
import artifact_history as H  # noqa: E402
import experiment_catalog as E  # noqa: E402
import resource_progress as rp  # noqa: E402

SCRIPT = Path(E.__file__).resolve()
NOW = 1_790_000_000.0
METRIC = {"key": "wer", "unit": "%", "direction": "min", "split": "validation", "scoring": "AMI Dev, oracle VAD"}
FIXED = {"kind": "fixed", "label": "fix", "value": 19.99, "split": "validation", "scoring": "AMI Dev, oracle VAD"}


def group_spec(**over):
    spec = {"display_name": "AMI 가변 화자 개선", "goal": "AMI Dev에서 var WER이 fix 19.99%보다 낮은가",
            "metric": dict(METRIC), "target": dict(FIXED), "status": "running"}
    spec.update(over)
    return spec


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_digests(folder):
    return {p.name: digest(p) for p in sorted(Path(folder).glob("*.json"))}


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items() if k != "AGENT_ARTIFACT_ROOT"}
    env["HOME"] = str(home)
    env.update(extra)
    return env


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        old = {k: os.environ.get(k) for k in ("HOME", "AGENT_ARTIFACT_ROOT")}
        os.environ["HOME"] = str(self.home)
        os.environ.pop("AGENT_ARTIFACT_ROOT", None)

        def restore():
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)

    def make_root(self, name="proj", parent="a", identity=True):
        root = self.base / parent / name / ".agent_reports"
        root.mkdir(parents=True)
        if identity:
            admission.ensure_root_identity(root)
        return root

    def register(self, root, spec, now=NOW):
        return E.run_write(root, lambda ctx: E.op_register(ctx, spec), now=now)

    def write_run(self, root, run_id, *, status="running", started=NOW - 3600, at=NOW - 180, eta=1800.0,
                  reached=12, total=40, unit="epoch", series=None, key="wer", stagnation=None,
                  split="validation", rows=None, ended=None):
        series = {"wer": [(10, 21.0), (12, 20.4)]} if series is None else series
        samples = [{"split": split, "epoch": epoch, "metrics": {name: value}}
                   for name, points in series.items() for epoch, value in points]
        summary = rp.metric_summary(samples, decision_metric=key, direction="min")
        observation = {"reached": reached, "total": total, "unit": unit, "eta_seconds": eta, "at": at,
                       "summary": summary, "stagnation": stagnation}
        run = {"run_id": run_id, "status": status, "started_at": started, "decision_metric": key}
        record = rp.public_run_record(run, status=status, observation=observation, metrics=[], started_at=started,
                                      ended_at=ended)
        if rows is not None:
            record["rows"] = rows
        self.assertIsNotNone(rp.write_public_run_record(root, run_id, record))

    def cond(self, status_doc, name):
        for group in status_doc["groups"]:
            for row in group["conditions"]:
                if row["display_name"] == name:
                    return row
        raise AssertionError(f"condition {name!r} not in status")

    def ids(self, result):
        return [row["id"] for row in result["result"]["conditions"]]

    def two_conditions(self, root, **group_over):
        spec = {"groups": [group_spec(**group_over)],
                "conditions": [
                    {"display_name": "0.3초 이하 짧은 발화 유지", "group": "AMI 가변 화자 개선",
                     "refs": {"runs": [{"run_id": "run-a"}]}},
                    {"display_name": "짧은 발화 제외", "group": "AMI 가변 화자 개선",
                     "refs": {"runs": [{"run_id": "run-b"}]}}]}
        return self.register(root, spec)


class RenameFollows(Base):
    def test_t1_rename_reaches_status_describe_and_resolve_without_touching_run_records(self):
        root = self.make_root()
        result = self.two_conditions(root)
        self.write_run(root, "run-a")
        self.write_run(root, "run-b", series={"wer": [(12, 22.0)]})
        before_files = tree_digests(root / "resource-runs")
        first = E.status(root, now=NOW)
        self.assertEqual(self.cond(first, "0.3초 이하 짧은 발화 유지")["latest"]["value"], 20.4)
        cond_a = self.ids(result)[0]
        E.run_write(root, lambda ctx: E.op_rename(ctx, "0.3초 이하 짧은 발화 유지", "짧은 발화 유지, 최대 10명"), now=NOW + 5)
        doc = E.load(root).doc
        after = E.status(root, now=NOW)
        self.assertEqual(self.cond(after, "짧은 발화 유지, 최대 10명")["id"], cond_a)
        self.assertNotIn("0.3초 이하 짧은 발화 유지", json.dumps(after["groups"], ensure_ascii=False))
        self.assertIn("짧은 발화 유지, 최대 10명", E.render_status(after, "ko"))
        self.assertEqual(E.resolve(doc, "0.3초 이하 짧은 발화 유지")["id"], cond_a)  # the old name still finds it
        self.assertEqual(E.describe_run(doc, "run-a")["name"], "짧은 발화 유지, 최대 10명")
        self.assertEqual(tree_digests(root / "resource-runs"), before_files)

    def test_t2_merge_moves_runs_and_names_and_repoints_comparisons(self):
        root = self.make_root()
        spec = {"groups": [group_spec()],
                "conditions": [
                    {"display_name": "A 조건", "group": "AMI 가변 화자 개선", "aliases": ["a-short"],
                     "refs": {"runs": [{"run_id": "run-a"}]}},
                    {"display_name": "B 조건", "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": "run-b"}]}},
                    {"display_name": "C 조건", "group": "AMI 가변 화자 개선", "compared_to": "A 조건"}]}
        ids = self.ids(self.register(root, spec))
        self.write_run(root, "run-a", started=NOW - 7200)
        self.write_run(root, "run-b", started=NOW - 600)
        E.run_write(root, lambda ctx: E.op_merge(ctx, "A 조건", "B 조건", NOW), now=NOW)
        doc = E.load(root).doc
        self.assertEqual([c["id"] for c in doc["conditions"]], [ids[1], ids[2]])
        for token in (ids[0], "A 조건", "a-short"):
            self.assertEqual(E.resolve(doc, token)["id"], ids[1], token)
        self.assertEqual(next(c for c in doc["conditions"] if c["id"] == ids[2])["compared_to"], ids[1])
        row = self.cond(E.status(root, now=NOW), "B 조건")
        self.assertEqual(row["current_run"]["run_id"], "run-b")  # the later start is the current run
        self.assertEqual(row["other_runs"], {"count": 1, "run_ids": ["run-a"]})
        self.assertEqual(doc["merged"][0]["from"], ids[0])


class NothingToShow(Base):
    def run_cli(self, *args, **env):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                              env=clean_env(self.home, **env))

    def test_t3_no_catalog_names_nothing_and_never_fails(self):
        root = self.make_root()
        self.write_run(root, "mpwav-20261111-aaa")
        self.write_run(root, "mpwav-20261111-bbb")
        report = E.status(root, now=NOW)
        self.assertEqual(report["catalog"]["status"], "missing")
        self.assertEqual(report["groups"], [])
        self.assertEqual(sorted(report["unlinked_runs"]["run_ids"]), ["mpwav-20261111-aaa", "mpwav-20261111-bbb"])
        self.assertIn("catalog-missing", {w["code"] for w in report["warnings"]})
        for command in ("status", "lint", "show"):
            done = self.run_cli(command, "--artifact-root", str(root), "--json")
            self.assertEqual(done.returncode, 0, (command, done.stderr))
            json.loads(done.stdout)
        text = E.render_status(report, "ko")
        self.assertIn("mpwav-20261111-aaa", text)
        self.assertFalse(E.catalog_path(root).exists())  # reading creates nothing
        self.assertFalse((root / H.HISTORY_REL).exists())

    def test_t3_a_root_that_does_not_exist_is_still_exit_zero(self):
        done = self.run_cli("status", "--artifact-root", str(self.base / "nowhere" / ".agent_reports"), "--json")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(json.loads(done.stdout)["catalog"]["status"], "missing")
        self.assertFalse((self.base / "nowhere").exists())


class Warnings(Base):
    def test_t4_lint_names_each_gap_by_code_and_place_and_blocks_nothing(self):
        root = self.make_root()
        spec = {"terms": [{"term": "기준 var", "meaning": "원고의 기준"}],
                "groups": [group_spec(), {"display_name": "목표 없는 묶음"},
                           {"display_name": "단위 없는 묶음", "goal": "g",
                            "metric": {"key": "loss", "direction": "min"}, "target": dict(FIXED)}],
                "conditions": [
                    {"display_name": "mpwav-run-1", "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": "mpwav-run-1"}]}},
                    {"display_name": "uniform-retain030-n10", "group": "AMI 가변 화자 개선",
                     "refs": {"configs": ["/x/uniform-retain030-n10.yaml"]}},
                    {"display_name": "같은 이름", "group": "AMI 가변 화자 개선", "aliases": ["별칭"]},
                    {"display_name": "같은 이름", "group": "AMI 가변 화자 개선", "aliases": ["별칭"]},
                    {"display_name": "기준 var", "group": "AMI 가변 화자 개선"},
                    {"display_name": "", "group": "AMI 가변 화자 개선"}]}
        written = self.register(root, spec)
        self.assertEqual(written["status"], "applied")  # warnings never stop the write
        doc = E.load(root).doc
        found = {}
        for warning in E.lint(doc, expect=["아직 없는 이름"], unlinked=["orphan-run"]):
            found.setdefault(warning["code"], []).append(warning["where"])
        ids = {c["display_name"]: c["id"] for c in doc["conditions"]}
        groups = {g["display_name"]: g["id"] for g in doc["groups"]}
        self.assertIn(f"conditions[{ids['mpwav-run-1']}].display_name", found["display-name-is-identifier"])
        self.assertIn(f"conditions[{ids['uniform-retain030-n10']}].display_name", found["display-name-is-identifier"])
        duplicate = next(w for w in found["display-name-duplicate"] if "," in w)
        self.assertEqual(duplicate.count("conditions["), 2)
        self.assertTrue(found["alias-duplicate"])
        self.assertIn(f"conditions[{ids['기준 var']}].display_name", found["term-conflict"])
        self.assertTrue(found["display-name-missing"])
        self.assertIn(f"groups[{groups['목표 없는 묶음']}].goal", found["group-goal-missing"])
        self.assertIn(f"groups[{groups['목표 없는 묶음']}].metric", found["metric-undeclared"])
        self.assertIn(f"groups[{groups['단위 없는 묶음']}].metric.unit", found["metric-unit-missing"])
        self.assertIn(f"groups[{groups['목표 없는 묶음']}].target", found["target-undeclared"])
        self.assertIn("expect[0]", found["condition-missing"])
        self.assertIn("resource-runs/orphan-run.json", found["condition-missing"])
        done = subprocess.run([sys.executable, str(SCRIPT), "lint", "--artifact-root", str(root), "--expect", "x"],
                              capture_output=True, text=True, timeout=60, env=clean_env(self.home))
        self.assertEqual(done.returncode, 0)

    def test_t4_a_broken_reference_is_the_one_thing_a_write_refuses(self):
        root = self.make_root()
        with self.assertRaises(E.CatalogError) as ctx:
            self.register(root, {"conditions": [{"display_name": "x", "group": "없는 묶음"}]})
        self.assertEqual(ctx.exception.code, "group-unknown")
        self.assertFalse(E.catalog_path(root).exists())


class Comparison(Base):
    def point(self, value=20.4, epoch=12, key="wer", nonfinite=False):
        return {"key": key, "epoch": epoch, "value": value, "nonfinite": nonfinite}

    def test_t5_compare_gives_a_difference_only_for_values_measured_alike(self):
        group = group_spec()
        ok = E.compare(group, self.point(20.4), None)
        self.assertEqual(ok["state"], "ok")
        self.assertAlmostEqual(ok["delta"], 0.41)
        self.assertEqual(ok["delta_unit"], "pp")  # percent values subtract into percentage points
        self.assertAlmostEqual(ok["relative_percent"], 0.41 / 19.99 * 100, places=4)
        self.assertIs(ok["better"], False)
        self.assertIs(E.compare(group, self.point(19.0), None)["better"], True)
        # (c) a fixed baseline whose evaluation conditions are not declared
        bare = group_spec(target={"kind": "fixed", "label": "fix", "value": 19.99})
        self.assertEqual(E.compare(bare, self.point(), None)["reason"], "split-or-scoring-unverified")
        # (d) scored differently
        other = group_spec(target={**FIXED, "scoring": "AMI Dev, system VAD"})
        self.assertEqual(E.compare(other, self.point(), None)["reason"], "scoring-mismatch")
        # (e) a split the run records do not carry
        dev = group_spec(metric={**METRIC, "split": "AMI Dev"}, target={**FIXED, "split": "AMI Dev"})
        result = E.compare(dev, self.point(), None)
        self.assertEqual((result["state"], result["reason"], result["delta"]), ("not-comparable", "split-not-observed", None))
        # (f) not a finite number
        self.assertEqual(E.compare(group, self.point(None, nonfinite=True), None)["reason"], "nonfinite")
        # (g) only a best value exists: no latest, no difference
        result = E.compare(group, {"key": "wer", "epoch": None, "value": None, "nonfinite": False, "best": {"value": 18.0}}, None)
        self.assertEqual((result["state"], result["reason"], result["delta"]), ("no-data", "no-observation", None))
        self.assertEqual(E.compare(group, None, None)["state"], "no-data")
        # (h) another metric
        self.assertEqual(E.compare(group, self.point(key="loss"), None)["reason"], "metric-mismatch")
        # (i) direction max flips `better`
        top = group_spec(metric={**METRIC, "direction": "max", "key": "acc", "unit": ""}, target={**FIXED, "value": 0.5})
        self.assertIs(E.compare(top, self.point(0.6, key="acc"), None)["better"], True)
        self.assertIsNone(E.compare(top, self.point(0.6, key="acc"), None)["delta_unit"])
        # undeclared metric / target
        self.assertEqual(E.compare(group_spec(metric=None), self.point(), None)["reason"], "metric-undeclared")
        self.assertEqual(E.compare(group_spec(target=None), self.point(), None)["reason"], "target-undeclared")

    def condition_target_group(self, root):
        spec = {"groups": [group_spec(target={"kind": "condition", "condition": "기준 조건"}, metric={**METRIC, "scoring": None})],
                "conditions": [
                    {"display_name": "기준 조건", "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": "run-ctl"}]}},
                    {"display_name": "실험 조건", "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": "run-exp"}]}}]}
        return self.register(root, spec)

    def test_t5_same_epoch_compares_and_different_epochs_do_not(self):
        root = self.make_root()
        self.condition_target_group(root)
        self.write_run(root, "run-ctl", series={"wer": [(12, 20.0)]})
        self.write_run(root, "run-exp", series={"wer": [(12, 20.5)]})
        row = self.cond(E.status(root, now=NOW), "실험 조건")
        self.assertEqual(row["comparison"]["state"], "ok")
        self.assertAlmostEqual(row["comparison"]["delta"], 0.5)
        self.assertEqual(row["comparison"]["delta_unit"], "pp")
        self.assertAlmostEqual(row["comparison"]["relative_percent"], 2.5)
        self.assertEqual(row["comparison"]["baseline"]["label"], "기준 조건")
        self.assertEqual(self.cond(E.status(root, now=NOW), "기준 조건")["comparison"]["reason"], "is-control")
        self.write_run(root, "run-exp", series={"wer": [(10, 20.5)]})  # (b) the control is further along
        row = self.cond(E.status(root, now=NOW), "실험 조건")
        self.assertEqual((row["comparison"]["state"], row["comparison"]["reason"]), ("not-comparable", "epoch-mismatch"))
        self.assertIsNone(row["comparison"]["delta"])
        self.assertEqual((row["comparison"]["value"]["epoch"], row["comparison"]["baseline"]["epoch"]), (10, 12))
        self.assertIn("비교 불가", E.render_status(E.status(root, now=NOW), "ko"))


class UnknownValues(Base):
    def test_t6_unknown_stays_unknown(self):
        root = self.make_root()
        spec = {"groups": [group_spec()], "conditions": [
            {"display_name": "eta 없음", "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": "run-noeta"}]}},
            {"display_name": "깨진 기록", "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": "run-broken"}]}},
            {"display_name": "기록 없음", "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": "run-gone"}]}},
            {"display_name": "실행 없음", "group": "AMI 가변 화자 개선"}]}
        self.register(root, spec)
        self.write_run(root, "run-noeta", eta=None, series={})
        (root / "resource-runs" / "run-broken.json").write_text("{not json", encoding="utf-8")
        report = E.status(root, now=NOW)
        row = self.cond(report, "eta 없음")
        self.assertIsNone(row["expected_at"])
        self.assertEqual(row["unknown"]["expected_at"], "eta-unknown")
        self.assertIsNone(row["since_last_progress_seconds"])
        self.assertIn("stagnation-unknown", row["anomalies"])
        self.assertIsNone(row["latest"])  # no value is not zero
        self.assertEqual((row["comparison"]["state"], row["comparison"]["reason"]), ("no-data", "no-observation"))
        broken = self.cond(report, "깨진 기록")
        self.assertIn("record-unreadable", broken["anomalies"])
        self.assertIsNone(broken["latest"])
        self.assertIsNone(broken["progress"])
        self.assertIn("record-missing", self.cond(report, "기록 없음")["anomalies"])
        self.assertEqual(self.cond(report, "실행 없음")["unknown"]["run"], "no-run-linked")
        waiting = {w["display_name"] for w in report["waiting"]}
        self.assertEqual(waiting, {"eta 없음"})

    def test_t7_stale_observation_boundary_failed_and_stagnant(self):
        root = self.make_root()
        names = {"run-899": NOW - 899, "run-900": NOW - 900, "run-901": NOW - 901}
        spec = {"groups": [group_spec()], "conditions": [
            {"display_name": run_id, "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": run_id}]}}
            for run_id in (*names, "run-failed", "run-stagnant")]}
        self.register(root, spec)
        for run_id, at in names.items():
            self.write_run(root, run_id, at=at)
        self.write_run(root, "run-failed", status="failed")
        self.write_run(root, "run-stagnant", stagnation={"state": "stagnant", "seconds_since_anchor": 7300.0})
        report = E.status(root, now=NOW)
        self.assertIn("observation-stale", self.cond(report, "run-901")["anomalies"])
        self.assertNotIn("observation-stale", self.cond(report, "run-900")["anomalies"])
        self.assertNotIn("observation-stale", self.cond(report, "run-899")["anomalies"])
        self.assertEqual(self.cond(report, "run-901")["observed_age_seconds"], 901)
        self.assertIn("status-failed", self.cond(report, "run-failed")["anomalies"])
        stagnant = self.cond(report, "run-stagnant")
        self.assertIn("stagnant", stagnant["anomalies"])
        self.assertEqual(stagnant["since_last_progress_seconds"], 7300)
        self.assertIsNotNone(self.cond(report, "run-899")["expected_at"])  # observed + eta


class ProjectBoundary(Base):
    def cli(self, *args, **env):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                              env=clean_env(self.home, **env))

    def test_t8_a_copied_catalog_is_foreign_and_never_written(self):
        root_a = self.make_root("proj", "a")
        root_b = self.make_root("proj", "b")  # same folder name, another parent
        self.register(root_a, {"groups": [group_spec(display_name="A 프로젝트 묶음")],
                               "conditions": [{"display_name": "A 전용 이름", "group": "A 프로젝트 묶음"}]})
        self.write_run(root_b, "b-run-1")
        target = E.catalog_path(root_b)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(E.catalog_path(root_a), target)
        before = target.read_bytes()
        read = E.load(root_b)
        self.assertEqual((read.status, read.code), ("invalid", "foreign-root"))
        report = E.status(root_b, now=NOW)
        self.assertNotIn("A 전용 이름", json.dumps(report, ensure_ascii=False))
        self.assertEqual(report["unlinked_runs"]["run_ids"], ["b-run-1"])
        with self.assertRaises(E.CatalogError) as ctx:
            E.run_write(root_b, lambda c: E.op_rename(c, "A 전용 이름", "x"))
        self.assertEqual((ctx.exception.code, ctx.exception.detail), ("catalog-invalid", "foreign-root"))
        self.assertEqual(target.read_bytes(), before)

    def test_t8_a_path_asks_about_that_project_even_when_a_root_is_set(self):
        root_a = self.make_root("proj", "a")
        root_b = self.make_root("proj", "b")
        self.register(root_b, {"groups": [group_spec(display_name="B 묶음")]})
        done = self.cli("status", "--path", str(root_b.parent), "--json", AGENT_ARTIFACT_ROOT=str(root_a))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(json.loads(done.stdout)["artifact_root"]), root_b)
        old = os.environ.get("AGENT_ARTIFACT_ROOT")
        os.environ["AGENT_ARTIFACT_ROOT"] = str(root_a)
        try:
            self.assertEqual(E.resolve_root(str(root_b.parent)), root_b)
            self.assertEqual(E.resolve_root(None), root_a)  # no path: the session's own root
        finally:
            os.environ.pop("AGENT_ARTIFACT_ROOT", None) if old is None else os.environ.update(AGENT_ARTIFACT_ROOT=old)

    def test_t9_a_linked_worktree_reads_the_primary_catalog(self):
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        primary, linked = self.base / "repo", self.base / "repo-wt"
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        env = clean_env(self.home, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        for command in (["init", "-q", str(primary)],):
            subprocess.run(git + command, check=True, env=env, capture_output=True)
        subprocess.run(git + ["-C", str(primary), "commit", "-q", "--allow-empty", "-m", "x"], check=True, env=env,
                       capture_output=True)
        subprocess.run(git + ["-C", str(primary), "worktree", "add", "-q", str(linked)], check=True, env=env,
                       capture_output=True)
        (primary / ".agent_reports").mkdir()
        root = E.resolve_root(str(primary))
        admission.ensure_root_identity(root)
        self.register(root, {"groups": [group_spec()]})
        seen = []
        for path in (primary, linked):
            done = self.cli("status", "--path", str(path), "--json")
            self.assertEqual(done.returncode, 0, done.stderr)
            report = json.loads(done.stdout)
            seen.append((report["artifact_root"], report["catalog"]["revision"], report["catalog"]["status"]))
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(seen[0][1:], (1, "ok"))
        self.assertEqual(Path(seen[0][0]), root)


class Writing(Base):
    def spawn(self, root, *args):
        return subprocess.Popen([sys.executable, str(SCRIPT), *args, "--artifact-root", str(root)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=clean_env(self.home))

    def test_t10_concurrent_registers_all_survive(self):
        root = self.make_root()
        procs = [self.spawn(root, "register", "--condition-name", f"조건 {i}") for i in range(8)]
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, (out, err))
        doc = E.load(root).doc
        self.assertEqual(sorted(c["display_name"] for c in doc["conditions"]), sorted(f"조건 {i}" for i in range(8)))
        self.assertEqual(doc["revision"], 8)
        json.loads(E.catalog_path(root).read_text(encoding="utf-8"))

    def test_t10_a_rename_and_a_register_at_the_same_time_both_land(self):
        root = self.make_root()
        self.register(root, {"conditions": [{"display_name": "옛 이름"}]})
        procs = [self.spawn(root, "rename", "옛 이름", "--name", "새 이름"),
                 self.spawn(root, "register", "--condition-name", "다른 조건")]
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, (out, err))
        doc = E.load(root).doc
        self.assertEqual(sorted(c["display_name"] for c in doc["conditions"]), ["다른 조건", "새 이름"])
        self.assertEqual(doc["revision"], 3)
        self.assertEqual(E.resolve(doc, "옛 이름")["display_name"], "새 이름")

    def test_t11_a_broken_file_refuses_every_write_and_stays_as_it_was(self):
        root = self.make_root()
        self.write_run(root, "r-1")
        path = E.catalog_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{not json")
        self.assertEqual(E.load(root).code, "json-invalid")
        for mutate in (lambda c: E.op_register(c, {"conditions": [{"display_name": "x"}]}),
                       lambda c: E.op_rename(c, "x", "y"), lambda c: E.op_add_term(c, "t", "m")):
            with self.assertRaises(E.CatalogError) as ctx:
                E.run_write(root, mutate)
            self.assertEqual(ctx.exception.code, "catalog-invalid")
        self.assertEqual(path.read_bytes(), b"{not json")
        report = E.status(root, now=NOW)
        self.assertEqual(report["unlinked_runs"]["run_ids"], ["r-1"])
        self.assertIn("catalog-invalid", {w["code"] for w in report["warnings"]})

    def test_t11_fields_this_version_does_not_know_are_kept(self):
        root = self.make_root()
        self.register(root, {"conditions": [{"display_name": "원래 이름"}]})
        path = E.catalog_path(root)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["future_top"] = {"a": 1}
        doc["conditions"][0]["future_field"] = "keep"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        E.run_write(root, lambda c: E.op_rename(c, "원래 이름", "바뀐 이름"))
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after["future_top"], {"a": 1})
        self.assertEqual(after["conditions"][0]["future_field"], "keep")
        self.assertEqual(after["conditions"][0]["display_name"], "바뀐 이름")

    def test_t12_a_write_is_recorded_and_a_dry_run_leaves_nothing(self):
        root = self.make_root()
        result = self.register(root, {"groups": [group_spec()], "conditions": [{"display_name": "이력 조건"}]})
        self.assertEqual((result["status"], result["events"]), ("applied", 2))
        lines = [json.loads(line) for path in sorted((root / H.HISTORY_REL).glob("*/*.jsonl"))
                 for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 2)
        for event in lines:
            self.assertEqual((event["kind"], event["target"]["type"], event["target"]["path"]),
                             ("meta", "project", E.CATALOG_REL))
            self.assertTrue(event["field"].startswith("experiments."))
        E.run_write(root, lambda c: E.op_rename(c, "이력 조건", "이력 조건 v2"), now=NOW + 1)
        fields = {json.loads(line)["field"] for path in (root / H.HISTORY_REL).glob("*/*.jsonl")
                  for line in path.read_text(encoding="utf-8").splitlines()}
        self.assertTrue(any(f.endswith(".display_name") and ".conditions." in f for f in fields))
        fresh = self.base / "fresh" / ".agent_reports"
        fresh.mkdir(parents=True)
        dry = E.run_write(fresh, lambda c: E.op_register(c, {"groups": [group_spec()]}), dry_run=True)
        self.assertEqual(dry["status"], "dry-run")
        self.assertEqual(list(fresh.iterdir()), [])  # not even a directory
        done = subprocess.run([sys.executable, str(SCRIPT), "register", "--artifact-root", str(root), "--dry-run",
                               "--condition-name", "쓰지 않는 조건"], capture_output=True, text=True, timeout=60,
                              env=clean_env(self.home))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("쓰지 않는 조건", E.catalog_path(root).read_text(encoding="utf-8"))
        self.assertEqual(E.load(root).revision, 2)

    def test_t14_retry_runs_and_a_run_claimed_by_another_condition(self):
        root = self.make_root()
        result = self.register(root, {"groups": [group_spec()], "conditions": [
            {"display_name": "재시도 조건", "group": "AMI 가변 화자 개선",
             "refs": {"runs": [{"run_id": "try-1"}, {"run_id": "try-2"}]}},
            {"display_name": "다른 조건", "group": "AMI 가변 화자 개선"}]})
        self.write_run(root, "try-1", started=NOW - 9000, series={"wer": [(5, 25.0)]})
        self.write_run(root, "try-2", started=NOW - 1000, series={"wer": [(3, 24.0)]})
        row = self.cond(E.status(root, now=NOW), "재시도 조건")
        self.assertEqual(row["current_run"]["run_id"], "try-2")
        self.assertEqual(row["other_runs"], {"count": 1, "run_ids": ["try-1"]})
        self.assertEqual(row["latest"]["epoch"], 3)
        other = self.ids(result)[1]
        before = E.catalog_path(root).read_bytes()
        with self.assertRaises(E.CatalogError) as ctx:
            E.run_write(root, lambda c: E.op_link(c, other, "try-1"))
        self.assertEqual(ctx.exception.code, "run-linked-elsewhere")
        self.assertEqual(E.catalog_path(root).read_bytes(), before)
        self.write_run(root, "run-rows", rows=[
            {"row_id": "arm-1", "started_at": NOW - 500, "metric_summary": {"metric": "wer", "latest": {"epoch": 7, "value": 22.2, "nonfinite": False},
                                                                            "latest_metrics": {"wer": {"epoch": 7, "value": 22.2, "nonfinite": False}}},
             "last_checkpoint": {"at": NOW - 60}, "eta": 600.0}])
        E.run_write(root, lambda c: E.op_link(c, "다른 조건", "run-rows", "arm-1"))
        row = self.cond(E.status(root, now=NOW), "다른 조건")
        self.assertEqual(row["latest"]["value"], 22.2)  # the linked row, not the run as a whole
        self.assertEqual(E.describe_run(E.load(root).doc, "run-rows", "arm-1")["name"], "다른 조건")
        E.run_write(root, lambda c: E.op_link(c, "다른 조건", "run-rows", "arm-9"))
        self.assertIn("row-missing", self.cond(E.status(root, now=NOW), "다른 조건")["anomalies"])


class ReviewFindings(Base):
    """The three paths the PR #1596 review reproduced; each failed before its fix."""

    def control_in_another_group(self, root, control_metric):
        spec = {"groups": [group_spec(display_name="실험 묶음",
                                      metric={**METRIC, "scoring": "oracle"},
                                      target={"kind": "condition", "condition": "대조"}),
                           {"display_name": "대조 묶음", "goal": "대조", "metric": control_metric}],
                "conditions": [{"display_name": "대조", "group": "대조 묶음", "refs": {"runs": [{"run_id": "run-ctl"}]}},
                               {"display_name": "실험", "group": "실험 묶음", "refs": {"runs": [{"run_id": "run-exp"}]}}]}
        self.register(root, spec)

    def test_t15_a_control_declared_with_another_unit_split_or_scoring_is_not_subtracted(self):
        same = {**METRIC, "scoring": "oracle"}
        cases = (("fraction", {**same, "unit": "fraction"}, "unit-mismatch"),
                 ("test", {**same, "split": "test"}, "split-mismatch"),
                 ("system", {**same, "scoring": "system"}, "scoring-mismatch"),
                 ("max", {**same, "direction": "max"}, "metric-mismatch"),
                 ("unscored", {**same, "scoring": None}, "split-or-scoring-unverified"),
                 ("same", dict(same), None))
        for index, (label, control_metric, reason) in enumerate(cases):
            with self.subTest(label):
                root = self.make_root(label, f"t15-{index}")
                self.control_in_another_group(root, control_metric)
                self.write_run(root, "run-ctl", series={"wer": [(12, 0.2 if label == "fraction" else 20.0)]})
                self.write_run(root, "run-exp", series={"wer": [(12, 20.0 if label == "fraction" else 20.5)]})
                report = E.status(root, now=NOW)
                comparison = self.cond(report, "실험")["comparison"]
                if reason is None:  # the same declaration in another group still compares
                    self.assertEqual(comparison["state"], "ok")
                    self.assertAlmostEqual(comparison["delta"], 0.5)
                    continue
                self.assertEqual((comparison["state"], comparison["reason"]), ("not-comparable", reason))
                self.assertIsNone(comparison["delta"])
                self.assertIsNone(comparison["relative_percent"])
                self.assertEqual((comparison["value"]["value"], comparison["baseline"]["value"]),
                                 (20.0 if label == "fraction" else 20.5, 0.2 if label == "fraction" else 20.0))
                text = E.render_status(report, "ko")
                self.assertIn("비교 불가", text)
                self.assertNotIn("%p", text)
                self.assertNotIn("0.2%", text)  # a fraction is never retold as a percent
        loose = self.make_root("loose", "t15-loose")  # a control in no group has no declaration to check
        self.register(loose, {"groups": [group_spec(target={"kind": "condition", "condition": "그룹 없는 대조"})],
                              "conditions": [{"display_name": "그룹 없는 대조", "refs": {"runs": [{"run_id": "run-ctl"}]}},
                                             {"display_name": "실험", "group": "AMI 가변 화자 개선",
                                              "refs": {"runs": [{"run_id": "run-exp"}]}}]})
        self.write_run(loose, "run-ctl", series={"wer": [(12, 20.0)]})
        self.write_run(loose, "run-exp", series={"wer": [(12, 20.5)]})
        comparison = self.cond(E.status(loose, now=NOW), "실험")["comparison"]
        self.assertEqual((comparison["state"], comparison["reason"]), ("not-comparable", "split-or-scoring-unverified"))
        self.assertIn("unit-mismatch", json.dumps(E.compare(
            group_spec(target={"kind": "condition", "condition": "cnd_000000000000"}),
            {"key": "wer", "epoch": 12, "value": 20.0, "nonfinite": False},
            {"key": "wer", "epoch": 12, "value": 0.2, "nonfinite": False}, {**METRIC, "unit": "fraction"})))

    def test_t16_a_former_name_or_run_id_another_condition_answers_to_picks_neither(self):
        root = self.make_root()
        ids = self.ids(self.register(root, {"conditions": [
            {"display_name": "old", "refs": {"runs": [{"run_id": "run-a"}]}}, {"display_name": "second"}]}))
        cond_a, cond_b = ids
        E.run_write(root, lambda c: E.op_rename(c, "old", "new"), now=NOW + 1)
        renamed = E.run_write(root, lambda c: E.op_rename(c, "second", "old"), now=NOW + 2)
        self.assertEqual(renamed["status"], "applied")  # the rename itself is not blocked ...
        overlap = [w for w in renamed["warnings"] if w["code"] == "name-overlap"]
        self.assertEqual(len(overlap), 1, renamed["warnings"])  # ... but lint names both places
        self.assertIn(f"conditions[{cond_a}].former_names", overlap[0]["where"])
        self.assertIn(f"conditions[{cond_b}].display_name", overlap[0]["where"])
        doc = E.load(root).doc
        self.assertEqual(E.resolve_ex(doc, "old"), (None, "ambiguous"))
        before = E.catalog_path(root).read_bytes()
        with self.assertRaises(E.CatalogError) as ctx:
            E.run_write(root, lambda c: E.op_link(c, "old", "late-run"))
        self.assertEqual(ctx.exception.code, "condition-ambiguous")
        self.assertIn(cond_a, ctx.exception.detail)
        self.assertIn(cond_b, ctx.exception.detail)
        self.assertEqual(E.catalog_path(root).read_bytes(), before)
        done = subprocess.run([sys.executable, str(SCRIPT), "link", "old", "--run", "late-run", "--artifact-root",
                               str(root)], capture_output=True, text=True, timeout=60, env=clean_env(self.home))
        self.assertEqual(json.loads(done.stdout)["error"], "condition-ambiguous")
        self.assertEqual(E.catalog_path(root).read_bytes(), before)
        E.run_write(root, lambda c: E.op_link(c, cond_b, "late-run"), now=NOW + 3)  # the id always works
        self.assertEqual(E.describe_run(E.load(root).doc, "late-run")["condition_id"], cond_b)
        # a current name equal to another condition's run id: resolve and describe_run agree on "neither"
        E.run_write(root, lambda c: E.op_rename(c, cond_b, "run-a"), now=NOW + 4)
        doc = E.load(root).doc
        self.assertEqual(E.resolve_ex(doc, "run-a"), (None, "ambiguous"))
        self.assertIsNone(E.describe_run(doc, "run-a"))
        found = [w for w in E.lint(doc, expect=["run-a"]) if w["code"] == "name-overlap"]
        self.assertTrue(any(f"conditions[{cond_a}].refs.runs" in w["where"] and
                            f"conditions[{cond_b}].display_name" in w["where"] for w in found), found)
        self.assertIn("expect[0]", [w["where"] for w in found])  # ambiguous is not reported as missing
        self.assertEqual(E.resolve(doc, "new")["id"], cond_a)
        self.assertEqual(E.resolve(doc, cond_a)["id"], cond_a)

    def test_t17_an_ended_row_is_not_shown_as_its_running_parent(self):
        root = self.make_root()
        self.register(root, {"groups": [group_spec()], "conditions": [
            {"display_name": "끝난 행", "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": "run-rows", "row_id": "arm-1"}]}},
            {"display_name": "진행 중 행", "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": "run-rows", "row_id": "arm-2"}]}},
            {"display_name": "함께 끝난 행", "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": "run-done", "row_id": "only"}]}}]})
        summary = {"metric": "wer", "latest": {"epoch": 7, "value": 22.2, "nonfinite": False},
                   "latest_metrics": {"wer": {"epoch": 7, "value": 22.2, "nonfinite": False}}}
        self.write_run(root, "run-rows", status="running", at=NOW - 30, eta=1800.0, rows=[
            {"row_id": "arm-1", "started_at": NOW - 4000, "ended_at": NOW - 100, "metric_summary": summary,
             "last_checkpoint": {"at": NOW - 100}, "eta": 600.0},
            {"row_id": "arm-2", "started_at": NOW - 90, "metric_summary": summary,
             "last_checkpoint": {"at": NOW - 60}, "eta": 600.0}])
        self.write_run(root, "run-done", status="done", ended=NOW - 50, rows=[
            {"row_id": "only", "started_at": NOW - 3600, "ended_at": NOW - 50, "metric_summary": summary,
             "last_checkpoint": {"at": NOW - 50}, "eta": 0.0}])
        report = E.status(root, now=NOW)
        ended = self.cond(report, "끝난 행")
        self.assertIsNone(ended["run_status"])
        self.assertIsNone(ended["current_run"]["status"])
        self.assertEqual(ended["unknown"]["run_status"], "row-ended")
        self.assertIsNone(ended["expected_at"])
        self.assertEqual(ended["unknown"]["expected_at"], "not-running")
        self.assertEqual(ended["current_run"]["started_at"], NOW - 4000)
        self.assertIsNone(ended["progress"])  # the parent's 12/40 is not this row's progress
        self.assertEqual(ended["observed_age_seconds"], 100)
        self.assertEqual(ended["latest"]["value"], 22.2)
        self.assertEqual(ended["anomalies"], [])
        live = self.cond(report, "진행 중 행")
        self.assertEqual(live["run_status"], "running")
        self.assertEqual(live["expected_at"], E._rfc3339(NOW - 60 + 600))  # the row's own ETA, not the run's
        self.assertIsNone(live["progress"])
        together = self.cond(report, "함께 끝난 행")
        self.assertEqual(together["run_status"], "done")  # the row's end is the run's end
        self.assertNotIn("run_status", together["unknown"])
        self.assertNotIn("끝난 행", [w["display_name"] for w in report["waiting"]])
        line = next(line for line in E.render_status(report, "ko").splitlines() if line.startswith("- 끝난 행"))
        self.assertNotIn("예상", line)
        # RV-PR1596-R2: the text says in words what state each row is in
        self.assertIn("행 종료, 결과 기록 없음", line)
        live_line = next(line for line in E.render_status(report, "ko").splitlines() if line.startswith("- 진행 중 행"))
        self.assertIn("실행 중", live_line)
        self.assertNotIn("실행 중", next(line for line in E.render_status(report, "ko").splitlines()
                                       if line.startswith("- 함께 끝난 행")))


class Text(Base):
    def test_t13_the_text_names_the_goal_the_condition_and_the_reason(self):
        root = self.make_root()
        self.two_conditions(root)
        self.write_run(root, "run-a")
        self.write_run(root, "mpwav-20261111-unregistered")
        text = E.render_status(E.status(root, now=NOW), "ko")
        self.assertIn("목표: AMI Dev에서 var WER이 fix 19.99%보다 낮은가", text)
        self.assertIn("0.3초 이하 짧은 발화 유지", text)
        self.assertIn("%p", text)
        self.assertIn("fix 19.99% 대비", text)
        self.assertIn("(상대 +", text)
        self.assertIn("짧은 발화 제외 — 최근 값 아직 없음", text)
        self.assertIn("이름 없는 실행 1건: mpwav-20261111-unregistered", text)
        self.assertNotIn("run-a", text)  # a registered run is called by its name
        self.assertIn("Experiments", E.render_status(E.status(root, now=NOW), "en"))  # English exists too
        bad = self.make_root("bad", "c")
        self.register(bad, {"groups": [group_spec(target={**FIXED, "scoring": None, "split": None})],
                            "conditions": [{"display_name": "평가 조건 미확인", "group": "AMI 가변 화자 개선",
                                            "refs": {"runs": [{"run_id": "bad-run"}]}}]})
        self.write_run(bad, "bad-run")
        unverified = E.render_status(E.status(bad, now=NOW), "ko")
        self.assertIn("비교 불가: 비교 기준의 평가 조건이 확인되지 않음", unverified)
        self.assertNotIn("%p", unverified)


class Scan(Base):
    """`scan_text`: internal names said without their catalog name. Advice only."""

    def setup_root(self):
        root = self.make_root()
        self.register(root, {"groups": [group_spec()], "conditions": [
            {"display_name": "잔향 줄임", "group": "AMI 가변 화자 개선", "aliases": ["E", "rev-a"],
             "refs": {"runs": [{"run_id": "run-1", "row_id": "arm-1"}]}},
            {"display_name": "잡음 줄임", "group": "AMI 가변 화자 개선", "aliases": ["noise-b"],
             "refs": {"runs": [{"run_id": "run-2"}]}}]})
        E.run_write(root, lambda ctx: E.op_rename(ctx, "잡음 줄임", "잡음 섞기"), now=NOW + 1)   # the old name is now a former name
        return root, E.load(root).doc

    def kinds(self, text, doc, **kw):
        return [(row["token"], row["kind"], row["name"], row["line"], row["col"]) for row in E.scan_text(text, doc, **kw)]

    def test_a_run_id_an_alias_and_a_former_name_are_found_with_their_place_and_name(self):
        _root, doc = self.setup_root()
        text = "먼저 run-1:arm-1 을 보고,\n그다음 rev-a 와 잡음 줄임, 마지막으로 run-2 를 본다."
        self.assertEqual(self.kinds(text, doc), [
            ("run-1:arm-1", "run-id", "잔향 줄임", 1, 4),
            ("rev-a", "alias", "잔향 줄임", 2, 5),
            ("잡음 줄임", "former-name", "잡음 섞기", 2, 13),
            ("run-2", "run-id", "잡음 섞기", 2, 26)])

    def test_the_catalog_name_in_the_same_text_silences_that_conditions_tokens_only(self):
        _root, doc = self.setup_root()
        self.assertEqual(self.kinds("잔향 줄임(run-1)과 run-2", doc), [("run-2", "run-id", "잡음 섞기", 1, 15)])
        self.assertEqual(self.kinds("잔향 줄임(run-1)과 잡음 섞기(run-2)", doc), [])

    def test_a_one_letter_alias_is_too_common_to_judge_and_a_token_counts_only_whole(self):
        _root, doc = self.setup_root()
        self.assertEqual(self.kinds("E는 좋고 E1과 base-E 와 Eagle 은 아니다", doc), [])   # `E` is registered, yet only a letter
        self.assertEqual(self.kinds("A와 B와 F는 정본에 없다", doc), [])        # nothing is guessed for unregistered letters
        self.assertEqual([row[0] for row in self.kinds("rev-ab 와 xrev-a 는 다르고 (rev-a)는 같다", doc)], ["rev-a"])

    # --- a wrong warning is worse than a missed one (review RV-PR1608-1113, finding 2) ---------------------

    def common_alias_root(self):
        """The review's catalog: one condition answering to `base`, `v2`, `0.3`, `E`, `oldname`, and `retain030`."""
        root = self.make_root("common")
        self.register(root, {"groups": [{"display_name": "소리 실험", "goal": "기준보다 낮은가"}], "conditions": [
            {"display_name": "잔향 감소", "group": "소리 실험", "aliases": ["E", "v2", "base", "0.3", "oldname", "retain030"],
             "refs": {"runs": [{"run_id": "run-rev-1", "row_id": "arm-1"}]}}]})
        E.run_write(root, lambda ctx: E.op_rename(ctx, "잔향 감소", "잔향 더 감소"), now=NOW + 1)   # `잔향 감소` is a former name now
        return E.load(root).doc

    def test_the_reviews_seven_ordinary_phrases_get_no_warning(self):
        doc = self.common_alias_root()
        for text in ("base.py 파일을 연다", "/tmp/base/output", "https://example.org/base", "`base = 1`",
                     "git rebase base", "버전 v2를 설치한다", "오차는 0.3이다"):
            with self.subTest(text=text):
                self.assertEqual(E.scan_text(text, doc), [])

    def test_a_clear_name_is_quiet_inside_code_quotes_paths_files_commands_and_options(self):
        doc = self.common_alias_root()
        for text in ("```\nrun-rev-1\n```", "    run-rev-1 = 1", "\trun-rev-1", "> run-rev-1 로 가자",
                     '"run-rev-1은 바꾸지 마"', "“run-rev-1은 그대로”", "`run-rev-1`", "/tmp/run-rev-1/log",
                     r"C:\work\run-rev-1\out", "run-rev-1.log 를 본다", "./run-rev-1", "https://x.example/run-rev-1",
                     "$ python3 train.py run-rev-1", "그다음 git log run-rev-1 를 본다", "--run run-rev-1", "run=run-rev-1",
                     "run-rev-1=좋음", "@run-rev-1", "#run-rev-1", "오차 0.3 과 1e-3, 2026-11-13, v2.1, E 와 base"):
            with self.subTest(text=text):
                self.assertEqual(E.scan_text(text, doc), [])

    def test_a_real_run_id_a_former_name_and_an_identifier_alias_still_warn(self):
        doc = self.common_alias_root()
        self.assertEqual(self.kinds("run-rev-1은 진행 중이고 잔향 감소는 예전 이름이다.\n(retain030)을 확인", doc), [
            ("run-rev-1", "run-id", "잔향 더 감소", 1, 1),
            ("잔향 감소", "former-name", "잔향 더 감소", 1, 19),
            ("retain030", "alias", "잔향 더 감소", 2, 2)])
        self.assertEqual([row[0] for row in self.kinds("- run-rev-1:arm-1 결과\n    - run-rev-1 다시", doc)],
                         ["run-rev-1:arm-1", "run-rev-1"])                 # a nested list item is prose, not code
        self.assertEqual([row[0] for row in self.kinds("run-rev-1. 끝", doc)], ["run-rev-1"])   # a full stop is not a file name
        self.assertEqual(self.kinds("예전 이름 oldname을 정리", doc), [])     # a plain-word alias is not clear enough

    # --- one lookup table, not one catalog walk per token (review RV-PR1608-1113, finding 1) --------------

    def big_doc(self, n, parent="a"):
        root = self.make_root(f"big{n}", parent)
        self.register(root, {"groups": [{"display_name": "규모 시험"}], "conditions": [
            {"display_name": f"조건 {i}", "group": "규모 시험", "aliases": [f"alias{i}x{j}" for j in range(4)],
             "refs": {"runs": [{"run_id": f"run-{i}-a"}]}} for i in range(n)]})
        return E.load(root).doc

    def test_fields_share_one_table_and_the_lookups_do_not_grow_with_the_catalog(self):
        from unittest import mock
        fields = [("understanding", "일반적인 질문입니다"), ("q", "어떤 작업을 할까요"), ("l0", "그대로 진행"),
                  ("m0", "추가 변경 없이 진행"), ("l1", "나중에 진행"), ("m1", "run-3-a 와 alias7x2 는 대기")]
        counts = {}
        for n in (32, 256):
            doc = self.big_doc(n)
            calls = {"index": 0, "claims": 0, "resolve": 0, "fold": 0}
            real_index, real_claims, real_resolve, real_fold = E.scan_index, E._claims, E.resolve_ex, E._fold

            def counted(key, real):
                def call(*args, **kwargs):
                    calls[key] += 1
                    return real(*args, **kwargs)
                return call
            with mock.patch.object(E, "scan_index", counted("index", real_index)), \
                    mock.patch.object(E, "_claims", counted("claims", real_claims)), \
                    mock.patch.object(E, "resolve_ex", counted("resolve", real_resolve)):
                hits = E.scan_fields(fields, doc)
                index = real_index(doc)
                with mock.patch.object(E, "_fold", counted("fold", real_fold)):
                    self.assertEqual(E.scan_fields(fields, doc, index=index), hits)
            self.assertEqual([(h["field"], h["token"]) for h in hits], [("m1", "run-3-a"), ("m1", "alias7x2")])
            self.assertEqual((calls["index"], calls["claims"], calls["resolve"]), (1, 0, 0))   # one table per question
            counts[n] = calls["fold"]
        self.assertEqual(counts[32], counts[256])                         # scanning texts never walks the catalog
        self.assertEqual(counts[256], 1)                                  # one fold of the question for the names
        named = E.scan_fields([("understanding", "조건 3 을 다룬다"), ("m1", "run-3-a 와 alias7x2")], self.big_doc(32, "again"))
        self.assertEqual([h["token"] for h in named], ["alias7x2"])        # a name in one field counts for all

    def test_unlinked_runs_are_reported_with_no_name_and_the_longest_token_wins_an_overlap(self):
        _root, doc = self.setup_root()
        self.assertEqual(self.kinds("mpwav-20261111-stray 도 보인다", doc, unlinked=["mpwav-20261111-stray"]),
                         [("mpwav-20261111-stray", "unregistered-run", None, 1, 1)])
        self.assertEqual(len(E.scan_text("run-1:arm-1", doc)), 1)                  # not `run-1` and `run-1:arm-1`

    def test_a_token_two_conditions_answer_to_is_left_alone_and_nothing_raises(self):
        _root, doc = self.setup_root()
        doc["conditions"][1]["aliases"].append("rev-a")                            # now ambiguous
        self.assertEqual([row[0] for row in self.kinds("rev-a run-2", doc)], ["run-2"])
        self.assertEqual(E.scan_text(None, doc), [])
        self.assertEqual(E.scan_text("run-1", None), [])
        self.assertEqual(E.scan_text("run-1", {"conditions": "broken"}), [])

    def test_the_command_reads_stdin_and_a_file_and_is_exit_zero_even_for_a_broken_catalog(self):
        root, _doc = self.setup_root()
        run = lambda *args, text="": subprocess.run([sys.executable, str(SCRIPT), "scan", "--artifact-root", str(root), *args],  # noqa: E731
                                                   input=text, capture_output=True, text=True, env=clean_env(self.home))
        result = run(text="rev-a 와 mpwav-20261111-stray")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["catalog"]["status"], "ok")
        self.assertEqual([w["token"] for w in report["warnings"]], ["rev-a"])     # the unlinked run needs a run record to be known
        self.write_run(root, "mpwav-20261111-stray")
        report = json.loads(run(text="rev-a 와 mpwav-20261111-stray").stdout)
        self.assertEqual([(w["token"], w["kind"]) for w in report["warnings"]], [("rev-a", "alias"), ("mpwav-20261111-stray", "unregistered-run")])
        path = self.base / "q.txt"
        path.write_text("잔향 줄임 과 run-2\n", encoding="utf-8")
        self.assertEqual([w["token"] for w in json.loads(run("--file", str(path)).stdout)["warnings"]], ["run-2"])
        bad = self.make_root("bad", "y")
        target = E.catalog_path(bad); target.parent.mkdir(parents=True, exist_ok=True); target.write_text("{no", encoding="utf-8")
        broken = subprocess.run([sys.executable, str(SCRIPT), "scan", "--artifact-root", str(bad)], input="run-1",
                                capture_output=True, text=True, env=clean_env(self.home))
        self.assertEqual(broken.returncode, 0)
        self.assertEqual(json.loads(broken.stdout)["warnings"], [])
        self.assertEqual(run("--file", str(self.base / "missing.txt")).returncode, 0)


class BaselineUnit(Base):
    def test_unit_mismatch_gives_a_raw_baseline_and_no_delta(self):
        """RV-PR1596-R2: a baseline measured in another unit is a number, not a value in this group's unit."""
        group = {"metric": {"key": "si_sdr", "unit": "dB", "direction": "max", "split": "validation", "scoring": "s"},
                 "target": {"kind": "condition", "condition": "cnd_x"}}
        control = {"key": "si_sdr", "unit": "%", "direction": "max", "split": "validation", "scoring": "s"}
        point = {"key": "si_sdr", "epoch": 3, "value": 9.0}
        result = E.compare(group, point, {"key": "si_sdr", "epoch": 3, "value": 3.0}, control)
        self.assertEqual((result["state"], result["reason"]), ("not-comparable", "unit-mismatch"))
        self.assertIsNone(result["delta"])
        self.assertEqual(result["baseline"]["value"], 3.0)
        self.assertNotIn("unit", result["baseline"])


class NoLeftovers(Base):
    def test_zz_no_child_process_is_left(self):
        with self.assertRaises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)


if __name__ == "__main__":
    unittest.main()
