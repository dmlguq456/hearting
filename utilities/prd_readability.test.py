#!/usr/bin/env python3
"""Fixtures for the warn-only PRD readability check; pure text, no real PRD is read."""
import contextlib
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prd_readability", ROOT / "utilities/prd_readability.py")
RD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RD)

TICKS = "`" * 3
ROUTE = "rt-b890afc55528891c"
REVISION = "rrev_" + "b85d2c7ab61dde803251145c739ed26c"
CYCLE = "cyc_" + "d7d135750ccb23b131334aaf9bc909f0"
ATTEMPT = "att-" + "b47208f4df4340818d96f4348d9d4cb6"
UUID = "6db11f8d-2458-4a7a-a3fd-99f531be7a53"


def doc(block=("- one item",), body=("## 1. Common", "", "Plain text."), title="# Title", gap=("",)):
    return "\n".join([title, *gap, RD.BEGIN, *block, RD.END, "", *body]) + "\n"


def rules(report):
    return report["counts"]["by_rule"]


def body_rules(*lines):
    """Rules hit by each line of a valid document's body, keyed by the line text."""
    report = RD.check_text(doc(body=["## 1. Common", "", *lines]))
    first = len(doc(body=[]).splitlines()) + 3
    by_line = {}
    for v in report["violations"]:
        by_line.setdefault(v["line"] - first, set()).add(v["rule"])
    return [by_line.get(i, set()) for i in range(len(lines))]


class PrdReadabilityTest(unittest.TestCase):
    def test_u1_clean_prd_reports_nothing_and_exact_numbers(self):
        block = [f"- item number {i:02d}" for i in range(10)]
        report = RD.check_text(doc(block))
        self.assertEqual(report["counts"]["total"], 0)
        self.assertEqual(report["base"], "none")
        self.assertEqual(report["schema"], "prd-readability/1")
        self.assertEqual(report["summary"], {"present": True, "lines": 10, "chars": sum(len(x) for x in block),
                                             "items": 10, "max_item_chars": len("item number 00")})
        self.assertEqual(report["violations"], [])
        self.assertEqual((report["listed"], report["truncated"]), (0, 0))

    def test_u2_summary_line_limit(self):
        self.assertEqual(RD.check_text(doc(["- a"] * 40))["counts"]["total"], 0)
        report = RD.check_text(doc(["- a"] * 41))
        self.assertEqual(rules(report), {"summary-lines": 1})
        self.assertEqual(report["violations"][0]["line"], 3)

    def test_u3_item_length_counts_characters_and_continuations(self):
        self.assertEqual(RD.check_text(doc(["- " + "가" * 160]))["counts"]["total"], 0)
        report = RD.check_text(doc(["- short", "- " + "가" * 161]))
        self.assertEqual(rules(report), {"summary-item-length": 1})
        self.assertEqual(report["violations"][0]["line"], 5)
        split_ok = ["- " + "가" * 80, "  " + "나" * 79]
        self.assertEqual(RD.check_text(doc(split_ok))["counts"]["total"], 0)
        split_bad = ["- first", "- " + "가" * 80, "  " + "나" * 80]
        report = RD.check_text(doc(split_bad))
        self.assertEqual(rules(report), {"summary-item-length": 1})
        self.assertEqual(report["violations"][0]["line"], 5)
        self.assertEqual(report["summary"]["max_item_chars"], 161)

    def test_u3b_paragraph_and_numbered_items_are_items(self):
        report = RD.check_text(doc(["Intro paragraph " + "x" * 150, "", "1. " + "y" * 161]))
        self.assertEqual(rules(report), {"summary-item-length": 2})
        self.assertEqual(sorted(v["line"] for v in report["violations"]), [4, 6])

    def test_u4_summary_char_limit(self):
        ok = ["- " + "x" * 98] * 25
        self.assertEqual(sum(len(x) for x in ok), 2500)
        self.assertEqual(RD.check_text(doc(ok))["counts"]["total"], 0)
        report = RD.check_text(doc(ok[:-1] + ["- " + "x" * 99]))
        self.assertEqual(rules(report), {"summary-chars": 1})
        self.assertEqual(report["summary"]["chars"], 2501)

    def test_u5_section_reference_only_in_summary(self):
        self.assertEqual(rules(RD.check_text(doc(["- see §3 for detail"]))), {"section-ref": 1})
        self.assertEqual(rules(RD.check_text(doc(["- see § 3"]))), {"section-ref": 1})
        self.assertEqual(RD.check_text(doc(body=["## 1. Common", "", "see §3 here"]))["counts"]["total"], 0)
        self.assertEqual(RD.check_text(doc(["- the § sign"]))["counts"]["total"], 0)

    def test_u6_marker_presence_and_position(self):
        report = RD.check_text("# Title\n\n## 1. Common\n\ntext\n")
        self.assertEqual(rules(report), {"summary-missing": 1})
        self.assertEqual(report["summary"], {"present": False})
        self.assertEqual(report["violations"][0]["line"], 1)
        only_begin = RD.check_text(f"# T\n\n{RD.BEGIN}\n- a\n")
        self.assertEqual(rules(only_begin), {"summary-missing": 1})
        reversed_pair = RD.check_text(f"# T\n\n{RD.END}\n- a\n{RD.BEGIN}\n")
        self.assertEqual(rules(reversed_pair), {"summary-missing": 1})
        after_text = RD.check_text(doc(title="# Title\n\nSome paragraph."))
        self.assertEqual(rules(after_text), {"summary-position": 1})
        quoted = RD.check_text(doc(title="# Title\n\n> v3 history quote"))
        self.assertEqual(rules(quoted), {"summary-position": 1})
        self.assertEqual(RD.check_text(doc(gap=("", "")))["counts"]["total"], 0)
        self.assertEqual(RD.check_text(f"{RD.BEGIN}\n- a\n{RD.END}\n")["counts"]["total"], 0)

    def test_u7_version_tags(self):
        heading = "## Blueprint ⟨v3, 2026-09-29⟩"
        self.assertEqual(RD.check_text(doc([heading, "- a"]))["counts"]["total"], 0)
        second_in_heading = RD.check_text(doc(["## Blueprint ⟨v3⟩ ⟨v2⟩", "- a"]))
        self.assertEqual(rules(second_in_heading), {"version-tag": 1})
        second_line = RD.check_text(doc([heading, "- changed ⟨v2, 2026-09-01⟩"]))
        self.assertEqual(rules(second_line), {"version-tag": 1})
        self.assertEqual(second_line["violations"][0]["area"], "summary")
        body = RD.check_text(doc(body=["## 3. X ⟨v177 · v188 개정⟩", "", "paragraph ⟨v12⟩ and 〈v4〉"]))
        self.assertEqual(rules(body), {"version-tag": 2})
        self.assertEqual({v["area"] for v in body["violations"]}, {"body"})
        plain = RD.check_text(doc(body=["## 1. Common", "", "(v2 API) and v1.2 and [v3] and ⟨no tag⟩"]))
        self.assertEqual(plain["counts"]["total"], 0)
        self.assertEqual(RD.check_text(doc(title="# Title (v35) — 2026-09-29"))["counts"]["total"], 0)
        self.assertEqual(rules(RD.check_text(doc(title="# Title ⟨v3⟩"))), {"version-tag": 1})

    def test_u8_internal_ids(self):
        cases = {"route-id": ROUTE, "revision-id": REVISION, "cycle-id": CYCLE,
                 "attempt-id": ATTEMPT, "session-id": UUID, "pane-id": "w1:p15"}
        for rule, value in cases.items():
            for text in (f"see {value} here", f"see `{value}` here"):
                with self.subTest(rule=rule, text=text):
                    self.assertEqual(body_rules(text), [{rule}])
        for text in ("rt-<id> session_id SessionEnd w1 prd-readability-owner rt-abc att-12 cyc_short",):
            self.assertEqual(body_rules(text), [set()])
        self.assertEqual(rules(RD.check_text(doc([f"- run {ROUTE}"]))), {"route-id": 1})

    def test_u9_commit_hash(self):
        sha256 = hashlib.sha256(b"x").hexdigest()
        positive = ["control commit `1600e12`; done", "at e9c35c48 we", "full " + "a1b2c3d4e5" * 4 + " end",
                    "commit:abc1234 merged", "(1600e12)"]
        negative = ["deadbeef", "1234567", sha256, "sha256:5aeb6a5a…", "bundle_digest sha256:1600e12ab",
                    "sha256=abc1234 ok", "MD5: abc1234", "0x1f2e3d4", "#a1b2c3d4", "/cycles/abc1234 dir",
                    "ckpt_1a2b3c4", "part -446655440000 of uuid", "preimage sha256 `ba9064b2…`", "bundle (`5aeb6a5a…`) sealed",
                "release 8b80c4a2... done", "digest `1600e12`", "words like decade and 2026-09-29"]
        for text, got in zip(positive, body_rules(*positive)):
            with self.subTest(text=text): self.assertEqual(got, {"commit-hash"})
        for text, got in zip(negative, body_rules(*negative)):
            with self.subTest(text=text): self.assertEqual(got, set())

    def test_u10_checksum_tables(self):
        def table(head, cell="1600e12"):
            return [head, "|---|---|", f"| a | {cell} |", f"| b | {cell} |"]
        for head in ("| file | sha256 |", "| file | Checksum |", "| 파일 | 체크섬 |", "| file | digest |"):
            with self.subTest(head=head):
                self.assertEqual(RD.check_text(doc(body=["## 1. Common", "", *table(head)]))["counts"]["total"], 0)
        plain = RD.check_text(doc(body=["## 1. Common", "", *table("| name | note |")]))
        self.assertEqual(rules(plain), {"commit-hash": 2})
        commits = RD.check_text(doc(body=["## 1. Common", "", *table("| commit hash | note |")]))
        self.assertEqual(rules(commits), {"commit-hash": 2})
        other_rules = RD.check_text(doc(body=["## 1. Common", "", *table("| file | sha256 |", "①")]))
        self.assertEqual(rules(other_rules), {"circled-char": 2})
        second = RD.check_text(doc(body=["## 1. Common", "", *table("| file | sha256 |"), "", *table("| name | note |")]))
        self.assertEqual(rules(second), {"commit-hash": 2})

    def test_u11_circled_characters(self):
        for text in ("①", "ⓐ", "ⅰ", "Ⅱ", "❶", "㉠", "⑴", "㊿", "⓪"):
            with self.subTest(text=text): self.assertEqual(body_rules(f"step {text} here"), [{"circled-char"}])
        self.assertEqual(body_rules("a ①ⓐⅰⅡ❶㉠⑴ b"), [{"circled-char"}])
        report = RD.check_text(doc(body=["## 1. Common", "", "a ①ⓐⅰⅡ❶㉠⑴ b"]))
        self.assertEqual(rules(report), {"circled-char": 1})
        self.assertEqual(body_rules("✦ ⚠️ ★ → 1) 🇰🇷 ✓"), [set()])

    def test_u12_fenced_code_is_skipped(self):
        noisy = f"rt-b890afc55528891c ① ⟨v3⟩ 1600e12 {UUID} w1:p15"
        four = "`" * 4
        body = ["## 1. Common", "", TICKS, noisy, TICKS, "", "~~~", noisy, "~~~", "",
                four, "inner", TICKS, noisy, four, "", TICKS + "mermaid", noisy, TICKS, "",
                "  " + TICKS, noisy, "  " + TICKS, "", "~~~~", noisy, "~~~", noisy, "~~~~"]
        self.assertEqual(RD.check_text(doc(body=body))["counts"]["total"], 0)
        unclosed = RD.check_text(doc(body=["## 1. Common", "", TICKS, noisy, "still fenced " + noisy]))
        self.assertEqual(unclosed["counts"]["total"], 0)
        after = RD.check_text(doc(body=["## 1. Common", "", TICKS, "code", TICKS, noisy]))
        self.assertEqual(rules(after), {"circled-char": 1, "commit-hash": 1, "pane-id": 1, "route-id": 1,
                                        "session-id": 1, "version-tag": 1})
        in_summary = RD.check_text(doc([TICKS, "- " + "x" * 300, "§4 " + noisy, TICKS, "- ok"]))
        self.assertEqual(in_summary["counts"]["total"], 0)
        self.assertEqual(in_summary["summary"]["items"], 1)

    def test_u12b_markers_inside_a_fence_are_not_markers(self):
        text = "# T\n\n" + TICKS + "\n" + RD.BEGIN + "\n- a\n" + RD.END + "\n" + TICKS + "\n"
        self.assertEqual(rules(RD.check_text(text)), {"summary-missing": 1})
        self.assertIsNone(RD.summary_bounds(text.splitlines()))

    def test_u13_new_versus_existing_lines(self):
        base = doc(body=["## 1. A", "old violation ① here", "keep line", "tail"])
        post = doc(body=["## 1. A", "old violation ① here", "new violation ② here", "keep line",
                         "changed line now has ③", "tail"])
        # the changed line only exists in the new text
        report = RD.check_text(post, base)
        self.assertEqual(report["base"], "preimage")
        self.assertEqual((report["counts"]["new"], report["counts"]["existing"]), (2, 1))
        ages = {v["line"]: v["age"] for v in report["violations"]}
        first = len(doc(body=[]).splitlines()) + 1
        self.assertEqual(ages, {first + 1: "existing", first + 2: "new", first + 4: "new"})
        self.assertEqual([v["age"] for v in report["violations"]], ["new", "new", "existing"])
        everything = RD.check_text(post)
        self.assertEqual((everything["counts"]["new"], everything["counts"]["existing"]), (3, 0))
        self.assertEqual(everything["base"], "none")
        self.assertEqual(report["counts"]["total"], report["counts"]["new"] + report["counts"]["existing"])
        self.assertEqual(sum(report["counts"]["by_rule"].values()), report["counts"]["total"])

    def test_u13b_block_rule_age_follows_the_base(self):
        long_block, short_block = ["- a"] * 41, ["- a"] * 5
        both = RD.check_text(doc(long_block), doc(long_block))
        self.assertEqual((both["counts"]["new"], both["counts"]["existing"]), (0, 1))
        grown = RD.check_text(doc(long_block), doc(short_block))
        self.assertEqual((grown["counts"]["new"], grown["counts"]["existing"]), (1, 0))
        legacy = RD.check_text(doc(short_block), "# Title\n\nold text\n")
        self.assertEqual(legacy["counts"]["total"], 0)
        still_missing = RD.check_text("# T\n\nnew\n", "# T\n\nold\n")
        self.assertEqual((still_missing["counts"]["new"], still_missing["counts"]["existing"]), (0, 1))

    def test_u13c_prefix_and_suffix_trim_never_overlap(self):
        cases = [
            (["a", "a", "a"], ["a", "a"], {3}),
            (["a", "b", "a", "b"], ["a", "b"], {3, 4}),
            (["a"], ["a", "a"], set()),
            (["x", "a", "b"], ["a", "b"], {1}),
            (["a", "b"], ["a", "b"], set()),
            ([], ["a"], set()),
            (["a", "b"], [], {1, 2}),
            (["a", "x", "b"], ["a", "b"], {2}),
            (["a", "x", "y", "b"], ["a", "q", "b"], {2, 3}),
        ]
        for lines, base, expected in cases:
            with self.subTest(lines=lines, base=base):
                self.assertEqual(RD.new_line_numbers(lines, base), expected)
        self.assertEqual(RD.new_line_numbers(["a", "b"], None), {1, 2})

    def test_u14_listing_is_capped_and_new_first(self):
        head = doc(body=["## 1. Common"])
        old = [f"old ① line {i}" for i in range(10)]
        new = [f"new ① line {i} " + "z" * 200 for i in range(50)]
        base = head + "\n".join(old) + "\n"
        post = head + "\n".join(old + new) + "\n"
        report = RD.check_text(post, base)
        self.assertEqual(report["counts"]["total"], 60)
        self.assertEqual((report["listed"], report["truncated"], len(report["violations"])), (50, 10, 50))
        self.assertTrue(all(v["age"] == "new" for v in report["violations"]))
        self.assertTrue(all(len(v["excerpt"]) <= RD.EXCERPT_CHARS for v in report["violations"]))
        self.assertEqual(report["counts"]["existing"], 10)
        listed = [v["line"] for v in report["violations"]]
        self.assertEqual(listed, sorted(listed))

    def test_u14b_existing_follow_new_when_both_fit(self):
        head = doc(body=["## 1. Common"])
        base = head + "old ①\n"
        post = head + "old ①\nnew ②\n"
        ages = [v["age"] for v in RD.check_text(post, base)["violations"]]
        self.assertEqual(ages, ["new", "existing"])

    def test_u15_cli(self):
        with tempfile.TemporaryDirectory() as td:
            prd, base = Path(td) / "prd.md", Path(td) / "base.md"
            prd.write_text(doc(body=["## 1. Common", "", "step ① and ②"]), encoding="utf-8")
            base.write_text(doc(body=["## 1. Common", "", "step ① and ②"]), encoding="utf-8")
            tool = [sys.executable, str(ROOT / "utilities/prd_readability.py")]
            human = subprocess.run(tool + ["check", str(prd)], capture_output=True, text=True)
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn("circled-char: 1", human.stdout)
            self.assertIn("lines=1/40", human.stdout)
            as_json = subprocess.run(tool + ["check", str(prd), "--json"], capture_output=True, text=True)
            self.assertEqual(as_json.returncode, 0)
            self.assertEqual(json.loads(as_json.stdout)["counts"]["new"], 1)
            with_base = subprocess.run(tool + ["check", str(prd), "--base", str(base), "--json"],
                                       capture_output=True, text=True)
            self.assertEqual(json.loads(with_base.stdout)["counts"]["existing"], 1)
            self.assertEqual(json.loads(with_base.stdout)["base"], "preimage")
            missing = subprocess.run(tool + ["check", str(Path(td) / "none.md")], capture_output=True, text=True)
            self.assertEqual(missing.returncode, 2)
            missing_base = subprocess.run(tool + ["check", str(prd), "--base", str(Path(td) / "none.md")],
                                          capture_output=True, text=True)
            self.assertEqual(missing_base.returncode, 2)
            abbreviated = subprocess.run(tool + ["check", str(prd), "--js"], capture_output=True, text=True)
            self.assertEqual(abbreviated.returncode, 2)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(RD.main(["check", str(prd)]), 0)

    def test_u16_odd_inputs(self):
        for raw in (b"", b"# T\n\xff\xfe broken \xc3\n", b"\x00\x01\x02", b"\n\n\n"):
            with self.subTest(raw=raw):
                report = RD.check_bytes(raw, raw)
                self.assertIn("counts", report)
                RD.check_bytes(raw, None)
        self.assertEqual(rules(RD.check_bytes(b"", None)), {"summary-missing": 1})
        crlf = doc(["- a", "- b"], body=["## 1. Common", "", "text"]).replace("\n", "\r\n")
        report = RD.check_bytes(crlf.encode(), None)
        self.assertEqual(report["counts"]["total"], 0)
        self.assertEqual(report["summary"]["lines"], 2)
        self.assertEqual(RD.check_bytes(crlf.encode(), crlf.encode())["counts"]["total"], 0)
        broken = RD.check_bytes(b"# T\n\n" + RD.BEGIN.encode() + b"\n- bad \xff \xc3(\n" + RD.END.encode() + b"\n", None)
        self.assertTrue(broken["summary"]["present"])

    def test_u17_documented_limits_match_the_code(self):
        expected = {RD.LIMITS["summary_lines"], RD.LIMITS["summary_item_chars"], RD.LIMITS["summary_chars"]}
        self.assertEqual(expected, {40, 160, 2500})
        docs = ("capabilities/autopilot-spec/references/prd-authoring.md",
                "capabilities/autopilot-spec/references/owner-execution.md",
                "capabilities/autopilot-spec.md")
        for rel in docs:
            text = (ROOT / rel).read_text(encoding="utf-8")
            found = {int(n.replace(",", "")) for n in re.findall(r"(\d[\d,]*)\*{0,2}\s+(?:lines|characters|chars)\b", text)}
            with self.subTest(doc=rel):
                self.assertLessEqual(expected, found)

    def test_u18_large_documents_stay_fast(self):
        head = doc(body=["## 1. Common"])
        distinct = [f"line {i} of the body" for i in range(8000)]
        edited = list(distinct)
        for i in (100, 2500, 4000, 7000): edited[i] = edited[i] + " ①"
        repeated = ["same line here"] * 8000
        edited_repeat = list(repeated)
        for i in (100, 2500, 4000, 7000): edited_repeat[i] = "same line here ①"
        shifted = ["x"] + distinct[:7000] + ["y"] + distinct[7000:]
        for name, base, post in (("distinct", distinct, edited), ("repeated", repeated, edited_repeat),
                                 ("shifted", distinct, shifted[:6000] + ["z ①"] + shifted[6000:]),
                                 ("reversed-halves", distinct[4000:] + distinct[:4000], distinct)):
            with self.subTest(name=name):
                started = time.monotonic()
                report = RD.check_text(head + "\n".join(post) + "\n", head + "\n".join(base) + "\n")
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertGreaterEqual(report["counts"]["total"], 0)
        report = RD.check_text(head + "\n".join(edited) + "\n", head + "\n".join(distinct) + "\n")
        self.assertEqual((report["counts"]["new"], report["counts"]["existing"]), (4, 0))

    def test_u19_stderr_line(self):
        self.assertIsNone(RD.stderr_line(RD.check_text(doc())))
        line = RD.stderr_line(RD.check_text(doc(body=["## 1. Common", "", "① and 1600e12", "② here", f"{ROUTE}"])))
        self.assertNotIn("\n", line)
        self.assertTrue(line.startswith("prd-readability: 4 warnings (new 4, existing 0): "))
        self.assertIn("circled-char 2", line)
        one = RD.stderr_line(RD.check_text(doc(body=["## 1. Common", "", "①"])))
        self.assertTrue(one.startswith("prd-readability: 1 warning "))
        many = doc(body=["## 1. Common", "", "① 1600e12", ROUTE, REVISION, CYCLE, ATTEMPT, UUID, "w1:p15"])
        self.assertTrue(RD.stderr_line(RD.check_text(many)).count(", …") == 1)

    def test_summary_items_helper(self):
        items = RD.summary_items(["## Head", "- one", "  more", "- two", "", "para a", "para b", "1. three"])
        self.assertEqual(items, [(1, "one more"), (3, "two"), (5, "para a para b"), (7, "three")])

    def test_fenced_mask_shapes(self):
        lines = ["a", TICKS, "b", TICKS, "c", "~~~", "d"]
        self.assertEqual(RD.fenced_mask(lines), [False, True, True, True, False, True, True])
        self.assertEqual(RD.fenced_mask([TICKS + "x`y", "z"]), [False, False])


if __name__ == "__main__":
    unittest.main()
