"""The README's Fleet screens: neutral demo rows, a current SVG, and bounded image files."""
from __future__ import annotations

import importlib.util
import os
import re
import struct
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

DOCS = ROOT / "docs"
HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]")
# Names that would mean a real project, host or person leaked into the demo rows or the images.
PRIVATE = re.compile(r"corrnet|uihyeop|dmlguq|moving4|larynx|xavier|mpwav|/home/nas|agent_setting", re.I)
CELL_W, CELL_H = 9, 18
PNG_LIMIT, GIF_LIMIT, SVG_LIMIT = 120_000, 600_000, 150_000


def png_size(path):
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", head[16:24])


def load_svg_generator():
    spec = importlib.util.spec_from_file_location("render_fleet_svg", ROOT / "tools" / "render-fleet-svg.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReadmeDemoRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ)
        cls.env.start()
        from tools.fleet.tests import readme_captures
        readme_captures.isolate()
        from tools.fleet import render
        cls.readme_captures = readme_captures
        cls.frames = readme_captures.frames(render, list(readme_captures.SCREENS))

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        time.tzset()

    def test_every_screen_draws_at_its_size_in_english(self):
        for name, (_view, _scope, cols, rows, _raw) in self.readme_captures.SCREENS.items():
            with self.subTest(screen=name):
                frame = self.frames[name]
                self.assertEqual((frame.cols, frame.rows), (cols, rows))
                text = "\n".join(frame.text_rows())
                self.assertIsNone(HANGUL.search(text), "Korean text on a README screen")
                self.assertIsNone(PRIVATE.search(text), "a private name on a README screen")

    def test_header_carries_the_current_version_and_no_unavailable_usage(self):
        from tools.fleet.tests import readme_demo
        header = "\n".join(self.frames["overview"].text_rows()[:2])
        self.assertIn(readme_demo.VERSION + " · managed", header)
        self.assertNotIn("unavailable", header)

    def test_screens_show_what_the_readme_says_they_show(self):
        text = {name: "\n".join(frame.text_rows()) for name, frame in self.frames.items()}
        self.assertIn("claude", text["overview"])
        self.assertIn("codex", text["overview"])
        self.assertIn("opencode", text["overview"])
        self.assertIn("answer needed", text["overview"])
        self.assertIn("plan ✓ › execute ●", text["overview"])
        self.assertIn("WAITING  1 question", text["questions"])
        self.assertIn("GPU", text["resources"])
        self.assertIn("1 link", text["session"])
        self.assertIn("owner raw", text["output"])
        self.assertIn("1,200 clips", text["output"])

    def test_demo_rows_name_only_fictional_projects(self):
        from tools.fleet.tests import readme_demo
        snapshot, hosts = readme_demo.snapshot()
        repos = {os.path.basename(s.cwd) for s in snapshot.sessions if not s.is_child}
        self.assertEqual(repos, set(readme_demo.PROJECTS))
        self.assertEqual({h["host"] for h in hosts["hosts"]}, {"devbox", "gpu-box"})


class PublishedFiles(unittest.TestCase):
    def test_svg_is_well_formed_current_and_plain(self):
        path = DOCS / "fleet.svg"
        raw = path.read_text(encoding="utf-8")
        root = ET.fromstring(raw)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertTrue(root.get("aria-label"))
        self.assertLess(len(raw.encode("utf-8")), SVG_LIMIT)
        for forbidden in ("<style", "<script", "<image", "href="):
            self.assertNotIn(forbidden, raw)
        self.assertIsNone(PRIVATE.search(raw))
        self.assertIsNone(HANGUL.search(raw))
        with mock.patch.dict(os.environ):
            want = load_svg_generator().render()
        time.tzset()
        self.assertEqual(raw, want, "docs/fleet.svg is stale: run python3 tools/render-fleet-svg.py")

    def test_images_exist_with_the_drawn_size_and_a_small_file(self):
        from tools.fleet.tests import readme_captures
        for name, (_view, _scope, cols, rows, _raw) in readme_captures.SCREENS.items():
            with self.subTest(image=name):
                path = DOCS / "fleet" / f"{name}.png"
                self.assertEqual(png_size(path), (cols * CELL_W, rows * CELL_H))
                self.assertLess(path.stat().st_size, PNG_LIMIT)
        gif = DOCS / "fleet" / "walkthrough.gif"
        self.assertEqual(gif.read_bytes()[:6], b"GIF89a")
        self.assertLess(gif.stat().st_size, GIF_LIMIT)

    def test_real_project_name_is_gone_from_public_pages(self):
        for relative in ("docs/fleet.svg", "docs/index.html", "tools/render-landing.py"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIsNone(re.search("corrnet", text, re.I), relative)


if __name__ == "__main__":
    unittest.main()
