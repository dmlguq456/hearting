#!/usr/bin/env python3
"""Render the public GitHub Pages surface: docs/index.html, docs/fleet.html, docs/map.html.

- index.html: the landing page. It leads with what Hearting is for (keeping
  agent work on the user's intent), shows Fleet's real screen pictures near the
  top, explains how a request runs, gives the quick start, and keeps the layer
  map (L0-L6) as a section further down.
- fleet.html: a page about Fleet, centred on the pictures under docs/fleet/.
- map.html: the capability catalog.

Sources: manifest.json (tracks, loops) and harness-manifest.json (capability
census, summaries, taxonomy). The pages are self-contained (no CDN, no
network), share one design system, and are dark-first with a light override.
The Fleet pictures are synthetic demo captures (tools/fleet/tests/readme_captures.py).

Usage:
  python3 tools/render-landing.py          # write docs/index.html, fleet.html, map.html
  python3 tools/render-landing.py --check  # verify the three files are current
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REPO_URL = "https://github.com/dmlguq456/hearting"
SITE_URL = "https://dmlguq456.github.io/hearting/"
INSTALL_CMD = (
    "curl -fsSL https://github.com/dmlguq456/hearting/"
    "releases/latest/download/install.sh | sh"
)
INSTALL_CLAUDE_CMD = INSTALL_CMD + " -s -- --runtime claude"


def load_data() -> dict:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    harness = json.loads((ROOT / "harness-manifest.json").read_text(encoding="utf-8"))
    # A compiler-internal capability is not a product surface: the pages never count or list it.
    caps = {name: spec for name, spec in harness["capabilities"].items()
            if spec["invocation"]["class"] != "compiler-internal"}
    units = harness["units"]
    entries = sorted(
        name for name, spec in caps.items()
        if spec["invocation"]["class"] == "entry-router"
    )
    roles = sorted({spec["role"] for spec in units.values()})
    return {
        "caps": caps,
        "units": units,
        "roles": roles,
        "entries": entries,
        "tracks": manifest["tracks"],
        "loops": manifest["loops"],
        "cap_total": len(caps),
        "entry_total": len(entries),
        "unit_total": len(units),
        "role_total": len(roles),
        "loop_total": len(manifest["loops"]),
    }


# ---------------------------------------------------------------- design system

CSS = r"""
  :root {
    color-scheme: dark light;
    --font-ui: "Pretendard Variable", Pretendard, -apple-system, BlinkMacSystemFont,
               "SF Pro Text", "Segoe UI", "Apple SD Gothic Neo", "Noto Sans KR", system-ui, sans-serif;
    --font-mono: "JetBrains Mono", ui-monospace, "SF Mono", SFMono-Regular, Menlo, monospace;

    --bg: #08080B;
    --bg-2: #0B0B10;
    --panel: rgba(255,255,255,.030);
    --panel-2: rgba(255,255,255,.058);
    --solid: #101016;
    --border: rgba(255,255,255,.085);
    --border-2: rgba(255,255,255,.16);
    --text: #ECECF2;
    --text-2: #9E9EAE;
    --text-3: #6B6B7B;

    --g1: #6D6DF6; --g2: #A855F7; --g3: #22D3EE;
    --accent: #8B8BFA;
    --accent-soft: rgba(109,109,246,.15);
    --ok: #34D399; --warn: #FBBF24; --danger: #FB7185; --info: #60A5FA;

    --term-bg: #0B0E17;
    --grid-line: rgba(255,255,255,.035);
    --wire: rgba(150,150,255,.34);
    --glow-a: rgba(109,109,246,.20);
    --glow-b: rgba(34,211,238,.11);
    --shadow: 0 1px 2px rgba(0,0,0,.5), 0 12px 34px rgba(0,0,0,.42);
    --shot-shadow: 0 2px 4px rgba(0,0,0,.5), 0 24px 70px rgba(0,0,0,.55), 0 0 0 1px rgba(255,255,255,.04);
    --r-lg: 18px; --r-md: 12px; --r-sm: 8px;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #FBFBFD;
      --bg-2: #F4F4F8;
      --panel: rgba(20,20,45,.028);
      --panel-2: rgba(20,20,45,.055);
      --solid: #FFFFFF;
      --border: rgba(20,20,45,.10);
      --border-2: rgba(20,20,45,.18);
      --text: #16161C;
      --text-2: #55555F;
      --text-3: #86868F;
      --accent: #4F4FD4;
      --accent-soft: rgba(109,109,246,.10);
      --grid-line: rgba(20,20,60,.045);
      --wire: rgba(90,90,190,.34);
      --glow-a: rgba(109,109,246,.14);
      --glow-b: rgba(34,180,238,.10);
      --shadow: 0 1px 2px rgba(20,20,45,.05), 0 10px 28px rgba(20,20,45,.07);
      --shot-shadow: 0 2px 4px rgba(20,20,45,.10), 0 22px 60px rgba(20,20,45,.22), 0 0 0 1px rgba(20,20,45,.10);
    }
  }

  * { box-sizing: border-box; margin: 0; }
  html { scroll-behavior: smooth; scroll-padding-top: 76px; }
  body {
    font-family: var(--font-ui); background: var(--bg); color: var(--text);
    line-height: 1.62; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
    font-size: 16px; overflow-x: hidden;
  }
  a { color: inherit; text-decoration: none; }
  code, .mono { font-family: var(--font-mono); font-variant-ligatures: none; }
  img { max-width: 100%; height: auto; }
  ::selection { background: rgba(109,109,246,.32); }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 0 28px; }
  .grad {
    background: linear-gradient(102deg, var(--g1) 4%, var(--g2) 52%, var(--g3) 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }

  /* ── top bar ─────────────────────────────────────────── */
  .top {
    position: sticky; top: 0; z-index: 40; border-bottom: 1px solid var(--border);
    background: color-mix(in srgb, var(--bg) 84%, transparent);
    -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px);
  }
  .top .in { display: flex; align-items: center; gap: 18px; height: 60px; }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 750; letter-spacing: -.02em; font-size: 17px; }
  .brand svg { display: block; }
  .top nav { display: flex; align-items: center; gap: 2px; margin-left: auto; }
  .top nav a {
    padding: 7px 13px; border-radius: 999px; font-size: 14.5px; color: var(--text-2);
    transition: background .15s, color .15s; white-space: nowrap;
  }
  .top nav a:hover { background: var(--panel-2); color: var(--text); }
  .top nav a.on { color: var(--text); background: var(--panel); }
  .top nav a.cta {
    margin-left: 6px; color: #fff; font-weight: 600;
    background: linear-gradient(115deg, var(--g1), var(--g2));
  }
  .top nav a.cta:hover { filter: brightness(1.1); }

  /* ── hero ────────────────────────────────────────────── */
  .hero { position: relative; padding: 56px 0 0; text-align: center; }
  .hero::before {
    content: ""; position: absolute; inset: 0 0 auto 0; height: 560px; pointer-events: none; z-index: -1;
    background:
      radial-gradient(780px 380px at 18% -6%, var(--glow-a), transparent 70%),
      radial-gradient(640px 340px at 86% 0%, var(--glow-b), transparent 72%);
  }
  .eyebrow {
    display: inline-flex; align-items: center; gap: 8px; font-size: 13.5px; font-weight: 600; color: var(--text-2);
    border: 1px solid var(--border-2); background: var(--panel); border-radius: 999px; padding: 5px 14px;
  }
  .eyebrow i { width: 6px; height: 6px; border-radius: 50%; background: linear-gradient(120deg, var(--g1), var(--g3)); }
  .hero h1 {
    margin: 22px auto 0; max-width: 960px;
    font-size: clamp(34px, 5.6vw, 60px); line-height: 1.07; letter-spacing: -.04em; font-weight: 790;
    text-wrap: balance;
  }
  .hero .lede { text-wrap: pretty; max-width: 760px; margin: 20px auto 0; font-size: clamp(16.5px, 1.7vw, 19px); color: var(--text-2); }
  .hero .lede b { color: var(--text); font-weight: 620; }
  .install {
    max-width: 760px; margin: 28px auto 0; display: flex; align-items: stretch; overflow: hidden; text-align: left;
    border: 1px solid var(--border-2); border-radius: var(--r-md);
    background: var(--solid); box-shadow: var(--shadow);
  }
  .install .p { display: flex; align-items: center; padding-left: 14px; color: var(--g2); font-family: var(--font-mono); font-size: 13.5px; }
  .install code {
    flex: 1; min-width: 0; font-size: 13.5px; padding: 14px 12px; color: var(--text);
    overflow-x: auto; white-space: nowrap; scrollbar-width: none;
  }
  .install code::-webkit-scrollbar { display: none; }
  .install button {
    border: 0; border-left: 1px solid var(--border); background: var(--panel);
    color: var(--text-2); font-family: var(--font-ui); font-size: 13.5px; font-weight: 600;
    padding: 0 18px; cursor: pointer; white-space: nowrap; transition: color .15s, background .15s;
  }
  .install button:hover { color: var(--text); background: var(--panel-2); }
  .fine { margin-top: 12px; font-size: 13.5px; color: var(--text-3); }
  .fine a { color: var(--text-2); text-decoration: underline; text-underline-offset: 3px; }
  .btns { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin-top: 22px; }
  .btn {
    display: inline-flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 620;
    border: 1px solid var(--border-2); border-radius: 999px; padding: 10px 20px; background: var(--panel);
    transition: all .15s;
  }
  .btn:hover { background: var(--panel-2); border-color: var(--accent); }
  .btn.primary { color: #fff; border-color: transparent; background: linear-gradient(115deg, var(--g1), var(--g2)); }
  .btn.primary:hover { filter: brightness(1.1); }

  /* screen pictures: real Fleet captures drawn from synthetic demo data */
  .shot {
    display: block; border-radius: 14px; overflow: hidden; background: var(--term-bg);
    box-shadow: var(--shot-shadow); line-height: 0;
  }
  .shot img { display: block; width: 100%; height: auto; }
  .shot.crop img { max-width: none; }
  a.shot { transition: transform .2s ease, box-shadow .2s ease; }
  a.shot:hover { transform: translateY(-3px); }
  .hero-shot { margin-top: 44px; }
  .hero-shot .shot { max-width: 1180px; margin: 0 auto; }
  figcaption, .cap-note { font-size: 13.5px; color: var(--text-3); line-height: 1.5; }
  .hero-shot figcaption { margin-top: 14px; text-align: center; }
  .hero-shot figcaption a { color: var(--text-2); text-decoration: underline; text-underline-offset: 3px; }

  /* ── sections ────────────────────────────────────────── */
  .sec { padding: 92px 0 0; }
  .kick {
    font-size: 12.5px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: var(--accent);
    margin-bottom: 14px; display: flex; align-items: center; gap: 10px;
  }
  .kick::after { content: ""; width: 54px; height: 1px; background: var(--border-2); }
  .sec h2 { font-size: clamp(27px, 3.6vw, 40px); line-height: 1.12; letter-spacing: -.035em; font-weight: 770; max-width: 860px; text-wrap: balance; }
  .sec .lead { margin-top: 16px; max-width: 760px; font-size: 17.5px; color: var(--text-2); }
  .sec .lead b { color: var(--text); font-weight: 620; }
  .sec .lead code, .card code, .qs code, .note code, .split p code, .split .t code {
    font-size: .88em; background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; padding: 1px 6px; color: var(--text);
  }
  .sec > .wrap > .grid, .sec > .wrap > .pipe, .sec > .wrap > .split { margin-top: 38px; }

  .card {
    border: 1px solid var(--border); border-radius: var(--r-lg); padding: 24px 26px;
    background: linear-gradient(160deg, var(--panel-2), var(--panel));
  }
  .card h3 { font-size: 18px; letter-spacing: -.02em; font-weight: 700; }
  .card p { margin-top: 9px; font-size: 15.5px; color: var(--text-2); }
  .card .num { font-family: var(--font-mono); font-size: 12.5px; font-weight: 700; color: var(--accent); margin-bottom: 12px; display: block; letter-spacing: .06em; }
  .card.hot { border-color: transparent; background:
      linear-gradient(var(--bg), var(--bg)) padding-box,
      linear-gradient(130deg, var(--g1), var(--g2), var(--g3)) border-box;
    border: 1px solid transparent; position: relative; }
  .card.hot::before { content: ""; position: absolute; inset: 0; border-radius: inherit; background: var(--accent-soft); pointer-events: none; }
  .card.hot > * { position: relative; }
  .why { display: grid; grid-template-columns: repeat(6, 1fr); gap: 14px; }
  .why .card { grid-column: span 2; }
  .why .card:nth-child(n+4) { grid-column: span 3; }
  .grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }

  /* how a request runs */
  .pipe { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0; }
  .pipe .st { position: relative; padding: 4px 24px 4px 0; }
  .pipe .st:not(:last-child)::after {
    content: ""; position: absolute; right: 8px; top: 15px; width: 8px; height: 8px;
    border-top: 2px solid var(--text-3); border-right: 2px solid var(--text-3); transform: rotate(45deg);
  }
  .pipe .dot {
    width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; margin-bottom: 14px;
    font-family: var(--font-mono); font-size: 13px; font-weight: 700; color: #fff;
    background: linear-gradient(130deg, var(--g1), var(--g2));
  }
  .pipe h3 { font-size: 16.5px; letter-spacing: -.015em; }
  .pipe p { margin-top: 6px; font-size: 14.5px; color: var(--text-2); }
  .fleetband {
    margin-top: 26px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    border: 1px dashed var(--border-2); border-radius: var(--r-md); padding: 13px 18px; background: var(--panel);
    font-size: 15px; color: var(--text-2);
  }
  .fleetband b { color: var(--text); }
  .fleetband a { margin-left: auto; color: var(--accent); font-weight: 620; white-space: nowrap; }
  .split { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; align-items: start; }
  .split h3 { font-size: 17px; letter-spacing: -.015em; margin-bottom: 10px; }
  .split p.t { font-size: 15.5px; color: var(--text-2); margin-top: 10px; }
  .split p.t b { color: var(--text); font-weight: 620; }
  .ladder2 { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 12px; }
  .ladder2 span {
    font-family: var(--font-mono); font-size: 12.5px; padding: 5px 10px; border-radius: 6px;
    border: 1px solid var(--border-2); color: var(--text); background: var(--panel-2);
  }
  .ladder2 span.first { border-color: var(--ok); color: var(--ok); background: transparent; }
  .ladder2 span.top { color: var(--danger); border-color: rgba(251,113,133,.4); background: rgba(251,113,133,.08); }

  /* the intent card the user actually approves */
  .rcard {
    border: 1px solid var(--border-2); border-radius: var(--r-md); background: var(--solid);
    padding: 15px 17px; box-shadow: var(--shadow);
  }
  .rcard .rh {
    font-size: 12px; font-weight: 700; letter-spacing: .09em; text-transform: uppercase;
    color: var(--text-3); margin-bottom: 10px; display: flex; align-items: center; gap: 8px;
  }
  .rcard .rh::after { content: ""; flex: 1; height: 1px; background: var(--border); }
  .rrow { display: flex; gap: 12px; font-size: 14px; line-height: 1.62; padding: 3px 0; }
  .rrow .rk {
    font-family: var(--font-mono); font-size: 11.5px; letter-spacing: .07em; text-transform: uppercase;
    color: var(--text-3); width: 70px; flex: none; padding-top: 3px;
  }
  .rrow .rv { color: var(--text-2); min-width: 0; }
  .rrow .rv b { color: var(--text); font-weight: 620; }
  .rrow .rv code {
    font-size: 13px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 5px; padding: 1px 6px; color: var(--accent);
  }
  .rops {
    display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-top: 11px; padding-top: 10px;
    border-top: 1px dashed var(--border);
  }
  .rop {
    font-size: 12.5px; font-weight: 600; border: 1px solid var(--border); border-radius: 999px;
    padding: 4px 13px; color: var(--text-3);
  }
  .rop.on { color: #fff; border-color: transparent; background: linear-gradient(120deg, var(--g1), var(--g2)); }
  .rops .rnote { font-size: 12.5px; color: var(--text-3); margin-left: auto; }

  /* Fleet showcase */
  .tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
  .card p a { color: var(--accent); white-space: nowrap; }
  .tile figcaption { margin-top: 12px; font-size: 14.5px; color: var(--text-2); }
  .card p a { color: var(--accent); white-space: nowrap; }
  .tile figcaption b { color: var(--text); display: block; font-size: 15.5px; margin-bottom: 2px; }
  .pill-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 26px; }
  .pill-row span {
    display: inline-flex; align-items: center; gap: 8px; font-size: 14px; color: var(--text-2);
    border: 1px solid var(--border); border-radius: 999px; padding: 6px 14px; background: var(--panel);
  }
  .pill-row code { font-size: .9em; }
  .pill-row span::before { content: "✓"; color: var(--ok); font-size: 12px; }
  .cmdrow { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-top: 28px; }
  .cmd {
    display: inline-flex; align-items: center; gap: 10px; font-family: var(--font-mono); font-size: 15px;
    border: 1px solid var(--border-2); border-radius: var(--r-md); padding: 10px 16px; background: var(--solid); box-shadow: var(--shadow);
  }
  .cmd i { font-style: normal; color: var(--g2); }

  /* quick start */
  .qs { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; counter-reset: q; }
  .qs .card { counter-increment: q; min-width: 0; }
  .qs .card:first-child { grid-column: 1 / -1; }
  .qs .card::before {
    content: counter(q); display: grid; place-items: center; width: 28px; height: 28px; border-radius: 50%;
    margin-bottom: 14px; background: var(--panel-2); border: 1px solid var(--border-2);
    font-size: 13px; font-weight: 700; color: var(--text);
  }
  .qs .card p { font-size: 14.5px; }
  .qs pre, pre.code {
    margin-top: 12px; font-family: var(--font-mono); font-size: 12.5px; line-height: 1.7; color: var(--text);
    background: var(--solid); border: 1px solid var(--border); border-radius: var(--r-sm); padding: 10px 12px;
    overflow-x: auto; white-space: pre; scrollbar-width: thin;
  }
  .note {
    margin-top: 18px; border: 1px solid var(--border); border-left: 3px solid var(--warn); border-radius: var(--r-sm);
    padding: 13px 17px; background: var(--panel); font-size: 15px; color: var(--text-2);
  }
  .note b { color: var(--text); }
  .note a { color: var(--accent); text-decoration: underline; text-underline-offset: 3px; }
  .links { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .links a {
    border: 1px solid var(--border); border-radius: var(--r-md); padding: 16px 18px; background: var(--panel);
    transition: all .15s; display: block;
  }
  .links a:hover { border-color: var(--border-2); background: var(--panel-2); }
  .links b { display: block; font-size: 16px; letter-spacing: -.01em; }
  .links span { font-size: 14px; color: var(--text-3); }
  .foot { margin-top: 96px; padding: 34px 0 56px; border-top: 1px solid var(--border); font-size: 14px; color: var(--text-3); }
  .foot .def { max-width: 640px; }
  .foot .def b { color: var(--text-2); }
  .foot .row { margin-top: 18px; display: flex; flex-wrap: wrap; gap: 8px 22px; }
  .foot .row a { color: var(--text-2); }
  .foot .row a:hover { color: var(--text); }

  /* ── fleet page ──────────────────────────────────────── */
  .scr { padding: 46px 0 52px; }
  .scr + .scr { border-top: 1px solid var(--border); }
  .scr-head { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr); gap: 44px; align-items: start; margin-bottom: 26px; }
  .scr .tag { font-family: var(--font-mono); font-size: 12px; font-weight: 700; letter-spacing: .08em; color: var(--accent); }
  .scr h3 { margin-top: 8px; font-size: clamp(24px, 3vw, 30px); letter-spacing: -.03em; line-height: 1.15; }
  .scr p { font-size: 16.5px; color: var(--text-2); }
  .scr code { font-size: .88em; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 1px 6px; color: var(--text); }
  .scr ul { margin: 12px 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 7px; }
  .scr li { position: relative; padding-left: 18px; font-size: 15px; color: var(--text-2); }
  .scr li::before { content: ""; position: absolute; left: 3px; top: 10px; width: 5px; height: 5px; border-radius: 50%; background: var(--accent); }
  .scr li b { color: var(--text); font-weight: 620; }
  .views { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  .views .card { padding: 18px 20px; }
  .views h3 { font-family: var(--font-mono); font-size: 14px; letter-spacing: .04em; color: var(--accent); }
  .views p { font-size: 14.5px; margin-top: 7px; }
  .narrow { display: grid; grid-template-columns: 1fr 1.25fr; gap: 36px; align-items: center; margin-top: 40px; }
  .narrow h3 { font-size: 22px; letter-spacing: -.02em; }
  .narrow p { margin-top: 10px; color: var(--text-2); }
  .keys { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }
  .keys span { font-size: 13.5px; color: var(--text-2); }
  .keys kbd {
    font-family: var(--font-mono); font-size: 12px; color: var(--text); border: 1px solid var(--border-2);
    border-bottom-width: 2px; border-radius: 6px; padding: 1px 7px; background: var(--panel-2); margin-right: 5px;
  }
  .limits { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
  .limits .big { font-size: 34px; font-weight: 780; letter-spacing: -.04em; line-height: 1.1; }
  .limits .big small { font-size: 15px; font-weight: 500; color: var(--text-3); letter-spacing: 0; margin-left: 4px; }
  .limits .card p { font-size: 14.5px; }
  .cta-band {
    margin-top: 96px; text-align: center; border: 1px solid var(--border-2); border-radius: 22px; padding: 54px 28px;
    background:
      radial-gradient(520px 220px at 20% 0%, var(--glow-a), transparent 70%),
      radial-gradient(420px 200px at 85% 100%, var(--glow-b), transparent 72%),
      var(--panel);
  }
  .cta-band h2 { margin: 0 auto; max-width: 640px; font-size: clamp(26px, 3.4vw, 36px); letter-spacing: -.035em; line-height: 1.15; }
  .cta-band p { margin: 14px auto 0; max-width: 560px; color: var(--text-2); font-size: 16.5px; }

  /* ── layer map ───────────────────────────────────────── */
  .mapsec { margin-top: 38px; }
  .foldhint {
    display: flex; align-items: center; gap: 9px; margin: 0 0 20px; width: max-content; max-width: 100%;
    font-size: 13px; color: var(--text-3);
    border: 1px solid var(--border); border-radius: 999px; padding: 7px 16px; background: var(--panel);
  }
  .foldhint b { color: var(--text-2); font-weight: 650; white-space: nowrap; }
  .diagram {
    position: relative; padding: 30px; border: 1px solid var(--border); border-radius: 24px;
    background:
      radial-gradient(760px 420px at 22% -6%, var(--glow-a), transparent 68%),
      radial-gradient(620px 380px at 88% 6%, var(--glow-b), transparent 70%),
      linear-gradient(var(--grid-line) 1px, transparent 1px) 0 0 / 100% 34px,
      linear-gradient(90deg, var(--grid-line) 1px, transparent 1px) 0 0 / 34px 100%,
      var(--bg-2);
  }
  #wires { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 0; overflow: visible; }
  .rows { position: relative; z-index: 1; display: grid; grid-template-columns: 236px minmax(0,1fr) 236px; gap: 22px; align-items: start; }
  .col:not(.main) .rail { position: sticky; top: 80px; }
  .col { display: flex; flex-direction: column; gap: 26px; }
  .col.main { gap: 30px; }

  .node {
    border: 1px solid var(--border); border-radius: var(--r-lg); background: var(--solid);
    padding: 20px 22px; box-shadow: var(--shadow); position: relative;
  }
  .node.solid { background: linear-gradient(165deg, var(--panel-2), var(--solid)); }
  .node > .head { display: flex; align-items: center; gap: 10px; margin-bottom: 0; flex-wrap: wrap; }
  .node > .head .lv {
    font-family: var(--font-mono); font-size: 12px; font-weight: 700; letter-spacing: .06em;
    color: var(--accent); border: 1px solid var(--accent); border-radius: 5px; padding: 2px 7px;
  }
  .node > .head h3 { font-size: 18.5px; letter-spacing: -.015em; font-weight: 700; }

  .utter { display: flex; align-items: center; gap: 14px; }
  .utter .av {
    width: 34px; height: 34px; border-radius: 50%; flex: none; display: grid; place-items: center;
    border: 1px solid var(--border-2); background: var(--panel-2); font-size: 11px; color: var(--accent);
  }
  .utter .say { font-size: 17px; }
  .utter .say em { font-style: normal; color: var(--text-3); }
  .utter .tail { margin-left: auto; flex: none; white-space: nowrap; font-size: 12px; color: var(--text-3); font-family: var(--font-mono); text-align: right; line-height: 1.5; }

  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    font-family: var(--font-mono); font-size: 13px; color: var(--text-2);
    border: 1px solid var(--border); border-radius: 6px; padding: 5px 10px; background: var(--panel);
    white-space: nowrap; transition: all .14s;
  }
  button.chip { cursor: pointer; font-family: var(--font-mono); }
  button.chip:hover { color: var(--text); border-color: var(--border-2); background: var(--panel-2); transform: translateY(-1px); }
  .chip.entry { color: var(--text); border-color: var(--border-2); background: var(--panel-2); font-weight: 600; }
  .chip.gate { color: var(--warn); border-color: rgba(251,191,36,.34); background: rgba(251,191,36,.07); }

  .contract { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 14px; }
  .sub { border: 1px solid var(--border); border-radius: var(--r-md); padding: 14px 15px; background: var(--panel); }
  .sub .t {
    font-size: 12px; font-weight: 700; letter-spacing: .09em; text-transform: uppercase;
    color: var(--text-3); margin-bottom: 11px;
  }
  .sub p { font-size: 13.5px; color: var(--text-2); margin-top: 10px; }
  .ladder { display: flex; align-items: center; gap: 3px; flex-wrap: wrap; }
  .ladder .st {
    font-family: var(--font-mono); font-size: 12.5px; padding: 4px 9px; border-radius: 5px; cursor: pointer;
    border: 1px solid var(--border); color: var(--text-3); background: var(--panel);
  }
  .ladder .st.on { color: var(--text); border-color: var(--border-2); background: var(--panel-2); }
  .ladder .st.top { color: var(--danger); border-color: rgba(251,113,133,.36); background: rgba(251,113,133,.08); }

  /* progressive disclosure: every layer is a block you open */
  .fold-sum { font-size: 14.5px; color: var(--text-2); line-height: 1.55; margin-top: 7px; }
  .rail .fold-sum { font-size: 13.5px; margin-top: 6px; }
  .foldable > .head, .foldable > .rh { cursor: pointer; user-select: none; }
  .chev {
    margin-left: auto; flex: none; width: 27px; height: 27px; border-radius: 50%;
    border: 1px solid var(--border-2); background: var(--panel); cursor: pointer;
    display: grid; place-items: center; color: var(--text-2); font: inherit; font-size: 12px;
    transition: transform .24s ease, background .15s, color .15s;
  }
  .foldable > .head:hover .chev, .foldable > .rh:hover .chev { background: var(--panel-2); color: var(--text); }
  .foldable.is-open .chev { transform: rotate(180deg); }
  .loops-head .chev { order: 9; margin-left: 0; }
  .loops .fold-sum { margin-top: 9px; }
  .fold-body {
    display: grid; grid-template-rows: 0fr; opacity: 0; margin-top: 0;
    transition: grid-template-rows .3s ease, opacity .22s ease, margin-top .3s ease;
  }
  .fold-body > .fold-inner { overflow: hidden; min-height: 0; }
  .foldable.is-open > .fold-body { grid-template-rows: 1fr; opacity: 1; margin-top: 17px; }
  @media (prefers-reduced-motion: reduce) {
    .fold-body, .chev, a.shot { transition: none; }
    html { scroll-behavior: auto; }
  }

  .tracks { display: flex; flex-direction: column; gap: 8px; }
  .track {
    display: flex; align-items: stretch; border-radius: 9px; overflow: hidden;
    border: 1px solid color-mix(in srgb, var(--tc) 26%, transparent);
    background: color-mix(in srgb, var(--tc) 7%, transparent);
  }
  .track .lab {
    width: 132px; flex: none; display: flex; align-items: center; padding: 11px 13px;
    font-size: 13.5px; font-weight: 650; color: var(--tc); letter-spacing: -.01em;
    border-right: 1px solid color-mix(in srgb, var(--tc) 22%, transparent);
  }
  .track .flow { flex: 1; min-width: 0; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 11px 13px; }
  .track .arw { color: var(--text-3); font-size: 12.5px; }

  /* dispatch */
  .fabric { display: grid; grid-template-columns: 1.15fr 1fr; gap: 16px; }
  .tree { display: flex; flex-direction: column; gap: 9px; }
  .tier { display: flex; align-items: center; gap: 10px; }
  .tier .d {
    font-family: var(--font-mono); font-size: 11.5px; font-weight: 700; color: var(--text-3);
    border: 1px solid var(--border); border-radius: 5px; padding: 3px 7px; flex: none; letter-spacing: .04em;
  }
  .tier .box {
    flex: 1; min-width: 0; border: 1px solid var(--border); border-radius: 9px; padding: 10px 13px;
    background: var(--panel); font-size: 14px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    cursor: pointer; text-align: left; color: inherit; font-family: inherit; transition: all .14s;
  }
  .tier .box:hover { border-color: var(--border-2); background: var(--panel-2); }
  .tier .box b { font-weight: 650; }
  .tier .box .m { font-family: var(--font-mono); font-size: 12px; color: var(--text-3); margin-left: auto; }
  .tier.w2 .box { border-color: var(--border-2); background: var(--panel-2); }
  .nway { padding-left: 34px; }
  .nway .nh { font-size: 12px; color: var(--text-3); font-family: var(--font-mono); margin-bottom: 6px; letter-spacing: .04em; }
  .legs { display: flex; gap: 7px; }
  .leg {
    flex: 1; border: 1px dashed var(--border-2); border-radius: 8px; padding: 9px 9px;
    font-family: var(--font-mono); font-size: 12px; color: var(--text-2); background: var(--panel);
    text-align: center; cursor: pointer; transition: all .14s;
  }
  .leg:hover { border-style: solid; background: var(--panel-2); color: var(--text); }
  .fall { display: flex; flex-direction: column; gap: 6px; }
  .fall .hop {
    display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 12.5px;
    color: var(--text-2); border: 1px solid var(--border); border-radius: 7px; padding: 8px 11px;
    background: var(--panel); cursor: pointer; width: 100%; text-align: left; transition: all .14s;
  }
  .fall .hop:hover { border-color: var(--border-2); background: var(--panel-2); }
  .fall .hop .i { width: 5px; height: 5px; border-radius: 50%; background: var(--ok); flex: none; }
  .fall .hop.last .i { background: var(--text-3); }
  .fall .hop .x { margin-left: auto; color: var(--text-3); font-size: 11.5px; }

  /* model budgets */
  .tiers { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .prof-row { display: flex; flex-direction: column; gap: 7px; }
  .pcard {
    display: block; border: 1px solid var(--border); border-radius: 9px;
    padding: 11px 13px; background: var(--panel); cursor: pointer; text-align: left; color: inherit;
    font-family: inherit; transition: all .14s; width: 100%;
  }
  .pcard:hover { border-color: var(--border-2); background: var(--panel-2); }
  .pcard .pn { font-family: var(--font-mono); font-size: 13.5px; font-weight: 650; }
  .pcard .pd { display: block; font-size: 12.5px; color: var(--text-3); margin-top: 2px; line-height: 1.45; }
  .pcard.deep .pn { color: var(--g2); }
  .pcard.bal .pn { color: var(--accent); }
  .pcard.light .pn { color: var(--g3); }
  .pcard.mini .pn { color: var(--text-3); }
  .pcard.mini { opacity: .75; }

  /* artifacts */
  .flowline { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 13px; }
  .flowline .fx {
    font-family: var(--font-mono); font-size: 13px; border: 1px solid var(--border-2); border-radius: 7px;
    padding: 6px 12px; background: var(--panel-2); color: var(--text); cursor: pointer;
  }
  .flowline .fx:hover { border-color: var(--accent); }
  .flowline .fa { color: var(--text-3); font-size: 13.5px; }
  .flowline .fn { font-size: 12.5px; color: var(--text-3); margin-left: 6px; }
  .buckets { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }
  .bucket { border: 1px solid var(--border); border-radius: 9px; padding: 12px 10px; background: var(--panel); text-align: center; }
  .bucket b { display: block; font-family: var(--font-mono); font-size: 12.5px; color: var(--text); }
  .bucket span { font-size: 12px; color: var(--text-3); }
  .bucket .ow { display: block; font-size: 11px; color: var(--accent); font-family: var(--font-mono); margin-top: 3px; }
  .bucket.hi { border-color: var(--border-2); background: var(--panel-2); }
  .rules { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 12px; }
  .rule {
    font-size: 12.5px; color: var(--text-2); border: 1px solid var(--border); border-radius: 7px;
    padding: 6px 11px; background: var(--panel); display: flex; align-items: center; gap: 6px;
  }
  .rule::before { content: "✓"; color: var(--ok); font-size: 11.5px; }

  /* side rails */
  .rail { border: 1px solid var(--border); border-radius: var(--r-lg); background: var(--solid); padding: 17px 18px; box-shadow: var(--shadow); }
  .rail .rh { display: flex; align-items: center; gap: 9px; margin-bottom: 0; }
  .rail .rh h4 { font-size: 15px; font-weight: 700; letter-spacing: -.01em; }
  .rail .rh .tag { font-family: var(--font-mono); font-size: 11px; color: var(--text-3); letter-spacing: .05em; }
  .rail .rd { font-size: 13px; color: var(--text-3); margin-bottom: 14px; line-height: 1.5; }
  .rail .thumb { margin-top: 14px; }
  .rail .thumb .shot { border-radius: 9px; }
  .rail .rlink { display: inline-block; margin-top: 12px; font-size: 13.5px; font-weight: 620; color: var(--accent); }
  .spine { display: flex; flex-direction: column; }
  .step { position: relative; padding-left: 23px; padding-bottom: 15px; }
  .step:last-child { padding-bottom: 0; }
  .step::before {
    content: ""; position: absolute; left: 4px; top: 5px; width: 8px; height: 8px; border-radius: 50%;
    background: var(--bg-2); border: 1.5px solid var(--accent);
  }
  .step:not(:last-child)::after {
    content: ""; position: absolute; left: 7.5px; top: 15px; bottom: 2px; width: 1px;
    background: linear-gradient(180deg, var(--accent), var(--border));
  }
  .step .sn { font-size: 13.5px; font-weight: 650; color: var(--text); }
  .step .sb { font-size: 12px; color: var(--text-3); line-height: 1.5; margin-top: 2px; }
  .step .sb code { font-size: 11.5px; color: var(--text-2); }

  /* loops strip */
  .loops { position: relative; z-index: 1; margin-top: 28px; }
  .loops-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
  .loops-head .ln {
    font-family: var(--font-mono); font-size: 12px; font-weight: 700; letter-spacing: .06em;
    color: var(--accent); border: 1px solid var(--accent); border-radius: 5px; padding: 2px 7px;
  }
  .loops-head h3 { font-size: 17px; font-weight: 700; }
  .loops-head::after { content: ""; flex: 1; height: 1px; background: var(--border); }
  .loop-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 9px; }
  .loop {
    border: 1px dashed var(--border-2); border-radius: var(--r-md); padding: 13px 14px; background: var(--panel);
    transition: all .14s; cursor: pointer; text-align: left; color: inherit; font-family: inherit; width: 100%;
  }
  .loop:hover { border-style: solid; background: var(--panel-2); }
  .loop .n { font-size: 14px; font-weight: 650; display: flex; align-items: center; gap: 6px; }
  .loop .n .k { font-family: var(--font-mono); font-size: 11px; color: var(--text-3); margin-left: auto; }
  .loop .b { font-size: 12.5px; color: var(--text-3); margin-top: 4px; line-height: 1.45;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }

  /* inspector */
  .inspector {
    position: fixed; right: 22px; bottom: 20px; width: 384px; z-index: 50;
    border: 1px solid var(--border-2); border-radius: var(--r-lg); background: var(--solid);
    box-shadow: 0 8px 40px rgba(0,0,0,.45); padding: 17px 19px;
    opacity: 0; transform: translateY(8px); pointer-events: none; transition: opacity .18s, transform .18s;
  }
  .inspector.on { opacity: 1; transform: none; pointer-events: auto; }
  .inspector .ih { display: flex; align-items: center; gap: 8px; }
  .inspector .ih .k {
    font-size: 11px; font-weight: 700; letter-spacing: .07em; text-transform: uppercase;
    color: var(--accent); border: 1px solid var(--accent); border-radius: 4px; padding: 2px 6px; white-space: nowrap;
  }
  .inspector .ih .t { font-family: var(--font-mono); font-size: 14.5px; font-weight: 650; }
  .inspector .ih button {
    margin-left: auto; border: 0; background: transparent; color: var(--text-3);
    font-size: 15px; cursor: pointer; line-height: 1; padding: 0 2px;
  }
  .inspector .ib { font-size: 14px; color: var(--text-2); margin-top: 9px; line-height: 1.55; }
  .inspector .im { font-family: var(--font-mono); font-size: 12px; color: var(--text-3); margin-top: 9px; }

  /* ── capability catalog page ─────────────────────────── */
  .maphead { padding: 52px 0 6px; }
  .maphead h1 { font-size: clamp(28px, 4.2vw, 44px); letter-spacing: -.035em; line-height: 1.1; }
  .maphead p { margin-top: 14px; color: var(--text-2); max-width: 660px; font-size: 17px; }
  .stats { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 22px; }
  .stat { border: 1px solid var(--border); border-radius: var(--r-md); background: var(--panel); padding: 10px 16px; min-width: 100px; }
  .stat b { display: block; font-size: 23px; letter-spacing: -.02em; }
  .stat span { font-size: 13px; color: var(--text-3); }
  .cap-group { margin-top: 34px; }
  .cap-group h3 { font-size: 17px; letter-spacing: -.015em; }
  .cap-group .desc { font-size: 13.5px; color: var(--text-3); margin: 3px 0 13px; }
  .cap-grid { display: grid; gap: 9px; grid-template-columns: repeat(auto-fill, minmax(272px, 1fr)); }
  .cap { border: 1px solid var(--border); border-radius: var(--r-md); background: var(--panel); padding: 13px 15px; transition: all .15s; }
  .cap:hover { border-color: var(--border-2); background: var(--panel-2); }
  .cap .name { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
  .cap .name .mono { font-size: 14px; font-weight: 650; color: var(--text); }
  .cap .pill {
    flex: none; font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase;
    border-radius: 999px; padding: 3px 8px; color: #fff; background: linear-gradient(120deg, var(--g1), var(--g2));
    white-space: nowrap;
  }
  .cap .pill.stage { color: var(--text-3); background: var(--panel-2); border: 1px solid var(--border); }
  .cap p { font-size: 13.5px; color: var(--text-2); margin-top: 6px;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }

  /* ── responsive ──────────────────────────────────────── */
  @media (max-width: 1100px) {
    .rows { grid-template-columns: minmax(0,1fr); }
    .col.main { order: -1; }
    .col:not(.main) .rail { position: static; }
    .fabric, .contract, .tiers { grid-template-columns: 1fr; }
    .buckets { grid-template-columns: repeat(3, 1fr); }
    .loop-grid { grid-template-columns: repeat(3, 1fr); }
    .qs { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .views { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 900px) {
    .scr-head { grid-template-columns: minmax(0, 1fr); gap: 12px; }
    .scr { padding: 32px 0 36px; }
    .narrow { grid-template-columns: 1fr; }
    .pipe { grid-template-columns: 1fr; gap: 22px; }
    .pipe .st { padding: 0 0 0 46px; }
    .pipe .st:not(:last-child)::after { display: none; }
    .pipe .dot { position: absolute; left: 0; top: 0; margin: 0; }
    .split { grid-template-columns: 1fr; }
    .tiles { grid-template-columns: 1fr; }
    .limits, .grid3, .links { grid-template-columns: 1fr; }
    .why { grid-template-columns: 1fr; }
    .why .card, .why .card:nth-child(n+4) { grid-column: auto; }
  }
  @media (max-width: 640px) {
    .wrap { padding: 0 18px; }
    .top .in { gap: 10px; }
    .top nav a { padding: 6px 9px; font-size: 13.5px; }
    .top nav a.hide-sm { display: none; }
    .top nav a.cta { margin-left: 2px; }
    .hero { padding-top: 36px; }
    .hero-shot { margin-top: 30px; }
    .sec { padding-top: 64px; }
    .qs, .views { grid-template-columns: minmax(0, 1fr); }
    .buckets { grid-template-columns: repeat(2, 1fr); }
    .loop-grid { grid-template-columns: 1fr; }
    .diagram { padding: 14px; border-radius: 16px; }
    .node { padding: 16px 16px; }
    .utter .tail { display: none; }
    .track { flex-direction: column; }
    .track .lab { width: auto; border-right: 0; border-bottom: 1px solid var(--border); }
    .inspector { left: 14px; right: 14px; bottom: 14px; width: auto; }
    .install .p { display: none; }
    .install code { font-size: 12px; }
    .install button { padding: 0 13px; }
    .cta-band { padding: 38px 20px; margin-top: 70px; }
    .fleetband a { margin-left: 0; }
    .rrow .rk { width: 58px; }
  }
"""

# The mark is a dry-stone wall in section: two face stones standing apart, and the
# hearting packed between them. The packing is what the wall's stability actually
# rests on, and it is the part nobody sees — which is the whole point of the name.
LOGO = (
    '<svg width="23" height="23" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
    '<defs><linearGradient id="lg" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">'
    '<stop offset="0" stop-color="#6D6DF6"/><stop offset=".55" stop-color="#A855F7"/>'
    '<stop offset="1" stop-color="#22D3EE"/></linearGradient></defs>'
    '<rect x="1.1" y="2.2" width="4.2" height="19.6" rx="1.5" fill="url(#lg)"/>'
    '<rect x="18.7" y="2.2" width="4.2" height="19.6" rx="1.5" fill="url(#lg)"/>'
    '<path d="M7.0 3.1 17.4 4.3 16.6 9.0 7.9 8.2Z" fill="url(#lg)" opacity=".8"/>'
    '<path d="M7.6 10.0 16.9 10.6 17.2 15.1 7.2 14.6Z" fill="url(#lg)" opacity=".52"/>'
    '<path d="M7.2 16.3 16.5 15.6 17.3 21.0 8.0 21.6Z" fill="url(#lg)" opacity=".8"/></svg>'
)


def copy_js(target_id: str) -> str:
    return (
        f"navigator.clipboard.writeText(document.getElementById('{target_id}').textContent)"
        ".then(()=>{this.textContent='Copied';setTimeout(()=>this.textContent='Copy',1400)})"
    )


TRACK_COLOR = {
    "--cat-1": "#A47AE0", "--cat-2": "#6B96F0", "--cat-3": "#5FC684",
    "--cat-4": "#C07FE0", "--cat-5": "#D9A93C", "--cat-6": "#85AEFF",
    "--cat-7": "#F87171", "--cat-8": "#D1D5DB",
}

# ----------------------------------------------------------------- Fleet pictures
# docs/fleet/*.png and walkthrough.gif are drawn from synthetic demo rows by
# tools/fleet/tests/readme_captures.py. Sizes are the drawn sizes (the test checks them).

SHOTS = {
    "overview": (1440, 810, "Fleet's Explorer OVERVIEW: eight sessions in four projects across Claude Code, Codex, and OpenCode, "
                 "grouped by project, each project's line showing its open requests, waiting answers, and latest result; every "
                 "session with its model, context gauge, and what it is doing now; one session waiting for an answer, two open "
                 "requests with their stage lines, and per-host CPU and GPU use at the bottom left."),
    "routes": (1440, 720, "ROUTES: two open requests, a code request in its execute stage and a lab request running its eval-asr "
               "worker, each with the worker's model and elapsed time."),
    "questions": (1440, 720, "QUESTIONS: one decision a Claude Code session has been waiting on for seven minutes, with since when and "
                  "how to answer it; one older approval wait from a Codex session listed under it; and three questions answered today."),
    "session": (1440, 720, "A session page: one Claude Code session's state, context gauge, and current activity, its request, the "
                "OpenCode session it steers, and the GPU it holds."),
    "resources": (1440, 720, "RESOURCES: two hosts with per-GPU utilisation and memory, the session and command behind each GPU "
                  "process, and CPU and memory use."),
    "output": (1440, 810, "A request's page: the stage graph with setup done, eval-asr running, eval-sep and report pending, and "
               "below it the owner's raw output listing an edit, a shell command, and their results."),
    "narrow": (900, 720, "Fleet's OVERVIEW in a 100-column terminal: the same sessions, projects, and request lines in a narrower layout."),
}
GIF_ALT = ("Fleet walk-through in four steps: the OVERVIEW of all sessions, the ROUTES list of open requests, one session's "
           "page, and one request's stage graph with the owner's raw output.")


def shot(name: str, eager: bool = False) -> str:
    w, h, alt = SHOTS[name]
    lazy = "" if eager else ' loading="lazy"'
    return (f'<a class="shot" href="fleet/{name}.png" target="_blank" rel="noopener">'
            f'<img src="fleet/{name}.png" width="{w}" height="{h}" alt="{html.escape(alt, quote=True)}"{lazy} /></a>')


def crop(name: str, x0: int, y0: int, w: int, h: int, href: str) -> str:
    """A window onto one drawn screen (the right-hand pane), at near full scale so the text stays readable."""
    iw, ih, alt = SHOTS[name]
    style = (f"width:{iw / w * 100:.4f}%;margin-left:-{x0 / w * 100:.4f}%;margin-top:-{y0 / w * 100:.4f}%")
    return (f'<a class="shot crop" href="{href}" style="aspect-ratio:{w}/{h}">'
            f'<img src="fleet/{name}.png" width="{iw}" height="{ih}" style="{style}" '
            f'alt="{html.escape(alt, quote=True)}" loading="lazy" /></a>')


def gif(href: str = "") -> str:
    if href:
        return (f'<a class="shot" href="{href}"><img src="fleet/walkthrough.gif" width="1440" height="810" '
                f'alt="{html.escape(GIF_ALT, quote=True)}" /></a>')
    return (f'<div class="shot"><img src="fleet/walkthrough.gif" width="1440" height="810" '
            f'alt="{html.escape(GIF_ALT, quote=True)}" /></div>')


# ------------------------------------------------------------------ page chrome

def topbar(active: str) -> str:
    def a(key: str, href: str, label: str, extra: str = "") -> str:
        cls = " ".join(x for x in ("on" if key == active else "", extra) if x)
        return f'<a href="{href}"' + (f' class="{cls}"' if cls else "") + f'>{label}</a>'

    return f"""<header class="top"><div class="wrap in">
    <a class="brand" href="index.html">{LOGO}<span>Hearting</span></a>
    <nav aria-label="Main">
      {a("why", "index.html#why", "Why", "hide-sm")}
      {a("fleet", "fleet.html", "Fleet")}
      {a("start", "index.html#quickstart", "Quick start", "hide-sm")}
      {a("map", "index.html#map", "Layer map", "hide-sm")}
      {a("catalog", "map.html", "Capabilities", "hide-sm")}
      <a class="cta" href="{REPO_URL}">GitHub &#8599;</a>
    </nav>
  </div></header>"""


def footer() -> str:
    return f"""<footer class="foot"><div class="wrap">
    <div class="def"><b>hearting</b> &nbsp;<i>(n.)</i> &mdash; the small stones packed by hand between the two
    faces of a dry stone wall. Unseen from either side, and the reason the wall stands.</div>
    <div class="row">
      <a href="index.html">Home</a><a href="fleet.html">Fleet</a><a href="map.html">Capabilities</a>
      <a href="{REPO_URL}">GitHub</a><a href="{REPO_URL}/releases">Releases</a>
      <a href="{REPO_URL}/blob/main/AGENTS.md">Purpose</a><span>MIT licensed</span>
    </div>
  </div></footer>"""


def page(title: str, description: str, body: str, active: str, scripts: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description, quote=True)}" />
<meta property="og:title" content="{html.escape(title, quote=True)}" />
<meta property="og:description" content="{html.escape(description, quote=True)}" />
<meta property="og:image" content="{SITE_URL}fleet/overview.png" />
<!-- GENERATED by tools/render-landing.py — DO NOT EDIT BY HAND. -->
<style>{CSS}</style>
</head>
<body>
{topbar(active)}
{body}
{footer()}
{scripts}
</body>
</html>
"""


# ------------------------------------------------------------------- layer map

ARTIFACT_BUCKETS = [
    ("plans/", "plan, logs, evidence, final report", "autopilot-code", True),
    ("research/", "surveys and external evidence", "autopilot-research", False),
    ("analysis_project/", "analysis of existing code", "analyze-project", False),
    ("spec/", "the blueprint, one current revision", "autopilot-spec", True),
    ("documents/", "drafted documents", "draft / refine", False),
]

ARTIFACT_RULES = [
    "one root per project, shared by every runtime and git worktree",
    "each request is one cycle under campaigns/&lt;campaign&gt;/&lt;cycle&gt;/artifacts/",
    "the owning capability writes each artifact",
    "a spec revision keeps the previous one",
]

MEMORY_STEPS = [
    ("One local store", "SQLite with full-text search, including CJK, shared by every session, project, "
     "and runtime on the machine."),
    ("Headlines first", "Where the runtime supports it, each prompt brings a few short candidate headlines. "
     "The agent reads a record in full before using it."),
    ("History is kept", "A changed decision supersedes the old one instead of deleting it. A hand-off stays "
     "pending until a later session takes it."),
    ("Idle sessions tidy themselves", "Once per stretch of work, an idle session saves a short session card, "
     "then compacts with the runtime's own compaction."),
    ("Local unless you opt in", "Sharing memory between machines is optional."),
]

MODEL_PROFILES = [
    ("deep", "deep", "hard judgment"),
    ("balanced-deep", "bal", "important judgment"),
    ("balanced", "bal", "planned execution, long multi-step work"),
    ("light", "light", "planned execution, checking, broad exploration"),
    ("mini", "mini", "small helper calls; refused for substantive stages"),
]


def build_info(d: dict) -> dict:
    """Inspector payload keyed by node id: capability summaries, loops, concepts."""
    info: dict[str, dict] = {}
    for name, spec in d["caps"].items():
        entry = spec["invocation"]["class"] == "entry-router"
        info[f"cap:{name}"] = {
            "kind": "entry point" if entry else "stage",
            "title": name,
            "body": str(spec.get("summary", "")).strip(),
            "meta": f"group: {spec['group']} · family: {spec['family']}",
        }
    for loop in d["loops"]:
        info[f"loop:{loop['mono']}"] = {
            "kind": "loop",
            "title": loop["mono"],
            "body": loop["blurb"],
            "meta": "runs on a schedule" if loop["type"] == "time" else "runs on demand",
        }
    concepts = [
        ("fx:main", "main session", "dispatch depth 0",
         "The session you talk to. It shows the intent card, starts the work, and brings the result back to you.",
         "does not run separable stages inline"),
        ("fx:owner", "owner", "dispatch depth 1",
         "One session keeps your request and its finish line to the end. It passes context between stages "
         "through files and reads each stage's short verdict rather than its whole output.",
         "bound to one route"),
        ("fx:stage", "stage workers", "dispatch depth 2",
         "Plan, plan check, execute, test, review, report. Each runs in its own session with its own model "
         "budget and write scope. Only execute edits source.",
         "dispatch depth 3 is forbidden"),
        ("fx:nway", "parallel legs", "dispatch",
         "Independent reviews can run as 2 to 4 sibling legs started together. They write separate "
         "artifacts and join before work continues. The checker runs on another runtime when one is available.",
         "2 to 4 legs"),
        ("fx:fallback", "checked fallback", "dispatch",
         "A stage tries the same runtime's headless worker, then another runtime's, then a native "
         "subagent, then inline. Every step keeps the same route, write scope and attempt identity, and a "
         "step down is recorded with its reason.",
         "never silent"),
        ("fx:registry", "attempt registry", "dispatch",
         "Every dispatched worker gets a row in one registry shared by all three runtimes. Fleet reads it, "
         "so a worker that stalled shows up as a stalled worker.",
         "one registry for every runtime"),
        ("fx:liveness", "liveness", "dispatch",
         "A worker is classified ALIVE, SUSPECT, DEAD, or EXITED from its recorded process id and start time.",
         "no waiting on a notification that may not arrive"),
        ("rt:card", "intent card", "contract",
         "Before work you asked for starts, the agent shows one short card: your words, what it understood, "
         "the scope, what it will not do, and when it is done. You answer Proceed, Edit, or Stop.",
         "those lines travel to the owner and every worker"),
        ("rt:seal", "fixed route", "contract",
         "When you proceed, the route is compiled and fixed for that request: capability, intensity, stages, "
         "write scopes, and model budgets. A worker cannot renegotiate it.",
         "also called a sealed route"),
        ("rt:intensity", "intensity", "contract",
         "A tiny reversible fix is done in place and recorded automatically. Above that: quick, standard, "
         "strong, thorough, adversarial. More intensity adds a written plan, independent reviews, and "
         "adversarial and security checks. Context pressure never lowers it.",
         "chosen by the frame"),
        ("md:role", "model roles", "model budgets",
         f"Shared contracts name behavior, never a vendor model. {d['unit_total']} stage units each bind "
         f"one of {d['role_total']} roles.",
         f"{d['role_total']} roles · {d['unit_total']} units"),
        ("md:profile", "model budgets", "model budgets",
         "deep for hard judgment, balanced-deep for important judgment, balanced or light for planned "
         "execution, mini for small helper calls. You decide which real model each budget uses.",
         "hearting model set <runtime> <budget> <model>"),
        ("ev:root", "artifact root", "artifacts",
         "Every request writes to one artifact root per project, shared by every runtime and by every git "
         "worktree of the project.",
         ".agent_reports/"),
        ("ev:order", "artifact order", "artifacts",
         "Research and analysis come first, then the spec, then plans. A request is one cycle under "
         "campaigns/<campaign>/<cycle>/artifacts/. A revision of the spec keeps the previous one.",
         "core/WORKFLOW.md"),
        ("lp:self", "maintainer loops", "self-improvement",
         "Drill replays behavior fixtures after instruction changes and drafts a diagnosis on failure. "
         "On-call and runtime-watch report incidents and runtime changes. None of them edits Hearting's "
         "own instructions; they propose.",
         f"{d['loop_total']} loops"),
    ]
    for key, title, kind, body, meta in concepts:
        info[key] = {"kind": kind, "title": title, "body": body, "meta": meta}
    return info


FOLD_SUMMARY = {
    "n-route": "One card you answer. Then the route is fixed for that request.",
    "n-tracks": "Four pipelines over {entry_total} entry points and {cap_total} capabilities.",
    "n-fabric": "The owner conducts, workers never route, and every worker shows up in Fleet.",
    "n-tiers": "deep &middot; balanced-deep &middot; balanced &middot; light &middot; mini &mdash; "
               "chosen per stage, never a vendor name.",
    "n-art": ".agent_reports/ &mdash; one root per project, one owning capability per artifact.",
    "n-mem": "SQLite + full-text search across every session, project, and runtime.",
    "n-fleetrail": "Sessions, requests, workers, questions, and GPUs on one screen.",
    "n-loops": "{loop_total} maintainer loops that test, corroborate, and propose &mdash; none "
               "edits Hearting's own instructions.",
}

CHEV = '<button class="chev" type="button" aria-label="Toggle this block">&#8964;</button>'


def fold_sum(node: str, d: dict) -> str:
    return f'<div class="fold-sum">{FOLD_SUMMARY[node].format(**d)}</div>'


def build_map(d: dict) -> str:
    caps = d["caps"]

    def cap_chip(name: str) -> str:
        spec = caps.get(name)
        if spec is None:
            return ""
        entry = spec["invocation"]["class"] == "entry-router"
        cls = "chip" + (" entry" if entry else "")
        return f'<button class="{cls}" data-info="cap:{name}">{html.escape(name)}</button>'

    tracks_html = []
    for track in d["tracks"]:
        color = TRACK_COLOR.get(track["color_token"], "#8B8BFA")
        flow = []
        for i, slug in enumerate(track["steps"]):
            if i:
                flow.append('<span class="arw">&rarr;</span>')
            flow.append(cap_chip(slug.split("__", 1)[-1] if "__" in slug else slug))
        for gate in track.get("gates", []):
            flow.append(f'<span class="chip gate">&#9679; {html.escape(gate)}</span>')
        tracks_html.append(
            f'<div class="track" style="--tc:{color}">'
            f'<div class="lab">{html.escape(track["label"])}</div>'
            f'<div class="flow">{"".join(flow)}</div></div>'
        )

    memory = "".join(
        f'<div class="step"><div class="sn">{title}</div><div class="sb">{body}</div></div>'
        for title, body in MEMORY_STEPS
    )
    buckets = "".join(
        f'<div class="bucket{" hi" if hi else ""}"><b>{name}</b><span>{desc}</span>'
        f'<span class="ow">{owner}</span></div>'
        for name, desc, owner, hi in ARTIFACT_BUCKETS
    )
    rules = "".join(f'<span class="rule">{r}</span>' for r in ARTIFACT_RULES)
    loops = "".join(
        f'<button class="loop" data-info="loop:{l["mono"]}">'
        f'<div class="n">{html.escape(l["name"])}<span class="k">{l["type"]}</span></div>'
        f'<div class="b">{html.escape(l["blurb"])}</div></button>'
        for l in d["loops"]
    )
    stage_chips = "".join(
        f'<span class="chip">{s}</span>' for s in
        ("code-plan", "plan-check", "code-execute", "impl-review", "code-test", "code-report")
    )
    profile_cards = "".join(
        f'<button class="pcard {css}" data-info="md:profile">'
        f'<span class="pn">{name}</span><span class="pd">{desc}</span></button>'
        for name, css, desc in MODEL_PROFILES
    )
    role_chips = "".join(
        f'<button class="chip" data-info="md:role">{html.escape(r)}</button>' for r in d["roles"]
    )
    overview_thumb = (
        f'<div class="thumb"><a class="shot" href="fleet.html">'
        f'<img src="fleet/overview.png" width="1440" height="810" loading="lazy" '
        f'alt="Fleet OVERVIEW: every session grouped by project" /></a></div>'
    )

    return f"""
    <div class="foldhint"><b>7 layers.</b> Click a layer to open it, and any chip to read what it is.</div>

    <div class="diagram" id="diagram">
      <svg id="wires" aria-hidden="true">
        <defs>
          <linearGradient id="wg" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="1200">
            <stop offset="0" stop-color="#6D6DF6" stop-opacity=".95"/>
            <stop offset="1" stop-color="#22D3EE" stop-opacity=".8"/>
          </linearGradient>
          <marker id="arrow" viewBox="0 0 8 8" refX="6.4" refY="4"
                  markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0 1 L7 4 L0 7 z" fill="#22D3EE" fill-opacity=".85"/>
          </marker>
        </defs>
      </svg>

      <div class="rows">
        <div class="col">
          <div class="rail foldable" id="n-mem">
            <div class="rh"><h4>Memory</h4><span class="tag">SQLITE &middot; FTS</span>{CHEV}</div>
            {fold_sum("n-mem", d)}
            <div class="fold-body"><div class="fold-inner">
              <div class="rd">Runs alongside every layer: the agent decides what matters, the code owns the mechanics.</div>
              <div class="spine">{memory}</div>
            </div></div>
          </div>
        </div>

        <div class="col main">
          <div class="node solid" id="n-utter">
            <div class="utter">
              <div class="av">&#9679;</div>
              <div class="say">&ldquo;Implement and test the login API, <em>then leave a change report.&rdquo;</em></div>
              <div class="tail">L0<br />one sentence in</div>
            </div>
          </div>

          <div class="node foldable is-open" id="n-route">
            <div class="head"><span class="lv">L1</span><h3>Intent card and route</h3>{CHEV}</div>
            {fold_sum("n-route", d)}
            <div class="fold-body"><div class="fold-inner">

            <div class="rcard">
              <div class="rh">Intent card &mdash; the whole handshake</div>
              <div class="rrow"><span class="rk">your words</span><span class="rv">&ldquo;The login API in the spec still isn&rsquo;t built &mdash; build it with tests&rdquo;</span></div>
              <div class="rrow"><span class="rk">intent</span><span class="rv">Implement and test the login API, then leave a change report</span></div>
              <div class="rrow"><span class="rk">scope</span><span class="rv">src/auth/**, tests/auth/**</span></div>
              <div class="rrow"><span class="rk">not doing</span><span class="rv">No schema or migration change</span></div>
              <div class="rrow"><span class="rk">done</span><span class="rv">Tests green, change report written</span></div>
              <div class="rrow"><span class="rk">route</span><span class="rv"><b>autopilot-code</b> &middot; <code>standard</code> &mdash; multi-file, needs independent verification</span></div>
              <div class="rops">
                <span class="rop on">Proceed</span><span class="rop">Edit</span><span class="rop">Stop</span>
                <span class="rnote">When there is nothing to choose, work starts with a one-line notice instead.</span>
              </div>
            </div>

            <div class="contract">
              <div class="sub"><div class="t">Fixed when you proceed</div>
                <div class="chips">
                  <button class="chip entry" data-info="rt:card">intent card</button>
                  <button class="chip entry" data-info="rt:seal">fixed route</button>
                </div>
                <p>Capability, intensity, stages, write scopes, and model budgets are fixed for the request.</p>
              </div>
              <div class="sub"><div class="t">How much runs</div>
                <div class="ladder">
                  <button class="st on" data-info="rt:intensity">tiny fix</button>
                  <button class="st on" data-info="rt:intensity">quick</button>
                  <button class="st on" data-info="rt:intensity">standard</button>
                  <button class="st on" data-info="rt:intensity">strong</button>
                  <button class="st on" data-info="rt:intensity">thorough</button>
                  <button class="st top" data-info="rt:intensity">adversarial</button>
                </div>
                <p>Bigger or riskier work adds a plan, independent reviews, and adversarial checks.</p>
              </div>
            </div>

            </div></div>
          </div>

          <div class="node foldable" id="n-tracks">
            <div class="head"><span class="lv">L2</span><h3>Pipelines</h3>{CHEV}</div>
            {fold_sum("n-tracks", d)}
            <div class="fold-body"><div class="fold-inner">
              <div class="tracks">{"".join(tracks_html)}</div>
            </div></div>
          </div>

          <div class="node solid foldable" id="n-fabric">
            <div class="head"><span class="lv">L3</span><h3>Dispatch</h3>{CHEV}</div>
            {fold_sum("n-fabric", d)}
            <div class="fold-body"><div class="fold-inner">
            <div class="fabric">
              <div class="tree">
                <div class="tier"><span class="d">d0</span>
                  <button class="box" data-info="fx:main"><b>main session</b> the one you talk to
                    <span class="m">frame</span></button></div>
                <div class="tier w2"><span class="d">d1</span>
                  <button class="box" data-info="fx:owner"><b>owner</b> keeps your request to the end
                    <span class="m">one session</span></button></div>
                <div class="tier"><span class="d">d2</span>
                  <button class="box" data-info="fx:stage"><b>stage workers</b> only execute edits source
                    <span class="m">lower-cost</span></button></div>
                <div class="chips" style="padding-left:34px">{stage_chips}</div>
                <div class="nway">
                  <div class="nh">parallel legs &middot; started together &middot; 2&ndash;4</div>
                  <div class="legs">
                    <button class="leg" data-info="fx:nway">leg 1</button>
                    <button class="leg" data-info="fx:nway">leg 2</button>
                    <button class="leg" data-info="fx:nway">leg 3</button>
                  </div>
                </div>
              </div>
              <div>
                <div class="sub" style="margin-bottom:12px"><div class="t">Checked fallback</div>
                  <div class="fall">
                    <button class="hop" data-info="fx:fallback"><i class="i"></i>same-runtime headless<span class="x">1</span></button>
                    <button class="hop" data-info="fx:fallback"><i class="i"></i>another runtime, headless<span class="x">2</span></button>
                    <button class="hop" data-info="fx:fallback"><i class="i"></i>native subagent<span class="x">3</span></button>
                    <button class="hop last" data-info="fx:fallback"><i class="i"></i>inline, reason recorded<span class="x">4</span></button>
                  </div>
                </div>
                <div class="sub"><div class="t">What Fleet reads</div>
                  <div class="chips">
                    <button class="chip" data-info="fx:registry">attempt registry</button>
                    <button class="chip" data-info="fx:liveness">liveness</button>
                  </div>
                </div>
              </div>
            </div>

            </div></div>
          </div>

          <div class="node foldable" id="n-tiers">
            <div class="head"><span class="lv">L4</span><h3>Model budget per stage</h3>{CHEV}</div>
            {fold_sum("n-tiers", d)}
            <div class="fold-body"><div class="fold-inner">
            <div class="tiers">
              <div class="sub"><div class="t">Model budgets</div>
                <div class="prof-row">{profile_cards}</div>
              </div>
              <div class="sub"><div class="t">Roles in the unit catalog &mdash; {d['role_total']}</div>
                <div class="chips">{role_chips}</div>
                <p>Shared contracts name roles and budgets, never a vendor model. You decide which real
                model each budget uses.</p>
              </div>
            </div>
            </div></div>
          </div>

          <div class="node foldable" id="n-art">
            <div class="head"><span class="lv">L5</span><h3>Artifacts</h3>{CHEV}</div>
            {fold_sum("n-art", d)}
            <div class="fold-body"><div class="fold-inner">
            <div class="flowline">
              <button class="fx" data-info="ev:order">research</button><span class="fa">&rarr;</span>
              <button class="fx" data-info="ev:order">spec</button><span class="fa">&rarr;</span>
              <button class="fx" data-info="ev:order">plans</button>
              <span class="fn">work leaves files you can read, not only a chat log</span>
            </div>
            <div class="buckets">{buckets}</div>
            <div class="rules">{rules}</div>

            </div></div>
          </div>
        </div>

        <div class="col">
          <div class="rail foldable" id="n-fleetrail">
            <div class="rh"><h4>Fleet</h4><span class="tag">LIVE VIEW</span>{CHEV}</div>
            {fold_sum("n-fleetrail", d)}
            <div class="fold-body"><div class="fold-inner">
              <div class="rd">Watches every layer to the left, read-only.</div>
              {overview_thumb}
              <a class="rlink" href="fleet.html">Tour Fleet &rarr;</a>
            </div></div>
          </div>
        </div>
      </div>

      <div class="loops foldable" id="n-loops">
        <div class="head loops-head"><span class="ln">L6</span><h3>Maintainer loops</h3>{CHEV}</div>
        {fold_sum("n-loops", d)}
        <div class="fold-body"><div class="fold-inner">
          <div class="loop-grid">{loops}</div>
        </div></div>
      </div>
    </div>"""


MAP_JS = r"""
(function () {
  "use strict";
  var INFO = JSON.parse(document.getElementById("info-data").textContent);

  /* wires: measured from real element boxes, so any reflow stays correct */
  var EDGES = [
    ["n-utter", "n-route"],
    ["n-route", "n-tracks"],
    ["n-tracks", "n-fabric"],
    ["n-fabric", "n-tiers"],
    ["n-tiers", "n-art"]
  ];

  function draw() {
    var svg = document.getElementById("wires");
    var host = document.getElementById("diagram");
    if (!svg || !host) return;
    while (svg.lastChild && svg.lastChild.nodeName !== "defs") svg.removeChild(svg.lastChild);
    var base = host.getBoundingClientRect();
    var ns = "http://www.w3.org/2000/svg";
    // userSpaceOnUse: a bounding-box gradient collapses on a zero-width vertical path.
    var grad = document.getElementById("wg");
    if (grad) grad.setAttribute("y2", String(Math.max(400, host.scrollHeight)));

    function box(id) {
      var el = document.getElementById(id);
      if (!el) return null;
      var r = el.getBoundingClientRect();
      return { l: r.left - base.left, t: r.top - base.top, w: r.width, h: r.height };
    }
    function path(dAttr) {
      // Soft halo behind the wire. An SVG filter can't do this here: a zero-width
      // vertical path gives an objectBoundingBox filter a zero-sized region.
      var halo = document.createElementNS(ns, "path");
      halo.setAttribute("d", dAttr);
      halo.setAttribute("fill", "none");
      halo.setAttribute("stroke", "#6D6DF6");
      halo.setAttribute("stroke-width", "7");
      halo.setAttribute("stroke-linecap", "round");
      halo.setAttribute("opacity", ".16");
      svg.appendChild(halo);
      var p = document.createElementNS(ns, "path");
      p.setAttribute("d", dAttr);
      p.setAttribute("fill", "none");
      p.setAttribute("stroke", "url(#wg)");
      p.setAttribute("stroke-width", "2");
      p.setAttribute("stroke-linecap", "round");
      p.setAttribute("marker-end", "url(#arrow)");
      svg.appendChild(p);
    }
    function dot(x, y) {
      var c = document.createElementNS(ns, "circle");
      c.setAttribute("cx", x); c.setAttribute("cy", y); c.setAttribute("r", "3");
      c.setAttribute("fill", "var(--wire)");
      svg.appendChild(c);
    }
    EDGES.forEach(function (e) {
      var a = box(e[0]), b = box(e[1]);
      if (!a || !b) return;
      var x = a.l + a.w / 2, y1 = a.t + a.h + 3, y2 = b.t - 3;
      if (y2 - y1 < 6) return;
      var mid = (y1 + y2) / 2;
      path("M" + x + " " + y1 + " C " + x + " " + mid + ", " + x + " " + mid + ", " + x + " " + y2);
      dot(x, y1);
    });
  }

  /* inspector */
  var panel = document.getElementById("inspector");
  function show(key) {
    var item = INFO[key];
    if (!item || !panel) return;
    panel.querySelector(".k").textContent = item.kind;
    panel.querySelector(".t").textContent = item.title;
    panel.querySelector(".ib").textContent = item.body;
    panel.querySelector(".im").textContent = item.meta || "";
    panel.classList.add("on");
  }
  document.addEventListener("click", function (ev) {
    var host = ev.target.closest ? ev.target.closest("[data-info]") : null;
    if (host) { show(host.getAttribute("data-info")); return; }
    if (panel && !(ev.target.closest && ev.target.closest(".inspector"))) panel.classList.remove("on");
  });
  var close = document.getElementById("insp-close");
  if (close) close.addEventListener("click", function () { panel.classList.remove("on"); });

  /* progressive disclosure */
  function setOpen(block, open) {
    block.classList.toggle("is-open", open);
    var chev = block.querySelector(":scope > .head > .chev, :scope > .rh > .chev");
    if (chev) chev.setAttribute("aria-expanded", open ? "true" : "false");
    setTimeout(draw, 320);
  }
  Array.prototype.forEach.call(document.querySelectorAll(".foldable"), function (block) {
    var head = block.querySelector(":scope > .head, :scope > .rh");
    if (!head) return;
    var chev = head.querySelector(".chev");
    if (chev) chev.setAttribute("aria-expanded", block.classList.contains("is-open") ? "true" : "false");
    head.addEventListener("click", function (ev) {
      if (ev.target.closest("a")) return;
      setOpen(block, !block.classList.contains("is-open"));
    });
  });

  draw();
  window.addEventListener("resize", draw);
  if (window.ResizeObserver) {
    var hostEl = document.getElementById("diagram");
    if (hostEl) new ResizeObserver(function () { draw(); }).observe(hostEl);
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(draw);
})();
"""


# ------------------------------------------------------------------ landing page

def render_index(d: dict) -> str:
    info_json = json.dumps(build_info(d), ensure_ascii=False).replace("</", "<\\/")
    body = f"""
<main>
<section class="hero"><div class="wrap">
  <span class="eyebrow"><i></i>For Claude Code, Codex, and OpenCode</span>
  <h1>Keep your coding agents <span class="grad">on your intent.</span></h1>
  <p class="lede">Agents differ in skill and cost, and you can no longer work without them.
  Hearting makes the work follow what you asked, <b>whichever agent runs it</b>. A capable model
  settles the intent and the plan. Lower-cost workers carry it out in small, checked steps.
  <b>Fleet</b> shows every one of them, live, on one screen.</p>

  <div class="install" id="install">
    <span class="p">$</span>
    <code id="cmd">{INSTALL_CMD}</code>
    <button onclick="{html.escape(copy_js("cmd"), quote=True)}">Copy</button>
  </div>
  <p class="fine">Linux and macOS &middot; checksummed release &middot;
    <a href="{REPO_URL}/releases">release notes</a> &middot;
    <a href="#quickstart">what you need first</a></p>

  <figure class="hero-shot">
    {gif("fleet.html")}
    <figcaption>Fleet, the live view of every agent: OVERVIEW &rarr; ROUTES &rarr; a session &rarr; a
    request's raw output. Real screens drawn from synthetic demo data.
    <a href="fleet.html">Take the Fleet tour &rarr;</a></figcaption>
  </figure>
</div></section>

<section class="sec" id="why"><div class="wrap">
  <div class="kick">Why Hearting</div>
  <h2>The work stays tied to what you asked, not to how capable the agent is.</h2>
  <p class="lead">Agents differ by vendor, by model, and by what your workplace gives you, and working
  without them is no longer an option. Hearting keeps the way you work the same across them. You stay in
  charge, and the work holds together even when the agent contributes little on its own.</p>
  <div class="grid why">
    <div class="card"><span class="num">01</span><h3>Consistent work, whatever the agent</h3>
      <p>Hearting confirms your intent with you before work starts, keeps each worker inside a bounded
      scope, and checks results independently.</p></div>
    <div class="card"><span class="num">02</span><h3>One way of working everywhere</h3>
      <p>The same requests, stages, artifacts, and memory on Claude Code, Codex, and OpenCode &mdash; one
      of them or all three. Fleet watches all of them in one view.</p></div>
    <div class="card hot"><span class="num">03</span><h3>A crutch for low-cost agents</h3>
      <p>Hearting cannot make a weak model smarter. It lets one carry out your intent anyway: a capable
      model settles the intent and the plan (the <i>frame</i>), and cheaper workers carry it out in small,
      checked steps (<i>dispatch</i>).</p></div>
    <div class="card"><span class="num">04</span><h3>Light on the agent</h3>
      <p>No step works only if an agent memorizes a long procedure. The procedure lives in the harness.</p></div>
    <div class="card"><span class="num">05</span><h3>Existing work first</h3>
      <p>Before planning, a cheap read-only scan lists what your code, earlier artifacts, memory, and git
      history already settled, so the plan starts from that instead of from what the model happens to know.</p></div>
  </div>
</div></section>

<section class="sec" id="how"><div class="wrap">
  <div class="kick">How a request runs</div>
  <h2>Say what you want. The harness does the routing.</h2>
  <p class="lead">Describe the outcome in your own words. There are no command names to learn.</p>
  <div class="pipe">
    <div class="st"><div class="dot">1</div><h3>You ask</h3>
      <p>&ldquo;Implement and test the login API, then leave a change report.&rdquo;</p></div>
    <div class="st"><div class="dot">2</div><h3>Prior-art scan</h3>
      <p>What the project already has: code, artifacts, memory, git history.</p></div>
    <div class="st"><div class="dot">3</div><h3>Frame</h3>
      <p>A capable model proposes the route. You are asked only when there is a real choice.</p></div>
    <div class="st"><div class="dot">4</div><h3>Owner and workers</h3>
      <p>One session keeps your request. Lower-cost workers plan, execute, test, and review in small
      steps; only <i>execute</i> edits source.</p></div>
    <div class="st"><div class="dot">5</div><h3>Report</h3>
      <p>What changed, what was verified, what is left, and what it cost.</p></div>
  </div>
  <div class="fleetband"><span><b>Fleet</b> shows every step above while it runs.</span>
    <a href="fleet.html">See it &rarr;</a></div>

  <div class="split" style="margin-top:34px">
    <div class="rcard">
      <div class="rh">The one card you answer</div>
      <div class="rrow"><span class="rk">your words</span><span class="rv">&ldquo;The login API in the spec still isn&rsquo;t built &mdash; build it with tests&rdquo;</span></div>
      <div class="rrow"><span class="rk">intent</span><span class="rv">Implement and test the login API, then leave a change report</span></div>
      <div class="rrow"><span class="rk">scope</span><span class="rv">src/auth/**, tests/auth/**</span></div>
      <div class="rrow"><span class="rk">not doing</span><span class="rv">No schema or migration change</span></div>
      <div class="rrow"><span class="rk">done</span><span class="rv">Tests green, change report written</span></div>
      <div class="rops"><span class="rop on">Proceed</span><span class="rop">Edit</span><span class="rop">Stop</span>
        <span class="rnote">illustrative card</span></div>
    </div>
    <div>
      <h3>Small things stay small</h3>
      <p class="t"><b>A tiny reversible fix</b> is made right in your folder and recorded automatically.
      Nothing to start or finish.</p>
      <div class="ladder2"><span class="first">tiny fix</span><span>quick</span><span>standard</span>
        <span>strong</span><span>thorough</span><span class="top">adversarial</span></div>
      <p class="t">A small change gets the frame and one quick worker. Bigger or riskier work adds a
      written plan, independent reviews, and finally adversarial or security checks.</p>
      <p class="t"><b>Each stage uses a model budget chosen for it</b> &mdash; <code>deep</code> for hard
      judgment, <code>light</code> for planned execution. You decide which real model each budget uses.</p>
    </div>
  </div>
</div></section>

<section class="sec" id="fleet"><div class="wrap">
  <div class="kick">Fleet</div>
  <h2>When agents work in the background, you shouldn&rsquo;t have to guess.</h2>
  <p class="lead">Is it still running? Is it stuck? Is it waiting for you? Did it finish? Fleet answers in one
  terminal view, for every Claude Code, Codex, and OpenCode session on your machine and every worker
  Hearting dispatched from them.</p>
  <figure class="hero-shot" style="margin-top:36px">
    {crop("questions", 292, 50, 1148, 340, "fleet.html#s-questions")}
    <figcaption>QUESTIONS: every agent waiting for your answer, since when, and how to answer it.
    Click for the full screen.</figcaption>
  </figure>
  <div class="grid grid3" style="margin-top:30px">
    <div class="card"><span class="num">ALIVE?</span><h3>What is running</h3>
      <p>Every session by project, with what it is doing now and its request&rsquo;s stage line. <a href="fleet.html#s-routes">ROUTES &rarr;</a></p></div>
    <div class="card"><span class="num">STUCK?</span><h3>What went quiet</h3>
      <p>A stalled worker shows as a stalled worker, not as a parent session that went quiet. <a href="fleet.html#problem">How &rarr;</a></p></div>
    <div class="card"><span class="num">WAITING?</span><h3>What needs you</h3>
      <p>Who asks, about what, for which request, and since when. Older waits stay until answered. <a href="fleet.html#s-questions">QUESTIONS &rarr;</a></p></div>
  </div>
  <div class="pill-row">
    <span>Built from the records the work itself writes</span>
    <span>A value it can&rsquo;t read shows as <code>?</code></span>
    <span>Watching changes nothing</span>
  </div>
  <div class="cmdrow">
    <a class="btn primary" href="fleet.html">Take the Fleet tour &rarr;</a>
    <span class="cmd"><i>$</i>fleet</span>
  </div>
</div></section>

<section class="sec" id="quickstart"><div class="wrap">
  <div class="kick">Quick start</div>
  <h2>Four steps from install to a watched task.</h2>
  <p class="lead">You need Linux or macOS, Python 3.11+ with PyYAML, Git, <code>curl</code> or
  <code>wget</code>, and at least one signed-in agent CLI. Claude Code alone is enough.</p>
  <div class="grid qs">
    <div class="card"><h3>Install</h3>
      <p>One line, no clone. For Claude Code only, end it with <code>sh -s -- --runtime claude</code>.</p>
      <pre>{html.escape(INSTALL_CMD)}</pre></div>
    <div class="card"><h3>Check</h3>
      <p>The doctor reports what is live and names any optional piece that is missing.</p>
      <pre>~/.local/bin/hearting runtime doctor \\
  --runtime all --strict</pre></div>
    <div class="card"><h3>Ask</h3>
      <p>Open your agent CLI inside a Git project and ask for something small, such as fixing a typo and
      running the existing test.</p></div>
    <div class="card"><h3>Watch</h3>
      <p>Run Fleet in a second terminal to see the session and any workers it dispatches.</p>
      <pre>fleet</pre></div>
  </div>
  <div class="note"><b>Read Permissions first.</b> Background Claude Code workers run in bypass mode by
  default, because nobody is there to answer a permission prompt. Sessions you start yourself keep your own
  settings. <a href="{REPO_URL}#permissions">Permissions in the README &rarr;</a></div>
</div></section>

<section class="sec" id="map"><div class="wrap">
  <div class="kick">The layer map</div>
  <h2>How it is built, from your sentence down to upkeep.</h2>
  <p class="lead">Seven layers sit between what you say and the work that gets done. Open one to see its
  parts. Memory runs alongside all of them, and Fleet watches all of them.</p>
  <div class="mapsec">{build_map(d)}</div>
</div></section>

<section class="sec" id="deeper"><div class="wrap">
  <div class="kick">Go deeper</div>
  <h2>{d['cap_total']} capabilities, {d['entry_total']} entry points, three runtimes.</h2>
  <div class="grid links" style="margin-top:30px">
    <a href="{REPO_URL}#readme"><b>README</b><span>Install, permissions, memory, models, and limits.</span></a>
    <a href="fleet.html"><b>Fleet tour</b><span>Every screen, and why you can trust it.</span></a>
    <a href="map.html"><b>Capability catalog</b><span>Every entry point and stage, with its summary.</span></a>
    <a href="{REPO_URL}/blob/main/MANUAL.md"><b>Manual</b><span>How the parts fit together.</span></a>
    <a href="{REPO_URL}/blob/main/AGENTS.md"><b>Purpose and principles</b><span>Why Hearting exists, and how it is maintained.</span></a>
    <a href="{REPO_URL}/releases"><b>Releases</b><span>Versions, notes, and the installer.</span></a>
  </div>
</div></section>
</main>

<div class="inspector" id="inspector" role="dialog" aria-live="polite">
  <div class="ih"><span class="k"></span><span class="t"></span>
    <button id="insp-close" aria-label="close">&times;</button></div>
  <div class="ib"></div>
  <div class="im"></div>
</div>
"""
    scripts = (f'<script type="application/json" id="info-data">{info_json}</script>\n'
               f'<script>{MAP_JS}</script>')
    return page(
        "Hearting — keep your coding agents on your intent",
        "Hearting keeps agent work on your intent, whichever agent runs it, on Claude Code, Codex, and "
        "OpenCode: a capable model settles the plan, lower-cost workers carry it out in small checked "
        "steps, and Fleet shows every session and worker live.",
        body, "home", scripts)


# ------------------------------------------------------------------- Fleet page

SCREENS = [
    ("overview", "OVERVIEW", "Every session, grouped by project.",
     "Each project's line says where it stands, and each session shows what it is doing right now.",
     ["<b>Per project:</b> open requests, questions waiting for you, what is left to decide, and when it last changed.",
      "<b>Per session:</b> runtime, model and effort, a short title, context left, what it is doing now, and for how long.",
      "<b>Marks:</b> working, idle, and waiting-for-you each look different.",
      "<b>Top row:</b> each runtime's usage-window gauges, and how many sessions are working, idle, or waiting."]),
    ("routes", "ROUTES", "Each request, stage by stage.",
     "A stage line such as <code>plan › execute › test › report</code>, with the workers running under it.",
     ["See which stage a request is in and which worker, on which model, is running it."]),
    ("questions", "QUESTIONS", "Everything waiting for your answer.",
     "Every agent waiting for an answer or an approval: which session asks, what kind of question, for which "
     "request, and since when.",
     ["Older waits stay listed until you answer or the work ends. Nothing drops off for its age.",
      "Answer from Fleet when the runtime offers a reply path. Otherwise Fleet points you to the session."]),
    ("session", "SESSION PAGE", "One session, in full.",
     "State, context left, what it is doing now, its request, the sessions it steers, and the GPU it holds.",
     ["Select any session in the tree to open its page."]),
    ("resources", "RESOURCES", "What is using the machine.",
     "CPU, memory, and per-GPU use on this machine and on any hosts you configured, with the session that owns "
     "each GPU process.",
     ["Even without configured hosts, Fleet shows this machine's CPU and GPU use."]),
    ("output", "REQUEST PAGE", "A request's stage graph and raw output.",
     "Select a request to see its stages, and under them the owner's raw output: tool calls and results in "
     "order, without opening the runtime itself.",
     ["Setup done, one stage running, the rest pending, and what the owner just did."]),
]

OTHER_VIEWS = [
    ("PEERS", "Which sessions watch or steer which, and their last exchange."),
    ("HISTORY", "Earlier sessions of each terminal seat, so work that continued in a new session stays traceable."),
    ("MEMORY", "Per project: the newest decisions, open work by stage, and the latest records saved to shared memory."),
    ("MODELS · SYSTEM", "Which real model each budget maps to, and the installed Hearting release."),
]


def render_fleet(d: dict) -> str:
    rows = []
    for name, tag, title, lead, bullets in SCREENS:
        lis = "".join(f"<li>{b}</li>" for b in bullets)
        rows.append(
            f'<div class="scr" id="s-{name}"><div class="scr-head"><div><span class="tag">{tag}</span>'
            f'<h3>{title}</h3></div><div><p>{lead}</p><ul>{lis}</ul></div></div>'
            f'{shot(name)}</div>'
        )
    views = "".join(f'<div class="card"><h3>{n}</h3><p>{t}</p></div>' for n, t in OTHER_VIEWS)

    body = f"""
<main>
<section class="hero"><div class="wrap">
  <span class="eyebrow"><i></i>Fleet &middot; part of Hearting</span>
  <h1>Every agent, <span class="grad">on one screen.</span></h1>
  <p class="lede">Background agents leave you guessing. Is it still running? Is it stuck? Is it waiting for
  you? Did it finish? Fleet answers in one terminal view, for <b>every Claude Code, Codex, and OpenCode
  session on your machine</b> and every worker Hearting dispatched from them.</p>
  <div class="btns">
    <span class="cmd"><i>$</i><span id="fcmd">fleet</span></span>
    <a class="btn primary" href="index.html#quickstart">Install Hearting</a>
    <a class="btn" href="#screens">See the screens</a>
  </div>
  <figure class="hero-shot">
    {gif()}
    <figcaption>OVERVIEW &rarr; ROUTES &rarr; a session page &rarr; a request's raw output. Every picture on this
    page is a real Fleet screen drawn from synthetic demo data, never from a real machine.</figcaption>
  </figure>
</div></section>

<section class="sec" id="problem"><div class="wrap">
  <div class="kick">The problem</div>
  <h2>A background agent is a black box until you open its terminal.</h2>
  <p class="lead">With several sessions across three runtimes, and workers dispatched under them, you end up
  opening windows one by one to learn the same three things.</p>
  <div class="grid grid3">
    <div class="card"><span class="num">ALIVE?</span><h3>What is running</h3>
      <p>Working, idle, and waiting-for-you have different marks. Each session shows what it is doing now and for
      how long.</p></div>
    <div class="card"><span class="num">STUCK?</span><h3>What went quiet</h3>
      <p>A stalled worker shows up as a stalled worker, not as a parent session that went quiet. Rows whose
      parent is gone are shown, not dropped.</p></div>
    <div class="card hot"><span class="num">WAITING?</span><h3>What needs you</h3>
      <p>QUESTIONS lists every agent waiting for your answer or approval, since when, and for which request.</p></div>
  </div>
</div></section>

<section class="sec" id="screens"><div class="wrap">
  <div class="kick">The screens</div>
  <h2>One screen per question.</h2>
  <p class="lead">Press <code>Tab</code> to move between views. Select a session or a request to open its
  page.</p>
  <div style="margin-top:22px">{"".join(rows)}</div>

  <div class="grid views" style="margin-top:26px">{views}</div>

  <div class="narrow">
    <div><h3>It fits a narrow terminal</h3>
    <p>The same OVERVIEW, laid out for a 100-column window. Click any picture on this page for the full-size
    image.</p>
    <div class="keys">
      <span><kbd>Tab</kbd>view</span><span><kbd>/</kbd>filter</span><span><kbd>Enter</kbd>open</span>
      <span><kbd>u</kbd>usage</span><span><kbd>q</kbd>quit</span>
    </div></div>
    {shot("narrow")}
  </div>
</div></section>

<section class="sec" id="trust"><div class="wrap">
  <div class="kick">Why you can trust it</div>
  <h2>It shows what the work recorded, and nothing it made up.</h2>
  <div class="grid grid3">
    <div class="card"><span class="num">01</span><h3>Built from the work&rsquo;s own records</h3>
      <p>Fleet reads the attempt registry the dispatcher writes and each runtime&rsquo;s own session state. It does
      not guess from what a terminal printed.</p></div>
    <div class="card"><span class="num">02</span><h3>Unknown stays unknown</h3>
      <p>A value Fleet could not read is shown as <code>?</code>. Never as zero, never as success.</p></div>
    <div class="card"><span class="num">03</span><h3>Watching changes nothing</h3>
      <p>Observation never completes, retries, or cleans up work. Fleet acts only on what you select, such as
      answering a waiting question.</p></div>
  </div>
</div></section>

<section class="sec" id="run"><div class="wrap">
  <div class="kick">Run it</div>
  <h2>Installed with Hearting. One word to start.</h2>
  <p class="lead">Open a second terminal next to your agent and run <code>fleet</code>.</p>
  <div class="split" style="margin-top:30px">
    <pre class="code">fleet              # the Explorer, full screen (needs curses)
fleet --once       # one plain snapshot
fleet --json       # the same state as JSON, for scripts
fleet-monitor      # the earlier group/process view</pre>
    <div>
      <p class="t" style="margin-top:0">Titles and Fleet&rsquo;s own words follow your language. Set
      <code>title_language</code> in <code>${{XDG_CONFIG_HOME:-~/.config}}/hearting/fleet.json</code> to override it.</p>
      <p class="t"><code>--once</code> and <code>--json</code> need no <code>curses</code>, so they also work on
      Windows under Git Bash (experimental).</p>
    </div>
  </div>
</div></section>

<section class="sec" id="limits"><div class="wrap">
  <div class="kick">Known limits</div>
  <h2>Fleet is not light yet.</h2>
  <p class="lead">Measured on a busy machine with a long work history and many live sessions, one Explorer
  window used:</p>
  <div class="grid limits">
    <div class="card"><div class="big">&asymp; 1/3<small>of one CPU core</small></div>
      <p>Lowering its CPU use continues in v4.x.</p></div>
    <div class="card"><div class="big">&asymp; 0.8<small>GB of memory</small></div>
      <p>One Explorer window, on that machine.</p></div>
    <div class="card"><div class="big">&asymp; 6<small>seconds to first screen</small></div>
      <p>The first full screen takes about this long to draw when the history is long.</p></div>
  </div>
  <div class="note">Answering a question from Fleet works only where the runtime offers a reply path. Where it
  does not, Fleet points you to the session instead.</div>
</div></section>

<section class="wrap"><div class="cta-band">
  <h2>See every agent without opening a single terminal.</h2>
  <p>Fleet comes with Hearting. Install once, then run <code>fleet</code>.</p>
  <div class="btns">
    <a class="btn primary" href="index.html#quickstart">Quick start</a>
    <a class="btn" href="{REPO_URL}#fleet-every-agent-on-one-screen">Fleet in the README</a>
    <a class="btn" href="index.html">Back to home</a>
  </div>
</div></section>
</main>
"""
    return page(
        "Fleet — every agent on one screen · Hearting",
        "Fleet is Hearting's live terminal view of every Claude Code, Codex, and OpenCode session and every "
        "dispatched worker: running, stuck, or waiting for you, on one screen.",
        body, "fleet")


# ------------------------------------------------------------------- map page

MAP_GROUP_ORDER = [
    ("research", "Research first", "Ground new intent in evidence before building.",
     ["autopilot-research", "analyze-project"]),
    ("code", "Code &amp; experiments", "Spec-governed implementation and rapid experiment loops.",
     ["autopilot-spec", "autopilot-code", "autopilot-lab", "autopilot-ship",
      "code-plan", "code-execute", "code-refine", "code-test", "code-report"]),
    ("docs", "Documents", "Papers, reports, proposals — drafted, refined, applied.",
     ["autopilot-draft", "autopilot-refine", "autopilot-apply",
      "draft-strategy", "draft-refine"]),
    ("design", "Design", "Reference-grounded visual design with token contracts.",
     ["autopilot-design", "design-init", "design-refs", "design-tokens",
      "design-components", "design-review", "design-handoff"]),
    ("ops", "Cross-project &amp; operations", "Continuity, inspection, and the user profile.",
     ["analyze-user", "audit", "session-tidy"]),
]


def render_map(d: dict) -> str:
    caps = d["caps"]
    listed: set[str] = set()
    groups_html = []
    for _key, title, desc, names in MAP_GROUP_ORDER:
        cards = []
        for name in names:
            spec = caps.get(name)
            if spec is None:
                continue
            listed.add(name)
            entry = spec["invocation"]["class"] == "entry-router"
            pill = "entry" if entry else "stage"
            pill_class = "" if entry else " stage"
            summary = html.escape(str(spec.get("summary", "")).strip())
            cards.append(
                f'<div class="cap">'
                f'<div class="name"><span class="mono">{name}</span>'
                f'<span class="pill{pill_class}">{pill}</span></div>'
                f"<p>{summary}</p></div>"
            )
        groups_html.append(
            f'<div class="cap-group"><h3>{title}</h3>'
            f'<div class="desc">{desc}</div>'
            f'<div class="cap-grid">{"".join(cards)}</div></div>'
        )
    orphans = sorted(set(caps) - listed)
    if orphans:
        cards = "".join(
            f'<div class="cap">'
            f'<div class="name"><span class="mono">{n}</span></div>'
            f'<p>{html.escape(str(caps[n].get("summary", "")).strip())}</p></div>'
            for n in orphans
        )
        groups_html.append(
            f'<div class="cap-group"><h3>Other</h3><div class="cap-grid">{cards}</div></div>'
        )

    body = f"""
<main><div class="wrap" style="padding-bottom:20px">
  <div class="maphead">
    <h1>Every capability, <span class="grad">one catalog.</span></h1>
    <p>One portable catalog projected into three runtimes. Entry points own whole pipelines; stages are the
    workers they dispatch. You never type these names: describe the outcome and the harness picks.</p>
    <div class="stats">
      <div class="stat"><b>{d['cap_total']}</b><span>capabilities</span></div>
      <div class="stat"><b>{d['entry_total']}</b><span>entry points</span></div>
      <div class="stat"><b>{d['unit_total']}</b><span>stage units</span></div>
      <div class="stat"><b>{d['role_total']}</b><span>model roles</span></div>
      <div class="stat"><b>3</b><span>runtimes</span></div>
    </div>
  </div>
  {"".join(groups_html)}
</div></main>
"""
    return page(
        "Capability catalog — Hearting",
        "Every Hearting capability and pipeline stage.",
        body, "catalog")


def main() -> int:
    check = "--check" in sys.argv[1:]
    data = load_data()
    outputs = {
        DOCS / "index.html": render_index(data),
        DOCS / "fleet.html": render_fleet(data),
        DOCS / "map.html": render_map(data),
    }
    if check:
        stale = [
            str(path.relative_to(ROOT))
            for path, want in outputs.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != want
        ]
        if stale:
            print("stale: " + ", ".join(stale))
            return 1
        print("docs/ landing up-to-date")
        return 0
    DOCS.mkdir(exist_ok=True)
    for path, want in outputs.items():
        path.write_text(want, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
