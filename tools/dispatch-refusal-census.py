#!/usr/bin/env python3
import argparse,json
from pathlib import Path
def rows(path):
    out=[]
    p=Path(path)
    if p.is_file():
        for line in p.read_text(errors="replace").splitlines():
            try: out.append(json.loads(line))
            except ValueError: pass
    return out
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--jobs",required=True); ap.add_argument("--state-root",required=True); ap.add_argument("--route-id"); ap.add_argument("--json",action="store_true"); a=ap.parse_args()
    jobs=rows(a.jobs); attempts=[r for r in jobs if r.get("eligibility_failure_class")]
    root=Path(a.state_root)/"launch-tuple"; files=list(root.glob("*.jsonl")); report=list((root/"_report").glob("*.jsonl")) if (root/"_report").is_dir() else []
    rej=[x for p in files+report for x in rows(p) if not a.route_id or x.get("route_id")==a.route_id]
    unrecorded=sum(int(x.get("unrecorded",0) or 0) for x in rej)
    if not attempts and not rej: interpretation="no-rejection-observed"
    elif not attempts and rej: interpretation="rejection-recorded-outside-attempt"
    elif attempts and not rej: interpretation="evidence-write-gap"
    else: interpretation="mixed"
    payload={"schema_version":1,"attempt_rows":len(attempts),"eligibility_failure_nonempty":len(attempts),"launch_tuple_rejections":len(rej),"launch_tuple_unrecorded":unrecorded,"rejection_classes":sorted({x.get("rejection_class") for x in rej if x.get("rejection_class")}),"interpretation":interpretation}
    print(json.dumps(payload,sort_keys=True) if a.json else interpretation); return 0
if __name__ == "__main__": main()
