"""The README's Fleet screens: neutral demo rows, a current SVG, and bounded image files."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

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
# Shapes that would mean a real machine, person or project leaked into the demo rows or the images:
# a home directory other than the demo one, a mount path, an e-mail address, a private-range IP,
# or a legacy install-directory name.
PRIVATE = re.compile(
    r"/home/(?!demo\b)[\w.-]+"
    r"|/Users/[\w.-]+"
    r"|/(?:mnt|nas|data|net)/[\w.-]+"
    r"|[\w.+-]+@[\w-]+\.[\w.-]+"
    r"|\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
    r"|\b(?:agent|claude|codex|opencode)_setting\b",
    re.I,
)
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
        for name, (_view, _scope, cols, rows, *_rest) in self.readme_captures.SCREENS.items():
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
        # Spec E9 rule 24, 11-17 (route row 5): a staged route reads its kind and place (`staged 2/4: execute`).
        self.assertRegex(text["overview"], r"staged \d+/\d+: execute")
        self.assertIn("◑ to answer 1", text["questions"])
        self.assertIn("GPU", text["resources"])
        # The session page's MESSAGES (the note it sent [e8]) and the session it is linked with.
        self.assertIn("MESSAGES", text["session"])
        self.assertIn("Profile 4, 8 and 16 loader workers", text["session"])
        # 11-17: the PEERS line sits under the page title (supervisor, flag, dash, supervised), and the routes stand
        # under a named zone rule.
        self.assertRegex(text["session"], r"PEERS +\[d1\] ⚑ ─ \[e8\]")
        self.assertRegex(text["session"], r"─ dispatch ─+")
        # The README's opening pictures: one moment, the request and the route [4f]'s panel shows,
        # each Fleet list whole (nothing cut off below it).
        # PEERS lists the newest link of each kind: the request [4f] sent [b2] stands as a PEER REQUEST link.
        self.assertRegex(text["peers"], r"\[4f\] claude +→ +\[b2\] codex +PEER REQUEST")
        self.assertIn("Test run for [4f]", text["overview"])
        self.assertRegex(text["routes"], r"Rate limiting +code\(\w+\) · staged 2/4: execute")
        self.assertIn("leg 1 · code dev", text["routes"])
        for name in ("overview", "peers", "routes"):
            self.assertNotIn("more below", text[name], name)
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
        for name, (_view, _scope, cols, rows, *_rest) in readme_captures.SCREENS.items():
            with self.subTest(image=name):
                path = DOCS / "fleet" / f"{name}.png"
                self.assertEqual(png_size(path), (cols * CELL_W, rows * CELL_H))
                self.assertLess(path.stat().st_size, PNG_LIMIT)
        gif = DOCS / "fleet" / "walkthrough.gif"
        self.assertEqual(gif.read_bytes()[:6], b"GIF89a")
        self.assertLess(gif.stat().st_size, GIF_LIMIT)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed (the pictures are drawn by a developer's tool)")
    def test_quantising_keeps_the_named_colours_as_they_are(self):
        """Median-cut once turned code blue into claude's teal and lab amber into the status yellow: the five fields' hues
        and the three zones' stay exact, and a picture of them alone keeps each as its own palette entry."""
        from PIL import Image
        from tools.fleet.tests import readme_captures
        named = readme_captures.named_colours()
        wanted = [(95, 135, 255), (255, 175, 0), (255, 95, 175), (215, 175, 135), (0, 175, 215),
                  (255, 135, 0), (135, 95, 255), (0, 215, 175)]
        for colour in wanted:
            self.assertIn(colour, named)
        # The neighbours that carried the wrong meaning: claude's teal, the status yellow (xterm 186), a muted blue.
        neighbours = [(88, 185, 177), (215, 215, 135), (112, 189, 191), (218, 218, 129), (133, 215, 202)]
        image = Image.new("RGB", (len(wanted) + len(neighbours) + 200, 1), (16, 16, 16))
        for x, colour in enumerate(wanted + neighbours):
            image.putpixel((x, 0), colour)
        for x in range(len(wanted) + len(neighbours), image.size[0]):       # many other colours, to force the merge
            image.putpixel((x, 0), ((x * 37) % 256, (x * 91) % 256, (x * 53) % 256))
        out = readme_captures.quantised(image).convert("RGB")
        self.assertLessEqual(len({c for _n, c in out.getcolors()}), readme_captures.COLOURS)
        for x, colour in enumerate(wanted):
            self.assertEqual(out.getpixel((x, 0)), colour)

    def test_no_private_path_or_address_on_public_pages(self):
        for relative in ("docs/fleet.svg", "docs/index.html", "docs/fleet.html", "docs/map.html",
                         "tools/render-landing.py"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIsNone(PRIVATE.search(text), relative)


if __name__ == "__main__":
    unittest.main()
