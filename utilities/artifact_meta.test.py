#!/usr/bin/env python3
"""Tests for `artifact_meta.py`: the public meta.json / project-meta.json contracts, IDs, provenance,
vocabulary, the single write path (intent + replay), read-only paths, and the one command.

Real activate/begin/close/finalize fixtures come from `artifact_producer.test.py`; they are built once
into a template root and copied per test.  The model is never called and no real artifact root is read
or written.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_history as H  # noqa: E402
import artifact_meta as M  # noqa: E402
import artifact_producer as P  # noqa: E402
from split_module_fixture import patch_split  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "artifact_meta_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
fixture = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixture)

PAST = 1_700_000_000.0
NOW = 1_790_000_000.0
CORE_DOC = Path(__file__).resolve().parents[1] / "core" / "ARTIFACT_META.md"
STATE = M.STATE_REL
PROJECT = M.PROJECT_REL


class _Builder(fixture.ProducerTestBase):
    def runTest(self):  # pragma: no cover -- never run, only used for its fixtures
        pass


_TEMPLATE = {}


def template():
    """One activated root with three campaigns (alpha: 3 cycles, beta: 2, gamma: 1), built once."""
    if _TEMPLATE:
        return _TEMPLATE
    attempt = os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID", None)
    builder = _Builder()
    builder.setUp()
    try:
        builder.activate()
        out = {}
        index = 0
        for name, count in (("alpha", 3), ("beta", 2), ("gamma", 1)):
            cycles = []
            for number in range(count):
                index += 1
                route, route_file = builder.route(slug=f"{name}{number}", campaign_key=name)
                begun = P.begin(builder.root, route_file=route_file, capability="autopilot-code",
                                intensity="direct", campaign_key=name, now=PAST + index)
                builder.write_output(begun, "reports/final_report.md", f"# {name} {number}\n".encode())
                builder.close(route, route_file)
                P.finalize(builder.root, cycle_id=begun["cycle_id"], primary="reports/final_report.md",
                           now=PAST + index)
                cycles.append(begun["cycle_id"])
            out[name] = {"campaign_id": begun["campaign_id"], "cycles": cycles}
        keep = tempfile.mkdtemp(prefix="artifact-meta-template-")
        shutil.copytree(builder.root, Path(keep) / "root")
        _TEMPLATE.update(path=Path(keep) / "root", campaigns=out)
    finally:
        builder._restore()
        if attempt is not None:
            os.environ["AGENT_DISPATCH_ATTEMPT_ID"] = attempt
    return _TEMPLATE


def tearDownModule():
    if _TEMPLATE:
        shutil.rmtree(_TEMPLATE["path"].parent, ignore_errors=True)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def files_of(root):
    """rel path -> bytes digest of every regular file (directories and mtimes are not part of it)."""
    out = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(root).as_posix()] = sha(path.read_bytes())
    return out


def strict_snapshot(root):
    """Every file AND directory with size, mtime and bytes: what a read-only command must not change."""
    rows = []
    for path in sorted(Path(root).rglob("*")):
        meta = path.lstat()
        digest = sha(path.read_bytes()) if path.is_file() and not path.is_symlink() else ""
        rows.append((path.relative_to(root).as_posix(), meta.st_size, meta.st_mtime_ns, digest))
    return rows


class MetaBase(unittest.TestCase):
    def setUp(self):
        tpl = template()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifact-root"
        shutil.copytree(tpl["path"], self.root)
        self.A, self.B, self.G = (tpl["campaigns"][n] for n in ("alpha", "beta", "gamma"))
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in (M.TITLE_DISABLE_ENV, "AGENT_DISPATCH_ATTEMPT_ID"):
            os.environ.pop(key, None)

    # -- helpers --------------------------------------------------------
    def write(self, mutate, **kw):
        kw.setdefault("now", NOW)
        return M.run_write(self.root, mutate, **kw)

    def set(self, campaign, cycle=None, by="human", **values):
        return self.write(lambda ws: M.op_set(ws, campaign["campaign_id"] if isinstance(campaign, dict) else campaign,
                                              cycle, values), actor_by=by)

    def vocab(self, *codes, by="human"):
        def mutate(ws):
            for code in codes:
                M.op_branch_add(ws, code, f"{code} 갈래", f"{code} 설명")
        return self.write(mutate, actor_by=by)

    def judge(self, campaign, *, title="모델 제목", summary="모델 요약", branches=("CMD",), kinds=("평가",),
              cycles=None, new_branches=(), **kw):
        entity = {"title": title, "summary": summary, "branches": list(branches), "kinds": list(kinds)}
        cycles = campaign["cycles"] if cycles is None else cycles
        kw.setdefault("now", NOW)
        return M.apply_judgement(self.root, campaign["campaign_id"], campaign=dict(entity),
                                 cycles={cid: dict(entity, title=f"{title} {i}") for i, cid in enumerate(cycles)},
                                 new_branches=new_branches, **kw)

    def meta(self, campaign):
        read = M.read_campaign_meta(self.root, campaign["campaign_id"])
        self.assertEqual(read.status, "ok", read.code)
        return read.doc

    def raw_meta_path(self, campaign):
        return self.root / M.read_campaign_meta(self.root, campaign["campaign_id"]).rel

    def project(self):
        return json.loads((self.root / PROJECT).read_text(encoding="utf-8"))

    def state(self):
        return json.loads((self.root / STATE).read_text(encoding="utf-8"))

    def events(self):
        return list(H.iter_events(self.root))

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = M.main([*argv])
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, out.getvalue())
        return code, json.loads(lines[0])


# ---------------------------------------------------------------------------
# the public contract
# ---------------------------------------------------------------------------


class PublicContractTest(MetaBase):
    def test_both_files_carry_exactly_the_published_shape(self):
        self.vocab("CMD", "TTS")
        self.set(self.A, title="제목", summary="요약", branches=["CMD", "TTS"], kinds=["학습"])
        self.set(self.A, self.A["cycles"][0], title="사이클 제목")
        doc = json.loads(self.raw_meta_path(self.A).read_text(encoding="utf-8"))
        self.assertEqual(list(doc)[:5], ["schema_version", "contract", "artifact_root_id", "campaign_id", "campaign"])
        self.assertEqual((doc["schema_version"], doc["contract"]), (1, "artifact-meta/v1"))
        self.assertEqual((doc["artifact_root_id"], doc["campaign_id"]), (fixture.ROOT_ID, self.A["campaign_id"]))
        campaign = doc["campaign"]
        self.assertEqual(list(campaign), ["short_id", "title", "summary", "branches", "kinds", "source"])
        self.assertEqual(set(campaign["source"]), {"short_id", "title", "summary", "branches", "kinds"})
        cycle = doc["cycles"][self.A["cycles"][0]]
        self.assertEqual(cycle["short_id"], "CMD-01.1")
        project = self.project()
        self.assertEqual(list(project), ["schema_version", "contract", "artifact_root_id", "display_name", "branches"])
        self.assertEqual((project["schema_version"], project["contract"]), (1, "artifact-project-meta/v1"))
        self.assertEqual([set(item) for item in project["branches"]], [{"code", "label", "note"}] * 2)
        # kinds, sources and counters never leak into the public project file or the meta file
        text = (self.root / PROJECT).read_text(encoding="utf-8")
        for word in ("kinds", "source", "high_water", "issued", "project_source"):
            self.assertNotIn(word, text)
        self.assertNotIn("high_water", self.raw_meta_path(self.A).read_text(encoding="utf-8"))
        self.assertTrue(self.raw_meta_path(self.A).read_bytes().endswith(b"\n"))
        self.assertEqual(self.raw_meta_path(self.A).relative_to(self.root).parts[0], "campaigns")
        self.assertEqual(self.raw_meta_path(self.A).name, "meta.json")

    def test_internal_state_is_a_separate_internal_file(self):
        self.vocab("CMD")
        self.set(self.A, branches=["CMD"])
        state = self.state()
        self.assertEqual(state["contract"], "artifact-meta-state/v1")
        self.assertEqual(state["branch_high_water"], {"CMD": 1})
        self.assertEqual(state["issued"]["CMD-01"], {"kind": "campaign", "id": self.A["campaign_id"]})
        self.assertEqual(state["project_source"]["branches.CMD"]["by"], "human")
        self.assertTrue(STATE.startswith(".runtime/artifact-producer/v1/"))

    def test_limits_at_and_over_their_boundary(self):
        M.check_title("가" * 120)
        M.check_summary("가" * 400)
        M.check_summary("")  # an empty summary is allowed, an empty title is not
        for bad in ("", "가" * 121):
            with self.assertRaises(M.MetaError):
                M.check_title(bad)
        with self.assertRaises(M.MetaError):
            M.check_summary("가" * 401)
        codes = ["".join(chr(65 + i // 26) + chr(65 + i % 26) for i in [n]) for n in range(17)]
        self.assertEqual(len(M.check_branches(codes[:16])), 16)
        with self.assertRaises(M.MetaError):
            M.check_branches(codes)
        aliases = [f"CMD-{n:02d}" for n in range(1, 18)]
        self.assertEqual(len(M.check_aliases(aliases[:16], cycle=False)), 16)
        with self.assertRaises(M.MetaError):
            M.check_aliases(aliases, cycle=False)
        self.assertEqual(M.check_kinds(list(M.KINDS)), list(M.KINDS))
        with self.assertRaises(M.MetaError):
            M.check_kinds(list(M.KINDS) * 3)  # more than 16 entries
        with self.assertRaises(M.MetaError):
            M.check_kinds(["학습", "학습"])
        with self.assertRaises(M.MetaError):
            M.check_kinds(["미지원"])

    def test_branch_codes_are_two_to_five_uppercase_ascii_letters(self):
        for good in ("CM", "CMD", "WAKE", "DEMOS", "ETC"):
            self.assertEqual(M.check_code(good), good)
        for bad in ("C", "ABCDEF", "cmd", "Cmd", "CM1", "가나", "ＣＭＤ", "", None, 5):
            with self.assertRaises(M.MetaError):
                M.check_code(bad)

    def test_texts_must_be_nfc_trimmed_one_line_without_control_characters(self):
        decomposed = unicodedata.normalize("NFD", "한글 제목")
        self.assertNotEqual(decomposed, unicodedata.normalize("NFC", decomposed))
        for bad in (decomposed, " 앞공백", "뒤공백 ", "두 줄\n입니다", "탭\t입니다", "널\x00문자", " 줄", 5, None):
            with self.subTest(bad=repr(bad)), self.assertRaises(M.MetaError):
                M.check_title(bad)
        self.assertEqual(M.check_title("한글 제목 (V8)"), "한글 제목 (V8)")

    def test_writers_refuse_what_the_contract_forbids_and_write_nothing(self):
        self.vocab("CMD")
        before = files_of(self.root)
        for values in ({"title": "가" * 121}, {"summary": "가" * 401}, {"title": ""}, {"kinds": ["없는성격"]},
                       {"branches": ["NOPE"]}, {"branches": ["cmd"]}, {"title": decompose("한글")}):
            with self.subTest(values=values), self.assertRaises(M.MetaError):
                self.set(self.A, **values)
        self.assertEqual(files_of(self.root), before)

    def test_reader_checks_known_fields_and_ignores_unknown_ones(self):
        self.vocab("CMD")
        self.set(self.A, title="제목", branches=["CMD"])
        path = self.raw_meta_path(self.A)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["groups"] = [{"future": True}]
        doc["campaign"]["future_field"] = {"x": 1}
        doc["campaign"]["source"]["future_source"] = {"by": "alien"}
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        read = M.read_campaign_meta(self.root, self.A["campaign_id"])
        self.assertEqual(read.status, "ok")
        # a writer keeps what it does not know
        self.set(self.A, summary="요약")
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after["groups"], [{"future": True}])
        self.assertEqual(after["campaign"]["future_field"], {"x": 1})
        self.assertEqual(after["campaign"]["source"]["future_source"], {"by": "alien"})
        self.assertEqual(after["campaign"]["summary"], "요약")

    def test_a_violating_campaign_is_skipped_alone_and_others_keep_working(self):
        self.vocab("CMD")
        self.set(self.A, title="알파", branches=["CMD"])
        self.set(self.B, title="베타", branches=["CMD"])
        path = self.raw_meta_path(self.A)
        good = json.loads(path.read_text(encoding="utf-8"))
        cases = {
            "contract-unknown": dict(good, contract="artifact-meta/v2"),
            "identity-mismatch": dict(good, campaign_id="camp_" + "9" * 32),
            "identity-mismatch-root": dict(good, artifact_root_id="root_" + "9" * 32),
            "title-invalid": dict(good, campaign=dict(good["campaign"], title="가" * 121)),
            "branch-code-invalid": dict(good, campaign=dict(good["campaign"], branches=["cmd"])),
            "cycle-foreign": dict(good, cycles={self.B["cycles"][0]: {"title": "남의 사이클"}}),
        }
        for label, doc in cases.items():
            with self.subTest(label):
                path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
                read = M.read_campaign_meta(self.root, self.A["campaign_id"])
                self.assertEqual(read.status, "invalid", label)
                self.assertEqual(read.code, label.replace("-root", ""))
                self.assertEqual(M.read_campaign_meta(self.root, self.B["campaign_id"]).status, "ok")
                self.assertEqual(M.effective_title(self.root, self.B["campaign_id"]), "베타")
        path.write_text(json.dumps(cases["title-invalid"], ensure_ascii=False), encoding="utf-8")
        broken = path.read_bytes()
        with self.assertRaises(M.MetaError):  # an unreadable meta is never corrected by overwriting it
            self.set(self.A, title="덮어쓰기")
        self.assertEqual(path.read_bytes(), broken)
        self.set(self.B, title="베타 수정")

    def test_duplicate_keys_symlinks_and_garbage_are_rejected_not_repaired(self):
        self.vocab("CMD")
        self.set(self.A, title="제목", branches=["CMD"])
        path = self.raw_meta_path(self.A)
        good = path.read_text(encoding="utf-8")
        for label, raw in (("dup", good.replace('"title": "제목",', '"title": "제목", "title": "또",', 1)),
                           ("garbage", "{not json"), ("array", "[]")):
            path.write_text(raw, encoding="utf-8")
            self.assertEqual(M.read_campaign_meta(self.root, self.A["campaign_id"]).status, "invalid", label)
        real = path.with_name("elsewhere.json")
        real.write_text(good, encoding="utf-8")
        path.unlink()
        path.symlink_to(real)
        self.assertEqual(M.read_campaign_meta(self.root, self.A["campaign_id"]).status, "invalid")

    def test_a_moved_cycle_moves_its_entry_with_a_new_number_and_keeps_the_old_one_as_alias(self):
        self.vocab("CMD", "TTS")
        self.set(self.A, branches=["CMD"])
        self.set(self.B, branches=["TTS"])
        moving = self.A["cycles"][2]
        self.set(self.A, moving, title="옮길 사이클")
        self.set(self.B, self.B["cycles"][0], title="베타 사이클")
        self.assertEqual(self.meta(self.A)["cycles"][moving]["short_id"], "CMD-01.1")
        record_path = self.root / ".runtime/artifact-producer/v1/cycles" / f"{moving}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["campaign_id"] = self.B["campaign_id"]  # the producer's current membership changes first
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(M.read_campaign_meta(self.root, self.A["campaign_id"]).code, "cycle-foreign")
        self.set(self.B, summary="다음 쓰기에서 항목이 따라옴")
        self.assertNotIn(moving, self.meta(self.A)["cycles"])
        entry = self.meta(self.B)["cycles"][moving]
        self.assertEqual((entry["short_id"], entry["aliases"], entry["title"]), ("TTS-01.2", ["CMD-01.1"], "옮길 사이클"))
        self.assertEqual(self.state()["issued"]["CMD-01.1"], {"kind": "cycle", "id": moving})
        moves = [e for e in self.events() if e["operation"] == "move"]
        self.assertEqual([e["target"]["id"] for e in moves], [moving])
        # an entry whose cycle has no record at all is dropped with a history line
        ghost = "cyc_" + "7" * 32
        path = self.raw_meta_path(self.B)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["cycles"][ghost] = {"title": "지워진 사이클"}
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        self.set(self.B, summary="정리")
        self.assertNotIn(ghost, self.meta(self.B)["cycles"])
        self.assertIn(ghost, [e["target"]["id"] for e in self.events() if e["operation"] == "delete"])

    def test_title_priority_is_meta_then_old_declaration_then_folder(self):
        locator = path_locator(self.root, self.A)
        self.assertEqual(M.effective_title(self.root, self.A["campaign_id"], locator=locator), locator)
        write_declaration(self.root, {self.A["campaign_id"]: "옛 선언 제목"})
        self.assertEqual(M.effective_title(self.root, self.A["campaign_id"], locator=locator), "옛 선언 제목")
        self.vocab("CMD")
        self.set(self.A, title="새 메타 제목")
        self.assertEqual(M.effective_title(self.root, self.A["campaign_id"], locator=locator), "새 메타 제목")
        self.raw_meta_path(self.A).write_text("{broken", encoding="utf-8")  # an invalid meta falls back, never raises
        self.assertEqual(M.effective_title(self.root, self.A["campaign_id"], locator=locator), "옛 선언 제목")


def decompose(text):
    return unicodedata.normalize("NFD", text)


def path_locator(root, campaign):
    for directory in sorted((root / "campaigns").iterdir()):
        record = directory / "campaign.json"
        if record.is_file() and json.loads(record.read_text(encoding="utf-8")).get("campaign_id") == campaign["campaign_id"]:
            return directory.name
    raise AssertionError("no campaign directory")


def write_declaration(root, titles):
    path = root / ".runtime/artifact-producer/v1/campaign-display-titles.json"
    doc = {"schema": "hearting-campaign-display-titles/v2", "artifact_root_id": fixture.ROOT_ID,
           "entries": [{"campaign_id": cid, "campaign_locator": "x", "display_title": title,
                        "manifest_bindings": [], "manifest_revision_ids": [], "manifest_digests": []}
                       for cid, title in titles.items()]}
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# short IDs
# ---------------------------------------------------------------------------


class IdentifierTest(MetaBase):
    def test_numbers_follow_the_representative_branch_and_cycles_follow_start_order(self):
        self.vocab("CMD", "TTS")
        self.judge(self.A, branches=["CMD"])
        self.judge(self.B, branches=["CMD", "TTS"])
        self.judge(self.G, branches=["TTS"])
        a, b, g = (self.meta(c)["campaign"]["short_id"] for c in (self.A, self.B, self.G))
        self.assertEqual((a, b, g), ("CMD-01", "CMD-02", "TTS-01"))
        cycles = self.meta(self.A)["cycles"]
        self.assertEqual([cycles[c]["short_id"] for c in self.A["cycles"]], ["CMD-01.1", "CMD-01.2", "CMD-01.3"])
        self.assertEqual(self.state()["cycle_high_water"][self.A["campaign_id"]], 3)
        self.assertEqual(self.state()["branch_high_water"], {"CMD": 2, "TTS": 1})

    def test_ninety_nine_becomes_one_hundred_and_a_manual_high_number_raises_the_water(self):
        self.vocab("CMD")
        self.set(self.A, branches=["CMD"], short_id="CMD-99")
        self.set(self.B, branches=["CMD"])
        self.assertEqual(self.meta(self.B)["campaign"]["short_id"], "CMD-100")
        self.set(self.G, branches=["CMD"], short_id="CMD-250")
        self.assertEqual(self.state()["branch_high_water"]["CMD"], 250)

    def test_a_removed_branch_or_deleted_number_is_never_reissued(self):
        self.vocab("CMD", "TTS")
        self.set(self.A, branches=["CMD"])
        self.set(self.A, branches=["TTS"])  # the representative changed: a new number, the old ID is an alias
        campaign = self.meta(self.A)["campaign"]
        self.assertEqual((campaign["short_id"], campaign["aliases"]), ("TTS-01", ["CMD-01"]))
        self.write(lambda ws: M.op_branch_remove(ws, "CMD"))
        self.vocab("CMD")  # the same code comes back: its number does not
        self.set(self.B, branches=["CMD"])
        self.assertEqual(self.meta(self.B)["campaign"]["short_id"], "CMD-02")
        self.assertEqual(self.state()["issued"]["CMD-01"]["id"], self.A["campaign_id"])

    def test_an_old_id_cannot_be_given_to_another_owner_but_its_owner_may_take_it_back(self):
        self.vocab("CMD", "TTS")
        self.set(self.A, branches=["CMD"])
        self.set(self.A, branches=["TTS"])
        self.set(self.B, branches=["CMD"])
        with self.assertRaises(M.MetaError) as ctx:
            self.set(self.B, short_id="CMD-01")
        self.assertEqual(ctx.exception.code, "id-reserved")
        self.set(self.A, branches=["CMD"], short_id="CMD-01")  # back to its own alias
        campaign = self.meta(self.A)["campaign"]
        self.assertEqual((campaign["short_id"], campaign["aliases"]), ("CMD-01", ["TTS-01"]))
        self.assertEqual(campaign["source"]["short_id"]["by"], "human")

    def test_a_manual_id_must_match_the_branch_and_the_parent_prefix(self):
        self.vocab("CMD", "TTS")
        self.set(self.A, branches=["CMD"])
        self.set(self.A, self.A["cycles"][0], title="사이클")
        for kwargs, code in (({"short_id": "TTS-05"}, "short-id-branch-mismatch"), ({"short_id": "cmd-1"}, "short-id-invalid"),
                             ({"short_id": "CMD-5"}, "short-id-invalid")):
            with self.subTest(kwargs), self.assertRaises(M.MetaError) as ctx:
                self.set(self.A, **kwargs)
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(M.MetaError) as ctx:
            self.set(self.A, self.A["cycles"][0], short_id="TTS-01.9")
        self.assertEqual(ctx.exception.code, "short-id-parent-mismatch")
        self.set(self.A, self.A["cycles"][0], short_id="CMD-01.9")
        self.assertEqual(self.meta(self.A)["cycles"][self.A["cycles"][0]]["short_id"], "CMD-01.9")
        self.assertEqual(self.state()["cycle_high_water"][self.A["campaign_id"]], 9)
        self.set(self.A, self.A["cycles"][1], title="다음 사이클")
        self.assertEqual(self.meta(self.A)["cycles"][self.A["cycles"][1]]["short_id"], "CMD-01.10")

    def test_a_representative_change_renumbers_cycles_and_keeps_every_old_id_as_alias(self):
        self.vocab("CMD", "TTS")
        self.judge(self.A, branches=["CMD"])
        before = self.meta(self.A)
        self.set(self.A, branches=["TTS", "CMD"])
        after = self.meta(self.A)
        self.assertEqual(after["campaign"]["short_id"], "TTS-01")
        self.assertEqual(after["campaign"]["aliases"], ["CMD-01"])
        for cycle_id in self.A["cycles"]:
            old, new = before["cycles"][cycle_id]["short_id"], after["cycles"][cycle_id]["short_id"]
            self.assertEqual((new.split(".")[0], new.split(".")[1]), ("TTS-01", old.split(".")[1]))
            self.assertEqual(after["cycles"][cycle_id]["aliases"], [old])
        self.assertEqual(self.state()["cycle_high_water"][self.A["campaign_id"]], 3)
        self.assertEqual(self.state()["issued"]["CMD-01.2"], {"kind": "cycle", "id": self.A["cycles"][1]})

    def test_only_the_newest_sixteen_aliases_are_public_but_every_owner_stays_known(self):
        self.vocab("CMD")
        self.set(self.A, branches=["CMD"])
        for number in range(10, 27):  # 17 changes
            self.set(self.A, short_id=f"CMD-{number}")
        campaign = self.meta(self.A)["campaign"]
        self.assertEqual(len(campaign["aliases"]), 16)
        self.assertNotIn("CMD-01", campaign["aliases"])  # the oldest left the public list
        self.assertEqual(self.state()["issued"]["CMD-01"]["id"], self.A["campaign_id"])
        self.set(self.B, branches=["CMD"])
        with self.assertRaises(M.MetaError) as ctx:
            self.set(self.B, short_id="CMD-01")
        self.assertEqual(ctx.exception.code, "id-reserved")

    def test_concurrent_writers_never_get_the_same_number(self):
        self.vocab("CMD")
        barrier = threading.Barrier(3)
        errors = []

        def work(campaign):
            try:
                barrier.wait()
                self.set(campaign, branches=["CMD"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(c,)) for c in (self.A, self.B, self.G)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        ids = sorted(self.meta(c)["campaign"]["short_id"] for c in (self.A, self.B, self.G))
        self.assertEqual(ids, ["CMD-01", "CMD-02", "CMD-03"])

    def test_a_missing_state_is_rebuilt_from_files_and_history_and_never_reissues(self):
        self.vocab("CMD")
        self.set(self.A, branches=["CMD"])
        self.set(self.B, branches=["CMD"])
        (self.root / STATE).unlink()
        gone = self.raw_meta_path(self.B)  # a campaign whose meta.json is deleted: only the history remembers CMD-02
        gone.unlink()
        self.set(self.G, branches=["CMD"])
        self.assertEqual(self.meta(self.G)["campaign"]["short_id"], "CMD-03")
        state = self.state()
        self.assertEqual(state["issued"]["CMD-02"], {"kind": "campaign", "id": self.B["campaign_id"]})
        self.assertEqual(state["issued"]["CMD-01"]["id"], self.A["campaign_id"])

    def test_a_damaged_state_is_a_typed_failure_and_changes_nothing(self):
        self.vocab("CMD")
        self.set(self.A, branches=["CMD"])
        for raw in (b"{not json", json.dumps({"schema_version": 1}).encode(),
                    json.dumps(dict(self.state(), artifact_root_id="root_" + "9" * 32)).encode()):
            (self.root / STATE).write_bytes(raw)
            before = files_of(self.root)
            with self.assertRaises(M.MetaError):
                self.set(self.B, branches=["CMD"])
            self.assertEqual(files_of(self.root), before)
            code, out = self.cli("show", "--artifact-root", str(self.root), "--campaign", self.A["campaign_id"])
            self.assertEqual(code, 65)

    def test_a_state_behind_the_files_only_grows(self):
        self.vocab("CMD")
        self.set(self.A, branches=["CMD"], short_id="CMD-40")
        state = self.state()
        state["branch_high_water"]["CMD"] = 3
        state["issued"].pop("CMD-40")
        (self.root / STATE).write_text(json.dumps(state), encoding="utf-8")
        self.set(self.B, branches=["CMD"])
        self.assertEqual(self.meta(self.B)["campaign"]["short_id"], "CMD-41")


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def proposal(**over):
    entity = {"title": "모델 제목", "summary": "모델 요약", "branches": ["CMD"], "kinds": ["평가"]}
    entity.update(over)
    return entity


class ProvenanceTest(MetaBase):
    def test_model_values_are_marked_model_and_rerunning_the_same_answer_changes_nothing(self):
        result = self.judge(self.A, new_branches=[{"code": "CMD", "label": "명령어", "note": "n"}])
        self.assertEqual(result["status"], "applied")
        campaign = self.meta(self.A)["campaign"]
        self.assertEqual({v["by"] for k, v in campaign["source"].items() if k != "short_id"}, {"model"})
        self.assertEqual(campaign["source"]["short_id"]["by"], "rule")
        files, events = files_of(self.root), len(self.events())
        again = self.judge(self.A, now=NOW + 99)
        self.assertEqual(again["status"], "no-change")
        self.assertEqual((files_of(self.root), len(self.events())), (files, events))
        self.assertEqual(self.meta(self.A)["campaign"]["source"]["title"]["at"], P._rfc3339(NOW))

    def test_a_person_or_agent_value_and_an_unknown_source_are_never_overwritten(self):
        self.vocab("CMD")
        self.set(self.A, title="사람 제목")
        self.set(self.A, summary="에이전트 요약", by="agent")
        path = self.raw_meta_path(self.A)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["campaign"]["kinds"] = ["문서"]  # a value with no source at all
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        self.judge(self.A, title="모델 제목", summary="모델 요약", kinds=["학습"], branches=["CMD"])
        campaign = self.meta(self.A)["campaign"]
        self.assertEqual((campaign["title"], campaign["summary"], campaign["kinds"]), ("사람 제목", "에이전트 요약", ["문서"]))
        self.assertEqual(campaign["branches"], ["CMD"])  # the open field is still filled
        self.assertEqual({k: campaign["source"][k]["by"] for k in ("title", "summary", "branches")},
                         {"title": "human", "summary": "agent", "branches": "model"})

    def test_release_keeps_the_value_and_the_next_judgement_refills_it(self):
        self.vocab("CMD")
        self.set(self.A, title="사람 제목", summary="사람 요약", branches=["CMD"], short_id="CMD-05")
        self.write(lambda ws: M.op_release(ws, self.A["campaign_id"], None, ["title", "short_id"]))
        campaign = self.meta(self.A)["campaign"]
        self.assertEqual(campaign["title"], "사람 제목")
        self.assertEqual((campaign["source"]["title"]["by"], campaign["source"]["short_id"]["by"],
                          campaign["source"]["summary"]["by"]), ("model", "rule", "human"))
        released = [e for e in self.events() if e["field"].startswith("campaign.source.")]
        self.assertEqual(sorted(e["field"] for e in released), ["campaign.source.short_id", "campaign.source.title"])
        self.judge(self.A, title="새 모델 제목", summary="새 모델 요약")
        campaign = self.meta(self.A)["campaign"]
        self.assertEqual((campaign["title"], campaign["summary"]), ("새 모델 제목", "사람 요약"))
        with self.assertRaises(M.MetaError):
            self.write(lambda ws: M.op_release(ws, self.A["campaign_id"], None, ["aliases"]))

    def test_a_person_who_sets_a_model_value_to_the_same_text_locks_it(self):
        self.vocab("CMD")
        self.judge(self.A, new_branches=[{"code": "CMD", "label": "명령어", "note": ""}])
        self.set(self.A, title="모델 제목")  # same text, new owner
        self.assertEqual(self.meta(self.A)["campaign"]["source"]["title"]["by"], "human")
        self.judge(self.A, title="또 다른 제목")
        self.assertEqual(self.meta(self.A)["campaign"]["title"], "모델 제목")

    def test_a_person_setting_between_the_proposal_and_the_write_wins(self):
        self.vocab("CMD")
        entity = proposal()  # the model's answer was already received...
        self.set(self.A, title="그 사이 사람이 고침")  # ...when a person edits
        M.apply_judgement(self.root, self.A["campaign_id"], campaign=entity, cycles={}, now=NOW)
        self.assertEqual(self.meta(self.A)["campaign"]["title"], "그 사이 사람이 고침")

    def test_the_model_does_not_move_a_representative_branch_that_would_renumber_a_fixed_id(self):
        self.vocab("CMD", "TTS")
        self.judge(self.A, branches=["CMD"])
        self.set(self.A, short_id="CMD-07")  # a person fixed the campaign ID
        self.judge(self.A, title="바뀐 제목", branches=["TTS"])
        campaign = self.meta(self.A)["campaign"]
        self.assertEqual((campaign["branches"], campaign["short_id"], campaign["title"]), (["CMD"], "CMD-07", "바뀐 제목"))
        # the same holds when only a cycle ID was fixed
        self.set(self.A, self.A["cycles"][0], short_id="CMD-07.8")
        self.write(lambda ws: M.op_release(ws, self.A["campaign_id"], None, ["short_id"]))
        self.judge(self.A, branches=["TTS"])
        self.assertEqual(self.meta(self.A)["campaign"]["branches"], ["CMD"])
        # a person asking for the change gets the whole renumbering as one request
        self.set(self.A, branches=["TTS"])
        after = self.meta(self.A)
        self.assertEqual(after["campaign"]["short_id"], "TTS-01")
        self.assertTrue(after["cycles"][self.A["cycles"][0]]["short_id"].startswith("TTS-01."))

    def test_an_old_declaration_title_is_kept_as_a_person_title_and_the_declaration_is_untouched(self):
        declaration = write_declaration(self.root, {self.A["campaign_id"]: "옛날에 정한 제목"})
        before = declaration.read_bytes()
        self.judge(self.A, new_branches=[{"code": "CMD", "label": "명령어", "note": ""}], title="모델이 쓴 제목")
        campaign = self.meta(self.A)["campaign"]
        self.assertEqual((campaign["title"], campaign["source"]["title"]["by"]), ("옛날에 정한 제목", "human"))
        self.assertEqual(campaign["summary"], "모델 요약")  # only the title is protected
        self.assertEqual(declaration.read_bytes(), before)
        # beta has no declaration entry, so the model may title it
        self.judge(self.B, title="모델이 쓴 베타 제목")
        self.assertEqual(self.meta(self.B)["campaign"]["title"], "모델이 쓴 베타 제목")
        self.assertEqual(self.meta(self.B)["campaign"]["source"]["title"]["by"], "model")

    def test_an_explicit_backfill_renews_an_old_declaration_title_and_keeps_a_persons(self):
        declaration = write_declaration(self.root, {self.A["campaign_id"]: "옛날에 정한 제목",
                                                    self.B["campaign_id"]: "베타 옛 제목"})
        before = declaration.read_bytes()
        cmd = [{"code": "CMD", "label": "명령어", "note": ""}]
        # an automatic review first copies the old title as a person's title
        self.judge(self.A, new_branches=cmd, title="자동 판정 제목")
        self.assertEqual(self.meta(self.A)["campaign"]["title"], "옛날에 정한 제목")
        # the supervised backfill renews it; the history keeps the old value
        self.judge(self.A, title="쉬운 새 제목", replace_legacy_titles=True)
        campaign = self.meta(self.A)["campaign"]
        self.assertEqual((campaign["title"], campaign["source"]["title"]["by"]), ("쉬운 새 제목", "model"))
        titles = [event for event in self.events()
                  if event["field"] == "campaign.title" and event["target"]["id"] == self.A["campaign_id"]]
        # history files are not in time order (random ids, same `at` here): look for the change itself
        self.assertIn(({"value": "옛날에 정한 제목"}, {"value": "쉬운 새 제목"}),
                      [(event["before"], event["after"]) for event in titles])
        # a campaign with no meta yet starts from the old title and is renewed in one write
        self.judge(self.B, title="베타 쉬운 제목", replace_legacy_titles=True)
        self.assertEqual(self.meta(self.B)["campaign"]["source"]["title"]["by"], "model")
        self.assertEqual(self.meta(self.B)["campaign"]["title"], "베타 쉬운 제목")
        # a title a person set through this tool is never renewed
        self.set(self.A, title="사람이 고친 제목")
        self.judge(self.A, title="또 다른 모델 제목", replace_legacy_titles=True)
        self.assertEqual(self.meta(self.A)["campaign"]["title"], "사람이 고친 제목")
        self.assertEqual(declaration.read_bytes(), before)

    def test_the_backfill_option_still_protects_titles_behind_a_bad_declaration(self):
        path = self.root / ".runtime/artifact-producer/v1/campaign-display-titles.json"
        cmd = [{"code": "CMD", "label": "명령어", "note": ""}]
        for content in ("{broken", None):
            with self.subTest(content=content):
                if content is None:  # a declaration title that breaks the title rules
                    write_declaration(self.root, {self.A["campaign_id"]: "가" * 200})
                else:
                    path.write_text(content, encoding="utf-8")
                before = path.read_bytes()
                self.judge(self.A, new_branches=cmd, title="모델 제목", replace_legacy_titles=True)
                self.assertNotIn("title", self.meta(self.A)["campaign"])
                self.assertEqual(path.read_bytes(), before)

    def test_an_unreadable_declaration_protects_every_title_instead_of_guessing(self):
        path = self.root / ".runtime/artifact-producer/v1/campaign-display-titles.json"
        path.write_text("{broken", encoding="utf-8")
        before = path.read_bytes()
        self.judge(self.A, new_branches=[{"code": "CMD", "label": "명령어", "note": ""}])
        campaign = self.meta(self.A)["campaign"]
        self.assertNotIn("title", campaign)
        self.assertEqual(campaign["summary"], "모델 요약")
        self.assertEqual(path.read_bytes(), before)

    def test_the_title_switch_keeps_only_the_campaign_title_out_of_the_judgement(self):
        self.judge(self.A, new_branches=[{"code": "CMD", "label": "명령어", "note": ""}], protect_title=True)
        campaign, cycles = self.meta(self.A)["campaign"], self.meta(self.A)["cycles"]
        self.assertNotIn("title", campaign)
        self.assertEqual((campaign["summary"], campaign["branches"]), ("모델 요약", ["CMD"]))
        self.assertTrue(all("title" in entry for entry in cycles.values()))

    def test_manifests_routes_campaign_records_and_declarations_stay_byte_identical(self):
        write_declaration(self.root, {self.B["campaign_id"]: "선언 제목"})
        before = files_of(self.root)
        self.vocab("CMD", "TTS")
        self.judge(self.A, new_branches=[])
        self.set(self.B, title="사람 제목", branches=["TTS"])
        self.write(lambda ws: M.op_branch_rename(ws, "TTS", "SND", None, None))
        self.write(lambda ws: M.op_branch_merge(ws, "SND", "CMD"))
        after = files_of(self.root)
        changed = {rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel)}
        allowed = re.compile(r"campaigns/[^/]+/meta\.json\Z|\.runtime/artifact-producer/v1/(project-meta\.json|artifact-meta-state\.json|history/.+)\Z")
        self.assertEqual([rel for rel in changed if not allowed.fullmatch(rel)], [])
        self.assertTrue(changed)


# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------


class VocabularyTest(MetaBase):
    def test_an_empty_project_takes_up_to_twelve_new_branches_and_the_rest_fall_back_to_etc(self):
        news = [{"code": f"B{chr(65 + i)}", "label": f"갈래 {i}", "note": ""} for i in range(13)]
        self.judge(self.A, branches=[news[0]["code"], news[12]["code"]], new_branches=news)
        branches = self.project()["branches"]
        self.assertEqual([b["code"] for b in branches if b["code"] != "ETC"], [n["code"] for n in news[:12]])
        self.assertEqual(branches[-1], {"code": "ETC", "label": "기타", "note": "기존 갈래에 맞지 않는 작업"})
        self.assertEqual(len(branches), 13)
        self.assertEqual(self.meta(self.A)["campaign"]["branches"], ["BA", "ETC"])  # the 13th became ETC
        self.assertEqual(self.state()["project_source"]["branches.BA"]["by"], "model")
        with self.assertRaises(M.MetaError) as ctx:  # a person cannot add a 13th general branch either
            self.vocab("ZZ")
        self.assertEqual(ctx.exception.code, "branch-limit")

    def test_a_project_with_branches_takes_at_most_one_new_one_per_judgement(self):
        self.vocab("CMD")
        self.judge(self.A, branches=["CMD", "NEW", "TWO"], new_branches=[
            {"code": "NEW", "label": "새 갈래", "note": ""}, {"code": "TWO", "label": "둘째", "note": ""}])
        self.assertEqual([b["code"] for b in self.project()["branches"]], ["CMD", "NEW", "ETC"])
        self.assertEqual(self.meta(self.A)["campaign"]["branches"], ["CMD", "NEW", "ETC"])

    def test_the_model_cannot_use_codes_outside_the_vocabulary_or_edit_existing_entries(self):
        self.vocab("CMD")
        before = self.project()["branches"]
        self.judge(self.A, branches=["UNKNOWN"], new_branches=[{"code": "CMD", "label": "다른 이름", "note": "x"}])
        self.assertEqual(self.project()["branches"][0], before[0])
        self.assertEqual(self.meta(self.A)["campaign"]["branches"], ["ETC"])  # unknown code -> the reserved fallback

    def test_adding_the_same_entry_twice_is_a_no_op_and_a_different_meaning_is_refused(self):
        self.vocab("CMD")
        result = self.write(lambda ws: M.op_branch_add(ws, "CMD", "CMD 갈래", "다른 설명"))
        self.assertEqual(result["status"], "no-change")
        with self.assertRaises(M.MetaError) as ctx:
            self.write(lambda ws: M.op_branch_add(ws, "CMD", "전혀 다른 이름", ""))
        self.assertEqual(ctx.exception.code, "branch-code-conflict")
        for bad in ("cmd", "C", "TOOLONG", "가나다"):
            with self.assertRaises(M.MetaError):
                self.write(lambda ws, c=bad: M.op_branch_add(ws, c, "x", ""))

    def test_import_is_checked_whole_merges_and_reruns_as_a_no_op(self):
        seed = {"branches": [{"code": "CMD", "label": "명령어 모델", "note": "에어컨"},
                             {"code": "TTS", "label": "TTS 데이터", "note": ""}]}
        self.write(lambda ws: M.op_branch_import(ws, seed))
        self.vocab("EXTRA")
        again = self.write(lambda ws: M.op_branch_import(ws, seed))
        self.assertEqual(again["status"], "no-change")
        self.assertEqual([b["code"] for b in self.project()["branches"]], ["CMD", "TTS", "EXTRA"])  # nothing was deleted
        before = files_of(self.root)
        for bad in ({"branches": [{"code": "AA", "label": "x", "note": ""}, {"code": "AA", "label": "y", "note": ""}]},
                    {"branches": [{"code": "ZZZ", "label": "x", "note": "", "extra": 1}]}, {"other": []}, []):
            with self.assertRaises(M.MetaError):
                self.write(lambda ws, s=bad: M.op_branch_import(ws, s))
        self.assertEqual(files_of(self.root), before)

    def test_renaming_the_label_keeps_the_code_and_renaming_the_code_follows_every_reference(self):
        self.vocab("CMD", "TTS")
        self.judge(self.A, branches=["CMD", "TTS"])
        self.judge(self.B, branches=["TTS", "CMD"])
        before = self.meta(self.A)["campaign"]["short_id"]
        self.write(lambda ws: M.op_branch_rename(ws, "CMD", None, "새 라벨", None))
        self.assertEqual(self.project()["branches"][0], {"code": "CMD", "label": "새 라벨", "note": "CMD 설명"})
        self.assertEqual(self.meta(self.A)["campaign"]["short_id"], before)
        self.write(lambda ws: M.op_branch_rename(ws, "CMD", "CMDX", None, None))
        a, b = self.meta(self.A), self.meta(self.B)
        self.assertEqual(a["campaign"]["branches"], ["CMDX", "TTS"])
        self.assertEqual((a["campaign"]["short_id"], a["campaign"]["aliases"]), ("CMDX-01", ["CMD-01"]))
        self.assertEqual(b["campaign"]["branches"], ["TTS", "CMDX"])  # not the representative: ID unchanged
        self.assertEqual(b["campaign"]["short_id"], "TTS-01")
        self.assertTrue(all(e["short_id"].startswith("CMDX-01.") for e in a["cycles"].values()))
        self.assertEqual([b["code"] for b in self.project()["branches"]], ["CMDX", "TTS"])
        self.assertEqual(self.state()["branch_high_water"]["CMD"], 1)  # the old counter stays
        self.assertEqual(self.state()["issued"]["CMD-01"]["id"], self.A["campaign_id"])
        with self.assertRaises(M.MetaError):
            self.write(lambda ws: M.op_branch_rename(ws, "CMDX", "TTS", None, None))
        with self.assertRaises(M.MetaError):
            self.write(lambda ws: M.op_branch_rename(ws, "TTS", None, None, None))

    def test_merging_replaces_references_in_order_without_duplicates_and_reissues_a_changed_representative(self):
        self.vocab("CMD", "TTS", "OLD")
        self.judge(self.A, branches=["OLD", "TTS"])
        self.judge(self.B, branches=["TTS", "OLD", "CMD"])
        self.write(lambda ws: M.op_branch_merge(ws, "OLD", "CMD"))
        a, b = self.meta(self.A)["campaign"], self.meta(self.B)["campaign"]
        self.assertEqual((a["branches"], b["branches"]), (["CMD", "TTS"], ["TTS", "CMD"]))
        self.assertEqual((a["short_id"], a["aliases"]), ("CMD-01", ["OLD-01"]))
        self.assertEqual(b["short_id"], "TTS-01")
        self.assertEqual([x["code"] for x in self.project()["branches"]], ["CMD", "TTS"])
        self.assertEqual(self.state()["issued"]["OLD-01"]["id"], self.A["campaign_id"])
        for _ in range(2):  # a cycle's own tags follow too
            self.assertTrue(all("OLD" not in e["branches"] for e in self.meta(self.A)["cycles"].values()))
        for code in ("CMD", "ETC", "NOPE"):
            with self.assertRaises(M.MetaError):
                self.write(lambda ws, c=code: M.op_branch_merge(ws, "ETC" if c == "ETC" else c, "CMD"))

    def test_remove_only_takes_unused_entries_and_never_the_reserved_one(self):
        self.vocab("CMD", "TTS")
        self.judge(self.A, branches=["CMD"])
        with self.assertRaises(M.MetaError) as ctx:
            self.write(lambda ws: M.op_branch_remove(ws, "CMD"))
        self.assertEqual(ctx.exception.code, "branch-in-use")
        self.write(lambda ws: M.op_branch_remove(ws, "TTS"))
        self.judge(self.B, branches=["CMD", "ZZ"], new_branches=[{"code": "ZZ", "label": "z", "note": ""}] * 0)
        self.assertIn("ETC", [b["code"] for b in self.project()["branches"]])
        with self.assertRaises(M.MetaError) as ctx:
            self.write(lambda ws: M.op_branch_remove(ws, "ETC"))
        self.assertEqual(ctx.exception.code, "branch-reserved")
        with self.assertRaises(M.MetaError):
            self.write(lambda ws: M.op_branch_rename(ws, "ETC", "OTHR", None, None))

    def test_fixed_kinds_are_the_seven_in_the_contract(self):
        self.assertEqual(M.KINDS, ("학습", "데이터", "평가", "문서", "운영", "조사", "배포"))
        code, out = self.cli("branches", "list", "--artifact-root", str(self.root))
        self.assertEqual((code, out["kinds"], out["reserved"]["code"]), (0, list(M.KINDS), "ETC"))

    def test_a_vocabulary_change_never_hides_behind_an_unreadable_meta(self):
        self.vocab("CMD", "TTS")
        self.judge(self.A, branches=["CMD"])
        self.raw_meta_path(self.A).write_text("{broken", encoding="utf-8")
        before = files_of(self.root)
        for op in (lambda ws: M.op_branch_remove(ws, "TTS"), lambda ws: M.op_branch_merge(ws, "TTS", "CMD"),
                   lambda ws: M.op_branch_rename(ws, "TTS", "TTX", None, None)):
            with self.assertRaises(M.MetaError) as ctx:
                self.write(op)
            self.assertEqual(ctx.exception.code, "meta-unreadable")
        self.assertEqual(files_of(self.root), before)

    def test_the_display_name_defaults_to_the_project_folder_and_a_person_can_change_it(self):
        self.vocab("CMD")
        self.assertEqual(self.project()["display_name"], self.root.name)
        self.write(lambda ws: M.op_set_project(ws, "명령어 인식 연구"))
        self.assertEqual(self.project()["display_name"], "명령어 인식 연구")
        self.assertEqual(self.state()["project_source"]["display_name"]["by"], "human")
        again = self.write(lambda ws: M.op_set_project(ws, "명령어 인식 연구"))
        self.assertEqual(again["status"], "no-change")


# ---------------------------------------------------------------------------
# history signal
# ---------------------------------------------------------------------------


class HistorySignalTest(MetaBase):
    def test_every_changed_field_is_one_line_in_one_transaction_and_a_no_op_writes_none(self):
        self.vocab("CMD")
        known = {e["event_id"] for e in self.events()}
        self.set(self.A, title="제목", summary="요약", branches=["CMD"])
        new = [e for e in self.events() if e["event_id"] not in known]  # file order is not a sequence
        fields = sorted(e["field"] for e in new)
        self.assertEqual(fields, ["campaign.branches", "campaign.short_id", "campaign.summary", "campaign.title"])
        self.assertEqual(len({e["transaction_id"] for e in new}), 1)
        by_field = {e["field"]: e for e in new}
        self.assertEqual((by_field["campaign.title"]["actor"]["by"], by_field["campaign.short_id"]["actor"]["by"]),
                         ("human", "rule"))
        self.assertEqual(by_field["campaign.title"]["before"], {"value": None})
        self.assertEqual(by_field["campaign.title"]["operation"], "add")
        self.assertEqual(by_field["campaign.title"]["target"]["path"], self.raw_meta_path(self.A).relative_to(self.root).as_posix())
        after_events = len(self.events())
        same = self.set(self.A, title="제목")
        self.assertEqual((same["status"], len(self.events())), ("no-change", after_events))

    def test_update_lines_carry_the_previous_value_and_long_values_a_digest(self):
        self.set(self.A, title="처음")
        self.set(self.A, title="나중")
        update = [e for e in self.events() if e["field"] == "campaign.title" and e["operation"] == "update"][0]
        self.assertEqual((update["before"], update["after"]), ({"value": "처음"}, {"value": "나중"}))
        self.set(self.A, summary="가" * 400)
        long_line = [e for e in self.events() if e["field"] == "campaign.summary"][0]
        self.assertEqual(set(long_line["after"]), {"digest", "bytes"})

    def test_actor_session_and_reason_come_from_the_caller_and_default_without_asking(self):
        code, out = self.cli("set", "--artifact-root", str(self.root), "--campaign", self.A["campaign_id"],
                             "--title", "에이전트 제목", "--by", "agent", "--session", "att-abc123", "--reason", "요청")
        self.assertEqual(code, 0, out)
        line = [e for e in self.events() if e["field"] == "campaign.title"][0]
        self.assertEqual((line["actor"], line["reason"]), ({"by": "agent", "session": "att-abc123", "harness": None,
                                                    "route": None, "attempt": None}, "요청"))
        code, _ = self.cli("set", "--artifact-root", str(self.root), "--campaign", self.B["campaign_id"], "--title", "기본값")
        line = [e for e in self.events() if e["target"]["id"] == self.B["campaign_id"]][0]
        self.assertEqual((line["actor"], bool(line["reason"])), ({"by": "human", "session": None, "harness": None,
                                                    "route": None, "attempt": None}, True))
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "att-envsession"}):
            self.cli("set", "--artifact-root", str(self.root), "--campaign", self.G["campaign_id"], "--title", "환경")
        line = [e for e in self.events() if e["target"]["id"] == self.G["campaign_id"]][0]
        self.assertEqual(line["actor"]["session"], "att-envsession")

    def test_project_vocabulary_changes_are_recorded_against_the_project(self):
        self.vocab("CMD")
        self.write(lambda ws: M.op_branch_rename(ws, "CMD", None, "새 이름", None))
        self.write(lambda ws: M.op_set_project(ws, "새 프로젝트"))
        rows = [e for e in self.events() if e["target"]["type"] == "project"]
        self.assertEqual(sorted((e["operation"], e["field"]) for e in rows),
                         [("add", "branches.CMD"), ("update", "branches.CMD"), ("update", "display_name")])
        self.assertEqual({e["target"]["path"] for e in rows}, {PROJECT})

    def test_history_files_are_never_rewritten_by_later_writes(self):
        self.vocab("CMD")
        self.set(self.A, title="하나")
        snapshot = {p: (p.stat().st_mtime_ns, sha(p.read_bytes()), p.stat().st_ino)
                    for p in (self.root / H.HISTORY_REL).rglob("*.jsonl")}
        self.set(self.A, title="둘")
        self.set(self.B, title="셋")
        for path, expected in snapshot.items():
            self.assertEqual((path.stat().st_mtime_ns, sha(path.read_bytes()), path.stat().st_ino), expected)


# ---------------------------------------------------------------------------
# the write path: intent, replay, failure
# ---------------------------------------------------------------------------


class WritePathTest(MetaBase):
    def intents(self):
        return M.pending_intents(self.root)

    def fail_on(self, name):
        real = P._write_atomic

        def selective(path, data, mode=0o644):
            if Path(path).name == name:
                raise OSError("injected disk failure")
            return real(path, data, mode)
        return patch_split(P, "_write_atomic", side_effect=selective)

    def test_a_failure_before_the_commit_point_writes_nothing_at_all(self):
        self.vocab("CMD")
        before = files_of(self.root)
        with self.assertRaises(M.MetaError):
            self.set(self.A, title="x" * 121)
        with self.assertRaises(M.MetaError):
            self.write(lambda ws: (M.op_set(ws, self.A["campaign_id"], None, {"title": "정상"}), M.check_code("bad")))
        self.assertEqual(files_of(self.root), before)
        self.assertEqual(self.intents(), [])

    def test_a_stop_in_the_middle_is_finished_by_the_next_write_with_each_event_once(self):
        self.vocab("CMD")
        with self.fail_on(Path(STATE).name), self.assertRaises(OSError):
            self.set(self.A, title="제목", branches=["CMD"])
        self.assertEqual(len(self.intents()), 1)  # the commit point was reached
        self.assertEqual(self.meta(self.A)["campaign"]["title"], "제목")  # meta.json is already replaced
        self.assertNotIn("CMD-01", self.state()["issued"])  # the state is not
        self.assertEqual([e["field"] for e in self.events() if e["target"]["id"] == self.A["campaign_id"]], [])
        self.set(self.B, title="다른 작업")  # any later write recovers first
        self.assertEqual(self.intents(), [])
        self.assertEqual(self.state()["issued"]["CMD-01"]["id"], self.A["campaign_id"])
        fields = sorted(e["field"] for e in self.events() if e["target"]["id"] == self.A["campaign_id"])
        self.assertEqual(fields, ["campaign.branches", "campaign.short_id", "campaign.title"])
        ids = [e["event_id"] for e in self.events()]
        self.assertEqual(len(ids), len(set(ids)))
        with self.locked():
            self.assertEqual(M.recover_locked(self.root), [])  # replaying finds nothing more

    def test_a_history_failure_after_the_files_are_written_is_rolled_forward_without_duplicates(self):
        self.vocab("CMD")
        with mock.patch.object(H, "publish_events_locked", side_effect=H.HistoryError("history-write-failed")):
            result = self.set(self.A, title="제목")
        self.assertEqual((result["status"], result["history"]), ("applied", "pending"))
        self.assertEqual(self.meta(self.A)["campaign"]["title"], "제목")  # nothing was rolled back
        self.assertEqual(len(self.intents()), 1)
        count = len(self.events())
        self.set(self.B, title="다음")
        self.assertEqual(self.intents(), [])
        fresh = [e for e in self.events() if e["target"]["id"] == self.A["campaign_id"] and e["field"] == "campaign.title"]
        self.assertEqual(len(fresh), 1)
        self.assertGreater(len(self.events()), count)

    def test_replaying_a_published_intent_is_idempotent(self):
        self.vocab("CMD")
        with mock.patch.object(H, "publish_events_locked", side_effect=H.HistoryError("down")):
            self.set(self.A, title="제목")
        name = self.intents()[0]
        keep = (self.root / H.STAGING_REL / name).read_bytes()
        self.set(self.B, title="다음")  # recovers and removes it
        (self.root / H.STAGING_REL / name).write_bytes(keep)  # a crash left the same intent behind again
        before = files_of(self.root)
        self.set(self.G, title="또 다음")
        after = files_of(self.root)
        self.assertEqual({r for r in after if r.startswith(".runtime/artifact-producer/v1/history/") and r in before},
                         {r for r in before if r.startswith(".runtime/artifact-producer/v1/history/")})
        titles = [e for e in self.events() if e["target"]["id"] == self.A["campaign_id"]]
        self.assertEqual(len(titles), 1)

    def test_a_file_someone_else_changed_is_kept_and_the_stale_intent_is_dropped(self):
        self.vocab("CMD")
        with self.fail_on(Path(STATE).name), self.assertRaises(OSError):
            self.set(self.A, title="제목", branches=["CMD"])
        path = self.raw_meta_path(self.A)
        foreign = json.loads(path.read_text(encoding="utf-8"))
        foreign["campaign"]["title"] = "외부에서 고친 제목"
        path.write_text(json.dumps(foreign, ensure_ascii=False), encoding="utf-8")
        foreign_bytes = path.read_bytes()
        result = self.write(lambda ws: M.op_set(ws, self.B["campaign_id"], None, {"title": "다음"}))
        self.assertEqual(result["recovered"][0]["conflict"], "intent-conflict")
        self.assertEqual(path.read_bytes(), foreign_bytes)
        self.assertEqual(self.intents(), [])

    def test_a_busy_admission_lock_is_a_typed_failure_and_changes_nothing(self):
        self.vocab("CMD")
        before = files_of(self.root)
        with self.locked():
            with self.assertRaises(M.MetaError) as ctx:
                M.run_write(self.root, lambda ws: M.op_set(ws, self.A["campaign_id"], None, {"title": "x"}),
                            lock_timeout=0)
        self.assertEqual(ctx.exception.code, "admission-busy")
        self.assertEqual(files_of(self.root), before)

    def locked(self):
        import artifact_admission as adm
        outer = self

        class Lock:
            def __enter__(self):
                self.fd = adm._acquire_lock(outer.root.resolve(), 5)

            def __exit__(self, *exc):
                adm._release_lock(outer.root.resolve(), self.fd)

        return Lock()


# ---------------------------------------------------------------------------
# reads and --dry-run never write
# ---------------------------------------------------------------------------


class ReadOnlyTest(MetaBase):
    def populated(self):
        self.vocab("CMD", "TTS")
        self.judge(self.A, branches=["CMD"])
        self.set(self.B, title="베타", branches=["TTS"])

    def test_dry_run_leaves_every_file_directory_and_mtime_alone(self):
        fresh_root = strict_snapshot(self.root)
        cases = [
            ("set", "--campaign", self.A["campaign_id"], "--title", "새 제목"),
            ("set", "--campaign", self.A["campaign_id"], "--cycle", self.A["cycles"][0], "--summary", "요약"),
            ("set", "--project", "--display-name", "새 이름"),
            ("branches", "add", "--code", "CMD", "--label", "명령어"),
        ]
        for argv in cases:  # an initial root: no meta.json, no project file, no state
            with self.subTest(argv):
                code, out = self.cli(*argv, "--artifact-root", str(self.root), "--dry-run")
                self.assertEqual((code, out["status"]), (0, "dry-run"))
                self.assertEqual(strict_snapshot(self.root), fresh_root)
        self.populated()
        populated = strict_snapshot(self.root)
        seed = Path(self._tmp.name) / "seed.json"
        seed.write_text(json.dumps({"branches": [{"code": "NEW", "label": "새", "note": ""}]}), encoding="utf-8")
        for argv in (("set", "--campaign", self.A["campaign_id"], "--title", "바꿈", "--branches", "TTS"),
                     ("release", "--campaign", self.A["campaign_id"], "--field", "title"),
                     ("branches", "rename", "--code", "CMD", "--new-code", "CMDX"),
                     ("branches", "merge", "--from", "TTS", "--into", "CMD"),
                     ("branches", "import", "--input", str(seed)),
                     ("branches", "remove", "--code", "TTS"),
                     ("set", "--campaign", self.A["campaign_id"], "--title", "")):
            with self.subTest(argv):
                code, out = self.cli(*argv, "--artifact-root", str(self.root), "--dry-run")
                self.assertIn(code, (0, 65))
                self.assertEqual(strict_snapshot(self.root), populated)
        for argv in (("set", "--campaign", "nope", "--title", "x"), ("set", "--campaign", self.A["campaign_id"]),
                     ("branches", "add", "--code", "bad", "--label", "x"), ("branches", "merge", "--from", "A")):
            with self.subTest(argv):
                code, out = self.cli(*argv, "--artifact-root", str(self.root), "--dry-run")
                self.assertEqual((code, out["status"]), (65, "blocked"))
                self.assertEqual(strict_snapshot(self.root), populated)

    def test_dry_run_reports_the_change_it_would_make(self):
        self.populated()
        code, out = self.cli("set", "--campaign", self.A["campaign_id"], "--title", "미리보기", "--artifact-root",
                             str(self.root), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual([c["field"] for c in out["changes"]], ["campaign.title"])
        self.assertEqual(out["changes"][0]["after"], {"value": "미리보기"})
        self.assertEqual(out["files"], [self.raw_meta_path(self.A).relative_to(self.root).as_posix()])
        self.assertNotEqual(self.meta(self.A)["campaign"]["title"], "미리보기")

    def test_the_judgement_dry_run_writes_nothing(self):
        self.vocab("CMD")
        before = strict_snapshot(self.root)
        result = self.judge(self.A, dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertTrue(result["changes"])
        self.assertEqual(strict_snapshot(self.root), before)

    def test_show_and_list_and_dry_run_report_a_pending_recovery_but_never_perform_it(self):
        self.populated()
        with mock.patch.object(H, "publish_events_locked", side_effect=H.HistoryError("down")):
            self.set(self.A, summary="남는 의도")
        self.assertEqual(len(M.pending_intents(self.root)), 1)
        snapshot = strict_snapshot(self.root)
        for argv in (("show", "--campaign", self.A["campaign_id"]), ("show", "--project"), ("branches", "list"),
                     ("set", "--campaign", self.A["campaign_id"], "--title", "x", "--dry-run"),
                     ("branches", "merge", "--from", "TTS", "--into", "CMD", "--dry-run")):
            with self.subTest(argv):
                code, out = self.cli(*argv, "--artifact-root", str(self.root))
                self.assertEqual(code, 0, out)
                self.assertEqual(out["pending_recovery"], 1)
                self.assertEqual(strict_snapshot(self.root), snapshot)
        self.assertEqual(len(M.pending_intents(self.root)), 1)

    def test_reading_modules_never_recover_or_create_anything(self):
        self.populated()
        with mock.patch.object(H, "publish_events_locked", side_effect=H.HistoryError("down")):
            self.set(self.A, summary="남는 의도")
        snapshot = strict_snapshot(self.root)
        M.read_campaign_meta(self.root, self.A["campaign_id"])
        M.read_project(self.root)
        M.effective_title(self.root, self.A["campaign_id"])
        M.legacy_title(self.root, self.A["campaign_id"])
        self.assertEqual(strict_snapshot(self.root), snapshot)


# ---------------------------------------------------------------------------
# the one command
# ---------------------------------------------------------------------------


class CommandTest(MetaBase):
    def test_show_set_release_and_branches_through_the_command(self):
        code, out = self.cli("branches", "add", "--artifact-root", str(self.root), "--code", "CMD", "--label", "명령어")
        self.assertEqual((code, out["status"]), (0, "applied"))
        code, out = self.cli("set", "--artifact-root", str(self.root), "--campaign", self.A["campaign_id"],
                             "--title", "제목", "--branches", "CMD", "--kinds", "학습,평가")
        self.assertEqual(code, 0, out)
        code, out = self.cli("show", "--artifact-root", str(self.root), "--campaign", "CMD-01")  # a short ID selects it
        self.assertEqual((code, out["entry"]["title"], out["entry"]["kinds"]), (0, "제목", ["학습", "평가"]))
        self.assertEqual(out["entry"]["source"]["title"]["by"], "human")
        code, out = self.cli("release", "--artifact-root", str(self.root), "--campaign", "CMD-01", "--field", "title")
        self.assertEqual((code, out["result"]["released"]), (0, ["title"]))
        code, out = self.cli("show", "--artifact-root", str(self.root), "--project")
        self.assertEqual((code, [b["code"] for b in out["branches"]]), (0, ["CMD"]))
        self.assertEqual(out["branches"][0]["source"]["by"], "human")

    def test_the_command_has_no_way_to_move_cycles_or_edit_manifests_or_routes(self):
        parser = M._parser()
        help_text = parser.format_help() + "".join(
            action.format_help() for choice in parser._subparsers._group_actions[0].choices.values()
            for action in [choice])
        for word in (r"manifest", r"route", r"\bmove\b", r"membership", r"--source"):
            self.assertIsNone(re.search(word, help_text), word)

    def test_argument_errors_and_unknown_targets_are_typed_json_with_a_nonzero_exit(self):
        for argv, code in ((("set",), "usage"), (("nonsense",), "usage"),
                           (("set", "--artifact-root", str(self.root), "--campaign", "camp_" + "0" * 32, "--title", "x"), "campaign-unknown"),
                           (("show", "--artifact-root", str(self.root), "--campaign", "NOPE-01"), "campaign-unknown"),
                           (("set", "--artifact-root", str(self.root / "missing"), "--campaign", "x", "--title", "y"), "root-invalid"),
                           (("set", "--artifact-root", str(self.root), "--project"), "nothing-to-set"),
                           (("branches", "import", "--artifact-root", str(self.root), "--input", str(self.root / "none.json")), "seed-unreadable")):
            with self.subTest(argv):
                status, out = self.cli(*argv)
                self.assertEqual((status, out["status"], out["code"]), (65, "blocked", code))

    def test_a_cycle_must_belong_to_the_named_campaign(self):
        self.vocab("CMD")
        with self.assertRaises(M.MetaError) as ctx:
            self.set(self.A, self.B["cycles"][0], title="남의 사이클")
        self.assertEqual(ctx.exception.code, "cycle-not-member")


# ---------------------------------------------------------------------------
# the contract document and its constants
# ---------------------------------------------------------------------------


def fenced_json(text):
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, flags=re.S)]


class ContractDocumentTest(unittest.TestCase):
    def setUp(self):
        self.text = CORE_DOC.read_text(encoding="utf-8")
        self.blocks = fenced_json(self.text)

    def block(self, contract):
        return next(b for b in self.blocks if b.get("contract") == contract)

    def test_every_json_example_parses_with_the_real_readers(self):
        meta = self.block("artifact-meta/v1")
        cycle_id = next(iter(meta["cycles"]))
        self.assertEqual(M._validate_meta_doc(meta, meta["artifact_root_id"], meta["campaign_id"],
                                              {cycle_id: {"campaign_id": meta["campaign_id"]}}), [])
        project = self.block("artifact-project-meta/v1")
        M._validate_project_doc(project, project["artifact_root_id"])
        self.assertEqual(list(project), ["schema_version", "contract", "artifact_root_id", "display_name", "branches"])
        state = self.block("artifact-meta-state/v1")
        M._validate_state(state, state["artifact_root_id"])
        line = self.block("artifact-history/v1")
        self.assertEqual(H.validate_event(json.loads(json.dumps(line)))["event_id"], line["event_id"])
        self.assertEqual(json.loads(H.event_bytes(line)), line)

    def test_the_documented_limits_and_names_match_the_constants(self):
        for needle in (M.META_CONTRACT, M.PROJECT_CONTRACT, M.STATE_CONTRACT, H.CONTRACT, M.META_NAME, M.PROJECT_REL,
                       M.STATE_REL, H.HISTORY_REL, "ETC", "기타", *M.KINDS):
            self.assertIn(needle, self.text, needle)
        for number in (M.TITLE_MAX, M.SUMMARY_MAX, M.LIST_MAX, M.GENERAL_BRANCH_MAX, H.VALUE_LIMIT):
            self.assertIn(str(number), self.text)
        for key in H.KEYS:
            self.assertIn(f"`{key}`", self.text)
        for kind in sorted(H.KINDS):
            self.assertIn(kind, self.text)
        self.assertIn("HEARTING_WORKFLOW_GROUP_REVIEW", self.text)
        self.assertIn(M.TITLE_DISABLE_ENV, self.text)

    def test_the_history_examples_are_the_ones_in_the_history_tests(self):
        spec = importlib.util.spec_from_file_location("artifact_history_test", Path(__file__).with_name("artifact_history.test.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        lines = [b for b in self.blocks if b.get("contract") == "artifact-history/v1"]
        self.assertEqual(lines, [module.EXAMPLE, module.LIFECYCLE_EXAMPLE])
        for line in lines:
            self.assertEqual(json.loads(H.event_bytes(line)), line)


if __name__ == "__main__":
    unittest.main()
