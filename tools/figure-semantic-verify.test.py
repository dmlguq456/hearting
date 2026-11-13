#!/usr/bin/env python3
"""Regression tests for fail-closed report spectrogram semantic QA."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import copy
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "tools" / "figure-semantic-verify.py"
CLAIM_TEXT = "이 그림은 전 대역 에너지를 보여준다."


def normalized_claim_id(text: str) -> str:
    normalized = " ".join(text.replace("**", "").split())
    return "claim-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def write_png(path: Path, width: int = 16, height: int = 12) -> None:
    rows = b"".join(b"\x00" + b"\x88\x44\x22" * width for _ in range(height))
    data = b"\x89PNG\r\n\x1a\n"
    data += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += png_chunk(b"IDAT", zlib.compress(rows))
    data += png_chunk(b"IEND", b"")
    path.write_bytes(data)


def valid_manifest(png_path: Path) -> dict:
    return {
        "schema_version": 1,
        "profile": "spectrogram-report-48k-full-band",
        "figure_groups": [
            {
                "id": "comparison",
                "kind": "spectrogram",
                "png_path": png_path.name,
                "sample_rate_hz": 48000,
                "min_hz": 0,
                "max_hz": 24000,
                "dynamic_range_db": 80,
                "shared_scale_per_figure": True,
                "colormap": "magma",
                "panels": [
                    {"id": "reference", "vmin_db": -80, "vmax_db": 0},
                    {"id": "estimate", "vmin_db": -80, "vmax_db": 0},
                ],
                "visual_review": {
                    "reviewed_png": png_path.name,
                    "png_sha256": hashlib.sha256(png_path.read_bytes()).hexdigest(),
                    "reviewer": "test-fixture",
                    "reviewed_at": "2026-07-14T17:00:00+09:00",
                    "evidence": "Representative PNG opened; axis, ticks, labels, colorbar, and scale inspected.",
                    "y_axis_0_24khz": True,
                    "ticks_readable": True,
                    "colorbar_present": True,
                    "shared_scale_confirmed": True,
                    "labels_readable": True,
                },
            }
        ],
        "metrics": [{"id": "low_band_metric", "min_hz": 20, "max_hz": 1000}],
        "claims": [
            {
                "id": normalized_claim_id(CLAIM_TEXT),
                "text": CLAIM_TEXT,
                "type": "full_band",
                "evidence": [{"kind": "figure_group", "id": "comparison"}],
            }
        ],
    }


class FigureSemanticVerifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.png = self.root / "spectrogram.png"
        self.report = self.root / "report.md"
        self.manifest_path = self.root / "manifest.json"
        write_png(self.png)
        self.report.write_text(
            "# 결과\n\n![Spectrogram](spectrogram.png)\n\n이 그림은 **전 대역**\n에너지를 보여준다.\n",
            encoding="utf-8",
        )
        self.manifest = valid_manifest(self.png)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def verify(self, manifest: dict | None = None) -> subprocess.CompletedProcess[str]:
        self.manifest_path.write_text(
            json.dumps(manifest or self.manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [sys.executable, str(VERIFIER), "--manifest", str(self.manifest_path), "--report", str(self.report)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def verify_raw(self, manifest_text: str) -> subprocess.CompletedProcess[str]:
        self.manifest_path.write_text(manifest_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(VERIFIER), "--manifest", str(self.manifest_path), "--report", str(self.report)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def assert_failure(self, manifest: dict, code: str) -> None:
        result = self.verify(manifest)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(code, result.stderr)

    def test_valid_full_band_report_passes_across_markdown_wrap(self) -> None:
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status=ok", result.stdout)

    def test_low_band_metric_change_does_not_change_figure_band(self) -> None:
        before = copy.deepcopy(self.manifest)
        after = copy.deepcopy(self.manifest)
        after["metrics"][0].update({"min_hz": 100, "max_hz": 500})
        figure_before = before["figure_groups"][0]
        figure_after = after["figure_groups"][0]
        self.assertEqual(
            (figure_before["sample_rate_hz"], figure_before["min_hz"], figure_before["max_hz"]),
            (figure_after["sample_rate_hz"], figure_after["min_hz"], figure_after["max_hz"]),
        )
        self.assertEqual(self.verify(before).returncode, 0)
        self.assertEqual(self.verify(after).returncode, 0)

    def test_one_khz_display_fails(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["figure_groups"][0]["max_hz"] = 1000
        self.assert_failure(manifest, "profile-mismatch")

    def test_missing_metadata_fails(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        del manifest["figure_groups"][0]["colormap"]
        self.assert_failure(manifest, "missing-field")

    def test_low_band_metric_cannot_support_full_band_claim(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["claims"][0]["evidence"] = [{"kind": "metric", "id": "low_band_metric"}]
        self.assert_failure(manifest, "unsupported-claim")

    def test_full_band_wording_cannot_be_retyped_as_low_band_high_frequency(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["claims"][0].update(
            {
                "type": "high_frequency",
                "claimed_band_hz": [20, 1000],
                "evidence": [{"kind": "metric", "id": "low_band_metric"}],
            }
        )
        self.assert_failure(manifest, "claim-type-mismatch")

    def test_full_band_wording_cannot_state_zero_to_one_khz(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        claim_text = "이 그림은 전 대역(0–1 kHz) 에너지를 보여준다."
        self.report.write_text(
            "# 결과\n\n![Spectrogram](spectrogram.png)\n\n" + claim_text + "\n",
            encoding="utf-8",
        )
        manifest["claims"][0].update(
            {"id": normalized_claim_id(claim_text), "text": claim_text}
        )
        self.assert_failure(manifest, "contradictory-full-band-range")

    def test_full_band_wording_cannot_hide_zero_to_one_khz_with_tilde(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        claim_text = "이 그림은 전 대역(0~1 kHz) 에너지를 보여준다."
        self.report.write_text(
            "# 결과\n\n![Spectrogram](spectrogram.png)\n\n" + claim_text + "\n",
            encoding="utf-8",
        )
        manifest["claims"][0].update(
            {"id": normalized_claim_id(claim_text), "text": claim_text}
        )
        self.assert_failure(manifest, "contradictory-full-band-range")

    def test_unregistered_band_sensitive_sentence_fails(self) -> None:
        self.report.write_text(
            self.report.read_text(encoding="utf-8") + "\n또한 고주파 성능이 개선됐다.\n",
            encoding="utf-8",
        )
        self.assert_failure(self.manifest, "unregistered-band-claim")

    def test_stale_visual_review_hash_fails(self) -> None:
        write_png(self.png, width=18)
        self.assert_failure(self.manifest, "stale-review")

    def test_truncated_png_fails_even_when_review_hash_is_updated(self) -> None:
        self.png.write_bytes(self.png.read_bytes()[:24])
        manifest = copy.deepcopy(self.manifest)
        manifest["figure_groups"][0]["visual_review"]["png_sha256"] = hashlib.sha256(
            self.png.read_bytes()
        ).hexdigest()
        self.assert_failure(manifest, "invalid-png")

    def test_crc_corrupt_png_fails_even_when_review_hash_is_updated(self) -> None:
        data = bytearray(self.png.read_bytes())
        idat_type = data.index(b"IDAT")
        data[idat_type + 4] ^= 0x01
        self.png.write_bytes(data)
        manifest = copy.deepcopy(self.manifest)
        manifest["figure_groups"][0]["visual_review"]["png_sha256"] = hashlib.sha256(data).hexdigest()
        self.assert_failure(manifest, "invalid-png")

    def test_indexed_png_without_required_palette_fails(self) -> None:
        data = b"\x89PNG\r\n\x1a\n"
        data += png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0))
        data += png_chunk(b"IDAT", zlib.compress(b"\x00\x00"))
        data += png_chunk(b"IEND", b"")
        self.png.write_bytes(data)
        manifest = copy.deepcopy(self.manifest)
        manifest["figure_groups"][0]["visual_review"]["png_sha256"] = hashlib.sha256(data).hexdigest()
        self.assert_failure(manifest, "invalid-png")

    def test_unequal_panel_scales_fail(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["figure_groups"][0]["panels"][1]["vmin_db"] = -60
        self.assert_failure(manifest, "unshared-scale")

    def test_false_visual_check_fails(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["figure_groups"][0]["visual_review"]["colorbar_present"] = False
        self.assert_failure(manifest, "visual-review-failed")

    def test_image_link_inside_code_fence_does_not_count_as_rendered_figure(self) -> None:
        self.report.write_text(
            "# 결과\n\n```md\n![Not rendered](spectrogram.png)\n```\n\n" + CLAIM_TEXT + "\n",
            encoding="utf-8",
        )
        self.assert_failure(self.manifest, "unlinked-figure")

    def test_image_link_inside_tilde_fence_does_not_count_as_rendered_figure(self) -> None:
        self.report.write_text(
            "# 결과\n\n~~~md\n![Not rendered](spectrogram.png)\n~~~\n\n" + CLAIM_TEXT + "\n",
            encoding="utf-8",
        )
        self.assert_failure(self.manifest, "unlinked-figure")

    def test_image_link_inside_html_pre_does_not_count_as_rendered_figure(self) -> None:
        self.report.write_text(
            "# 결과\n\n<pre>![Not rendered](spectrogram.png)</pre>\n\n" + CLAIM_TEXT + "\n",
            encoding="utf-8",
        )
        self.assert_failure(self.manifest, "unlinked-figure")

    def test_backslash_escaped_image_link_does_not_count_as_rendered_figure(self) -> None:
        self.report.write_text(
            "# 결과\n\n\\![Not rendered](spectrogram.png)\n\n" + CLAIM_TEXT + "\n",
            encoding="utf-8",
        )
        self.assert_failure(self.manifest, "unlinked-figure")

    def test_image_link_inside_double_backtick_code_span_does_not_count(self) -> None:
        self.report.write_text(
            "# 결과\n\n``![Not rendered](spectrogram.png)``\n\n" + CLAIM_TEXT + "\n",
            encoding="utf-8",
        )
        self.assert_failure(self.manifest, "unlinked-figure")

    def test_high_frequency_claim_requires_explicit_band(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        high_text = "고주파 성능이 개선됐다."
        self.report.write_text(
            self.report.read_text(encoding="utf-8") + "\n" + high_text + "\n",
            encoding="utf-8",
        )
        manifest["claims"].append(
            {
                "id": normalized_claim_id(high_text),
                "text": high_text,
                "type": "high_frequency",
                "evidence": [{"kind": "figure_group", "id": "comparison"}],
            }
        )
        self.assert_failure(manifest, "missing-claim-band")

    def test_high_frequency_wording_cannot_hide_low_band_annotation(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        high_text = "고주파 성능이 개선됐다."
        self.report.write_text(
            self.report.read_text(encoding="utf-8") + "\n" + high_text + "\n",
            encoding="utf-8",
        )
        manifest["claims"].append(
            {
                "id": normalized_claim_id(high_text),
                "text": high_text,
                "type": "high_frequency",
                "claimed_band_hz": [20, 1000],
                "evidence": [{"kind": "metric", "id": "low_band_metric"}],
            }
        )
        self.assert_failure(manifest, "claim-range-not-in-text")

    def test_unrelated_low_band_range_cannot_annotate_later_high_frequency_term(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        high_text = "저주파 metric(20–1000 Hz)을 계산했고 고주파 성능이 개선됐다."
        self.report.write_text(
            self.report.read_text(encoding="utf-8") + "\n" + high_text + "\n",
            encoding="utf-8",
        )
        manifest["claims"].append(
            {
                "id": normalized_claim_id(high_text),
                "text": high_text,
                "type": "high_frequency",
                "claimed_band_hz": [20, 1000],
                "evidence": [{"kind": "metric", "id": "low_band_metric"}],
            }
        )
        self.assert_failure(manifest, "claim-range-not-in-text")

    def test_high_frequency_claim_with_matching_prose_range_passes(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        high_text = "고주파(8–24 kHz) 에너지를 비교했다."
        self.report.write_text(
            self.report.read_text(encoding="utf-8") + "\n" + high_text + "\n",
            encoding="utf-8",
        )
        manifest["claims"].append(
            {
                "id": normalized_claim_id(high_text),
                "text": high_text,
                "type": "high_frequency",
                "claimed_band_hz": [8000, 24000],
                "evidence": [{"kind": "figure_group", "id": "comparison"}],
            }
        )
        result = self.verify(manifest)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_metric_and_claim_cannot_exceed_nyquist(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        impossible_text = "고주파(30–40 kHz) 에너지를 비교했다."
        self.report.write_text(
            self.report.read_text(encoding="utf-8") + "\n" + impossible_text + "\n",
            encoding="utf-8",
        )
        manifest["metrics"].append({"id": "impossible", "min_hz": 30000, "max_hz": 40000})
        manifest["claims"].append(
            {
                "id": normalized_claim_id(impossible_text),
                "text": impossible_text,
                "type": "high_frequency",
                "claimed_band_hz": [30000, 40000],
                "evidence": [{"kind": "metric", "id": "impossible"}],
            }
        )
        result = self.verify(manifest)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("invalid-range", result.stderr)
        self.assertIn("invalid-claim-band", result.stderr)

    def test_malformed_claim_and_evidence_ids_fail_without_crashing(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["claims"][0]["id"] = ["not", "a", "string"]
        manifest["claims"][0]["evidence"][0]["id"] = {"bad": "id"}
        result = self.verify(manifest)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("invalid-string", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_nul_in_asset_path_fails_without_crashing(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["figure_groups"][0]["png_path"] = "bad\x00.png"
        result = self.verify(manifest)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("invalid-path", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_duplicate_json_field_is_rejected(self) -> None:
        manifest_text = json.dumps(self.manifest, ensure_ascii=False)
        manifest_text = manifest_text.replace('"max_hz": 24000', '"max_hz": 1000, "max_hz": 24000', 1)
        result = self.verify_raw(manifest_text)
        self.assertEqual(result.returncode, 66, result.stdout + result.stderr)
        self.assertIn("reason=duplicate-field", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_symlink_loop_input_path_fails_without_traceback(self) -> None:
        loop_a = self.root / "loop-a.json"
        loop_b = self.root / "loop-b.json"
        loop_a.symlink_to(loop_b.name)
        loop_b.symlink_to(loop_a.name)
        result = subprocess.run(
            [sys.executable, str(VERIFIER), "--manifest", str(loop_a), "--report", str(self.report)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 66, result.stdout + result.stderr)
        self.assertIn("reason=input-error", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
