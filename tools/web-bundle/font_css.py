"""Embed vendored code fonts in standalone HTML without network requests."""
from __future__ import annotations

import base64
from pathlib import Path


def d2coding_font_css() -> str:
    font_dir = Path(__file__).resolve().parent / "fonts" / "d2coding-1.4.0"
    license_text = (font_dir / "LICENSE.md").read_text(encoding="utf-8")
    rules = [f"/* D2Coding 1.4.0 — bundled under the SIL Open Font License.\n{license_text}*/"]
    for face, weight in (("Regular", 400), ("Bold", 700)):
        encoded = base64.b64encode((font_dir / f"D2Coding-{face}.woff2").read_bytes()).decode("ascii")
        rules.append(
            '@font-face { font-family: "D2Coding"; font-style: normal; '
            f"font-weight: {weight}; font-display: swap; "
            'font-feature-settings: "calt" 0, "liga" 0; '
            f'src: url("data:font/woff2;base64,{encoded}") format("woff2"); }}'
        )
    return "\n".join(rules) + "\n"
