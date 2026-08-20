"""Self-contained render capture for plan item (3) — not the plan's exp5/render_probe
shard scripts (write_scope doesn't cover those); builds two owner cards directly with
Session/DispatchJob so no snap.json dependency is needed.

Scenario A: descendant present (depth-2 child still 'working').
Scenario B: every descendant folded to 'done' (the F-81 regression this item fixes).
"""
import sys, os, re

FLEET_ROOT = sys.argv[1]   # .../tools
TAG = sys.argv[2]          # "before" | "after"

sys.path.insert(0, FLEET_ROOT)
from fleet.model import Session, DispatchJob, ProgressProjection, WorkProjection
from fleet import render

ROUTE = [("frame", "done"), ("plan", "done"), ("plan-check", "done"),
         ("execute", "active"), ("impl-review", "pending"),
         ("test", "pending"), ("report", "pending")]

def _owner(slug, liveness="working"):
    nodes = [{"id": nid, "state": st} for nid, st in ROUTE]
    work = WorkProjection(
        source="route-exact", route_id="rt-cap-" + slug, stage_label="execute",
        node_state="active", progress=ProgressProjection(3, 7),
        _route_view={"view": {"nodes": nodes}},
    )
    return DispatchJob(key="code", slug=slug, cwd="/tmp/cap-" + slug, harness="claude",
                       depth=1, intensity="thorough", liveness=liveness,
                       work_projection=work)

def _text(l):
    return "".join(s[0] if isinstance(s, (list, tuple)) else str(s) for s in l)

def build(width, layout, descendants_done):
    owner = _owner("cap-owner-a" if not descendants_done else "cap-owner-b")
    session = Session(harness="claude", pid=9100 if not descendants_done else 9101,
                      proc_start="root", cwd=owner.cwd, session_id="sid-cap-" + owner.slug,
                      slug="cap-parent", liveness="working")
    owner.parent_sid = session.session_id
    owner.is_child = True
    jobs = [owner]
    if descendants_done:
        child = DispatchJob(key="code-execute", slug=owner.slug + "-child", cwd=owner.cwd,
                            harness="claude", depth=2, liveness="done",
                            parent_slug=owner.slug, is_child=True)
        jobs.append(child)
    else:
        child = DispatchJob(key="code-execute", slug=owner.slug + "-child", cwd=owner.cwd,
                            harness="claude", depth=2, liveness="working",
                            parent_slug=owner.slug, is_child=True)
        jobs.append(child)
    lines = render._build_lines([session], jobs, "both", False, [], layout=layout,
                                term_width=width)
    out = []
    for l in lines:
        if l is None:
            out.append("")
        else:
            out.append(re.sub(r"\x00[^\x00]*\x00", "", _text(l)).rstrip())
    return out

def dump(term_width, layout, tint, descendants_done):
    render._TINT_OK = tint
    render.set_show_all(False)
    lines = build(term_width, layout, descendants_done)
    return "\n".join(lines)

variants = [
    (140, "wide", False, False, "140_tint-off_desc-live"),
    (140, "wide", False, True,  "140_tint-off_desc-done"),
    (140, "wide", True,  False, "140_tint-on_desc-live"),
    (140, "wide", True,  True,  "140_tint-on_desc-done"),
    (80,  "narrow", False, False, "80_tint-off_desc-live"),
    (80,  "narrow", False, True,  "80_tint-off_desc-done"),
    (80,  "narrow", True,  False, "80_tint-on_desc-live"),
    (80,  "narrow", True,  True,  "80_tint-on_desc-done"),
]

outdir = sys.argv[3]
os.makedirs(outdir, exist_ok=True)
for width, layout, tint, done, label in variants:
    text = dump(width, layout, tint, done)
    path = os.path.join(outdir, "%s_%s.txt" % (TAG, label))
    with open(path, "w") as f:
        f.write(text + "\n")
    print("wrote", path)
