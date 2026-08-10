#!/usr/bin/env python3
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
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

    def test_declared_media_must_decode_and_bind_one_to_one(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root)
            media = root / "media"
            with wave.open(str(media / "audio.wav"), "wb") as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000); wav.writeframes(struct.pack("<h", 0) * 80)
            (media / "waveform.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (media / "spectrogram.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (media / "playback.html").write_text('audio.wav waveform.png spectrogram.png', encoding="utf-8")
            kinds = ("audio", "waveform", "spectrogram", "playback")
            names = ("audio.wav", "waveform.png", "spectrogram.png", "playback.html")
            for name, kind in zip(names, kinds):
                row = {"path": "media/" + name, "sha256": digest(media / name)}
                data["files"].append(dict(row)); data["media"].append(dict(row, sample_id="s1", kind=kind))
            path.write_text(json.dumps(data)); self.assertEqual(B.VERIFY.verify(path)["media"], 4)
            (media / "audio.wav").write_text("broken"); data["files"][2]["sha256"] = digest(media / "audio.wav"); data["media"][0]["sha256"] = data["files"][2]["sha256"]; path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "decode failed"): B.VERIFY.verify(path)

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
