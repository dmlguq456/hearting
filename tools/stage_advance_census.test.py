import json, subprocess, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class CensusTest(unittest.TestCase):
 def test_schema_and_invalid(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td); (p/'a.json').write_text(json.dumps({'advance_class':'sealed','nodes':[]}));
   r=subprocess.run(['python3',str(ROOT/'tools/stage-advance-census.py'),'--routes',td,'--topologies',str(ROOT/'capabilities/topologies.json'),'--json'],capture_output=True,text=True)
   self.assertEqual(r.returncode,0); self.assertEqual(json.loads(r.stdout)['sealed_route_total'],1)
if __name__=='__main__': unittest.main()
