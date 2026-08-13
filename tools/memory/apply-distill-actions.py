#!/usr/bin/env python3
"""Apply memory distillation JSON-lines actions.

The distiller model only proposes JSON objects. This script owns shape checks,
snapshot membership checks, and argv-only calls into mem.py.
"""
import argparse
import json
import os
import subprocess
import sys

AUTOMATIC_TYPES = {
    "decision", "user-correction", "unresolved-obligation", "artifact-pointer",
}
CAPSULE_LISTS = {
    "aliases": "--alias", "entities": "--entity", "topics": "--topic",
    "artifact_refs": "--artifact-ref",
}


def _load_snapshot_ids(path):
    if not path:
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            return set(fh.read().split())
    except OSError:
        return set()


def apply_actions(out_path, mem_path, mode="increment", snapshot_ids_path="",
                  deny_reattribute=False):
    # In curate mode this is a destructive allowlist, not every id printed in the
    # snapshot. `curate-snapshot` deliberately omits PROTECTED PENDING handoff/
    # thread ids, so model output cannot prune or merge them through this layer.
    destructive_ids = _load_snapshot_ids(snapshot_ids_path)

    def member(rid):
        return (mode != "curate") or (rid in destructive_ids)

    # D-37: mode=curate is the D-18 session-end curator path. Attribute its
    # journal actor deterministically as curator rather than distiller, even
    # when the parent runs with MEM_DISTILL=1.
    mem_env = os.environ.copy()
    if mode == "curate":
        mem_env["MEM_ACTOR"] = "curator"

    try:
        with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        lines = []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        try:
            rec = json.loads(line)
        except Exception:
            sys.stderr.write(f"[distill-parse] skip malformed: {line[:120]!r}\n")
            continue
        if not isinstance(rec, dict):
            sys.stderr.write("[distill-parse] skip non-object\n")
            continue

        action = rec.get("action")
        if action is None and rec.get("tier") and rec.get("type") and isinstance(rec.get("body"), str):
            action = "add"

        # `mem delete` is a user-controlled path and is never a curator action.
        # Keep this explicit so a future mem.py delete surface cannot accidentally
        # become reachable from untrusted distiller output.
        if action == "delete":
            sys.stderr.write("[distill-parse] skip delete: unsupported destructive action\n")
            continue

        # increment = add-only, enforced (not merely prompted). The turn-nudge/fast
        # tier reads untrusted transcript delta with no snapshot whitelist, so a
        # prompt-injected model could name id-mutations (prune/merge/graduate/...)
        # that member() would wave through under mode != "curate" (always True).
        # Reject id-mutations outside curate mode so only the snapshot-grounded deep
        # curator can ever delete/merge/graduate. Closes the P-25 whitelist bypass
        # for every adapter at the shared applier (deterministic, §0.5).
        if action in ("reinforce", "prune", "graduate", "reattribute", "merge", "supersede") and mode != "curate":
            sys.stderr.write(f"[distill-parse] skip {action}: id-mutation not allowed in {mode} mode (add-only)\n")
            continue

        # Periodic curation runs without conversation evidence; adopting orphan
        # records under that blindness is guesswork (2026-08-13 field run:
        # 40 foreign records absorbed). The dispatcher passes --deny-reattribute
        # for that mode so the denial is enforced on untrusted worker output,
        # not merely requested in the prompt.
        if action == "reattribute" and deny_reattribute:
            sys.stderr.write("[distill-parse] skip reattribute: denied in periodic curation\n")
            continue

        if action == "add":
            tier = rec.get("tier")
            rtype = rec.get("type")
            body = rec.get("body")
            if tier not in ("working", "durable"):
                sys.stderr.write(f"[distill-parse] skip bad tier: {tier!r}\n")
                continue
            if rtype not in AUTOMATIC_TYPES:
                sys.stderr.write(f"[distill-parse] skip unsupported automatic type: {rtype!r}\n")
                continue
            if not isinstance(body, str) or not body:
                sys.stderr.write("[distill-parse] skip missing/empty body\n")
                continue
            if len(body) > 2000:
                sys.stderr.write(f"[distill-parse] skip body too long ({len(body)})\n")
                continue
            headline = rec.get("headline")
            if not isinstance(headline, str) or not headline.strip() or len(headline) > 240:
                sys.stderr.write("[distill-parse] skip missing/invalid headline\n")
                continue
            capsule = {}
            capsule_ok = True
            for field in CAPSULE_LISTS:
                value = rec.get(field, [])
                if (not isinstance(value, list) or len(value) > 24
                        or not all(isinstance(item, str) and item.strip() and len(item) <= 160
                                   for item in value)):
                    sys.stderr.write(f"[distill-parse] skip invalid {field}\n")
                    capsule_ok = False
                    break
                capsule[field] = value
            if not capsule_ok:
                continue
            if rtype == "artifact-pointer" and not capsule["artifact_refs"]:
                sys.stderr.write("[distill-parse] skip artifact-pointer without artifact_refs\n")
                continue
            argv = ["python3", mem_path, "add", tier, rtype, body, "--headline", headline]
            for field, option in CAPSULE_LISTS.items():
                for value in capsule[field]:
                    argv.extend([option, value])
            subprocess.run(argv, env=mem_env)

        elif action in ("reinforce", "prune", "graduate", "reattribute"):
            rid = rec.get("id")
            if not isinstance(rid, str) or not rid:
                sys.stderr.write(f"[distill-parse] skip {action}: missing id\n")
                continue
            if not member(rid):
                sys.stderr.write(f"[distill-parse] skip non-destructive-allowlist id ({action}): {rid!r}\n")
                continue
            if action == "graduate":
                subprocess.run(["python3", mem_path, "graduate", rid, "--to", "durable"], env=mem_env)
            else:
                subprocess.run(["python3", mem_path, action, rid], env=mem_env)

        elif action == "merge":
            ids = rec.get("ids")
            canonical = rec.get("canonical")
            if (not isinstance(ids, list) or len(ids) < 2
                    or not all(isinstance(i, str) and i for i in ids)):
                sys.stderr.write("[distill-parse] skip merge: bad ids\n")
                continue
            if not isinstance(canonical, str) or canonical not in ids:
                sys.stderr.write("[distill-parse] skip merge: bad canonical\n")
                continue
            if not all(member(i) for i in ids):
                sys.stderr.write("[distill-parse] skip merge: id outside destructive allowlist\n")
                continue
            subprocess.run(["python3", mem_path, "merge", "--canonical", canonical, *ids], env=mem_env)

        elif action == "supersede":
            rid = rec.get("id")
            by_rid = rec.get("by")
            if not all(isinstance(value, str) and value for value in (rid, by_rid)):
                sys.stderr.write("[distill-parse] skip supersede: missing id/by\n")
                continue
            if not member(rid) or not member(by_rid):
                sys.stderr.write("[distill-parse] skip supersede: id outside destructive allowlist\n")
                continue
            subprocess.run(["python3", mem_path, "supersede", rid, "--by", by_rid], env=mem_env)

        else:
            sys.stderr.write(f"[distill-parse] skip unknown action: {action!r}\n")

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("out_path")
    parser.add_argument("mem_path")
    parser.add_argument("--mode", choices=("increment", "curate"), default="increment")
    parser.add_argument("--snapshot-ids", default="")
    parser.add_argument("--deny-reattribute", action="store_true",
                        help="Reject reattribute actions (periodic curation)")
    args = parser.parse_args(argv)
    return apply_actions(args.out_path, args.mem_path, args.mode, args.snapshot_ids,
                         deny_reattribute=args.deny_reattribute)


if __name__ == "__main__":
    raise SystemExit(main())
