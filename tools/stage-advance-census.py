#!/usr/bin/env python3
import argparse, json, sys
from datetime import datetime
from pathlib import Path

def load_routes(path):
    routes=[]; invalid=[]
    for p in sorted(Path(path).glob("*.json")):
        try:
            row=json.loads(p.read_text()); row["_path"]=str(p); routes.append(row)
        except (OSError, ValueError): invalid.append(p.name)
    return routes, invalid
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--routes",required=True); ap.add_argument("--topologies",required=True); ap.add_argument("--json",action="store_true"); a=ap.parse_args()
    routes, invalid=load_routes(a.routes)
    sealed=[r for r in routes if r.get("advance_class") or r.get("sealed") or r.get("advance_sealed")]
    nodes={"runtime_eligible":0,"model_required":0,"unsealed":0}
    for r in routes:
        for n in r.get("nodes",[]) if isinstance(r.get("nodes"),list) else []:
            if n.get("runtime_eligible"): nodes["runtime_eligible"]+=1
            if n.get("model_required"): nodes["model_required"]+=1
            if not n.get("advance_class") and not n.get("sealed"): nodes["unsealed"]+=1
    dates=[]
    for r in sealed:
        for key in ("created_at","created","mtime"):
            if r.get(key):
                try: dates.append(datetime.fromisoformat(str(r[key]).replace("Z","+00:00"))); break
                except ValueError: pass
    payload={"schema_version":1,"route_total":len(routes),"sealed_route_total":len(sealed),"sealed_route_percent":round(100*len(sealed)/len(routes),2) if routes else 0,"node_counts":nodes,"sealed_window":{"first":min(dates).isoformat() if dates else None,"last":max(dates).isoformat() if dates else None,"span_days":(max(dates)-min(dates)).total_seconds()/86400 if len(dates)>1 else 0,"arrival_routes_per_day":len(sealed)/max((max(dates)-min(dates)).total_seconds()/86400,1) if dates else 0},"route_axis":{},"recipe_axis":{},"invalid_routes":invalid}
    print(json.dumps(payload,sort_keys=True) if a.json else f"routes={len(routes)} sealed={len(sealed)} invalid={len(invalid)}")
    return 1 if invalid else 0
if __name__ == "__main__": raise SystemExit(main())
