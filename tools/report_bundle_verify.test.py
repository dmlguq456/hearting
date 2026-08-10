#!/usr/bin/env python3
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest import mock
import wave

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("bundle", HERE / "report-bundle.py")
B = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(B)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BundleV2Tests(unittest.TestCase):
    def prose(self, root, project="proj", experiment="exp", version="v1"):
        (root / "media").mkdir()
        (root / "REPORT.md").write_text("# Report\n[Open report](index.html)\n", encoding="utf-8")
        (root / "index.html").write_text('<h1>Report</h1><a href="REPORT.md">source</a>', encoding="utf-8")
        files = [{"path": name, "sha256": digest(root / name)} for name in ("REPORT.md", "index.html")]
        data = {"schema_version": 2, "bundle_id": project + "/" + experiment, "project": project,
                "experiment_id": experiment, "version": version, "entrypoint": "index.html",
                "files": files, "media": []}
        path = root / "report_manifest.json"; path.write_text(json.dumps(data), encoding="utf-8")
        return path, data

    def add_media(self, root, data):
        media = root / "media"
        with wave.open(str(media / "audio.wav"), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000); wav.writeframes(struct.pack("<h", 0) * 80)
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        (media / "waveform.png").write_bytes(png)
        (media / "spectrogram.png").write_bytes(png)
        (media / "playback.html").write_text(
            '<audio controls src="audio.wav"></audio><img src="waveform.png"><img src="spectrogram.png">',
            encoding="utf-8",
        )
        kinds = ("audio", "waveform", "spectrogram", "playback")
        names = ("audio.wav", "waveform.png", "spectrogram.png", "playback.html")
        for name, kind in zip(names, kinds):
            row = {"path": "media/" + name, "sha256": digest(media / name)}
            data["files"].append(dict(row)); data["media"].append(dict(row, sample_id="s1", kind=kind))

    def test_prose_only_passes(self):
        with tempfile.TemporaryDirectory() as td:
            result = B.VERIFY.verify(self.prose(Path(td))[0])
            self.assertEqual(result["bundle_classification"], "bundle/v2")
            self.assertEqual(result["samples"], 0)

    def test_root_escape_missing_hash_and_unlisted_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root)
            (root / "index.html").write_text('<a href="../escape">x</a>', encoding="utf-8")
            data["files"][1]["sha256"] = digest(root / "index.html"); path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "escapes root"): B.VERIFY.verify(path)
            (root / "index.html").write_text('<a href="missing.md">x</a>', encoding="utf-8")
            data["files"][1]["sha256"] = digest(root / "index.html"); path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "missing internal"): B.VERIFY.verify(path)
            (root / "index.html").write_text("ok", encoding="utf-8"); path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "hash mismatch"): B.VERIFY.verify(path)
            data["files"][1]["sha256"] = digest(root / "index.html"); (root / "extra.txt").write_text("x"); path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "inventory mismatch"): B.VERIFY.verify(path)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg actual-decode fixture")
    def test_declared_media_must_actually_decode_and_dom_bind_one_to_one(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root)
            media = root / "media"; self.add_media(root, data)
            path.write_text(json.dumps(data)); self.assertEqual(B.VERIFY.verify(path)["media"], 4)
            (media / "waveform.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            data["files"][3]["sha256"] = digest(media / "waveform.png"); data["media"][1]["sha256"] = data["files"][3]["sha256"]; path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "decode failed"): B.VERIFY.verify(path)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg actual-decode fixture")
    def test_playback_comments_do_not_count_as_dom_bindings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root); self.add_media(root, data)
            playback = root / "media/playback.html"
            playback.write_text("<!-- audio.wav waveform.png spectrogram.png -->", encoding="utf-8")
            data["files"][-1]["sha256"] = digest(playback); data["media"][-1]["sha256"] = digest(playback)
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "does not bind"): B.VERIFY.verify(path)

    def test_declared_media_fails_closed_when_ffmpeg_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root); self.add_media(root, data); path.write_text(json.dumps(data))
            with mock.patch.object(B.VERIFY.shutil, "which", return_value=None):
                with self.assertRaisesRegex(ValueError, "ffmpeg unavailable"): B.VERIFY.verify(path)

    def test_ffmpeg_decode_invocation_is_shell_free_bounded_and_output_free(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(B.VERIFY.shutil, "which", return_value="/usr/bin/ffmpeg"), mock.patch.object(
            B.VERIFY.subprocess, "run", return_value=completed,
        ) as run:
            B.VERIFY._decode_media(Path("/fixture/audio.wav"), "audio")
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertIs(run.call_args.kwargs["stdout"], B.VERIFY.subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stderr"], B.VERIFY.subprocess.DEVNULL)

    def test_reference_srcset_style_and_inventory_links_are_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root)
            cases = (
                (root / "REPORT.md", "[outside][escape]\n\n[escape]: ../outside.md\n", "escapes root"),
                (root / "index.html", '<img srcset="REPORT.md 1x, ../outside.png 2x">', "escapes root"),
                (root / "index.html", "<style>body{background:url('../outside.png')}</style>", "escapes root"),
                (root / "index.html", "<style>@import '../outside.css';</style>", "escapes root"),
                (root / "index.html", '<a href="report_manifest.json">manifest</a>', "not in bundle inventory"),
            )
            for target, text, error in cases:
                (root / "REPORT.md").write_text("# Report\n[Open report](index.html)\n", encoding="utf-8")
                (root / "index.html").write_text('<h1>Report</h1><a href="REPORT.md">source</a>', encoding="utf-8")
                target.write_text(text, encoding="utf-8")
                for row in data["files"]: row["sha256"] = digest(root / row["path"])
                path.write_text(json.dumps(data))
                with self.subTest(text=text), self.assertRaisesRegex(ValueError, error): B.VERIFY.verify(path)

    def test_publish_is_atomic_idempotent_and_collision_safe(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); source = base / "source"; source.mkdir(); self.prose(source)
            store = base / "store"; store.mkdir()
            result = B.publish(source, "proj", "exp", "v1", store)
            self.assertEqual(result["status"], "published")
            self.assertEqual(B.publish(source, "proj", "exp", "v1", store)["status"], "unchanged")
            (source / "REPORT.md").write_text("changed"); data=json.loads((source / "report_manifest.json").read_text()); data["files"][0]["sha256"] = digest(source / "REPORT.md"); (source / "report_manifest.json").write_text(json.dumps(data))
            with self.assertRaisesRegex(B.BundleError, "collision"): B.publish(source, "proj", "exp", "v1", store)

    def test_identity_and_version_are_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td); self.prose(source)
            plan=B.backfill_plan(source,"proj","exp","release-7")
            self.assertEqual(plan["version"],"release-7"); self.assertFalse(plan["mutation"])
            self.assertEqual(plan["status"],"ready-v2")
            with self.assertRaisesRegex(B.BundleError,"identity"): B.publish(source,"proj","other","v1",Path(td))

    def test_generic_documents_outside_media_are_inventory_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root)
            for index in range(15):
                document = root / ("doc-%02d.md" % index)
                document.write_text("[report](REPORT.md)\n", encoding="utf-8")
                data["files"].append({"path": document.name, "sha256": digest(document)})
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(len(data["files"]), 17)
            self.assertEqual(B.VERIFY.verify(path)["bundle_classification"], "bundle/v2")

    def test_backfill_maps_briefing_excludes_pipeline_summary_and_generates_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "00_briefing.md").write_text("brief")
            for index in range(15): (root / ("doc-%02d.md" % index)).write_text("doc")
            (root / "pipeline_summary.md").write_text("internal")
            plan = B.backfill_plan(root, "legacy", "parent", "v1")
            self.assertEqual(plan["renames"], {"00_briefing.md": "REPORT.md"})
            self.assertEqual(plan["excluded"], ["pipeline_summary.md"])
            self.assertEqual(plan["generated"], ["index.html"])
            self.assertEqual(len(plan["candidates"]["documents"]), 16)
            self.assertEqual(plan["status"], "needs-canonicalization")


if __name__ == "__main__":
    unittest.main()
