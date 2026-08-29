import json, subprocess, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class CensusTest(unittest.TestCase):
 def test_four_interpretations(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td); j=p/'jobs.log'; j.write_text('');
   r=subprocess.run(['python3',str(ROOT/'tools/dispatch-refusal-census.py'),'--jobs',str(j),'--state-root',td,'--json'],capture_output=True,text=True)
   self.assertEqual(json.loads(r.stdout)['interpretation'],'no-rejection-observed')
if __name__=='__main__': unittest.main()
