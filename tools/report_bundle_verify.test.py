#!/usr/bin/env python3
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
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
    def test_cross_repo_v2_golden_shapes_are_exact(self):
        link = json.loads((HERE.parent / "capabilities/report-bundle-link-existing.request.example.json").read_text())
        receipt = json.loads((HERE.parent / "capabilities/report-bundle-receipt.v2.example.json").read_text())
        self.assertEqual(set(link), {"schema_version", "bundle_id", "version", "entrypoint", "mode", "documents"})
        self.assertEqual(set(link["documents"][0]), {"document_id", "note_id"})
        self.assertEqual(set(receipt), {"schema_version", "event", "status", "completed_at", "bundle_id", "version", "entrypoint"})
        for value in (link, receipt):
            encoded = json.dumps(value)
            for forbidden in ("source_path", "source_capability", "project_root", "body"):
                self.assertNotIn(forbidden, encoded)

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

    def test_active_html_and_svg_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root)
            cases = (
                "<script>location='x'</script>",
                '<img src="REPORT.md" onerror="alert(1)">',
                '<a href="javascript:alert(1)">x</a>',
                '<meta http-equiv="refresh" content="0; url=REPORT.md">',
                '<iframe srcdoc="<p>x</p>"></iframe>',
                '<base href="https://example.invalid/"><a href="REPORT.md">x</a>',
                '<form action="https://example.invalid/"><button>send</button></form>',
            )
            for html in cases:
                (root / "index.html").write_text(html, encoding="utf-8")
                data["files"][1]["sha256"] = digest(root / "index.html")
                path.write_text(json.dumps(data))
                with self.subTest(html=html), self.assertRaisesRegex(ValueError, "active HTML content forbidden"):
                    B.VERIFY.verify(path)
            svg = root / "diagram.svg"; svg.write_text('<svg onload="alert(1)"></svg>', encoding="utf-8")
            data["files"].append({"path": svg.name, "sha256": digest(svg)})
            (root / "index.html").write_text('<img src="diagram.svg">', encoding="utf-8")
            data["files"][1]["sha256"] = digest(root / "index.html"); path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "active HTML content forbidden"): B.VERIFY.verify(path)

    def test_markdown_raw_html_resources_and_active_content_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root)
            cases = (
                ('<img src="https://evil.invalid/x.png">', "non-self-contained resource"),
                ("<script>alert(1)</script>", "active HTML content forbidden"),
                ('<img src="index.html" onerror="alert(1)">', "active HTML content forbidden"),
                ('<a href="javascript:alert(1)">x</a>', "active HTML content forbidden"),
                ('<meta http-equiv="refresh" content="0; url=index.html">', "active HTML content forbidden"),
            )
            for markdown, error in cases:
                (root / "REPORT.md").write_text(markdown, encoding="utf-8")
                data["files"][0]["sha256"] = digest(root / "REPORT.md"); path.write_text(json.dumps(data))
                with self.subTest(markdown=markdown), self.assertRaisesRegex(ValueError, error): B.VERIFY.verify(path)
            (root / "REPORT.md").write_text("```html\n<script>example</script>\n```\n[report](index.html)\n", encoding="utf-8")
            data["files"][0]["sha256"] = digest(root / "REPORT.md"); path.write_text(json.dumps(data))
            self.assertEqual(B.VERIFY.verify(path)["bundle_classification"], "bundle/v2")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg actual-decode fixture")
    def test_audio_format_parity_wav_mp3_ogg(self):
        for suffix in (".wav", ".mp3", ".ogg"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as td:
                root = Path(td); path, data = self.prose(root); self.add_media(root, data)
                audio = root / "media/audio.wav"
                if suffix != ".wav":
                    converted = audio.with_suffix(suffix)
                    subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-i", str(audio), str(converted)], check=True)
                    audio.unlink(); data["files"][2]["path"] = "media/" + converted.name
                    data["media"][0]["path"] = "media/" + converted.name
                    playback = root / "media/playback.html"
                    playback.write_text(playback.read_text().replace("audio.wav", converted.name), encoding="utf-8")
                audio = root / data["media"][0]["path"]
                data["files"][2]["sha256"] = digest(audio); data["media"][0]["sha256"] = digest(audio)
                data["files"][-1]["sha256"] = digest(root / "media/playback.html")
                data["media"][-1]["sha256"] = data["files"][-1]["sha256"]
                path.write_text(json.dumps(data)); self.assertEqual(B.VERIFY.verify(path)["media"], 4)

    def test_audio_extension_and_hardlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root); self.add_media(root, data)
            audio = root / "media/audio.wav"; renamed = root / "media/audio.aac"; audio.rename(renamed)
            data["files"][2]["path"] = data["media"][0]["path"] = "media/audio.aac"
            data["files"][2]["sha256"] = data["media"][0]["sha256"] = digest(renamed)
            playback = root / "media/playback.html"; playback.write_text(playback.read_text().replace("audio.wav", "audio.aac"))
            data["files"][-1]["sha256"] = data["media"][-1]["sha256"] = digest(playback); path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "WAV, MP3, or OGG"): B.VERIFY.verify(path)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root)
            os.link(root / "REPORT.md", root / "duplicate.md")
            data["files"].append({"path": "duplicate.md", "sha256": digest(root / "duplicate.md")}); path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "unsafe bundle file"): B.VERIFY.verify(path)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, _ = self.prose(root); outside = root.parent / (root.name + "-manifest-link")
            try:
                os.link(path, outside)
                with self.assertRaisesRegex(ValueError, "single-link regular file"): B.VERIFY.verify(path)
            finally:
                if outside.exists(): outside.unlink()

    def test_integrity_reason_codes_distinguish_decode_playback_and_transient(self):
        self.assertEqual(B._integrity_reason(ValueError("media decode failed: x")), "decode")
        self.assertEqual(B._integrity_reason(ValueError("playback decode failed: x")), "playback")
        self.assertEqual(B._integrity_reason(ValueError("media decode failed: ffmpeg unavailable")), "decode")
        self.assertEqual(B._integrity_reason(ValueError("bundle storage transient failure")), "transient")
        self.assertEqual(B._integrity_reason(OSError(5, "Input/output error")), "transient")

    def test_publish_is_atomic_idempotent_and_collision_safe(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); source = base / "source"; source.mkdir(); self.prose(source)
            store = base / "store"; store.mkdir()
            result = B.publish(source, "proj", "exp", "v1", store)
            self.assertEqual(result["status"], "published")
            self.assertEqual(B.publish(source, "proj", "exp", "v1", store)["status"], "unchanged")
            (source / "REPORT.md").write_text("changed"); data=json.loads((source / "report_manifest.json").read_text()); data["files"][0]["sha256"] = digest(source / "REPORT.md"); (source / "report_manifest.json").write_text(json.dumps(data))
            with self.assertRaisesRegex(B.BundleError, "collision"): B.publish(source, "proj", "exp", "v1", store)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root / "staging"; target = root / "report"
            source.mkdir(); target.mkdir()
            with self.assertRaisesRegex(B.BundleError, "collision"): B._rename_noreplace(source, target)
            self.assertTrue(source.is_dir()); self.assertTrue(target.is_dir())

    def test_integrity_sweep_writes_bundle_state_only_on_transition(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); source = base / "source"; source.mkdir(); self.prose(source)
            store = base / "store"; store.mkdir(); B.publish(source, "proj", "exp", "v1", store)
            state = base / "monitor/state.json"; heartbeat = base / "monitor/heartbeat.json"
            first = B.run_integrity(store, state, heartbeat, checked_at="2026-08-11T00:00:00+00:00")
            self.assertEqual(first["bundle_state_writes"], 1); self.assertEqual(first["heartbeat_writes"], 1)
            state_bytes = state.read_bytes(); first_heartbeat = heartbeat.read_bytes()
            second = B.run_integrity(store, state, heartbeat, checked_at="2026-08-11T00:01:00+00:00")
            self.assertEqual(second["bundle_state_writes"], 0); self.assertEqual(state.read_bytes(), state_bytes)
            self.assertNotEqual(heartbeat.read_bytes(), first_heartbeat)
            with mock.patch.object(B.VERIFY, "verify", side_effect=OSError(5, "Input/output error")):
                transient = B.run_integrity(store, state, heartbeat, checked_at="2026-08-11T00:01:30+00:00")
            self.assertEqual(transient["bundles"]["proj/exp/v1"], {"status": "checking", "reason": "transient"})
            self.assertEqual(transient["effective_bundles"]["proj/exp/v1"], {"status": "healthy", "reason": None})
            self.assertEqual(transient["bundle_state_writes"], 0); self.assertEqual(state.read_bytes(), state_bytes)
            report = store / "proj/exp/v1/report/REPORT.md"; report.write_text("corrupt", encoding="utf-8")
            broken = B.run_integrity(store, state, heartbeat, checked_at="2026-08-11T00:02:00+00:00")
            self.assertEqual(broken["bundle_state_writes"], 1)
            self.assertEqual(broken["bundles"]["proj/exp/v1"], {"status": "broken", "reason": "hash"})
            shutil.rmtree(store / "proj/exp/v1/report")
            missing = B.run_integrity(store, state, heartbeat, checked_at="2026-08-11T00:03:00+00:00")
            self.assertEqual(missing["bundles"]["proj/exp/v1"], {"status": "broken", "reason": "missing"})
            state_bytes = state.read_bytes()
            unchanged = B.run_integrity(store, state, heartbeat, checked_at="2026-08-11T00:04:00+00:00")
            self.assertEqual(unchanged["bundle_state_writes"], 0); self.assertEqual(state.read_bytes(), state_bytes)

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

    def test_original_logs_are_hash_bound_without_manifest_shape_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path, data = self.prose(root)
            logs = root / "logs"; logs.mkdir(); raw = logs / "eval.log"
            raw.write_text("step=1 loss=0.25\n", encoding="utf-8")
            data["files"].append({"path": "logs/eval.log", "sha256": digest(raw)})
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(set(data), B.VERIFY.V2_KEYS)
            self.assertEqual(B.VERIFY.verify(path)["bundle_classification"], "bundle/v2")
            raw.write_text("step=1 loss=0.20\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"): B.VERIFY.verify(path)

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

    def test_authoritative_38_link_only_census_emits_ids_only_requests(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve(); bundles = []
            for index in range(38):
                source = root / ("bundle-%02d" % index); source.mkdir()
                manifest, manifest_data = self.prose(source, project="proj", experiment="exp-%02d" % index)
                documents = [{
                    "document_id": "report", "source_path": "REPORT.md",
                    "source_sha256": digest(source / "REPORT.md"), "parent_document_id": None,
                    "note_id": "note-%02d" % index, "note_body_sha256": "0" * 64,
                    "note_revision": index + 1,
                }]
                if index == 0:
                    child = source / "child.md"; child.write_text("[report](REPORT.md)\n", encoding="utf-8")
                    manifest_data["files"].append({"path": "child.md", "sha256": digest(child)})
                    manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
                    documents.append({
                        "document_id": "child", "source_path": "child.md", "source_sha256": digest(child),
                        "parent_document_id": "report", "note_id": "note-child", "note_body_sha256": "1" * 64,
                        "note_revision": 1,
                    })
                bundles.append({
                    "source_root": str(source), "project_root": str(root), "project": "proj",
                    "experiment_id": "exp-%02d" % index, "version": "v1", "already_generated": False,
                    "documents": documents,
                })
                self.assertTrue(manifest.is_file())
            inventory = root / "inventory.json"
            inventory.write_text(json.dumps({"schema_version": 1, "candidate_count": 38, "bundles": bundles}), encoding="utf-8")
            result = B.link_existing_plan(inventory)
            self.assertEqual(result["candidate_count"], 38); self.assertEqual(result["document_count"], 39); self.assertFalse(result["mutation"])
            self.assertEqual(result["proof"]["l2_notes_update_rows"], 0)
            exact = {"schema_version", "bundle_id", "version", "entrypoint", "mode", "documents"}
            self.assertTrue(all(set(request) == exact for request in result["requests"]))
            encoded = json.dumps(result["requests"])
            for forbidden in ("source_root", "project_root", "source_path", "source_sha256", "body", "revision"):
                self.assertNotIn(forbidden, encoded)
            data = json.loads(inventory.read_text()); data["bundles"][0]["documents"].reverse(); inventory.write_text(json.dumps(data))
            with self.assertRaisesRegex(B.BundleError, "complete manifest document set"): B.link_existing_plan(inventory)
            data["bundles"][0]["documents"].reverse(); data["bundles"][0]["documents"][1]["parent_document_id"] = "missing"
            inventory.write_text(json.dumps(data))
            with self.assertRaisesRegex(B.BundleError, "hierarchy"): B.link_existing_plan(inventory)
            data["bundles"][0]["documents"][1]["parent_document_id"] = "report"; inventory.write_text(json.dumps(data))
            data["bundles"][0]["documents"][1]["source_path"] = "REPORT.md"; inventory.write_text(json.dumps(data))
            with self.assertRaisesRegex(B.BundleError, "complete manifest document set"): B.link_existing_plan(inventory)
            data["bundles"][0]["documents"][1]["source_path"] = "child.md"; inventory.write_text(json.dumps(data))
            outside_project = root / "other-project-root"; outside_project.mkdir()
            data["bundles"][0]["project_root"] = str(outside_project); inventory.write_text(json.dumps(data))
            with self.assertRaisesRegex(B.BundleError, "contained by project_root"): B.link_existing_plan(inventory)
            data["bundles"][0]["project_root"] = str(root); inventory.write_text(json.dumps(data))
            data = json.loads(inventory.read_text()); data["bundles"][1]["documents"][0]["note_id"] = "note-00"
            inventory.write_text(json.dumps(data))
            with self.assertRaisesRegex(B.BundleError, "duplicate"): B.link_existing_plan(inventory)
            data["bundles"][1]["documents"][0]["note_id"] = "note-01"
            data["bundles"][1]["project"] = "aliased-project"
            inventory.write_text(json.dumps(data))
            with self.assertRaisesRegex(B.BundleError, "alias collision"): B.link_existing_plan(inventory)
            data["bundles"][1]["project"] = "proj"
            data["bundles"][0]["documents"][0]["source_sha256"] = "f" * 64
            inventory.write_text(json.dumps(data))
            with self.assertRaisesRegex(B.BundleError, "manifest-bound"): B.link_existing_plan(inventory)
            data["bundles"][0]["documents"][0]["source_sha256"] = digest(Path(data["bundles"][0]["source_root"]) / "REPORT.md")
            data["bundles"][0]["already_generated"] = True; inventory.write_text(json.dumps(data))
            with self.assertRaisesRegex(B.BundleError, "already-generated"): B.link_existing_plan(inventory)
            data["bundles"][0]["already_generated"] = False; inventory.write_text(json.dumps(data))
            data = json.loads(inventory.read_text()); data["candidate_count"] = 37; data["bundles"].pop()
            inventory.write_text(json.dumps(data))
            with self.assertRaisesRegex(B.BundleError, "exactly 38"): B.link_existing_plan(inventory)


if __name__ == "__main__":
    unittest.main()
