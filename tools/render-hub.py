#!/usr/bin/env python3
"""Render a self-contained static hub page from ``manifest.json``.

``manifest.json`` is a GENERATED COMPATIBILITY VIEW (see ``tools/build-manifest.py``)
— this script only READS it; it never hand-edits or regenerates ``manifest.json``
itself. Run ``tools/build-manifest.py`` (or ``tools/generate.py``) first if
``manifest.json`` is stale.

``hub.html`` is a single self-contained HTML file: the navigation-relevant slice of
``manifest.json`` (skills, agents, hooks, loops, tracks, topology_registry summary)
is embedded as a ``<script type="application/json">`` block, and inline JS renders
the map + skill-catalog views and per-item detail from that embedded data. It must
open directly via ``file://`` with zero fetch/CDN/external-network calls. Operational
views (approval queue, memory) are out of scope by design — see the router task that
introduced this generator.

The map view also carries a governance strip (core/CORE.md, WORKFLOW.md,
CONVENTIONS.md, DESIGN_PRINCIPLES.md, MEMORY.md, OPERATIONS.md) rendered as plain,
pre-escaped HTML at generation time — not a second JSON blob. Each card's one-line
summary is the doc's own leading Markdown blockquote line, read straight off disk
(no fetch, no network): deterministic and reproducible as long as the six docs
don't change between runs, exactly like the manifest-driven slice above.

Determinism: no timestamps or randomness. The embedded payload is a straight,
order-preserving slice of the already-deterministic ``manifest.json`` (stable-sorted
upstream by ``tools/build-manifest.py``), and the surrounding HTML/CSS/JS shell is a
fixed string. Re-running produces a byte-identical ``hub.html``.

Usage:
  python3 tools/render-hub.py            # write hub.html at repo root
  python3 tools/render-hub.py --check    # render in memory, diff vs existing, exit 1 on drift
"""
from __future__ import annotations

import html
import json
import os
import sys

# realpath (not abspath): this script is executed via the collapsed adapter symlink
# (adapters/claude/tools/render-hub.py -> ../../../tools/render-hub.py). abspath keeps
# the symlink path, resolving REPO_ROOT to repo/adapters/claude (double-path bug);
# realpath follows the link to the canonical tools/, giving the true repo root either way.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "web-bundle"))
from font_css import d2coding_font_css

MANIFEST_PATH = os.path.join(REPO_ROOT, "manifest.json")
HUB_PATH = os.path.join(REPO_ROOT, "hub.html")

# Hub scope: navigation/catalog data only. Operational (approval queue) and memory
# views are intentionally excluded, per the task that introduced this generator.
VIEW_FIELDS = (
    "generated_from",
    "manifest_version",
    "topology_registry",
    "skills",
    "agents",
    "hooks",
    "loops",
    "tracks",
)

DATA_PLACEHOLDER = "__HUB_DATA_JSON__"
GOVERNANCE_PLACEHOLDER = "__HUB_GOVERNANCE_HTML__"

# Governance strip — repo-relative (label, path) pairs. "CORE" stands in for the
# family's CLAUDE-equivalent bootstrap doc: this repo has no top-level CLAUDE.md
# (that file is an adapter-side projection outside the portable repo), so this maps
# to core/CORE.md, the doc every adapter bootstrap reads first (see core/CORE.md
# Source Order / adapters/claude/CLAUDE.md item 1). Fixed list — code-level curation,
# same allowance as the ARTIFACT_DIRS folder list below.
GOVERNANCE_DOCS = (
    ("CORE", "core/CORE.md"),
    ("WORKFLOW", "core/WORKFLOW.md"),
    ("CONVENTIONS", "core/CONVENTIONS.md"),
    ("DESIGN_PRINCIPLES", "core/DESIGN_PRINCIPLES.md"),
    ("MEMORY", "core/MEMORY.md"),
    ("OPERATIONS", "core/OPERATIONS.md"),
)


def load_manifest():
    if not os.path.exists(MANIFEST_PATH):
        sys.stderr.write(
            "manifest.json missing — run `python3 tools/build-manifest.py` first\n"
        )
        sys.exit(2)
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def build_view(manifest):
    """Slice manifest.json down to the hub page's scope. Field/list order is
    preserved exactly as in manifest.json (already stable-sorted upstream), so
    this stays a pure, deterministic projection — no re-sorting or re-shaping."""
    missing = [field for field in VIEW_FIELDS if field not in manifest]
    if missing:
        sys.stderr.write(
            "manifest.json missing expected field(s): %s\n" % ", ".join(missing)
        )
        sys.exit(2)
    return {field: manifest[field] for field in VIEW_FIELDS}


def _doc_summary(abs_path, limit=150):
    """First non-empty Markdown blockquote ('> ...') line in the doc, trimmed.
    Every core/*.md doc opens with a '# Title' then a '> one-line summary'
    blockquote by convention — a plain, deterministic disk read, not a fetch."""
    try:
        with open(abs_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            summary = stripped.lstrip(">").strip()
            if summary:
                if len(summary) > limit:
                    summary = summary[:limit].rstrip() + "…"
                return summary
    return None


def build_governance_html():
    """Pre-render the governance strip's 6 doc cards as plain HTML (escaped),
    spliced into PAGE_TEMPLATE at GOVERNANCE_PLACEHOLDER. Deliberately NOT part
    of the embedded JSON payload — keeps the page to exactly one JSON script
    block (manifest-derived nav/catalog data only)."""
    cards = []
    for label, rel_path in GOVERNANCE_DOCS:
        abs_path = os.path.join(REPO_ROOT, rel_path)
        summary = _doc_summary(abs_path) or "(no summary line found)"
        cards.append(
            '<a class="hub-gov-card" href="{href}">'
            '<div class="hub-gov-card-title mono">{label}</div>'
            '<div class="hub-gov-card-path mono">{path}</div>'
            '<div class="hub-gov-card-summary">{summary}</div>'
            "</a>".format(
                href=html.escape(rel_path, quote=True),
                label=html.escape(label),
                path=html.escape(rel_path),
                summary=html.escape(summary),
            )
        )
    return "\n      ".join(cards)


def _embed_json(data):
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
    # Never let embedded data terminate the surrounding <script> element early.
    return text.replace("</", "<\\/")


PAGE_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>에이전트 허브</title>
<!--
  GENERATED by tools/render-hub.py — DO NOT EDIT BY HAND.
  Source: manifest.json (generated compatibility view; canonical: harness-manifest.json)
  + the 6 core/*.md governance docs (read directly, not fetched).
  Self-contained: no fetch/CDN/network. Nav/catalog data is embedded as JSON below;
  inline JS renders the DOM from it. Regenerate: python3 tools/render-hub.py
  Verify:     python3 tools/render-hub.py --check
-->
<style>
__HUB_FONT_CSS__
  /* ════ Design tokens — ported from the companion worklog app app/globals.css (:root, .hub-root)
     light values default in :root; dark values re-defined under prefers-color-scheme.
     No raw hex/rgba outside this block — the rest of the file reads var(--…) only. ════ */
  :root {
    color-scheme: light dark;

    /* type */
    --font-ui:   "Pretendard Variable", "Pretendard", -apple-system, BlinkMacSystemFont,
                 "SF Pro Text", "Apple SD Gothic Neo", "Noto Sans KR", system-ui, sans-serif;
    --font-mono: "D2Coding", "JetBrains Mono", ui-monospace,
                 "SF Mono", SFMono-Regular, Menlo, monospace;
    /* hub-scope typography (the companion worklog app .hub-root bumps the app scale by ~+1px) */
    --text-micro:      12px;
    --text-caption:    13.5px;
    --text-meta:       14px;
    --text-sm:         15px;
    --text-display-sm: 25px;

    /* surfaces / borders / text (the companion worklog app :root, light) */
    --surface:          #FFFFFF;
    --surface-2:        #EDEDF1;
    --surface-3:        #E6E6EB;
    --surface-recessed: #F0F0F3;
    --border:           #ECECEE;
    --border-strong:    #DCDCE0;
    --text:             #1D1D1F;
    --text-2:           #5E5E63;
    --text-3:           #86868B;
    --text-4:           #AEAEB2;
    --bg-grouped:       #FCFCFC;

    /* radius / shadow (the companion worklog app :root --r-card + .hub-root light --shadow-card) */
    --r-card:        19px;
    --shadow-card:   0 0 0 0.5px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.05), 0 8px 20px rgba(0,0,0,0.05);
    /* #detail-panel drawer shadow — was inline rgba() at the rule site (2 spots incl. the
       dark @media override); promoted to a token so color stays centralized in this block. */
    --shadow-drawer: -8px 0 32px rgba(0, 0, 0, 0.18);

    /* hub accent palette (the companion worklog app .hub-root, light).
       --hub-divider is the companion worklog app's color-mix(in srgb, --border-strong 50%, --text-3)
       resolved to its flat sRGB average (#DCDCE0 + #86868B ÷ 2) — a plain hex here keeps
       the "no raw color outside this block" invariant without needing color-mix at the
       divider call sites. */
    --hub-divider:        #B1B1B6;
    --hub-mem:            #4040B8;
    --hub-mem-solid:      #8B8CEF;
    --hub-tk-amber:       #9A6700;
    --hub-track-doc:      #3A6EE0;
    --hub-track-research: #7A4FC0;
    --hub-track-app:      #2E7D52;
    --hub-track-lib:      #9A6700;
    --hub-track-mem:      #4040B8;

    /* track color_token palette — manifest tracks reference these directly
       (the companion worklog app globals.css catalog hue tokens, light) */
    --cat-1: #7A4FC0;
    --cat-2: #3A6EE0;
    --cat-3: #46A35E;
    --cat-4: #9B59B6;
    --cat-5: #C98A22;
    --cat-6: #5B8DEF;
    --cat-7: #dc2626;
    --cat-8: #4b5563;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --surface:          #1C1C1E;
      --surface-2:        #2C2C2E;
      --surface-3:        #3A3A3C;
      --surface-recessed: #161618;
      --border:           #38383A;
      --border-strong:    #48484A;
      --text:             #F5F5F7;
      --text-2:           #AEAEB2;
      --text-3:           #8E8E93;
      --text-4:           #636366;
      --bg-grouped:       #18181A;

      --shadow-card:   0 0 0 0.5px rgba(255,255,255,0.06), 0 1px 2px rgba(0,0,0,0.5);
      --shadow-drawer: -8px 0 32px rgba(0, 0, 0, 0.5);

      /* dark --hub-divider = the companion worklog app var(--hairline-strong) dark; kept as the
         --border-strong dark hex stand-in (unchanged this pass — reviewed OK). */
      --hub-divider:        #48484A;
      --hub-mem:            #A5A6F2;
      --hub-mem-solid:      #8B8CEF;
      --hub-tk-amber:       #E0B05A;
      --hub-track-doc:      #6B96F0;
      --hub-track-research: #A47AE0;
      --hub-track-app:      #5FC684;
      --hub-track-lib:      #D9A93C;
      --hub-track-mem:      #8B8CEF;

      --cat-1: #A47AE0;
      --cat-2: #6B96F0;
      --cat-3: #5FC684;
      --cat-4: #C07FE0;
      --cat-5: #E0B05A;
      --cat-6: #85AEFF;
      --cat-7: #f87171;
      --cat-8: #d1d5db;
    }
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg-grouped);
    color: var(--text);
    font-family: var(--font-ui);
    line-height: 1.55;
  }
  button, input { font-family: inherit; color: inherit; }
  button { cursor: pointer; }
  code, .mono { font-family: var(--font-mono); font-variant-ligatures: none; }

  /* ── header / sub-nav (ported from ViewHeader eyebrow+title + HubSubNav segmented tabs) ── */
  header.hub-header {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    flex-wrap: wrap;
    padding: 20px clamp(16px, 4vw, 48px) 16px;
    border-bottom: 1px solid var(--border);
    background: var(--bg-grouped);
  }
  .hub-eyebrow {
    font-size: var(--text-caption); font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.04em; color: var(--text-3); margin-bottom: 4px;
  }
  header.hub-header h1 {
    margin: 0; font-size: var(--text-display-sm); font-weight: 680;
    letter-spacing: -0.5px; color: var(--text);
  }
  .hub-meta { color: var(--text-3); font-size: var(--text-sm); margin-top: 5px; }
  .hub-meta code { background: var(--surface-2); border-radius: 4px; padding: 1px 5px; font-family: var(--font-mono); }
  nav.hub-tabs {
    display: inline-flex; gap: 2px; padding: 2px;
    background: var(--surface-2); border-radius: 999px; flex: none;
  }
  nav.hub-tabs button {
    font: inherit; font-size: var(--text-sm); font-weight: 500; color: var(--text-2);
    border: none; background: transparent; border-radius: 999px; padding: 6px 16px;
    transition: background 0.15s ease, color 0.15s ease;
  }
  nav.hub-tabs button[aria-pressed="true"] {
    background: var(--surface); color: var(--text); font-weight: 600; box-shadow: var(--shadow-card);
  }

  main { padding: 20px clamp(16px, 4vw, 48px) 80px; }
  section.hub-view[hidden] { display: none; }
  .hub-shell { max-width: 1440px; margin: 0 auto; }

  /* ── governance strip (1tier — core/*.md, repo-relative links) ── */
  .hub-gov {
    padding: 16px; border-radius: var(--r-card); background: var(--surface);
    box-shadow: var(--shadow-card);
    border: 1px solid color-mix(in srgb, var(--hub-track-lib) 20%, transparent);
    margin-bottom: 16px;
  }
  .hub-gov-head {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 12px; flex-wrap: wrap; gap: 8px;
  }
  .hub-gov-label { font-size: var(--text-sm); color: var(--hub-tk-amber); }
  .hub-gov-sub { font-size: var(--text-meta); color: var(--text-4); }
  .hub-gov-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
  .hub-gov-card {
    padding: 13px 15px; border-radius: var(--r-card); background: var(--surface-2);
    box-shadow: var(--shadow-card); text-decoration: none; display: block; color: inherit;
    min-width: 0; overflow: hidden;
  }
  .hub-gov-card-title {
    font-size: var(--text-sm); font-weight: 600; color: var(--hub-tk-amber);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .hub-gov-card-path {
    font-size: var(--text-micro); color: var(--text-4); margin: 2px 0 5px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .hub-gov-card-summary {
    font-size: var(--text-meta); color: var(--text-3); line-height: 1.5;
    overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  }

  /* ── layered region: depth rail + content column ── */
  .hub-layered { display: flex; gap: 16px; align-items: stretch; }
  .hub-depth-rail {
    width: 52px; flex: none; display: flex; flex-direction: column; align-items: center; padding: 6px 0;
  }
  .hub-depth-in { font-size: var(--text-sm); font-weight: 700; color: var(--hub-track-lib); }
  .hub-depth-core { font-size: var(--text-micro); color: var(--text-3); margin-top: 2px; letter-spacing: 1px; }
  .hub-depth-bar-a { flex: 1; width: 2px; margin: 9px 0; border-radius: 2px;
    background: linear-gradient(180deg, color-mix(in srgb, var(--hub-track-lib) 60%, transparent), var(--border)); }
  .hub-depth-bar-b { flex: 1; width: 2px; margin: 9px 0; border-radius: 2px;
    background: linear-gradient(180deg, var(--border), transparent); }
  .hub-depth-vert { writing-mode: vertical-rl; font-size: var(--text-micro); color: var(--text-3); letter-spacing: 3px; }
  .hub-content { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 12px; }

  /* L0 — utterance band */
  .hub-l0 {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    padding: 13px 16px; border-radius: var(--r-card); background: var(--surface-2);
    border: 1px solid color-mix(in srgb, var(--hub-track-lib) 20%, transparent);
  }
  .hub-l0-dot {
    width: 18px; height: 18px; border-radius: 50%; flex: none;
    border: 1.5px solid color-mix(in srgb, var(--hub-track-lib) 45%, transparent);
    display: inline-flex; align-items: center; justify-content: center;
  }
  .hub-l0-dot span { width: 9px; height: 9px; border-radius: 50%; background: var(--hub-track-lib); }
  .hub-l0-title { font-size: var(--text-sm); color: var(--text); font-weight: 500; }
  .hub-l0-sub { font-size: var(--text-meta); color: var(--text-3); }
  .hub-l0-examples { display: flex; gap: 7px; margin-left: auto; flex-wrap: wrap; }
  .hub-l0-example {
    font-size: var(--text-sm); color: var(--text-3); padding: 5px 9px;
    background: var(--surface); border-radius: 5px; box-shadow: var(--shadow-card);
  }

  /* main stack (L1-L3) + right rail row */
  .hub-main-row { display: flex; gap: 14px; }
  .hub-main-stack { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 12px; }
  .hub-card { padding: 16px; border-radius: var(--r-card); background: var(--surface); box-shadow: var(--shadow-card); }
  .hub-card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 6px; }
  .hub-card-head-left { display: flex; align-items: center; gap: 9px; }
  .hub-layer-dot {
    width: 18px; height: 18px; border-radius: 50%; flex: none;
    border: 1.5px solid color-mix(in srgb, var(--text-2) 22%, transparent);
    display: inline-flex; align-items: center; justify-content: center;
  }
  .hub-layer-dot span { width: 7px; height: 7px; border-radius: 50%; background: var(--text-2); }
  .hub-layer-id { font-size: var(--text-micro); color: var(--text-3); }
  .hub-layer-label { font-size: var(--text-sm); color: var(--text-3); }
  .hub-layer-label b { color: var(--text-4); font-weight: 400; }
  .hub-card-head-right { font-size: var(--text-meta); color: var(--text-4); }

  /* L1 track rows */
  .hub-tracks { display: flex; flex-direction: column; gap: 9px; }
  .hub-track-row {
    display: flex; align-items: stretch; border-radius: 7px; overflow: hidden;
    background: color-mix(in srgb, var(--tc, var(--text-3)) 5%, transparent);
    border: 1px solid color-mix(in srgb, var(--tc, var(--text-3)) 16%, transparent);
  }
  .hub-track-label {
    width: 132px; flex: none; display: flex; align-items: center; padding: 8px 12px;
    border-right: 1px solid color-mix(in srgb, var(--tc, var(--text-3)) 16%, transparent);
    font-size: var(--text-sm); font-weight: 600; color: var(--tc, var(--text-3));
  }
  .hub-track-steps { flex: 1; min-width: 0; display: flex; align-items: center; gap: 8px; padding: 8px 13px; flex-wrap: wrap; }
  .hub-step-chip {
    font-size: var(--text-meta); padding: 6px 10px; border-radius: 5px; white-space: nowrap;
    background: var(--surface-2); color: var(--tc, var(--text-3));
    border: 1px solid color-mix(in srgb, var(--tc, var(--text-3)) 30%, transparent);
  }
  button.hub-step-chip { cursor: pointer; }
  .hub-step-arrow { color: var(--border-strong); }
  .hub-track-gates { display: flex; gap: 6px; margin-left: 8px; flex-wrap: wrap; align-items: center; }
  .hub-gate-chip {
    font-size: var(--text-micro); color: var(--text-4); padding: 3px 8px; border-radius: 999px;
    border: 1px dashed color-mix(in srgb, var(--text-3) 45%, transparent); white-space: nowrap;
  }

  /* L2 agents */
  .hub-grid-agents { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
  .hub-agent-card {
    padding: 13px 14px; border-radius: 6px; background: var(--surface-2); box-shadow: var(--shadow-card);
    display: flex; flex-direction: column; gap: 6px; min-width: 0; overflow: hidden; text-align: left;
    border: none;
  }
  .hub-agent-name { font-size: var(--text-sm); color: var(--text); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hub-agent-blurb {
    font-size: var(--text-micro); color: var(--text-3); line-height: 1.45;
    overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  }
  .hub-agent-meta { font-size: var(--text-micro); color: var(--text-4); margin-top: auto; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* L3 artifacts */
  .hub-grid-artifacts { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(118px, 100%), 1fr)); gap: 10px; }
  .hub-artifact-card { padding: 11px 10px; border-radius: 6px; background: var(--surface-2); box-shadow: var(--shadow-card); text-align: center; }
  .hub-artifact-card.accent { border: 1px solid color-mix(in srgb, var(--hub-track-research) 25%, transparent); }
  .hub-artifact-dir { font-size: var(--text-micro); color: var(--text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hub-artifact-card.accent .hub-artifact-dir { color: var(--hub-track-research); }
  .hub-artifact-label { font-size: var(--text-micro); color: var(--text-4); margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* right rail — hooks only (memory rail intentionally excluded, per task scope) */
  .hub-rails { width: 270px; flex: none; display: flex; flex-direction: column; gap: 12px; }
  .hub-hooks-rail {
    padding: 16px 14px; border-radius: var(--r-card); background: var(--surface);
    border: 1px solid color-mix(in srgb, var(--hub-track-lib) 20%, transparent);
  }
  .hub-hooks-rail-title { font-size: var(--text-sm); color: var(--hub-tk-amber); margin-bottom: 4px; }
  .hub-hooks-rail-sub { font-size: var(--text-meta); color: var(--text-3); line-height: 1.5; margin-bottom: 14px; }
  .hub-hooks-list { display: flex; flex-direction: column; gap: 9px; }
  .hub-hook-card {
    padding: 9px 11px; border-radius: 6px; background: var(--surface-2); display: block;
    width: 100%; text-align: left; min-width: 0; overflow: hidden; border: 1px solid var(--hub-divider);
  }
  .hub-hook-card.hard { border-color: color-mix(in srgb, var(--hub-track-lib) 20%, transparent); }
  .hub-hook-name {
    font-size: var(--text-sm); color: var(--text-2); font-weight: 400; line-height: 1.3;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .hub-hook-card.hard .hub-hook-name { color: var(--text); font-weight: 600; }
  .hub-hook-badge { margin-left: 6px; font-size: var(--text-micro); color: var(--hub-track-lib); }
  .hub-hook-mono { font-size: var(--text-micro); color: var(--text-3); margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hub-hooks-rail-foot {
    margin-top: 12px; font-size: var(--text-meta); color: var(--text-4); line-height: 1.5;
    border-top: 1px solid var(--hub-divider); padding-top: 10px;
  }

  /* L4 transition + loops */
  .hub-l4-row { display: flex; align-items: center; gap: 12px; margin: 26px 0 16px; margin-left: 68px; margin-right: 284px; }
  .hub-l4-line { flex: 1; height: 1px; background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--text-3) 60%, transparent)); }
  .hub-l4-line.right { background: linear-gradient(90deg, color-mix(in srgb, var(--text-3) 60%, transparent), transparent); }
  .hub-l4-badge { display: inline-flex; align-items: center; gap: 9px; padding: 6px 14px; border-radius: 999px; background: var(--surface-2); border: 1px solid var(--hub-divider); }
  .hub-l4-badge .id { font-size: var(--text-micro); color: var(--text-3); }
  .hub-l4-badge .label { font-size: var(--text-sm); color: var(--text-2); font-weight: 600; }
  .hub-l4-badge .sub { font-size: var(--text-meta); color: var(--text-4); }
  .hub-grid-loops { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(236px, 100%), 1fr)); gap: 14px; margin-left: 68px; margin-right: 284px; }
  .hub-loop-card {
    padding: 14px 16px; border-radius: var(--r-card); background: var(--surface); box-shadow: var(--shadow-card);
    display: block; min-width: 0; overflow: hidden; text-align: left; width: 100%; border: none;
  }
  .hub-loop-top { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .hub-loop-name { font-size: var(--text-sm); font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 1; }
  .hub-loop-sched { font-size: var(--text-micro); flex: none; max-width: 132px; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: right; }
  .hub-loop-mono { font-size: var(--text-micro); color: var(--text-4); margin: 4px 0 7px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hub-loop-blurb {
    font-size: var(--text-sm); color: var(--text-3); line-height: 1.5;
    overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  }

  /* ── skill catalog view ── */
  .hub-skills-toolbar { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 16px; }
  .hub-search-wrap { position: relative; flex: 1; min-width: 220px; }
  .hub-search-icon { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); font-size: var(--text-sm); color: var(--text-3); }
  .hub-search-input {
    width: 100%; padding: 9px 12px 9px 32px; border-radius: 8px; background: var(--surface-2);
    border: 1px solid var(--border); color: var(--text); font-size: var(--text-sm); outline: none;
  }
  .hub-group-chips { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .hub-group-chip {
    display: inline-flex; align-items: center; padding: 5px 12px; border-radius: 6px;
    font-size: var(--text-sm); font-weight: 400; color: var(--text-3);
    background: transparent; border: 1px solid transparent; user-select: none;
  }
  .hub-group-chip[aria-pressed="true"] { font-weight: 600; color: var(--text); background: var(--surface-2); border-color: var(--border-strong); }
  .hub-skills-count { font-size: var(--text-micro); color: var(--text-3); }
  .hub-grid-skills { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(312px, 100%), 1fr)); gap: 14px; }
  .hub-skill-card {
    padding: 15px 17px; border-radius: var(--r-card); background: var(--surface); box-shadow: var(--shadow-card);
    display: block; width: 100%; text-align: left; min-width: 0; overflow: hidden; border: none;
  }
  .hub-skill-head { display: flex; align-items: center; gap: 9px; margin-bottom: 7px; flex-wrap: wrap; }
  .hub-skill-name { font-size: var(--text-sm); font-weight: 600; white-space: nowrap; }
  .hub-skill-group-badge { font-size: var(--text-micro); color: var(--text-3); padding: 2px 7px; border-radius: 4px; background: var(--surface-2); box-shadow: var(--shadow-card); white-space: nowrap; flex-shrink: 0; }
  .hub-skill-modes { font-size: var(--text-micro); color: var(--text-4); margin-left: auto; min-width: 0; max-width: 220px; flex-shrink: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hub-skill-blurb {
    font-size: var(--text-sm); color: var(--text-3); line-height: 1.55;
    overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  }
  .hub-empty { color: var(--text-2); font-size: var(--text-sm); padding: 32px 16px; text-align: center; line-height: 1.6; }

  /* ── detail side panel (generic per-item field dump; unchanged behavior) ── */
  #detail-panel {
    position: fixed; right: 0; top: 0; bottom: 0; width: min(360px, 92vw);
    background: var(--surface); box-shadow: var(--shadow-drawer);
    border-left: 1px solid var(--border);
    padding: 16px; overflow-y: auto; transform: translateX(100%);
    transition: transform 0.15s ease-out;
  }
  #detail-panel.open { transform: translateX(0); }
  #detail-panel h3 { margin: 0 0 4px; color: var(--text); }
  #detail-panel table { border-collapse: collapse; width: 100%; margin-top: 10px; }
  #detail-panel th, #detail-panel td {
    text-align: left; vertical-align: top; font-size: var(--text-meta);
    padding: 4px 6px; border-bottom: 1px solid var(--border); color: var(--text);
  }
  #detail-panel th { color: var(--text-3); font-weight: 500; width: 34%; white-space: nowrap; }
  #detail-close { all: unset; cursor: pointer; float: right; color: var(--text-3); font-size: var(--text-sm); }
  footer.hub-footer { color: var(--text-3); font-size: var(--text-micro); text-align: center; padding: 12px; }

  @media (max-width: 900px) {
    .hub-layered { flex-direction: column; }
    .hub-depth-rail { display: none; }
    .hub-main-row { flex-direction: column; }
    .hub-rails { width: 100%; }
    .hub-l4-row, .hub-grid-loops { margin-left: 0; margin-right: 0; }
  }
  @media (max-width: 700px) {
    .hub-gov-grid, .hub-grid-artifacts, .hub-grid-agents { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
</style>
</head>
<body>
<header class="hub-header">
  <div>
    <div class="hub-eyebrow">manifest.json · hub</div>
    <h1>에이전트 허브</h1>
    <div class="hub-meta" id="hub-meta"></div>
  </div>
  <nav class="hub-tabs">
    <button type="button" id="tab-map" aria-pressed="true">지도</button>
    <button type="button" id="tab-catalog" aria-pressed="false">스킬 카탈로그</button>
  </nav>
</header>
<main>
  <section class="hub-view" id="view-map">
    <div class="hub-shell">
      <div class="hub-gov">
        <div class="hub-gov-head">
          <span class="hub-gov-label mono">governance/</span>
          <span class="hub-gov-sub">core 정의 문서 — 아래 모든 층의 출처</span>
        </div>
        <div class="hub-gov-grid">
      __HUB_GOVERNANCE_HTML__
        </div>
      </div>
      <div id="hub-layered-mount"></div>
    </div>
  </section>
  <section class="hub-view" id="view-catalog" hidden>
    <div class="hub-shell" id="hub-catalog-mount"></div>
  </section>
</main>
<aside id="detail-panel" aria-hidden="true"></aside>
<footer class="hub-footer">자동 생성됨 · tools/render-hub.py · 직접 수정하지 마세요</footer>
<script type="application/json" id="hub-data">
__HUB_DATA_JSON__
</script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("hub-data").textContent);

  // fam -> accent token, restored to match the companion worklog app's own FAM_COLORS (not the
  // groupSkillsByTrack track-membership mapping, which is a different axis):
  // code->research, doc->doc, app|design->app, ops->mem (indigo; distinct from the
  // governance/hooks amber accent). pre/sub have no AA-safe accent of their own in
  // the source (its gray there is ~3.62:1, itself sub-AA) — de-emphasize to
  // --text-2 (~7:1 on white) instead of inheriting the --hub-track-lib amber
  // fallback, which collided with the governance/hooks "amber = curated" role.
  var FAM_TRACK_TOKEN = {
    doc: "--hub-track-doc",
    code: "--hub-track-research",
    app: "--hub-track-app",
    design: "--hub-track-app",
    ops: "--hub-track-mem"
  };
  function famToken(fam) {
    if (fam === "pre" || fam === "sub") return "var(--text-2)";
    var token = FAM_TRACK_TOKEN[fam];
    return token ? "var(" + token + ")" : "var(--text-2)";
  }
  // color_token as stored on a track row: usually a bare "--cat-N" name, but
  // tolerate an already-wrapped var()/hex/rgb value defensively.
  function cssToken(tok, fallback) {
    if (!tok) return fallback || "var(--text-3)";
    return /^(var\\(|#|rgb)/.test(tok) ? tok : "var(" + tok + ")";
  }
  // manifest tracks[].color_token is a generic --cat-N hue (not AA-tuned for light
  // mode: e.g. --cat-5 (library) label text on white is ~2.7:1, sub-AA). the companion worklog app's own
  // HUB_TRACKS uses separate --hub-track-{doc,research,app,lib} tokens tuned darker
  // for light-mode AA. Map by track.id to those instead; manifest.json itself is
  // untouched and iteration order/steps/gates still come straight from it — only
  // the color *interpretation* is replaced. Unknown ids fall back to the raw
  // color_token so a future track family doesn't silently go uncolored.
  var TRACK_ID_TO_HUB_TOKEN = {
    "research-lab": "--hub-track-research",
    "library": "--hub-track-lib",
    "document": "--hub-track-doc",
    "app": "--hub-track-app"
  };
  function trackColor(track) {
    var known = TRACK_ID_TO_HUB_TOKEN[track.id];
    return known ? "var(" + known + ")" : cssToken(track.color_token, "var(--hub-track-lib)");
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    for (var key in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, key)) continue;
      if (key === "class") node.className = attrs[key];
      else if (key === "text") node.textContent = attrs[key];
      else node.setAttribute(key, attrs[key]);
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function orEmpty(value) {
    if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
    if (value === null || value === undefined || value === "") return "—";
    return String(value);
  }

  // ── generic per-item detail panel: dumps every field of the clicked item, so
  //    no per-kind field list needs to be hardcoded here. ──
  var panel = document.getElementById("detail-panel");
  function hideDetail() {
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
  }
  function showDetail(kindLabel, item) {
    while (panel.firstChild) panel.removeChild(panel.firstChild);
    panel.appendChild(el("button", { id: "detail-close", type: "button", text: "닫기" }, []));
    panel.appendChild(el("h3", { text: item.name || item.id || item.slug || kindLabel }, []));
    panel.appendChild(el("div", { class: "hub-meta", text: kindLabel }, []));
    var table = el("table", {}, []);
    Object.keys(item).forEach(function (key) {
      var raw = item[key];
      var value = Array.isArray(raw) ? raw.join(", ") : (raw && typeof raw === "object" ? JSON.stringify(raw) : String(raw));
      table.appendChild(el("tr", {}, [
        el("th", { text: key }, []),
        el("td", { text: value === "" ? "—" : value }, []),
      ]));
    });
    panel.appendChild(table);
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
    document.getElementById("detail-close").addEventListener("click", hideDetail);
  }

  function renderMeta() {
    var meta = document.getElementById("hub-meta");
    var parts = [
      DATA.skills.length + " skills",
      DATA.agents.length + " agents",
      DATA.hooks.length + " hooks",
      DATA.loops.length + " loops",
      DATA.tracks.length + " tracks",
    ];
    meta.appendChild(el("span", { class: "mono", text: DATA.generated_from + " · manifest v" + orEmpty(DATA.manifest_version) }, []));
    meta.appendChild(document.createTextNode(" · "));
    meta.appendChild(el("span", { class: "mono" }, [parts.join(" · ")]));
  }

  // ── L0 utterance band ──
  function renderL0() {
    var examples = ["\\"이 코드 분석해줘\\"", "\\"X 기능 추가\\"", "\\"실험 결과 비교\\""].map(function (ex) {
      return el("span", { class: "hub-l0-example mono" }, [ex]);
    });
    return el("div", { class: "hub-l0" }, [
      el("span", { class: "hub-l0-dot" }, [el("span", {}, [])]),
      el("span", { class: "hub-layer-id mono" }, ["L0"]),
      el("span", { class: "hub-l0-title" }, ["자연어 한 줄로 스킬을 부른다"]),
      el("span", { class: "hub-l0-sub" }, ["· 사용자 · 운전자"]),
      el("div", { class: "hub-l0-examples" }, examples),
    ]);
  }

  // ── L1 skill-track rows ──
  function renderTracks() {
    var skillBySlug = {};
    DATA.skills.forEach(function (skill) { skillBySlug[skill.slug] = skill; });
    var card = el("div", { class: "hub-card" }, [
      el("div", { class: "hub-card-head" }, [
        el("span", { class: "hub-card-head-left" }, [
          el("span", { class: "hub-layer-dot" }, [el("span", {}, [])]),
          el("span", { class: "hub-layer-id mono" }, ["L1"]),
          el("span", { class: "hub-layer-label mono" }, ["skills/ ", el("b", {}, ["— 동사 · 추적형 파이프"])]),
        ]),
        el("span", { class: "hub-card-head-right" }, ["작업 본질에 맞춰 4 트랙 · 🔒 hook 게이트"]),
      ]),
    ]);
    var rows = el("div", { class: "hub-tracks" }, []);
    // app last, stable order otherwise (matches the reference's visual rhythm).
    var ordered = DATA.tracks.slice().sort(function (a, b) {
      return (a.id === "app" ? 1 : 0) - (b.id === "app" ? 1 : 0);
    });
    ordered.forEach(function (track) {
      var color = trackColor(track);
      var row = el("div", { class: "hub-track-row" }, []);
      row.style.setProperty("--tc", color);
      row.appendChild(el("div", { class: "hub-track-label" }, [track.label]));
      var steps = el("div", { class: "hub-track-steps" }, []);
      (track.steps || []).forEach(function (slug, i) {
        var skill = skillBySlug[slug];
        var label = skill ? skill.name : slug.replace(/^skill__/, "");
        var chip = el("button", { type: "button", class: "hub-step-chip mono", title: label + " 설명 보기" }, [label]);
        chip.addEventListener("click", function () { showDetail("스킬", skill || { slug: slug }); });
        steps.appendChild(chip);
        if (i < track.steps.length - 1) steps.appendChild(el("span", { class: "hub-step-arrow" }, ["→"]));
      });
      if (track.gates && track.gates.length) {
        var gatesWrap = el("span", { class: "hub-track-gates" }, []);
        track.gates.forEach(function (gate) {
          gatesWrap.appendChild(el("span", { class: "hub-gate-chip mono" }, ["🔒 " + gate]));
        });
        steps.appendChild(gatesWrap);
      }
      row.appendChild(steps);
      rows.appendChild(row);
    });
    card.appendChild(rows);
    return card;
  }

  // ── L2 agents ──
  function renderAgents() {
    var card = el("div", { class: "hub-card" }, [
      el("div", { class: "hub-card-head" }, [
        el("span", { class: "hub-card-head-left" }, [
          el("span", { class: "hub-layer-dot" }, [el("span", {}, [])]),
          el("span", { class: "hub-layer-id mono" }, ["L2"]),
          el("span", { class: "hub-layer-label mono" }, ["agents/ ", el("b", {}, ["— skill 이 일을 맡기는 팀 (" + DATA.agents.length + ")"])]),
        ]),
      ]),
    ]);
    var grid = el("div", { class: "hub-grid-agents" }, []);
    DATA.agents.forEach(function (agent) {
      var btn = el("button", { type: "button", class: "hub-agent-card" }, [
        el("div", { class: "hub-agent-name" }, [agent.name]),
      ]);
      if (agent.blurb) btn.appendChild(el("div", { class: "hub-agent-blurb" }, [agent.blurb]));
      var metaText = (agent.model || "—") + (agent.modes && agent.modes.length ? " · " + agent.modes.join(", ") : "");
      btn.appendChild(el("div", { class: "hub-agent-meta mono" }, [metaText]));
      btn.addEventListener("click", function () { showDetail("에이전트", agent); });
      grid.appendChild(btn);
    });
    if (DATA.agents.length === 0) grid.appendChild(el("span", { class: "hub-empty" }, ["ingest 대기"]));
    card.appendChild(grid);
    return card;
  }

  // ── L3 artifact folders (fixed 6, code-level curation — not manifest-driven) ──
  var ARTIFACT_DIRS = [
    { dir: "research/",    label: "분야 조사" },
    { dir: "analysis/",    label: "사전 분석" },
    { dir: "documents/",   label: "문서" },
    { dir: "spec/",        label: "청사진 · 최신", accent: true },
    { dir: "plans/",       label: "작업 사이클" },
    { dir: "experiments/", label: "실험" },
  ];
  function renderArtifacts() {
    var card = el("div", { class: "hub-card" }, [
      el("div", { class: "hub-card-head" }, [
        el("span", { class: "hub-card-head-left" }, [
          el("span", { class: "hub-layer-dot" }, [el("span", {}, [])]),
          el("span", { class: "hub-layer-id mono" }, ["L3"]),
          el("span", { class: "hub-layer-label mono" }, [".agent_reports/ ", el("b", {}, ["— 산출물이 쌓이는 곳"])]),
        ]),
        el("span", { class: "hub-card-head-right" }, ["코드 = spec/ + plans/ 형제 2-bucket"]),
      ]),
    ]);
    var grid = el("div", { class: "hub-grid-artifacts" }, []);
    ARTIFACT_DIRS.forEach(function (a) {
      grid.appendChild(el("div", { class: "hub-artifact-card" + (a.accent ? " accent" : "") }, [
        el("div", { class: "hub-artifact-dir mono" }, [a.dir]),
        el("div", { class: "hub-artifact-label" }, [a.label]),
      ]));
    });
    card.appendChild(grid);
    return card;
  }

  // ── right rail: hooks (memory rail intentionally excluded) ──
  function renderHooksRail() {
    var curated = DATA.hooks.filter(function (h) {
      return h.hard_block === true || (h.mono && h.mono.indexOf("mem-") === 0);
    });
    var rail = el("div", { class: "hub-hooks-rail" }, [
      el("div", { class: "hub-hooks-rail-title mono" }, ["hooks/"]),
      el("div", { class: "hub-hooks-rail-sub" }, ["결정론 가드 — 판단 대신 코드가 강제"]),
    ]);
    var list = el("div", { class: "hub-hooks-list" }, []);
    curated.forEach(function (hk) {
      var card = el("button", { type: "button", class: "hub-hook-card" + (hk.hard_block ? " hard" : "") }, []);
      var nameLine = el("div", { class: "hub-hook-name" }, [hk.name]);
      if (hk.hard_block) nameLine.appendChild(el("span", { class: "hub-hook-badge mono" }, ["HARD"]));
      card.appendChild(nameLine);
      card.appendChild(el("div", { class: "hub-hook-mono mono" }, [hk.mono]));
      card.addEventListener("click", function () { showDetail("훅", hk); });
      list.appendChild(card);
    });
    if (curated.length === 0) list.appendChild(el("span", { class: "hub-empty" }, ["ingest 대기"]));
    rail.appendChild(list);
    var hardCount = curated.filter(function (h) { return h.hard_block === true; }).length;
    var memCount = curated.length - hardCount;
    rail.appendChild(el("div", { class: "hub-hooks-rail-foot" }, [
      hardCount + "개 하드 차단 게이트" + (memCount ? " + 메모리 훅 " + memCount + "개" : ""),
    ]));
    return rail;
  }

  // ── L4 divider + loops ──
  function renderL4() {
    var frag = document.createDocumentFragment();
    frag.appendChild(el("div", { class: "hub-l4-row" }, [
      el("div", { class: "hub-l4-line" }, []),
      el("span", { class: "hub-l4-badge" }, [
        el("span", { class: "id mono" }, ["L4"]),
        el("span", { class: "label" }, ["세션 밖 · loops"]),
        el("span", { class: "sub" }, ["가장 바깥 · 알아서 돎"]),
      ]),
      el("div", { class: "hub-l4-line right" }, []),
    ]));
    var grid = el("div", { class: "hub-grid-loops" }, []);
    DATA.loops.forEach(function (lp) {
      var card = el("button", { type: "button", class: "hub-loop-card" }, [
        el("div", { class: "hub-loop-top" }, [
          el("span", { class: "hub-loop-name" }, [lp.name]),
          el("span", { class: "hub-loop-sched mono", style: lp.type === "event" ? "color:var(--hub-track-lib)" : "color:var(--hub-track-app)" }, [lp.schedule || (lp.type === "event" ? "사건형" : "")]),
        ]),
        el("div", { class: "hub-loop-mono mono" }, [lp.mono + " · " + (lp.type === "event" ? "사건형" : "시간형")]),
      ]);
      if (lp.blurb) card.appendChild(el("div", { class: "hub-loop-blurb" }, [lp.blurb]));
      card.addEventListener("click", function () { showDetail("루프", lp); });
      grid.appendChild(card);
    });
    if (DATA.loops.length === 0) grid.appendChild(el("span", { class: "hub-empty" }, ["루프 ingest 대기"]));
    frag.appendChild(grid);
    return frag;
  }

  function renderMap() {
    var mount = document.getElementById("hub-layered-mount");
    var depthRail = el("div", { class: "hub-depth-rail" }, [
      el("span", { class: "hub-depth-in" }, ["안"]),
      el("span", { class: "hub-depth-core mono" }, ["CORE"]),
      el("div", { class: "hub-depth-bar-a" }, []),
      el("span", { class: "hub-depth-vert mono" }, ["세션 안"]),
      el("div", { class: "hub-depth-bar-b" }, []),
      el("span", {}, ["↓"]),
    ]);
    var mainRow = el("div", { class: "hub-main-row" }, [
      el("div", { class: "hub-main-stack" }, [renderTracks(), renderAgents(), renderArtifacts()]),
      el("div", { class: "hub-rails" }, [renderHooksRail()]),
    ]);
    var content = el("div", { class: "hub-content" }, [renderL0(), mainRow]);
    mount.appendChild(el("div", { class: "hub-layered" }, [depthRail, content]));
    mount.appendChild(renderL4());
  }

  // ── skill catalog view ──
  var GROUP_LABELS = { entry: "파이프", pre: "사전분석", ops: "운영", sub: "sub" };
  var GROUP_ORDER = ["entry", "pre", "ops", "sub"];
  var skillsState = { query: "", group: "all" };

  function renderCatalog() {
    var mount = document.getElementById("hub-catalog-mount");
    while (mount.firstChild) mount.removeChild(mount.firstChild);

    var toolbar = el("div", { class: "hub-skills-toolbar" }, []);
    var searchWrap = el("div", { class: "hub-search-wrap" }, [
      el("span", { class: "hub-search-icon" }, ["⌕"]),
    ]);
    var input = el("input", { class: "hub-search-input", type: "text", placeholder: "스킬·의의 검색…", value: skillsState.query }, []);
    input.addEventListener("input", function (e) { skillsState.query = e.target.value; renderCatalog(); });
    searchWrap.appendChild(input);
    toolbar.appendChild(searchWrap);

    var chips = el("div", { class: "hub-group-chips" }, []);
    var allChip = el("button", { type: "button", class: "hub-group-chip", "aria-pressed": String(skillsState.group === "all") }, ["전체"]);
    allChip.addEventListener("click", function () { skillsState.group = "all"; renderCatalog(); });
    chips.appendChild(allChip);
    GROUP_ORDER.forEach(function (gk) {
      var chip = el("button", { type: "button", class: "hub-group-chip", "aria-pressed": String(skillsState.group === gk) }, [GROUP_LABELS[gk]]);
      chip.addEventListener("click", function () { skillsState.group = gk; renderCatalog(); });
      chips.appendChild(chip);
    });
    toolbar.appendChild(chips);

    var filtered = DATA.skills.filter(function (sk) {
      if (skillsState.group !== "all" && sk.group !== skillsState.group) return false;
      if (skillsState.query.trim()) {
        var q = skillsState.query.trim().toLowerCase();
        var hay = (sk.name + " " + (sk.blurb || "")).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
    toolbar.appendChild(el("span", { class: "hub-skills-count mono" }, [filtered.length + " / " + DATA.skills.length]));
    mount.appendChild(toolbar);

    if (filtered.length === 0) {
      mount.appendChild(el("div", { class: "hub-empty" }, ["일치하는 스킬이 없습니다."]));
      return;
    }
    var grid = el("div", { class: "hub-grid-skills" }, []);
    filtered.forEach(function (sk) {
      var groupLabel = GROUP_LABELS[sk.group] || sk.group;
      var head = el("div", { class: "hub-skill-head" }, [
        el("span", { class: "hub-skill-name mono", style: "color:" + famToken(sk.fam) }, [sk.name]),
        el("span", { class: "hub-skill-group-badge" }, [groupLabel]),
      ]);
      if (sk.modes && sk.modes.length) {
        head.appendChild(el("span", { class: "hub-skill-modes mono", title: sk.modes.join(" · ") }, [sk.modes.join(" · ")]));
      }
      var card = el("button", { type: "button", class: "hub-skill-card" }, [head]);
      if (sk.blurb) card.appendChild(el("div", { class: "hub-skill-blurb" }, [sk.blurb]));
      card.addEventListener("click", function () { showDetail("스킬", sk); });
      grid.appendChild(card);
    });
    mount.appendChild(grid);
  }

  function activateTab(name) {
    var mapView = document.getElementById("view-map");
    var catalogView = document.getElementById("view-catalog");
    var mapTab = document.getElementById("tab-map");
    var catalogTab = document.getElementById("tab-catalog");
    var showMap = name === "map";
    mapView.hidden = !showMap;
    catalogView.hidden = showMap;
    mapTab.setAttribute("aria-pressed", String(showMap));
    catalogTab.setAttribute("aria-pressed", String(!showMap));
  }

  document.getElementById("tab-map").addEventListener("click", function () { activateTab("map"); });
  document.getElementById("tab-catalog").addEventListener("click", function () { activateTab("catalog"); });

  renderMeta();
  renderMap();
  renderCatalog();
})();
</script>
</body>
</html>
"""


def render(manifest):
    payload = _embed_json(build_view(manifest))
    text = PAGE_TEMPLATE.replace(DATA_PLACEHOLDER, payload)
    text = text.replace(GOVERNANCE_PLACEHOLDER, build_governance_html())
    text = text.replace("__HUB_FONT_CSS__", d2coding_font_css())
    return text


def main(argv):
    check = "--check" in argv
    manifest = load_manifest()
    text = render(manifest)
    if check:
        existing = ""
        if os.path.exists(HUB_PATH):
            with open(HUB_PATH, encoding="utf-8") as fh:
                existing = fh.read()
        if text != existing:
            sys.stderr.write("hub.html drift: run `python3 tools/render-hub.py`\n")
            return 1
        print("hub.html up-to-date")
        return 0
    with open(HUB_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    print("wrote %s" % os.path.relpath(HUB_PATH, REPO_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
