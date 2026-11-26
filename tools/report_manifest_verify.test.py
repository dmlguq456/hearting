#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import hashlib,importlib.util,io,json,tempfile,unittest,sys
from unittest import mock
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"utilities"))
P=Path(__file__).with_name("report-manifest-verify.py"); S=importlib.util.spec_from_file_location("report",P); V=importlib.util.module_from_spec(S); S.loader.exec_module(V)
class TestReport(unittest.TestCase):
 def fixture(self,root):
  names=["audio.wav","waveform.png","spectrogram.png","playback.html"]
  for n in names: (root/n).write_text(n)
  links=" ".join(names); (root/"REPORT.md").write_text("score 7 "+links); (root/"report.html").write_text("<p>score 7 "+links+"</p>")
  def row(n,kind=None): return {"path":n,"sha256":hashlib.sha256((root/n).read_bytes()).hexdigest(),**({"sample_id":"s1","kind":kind} if kind else {})}
  data={"schema_version":1,"outputs":{"markdown":row("REPORT.md"),"html":row("report.html")},"summary_stats":{"score":7},"house_parameters":{"sample_rate_hz":48000,"frequency_band_hz":[0,24000]},"media":[row(n,k) for n,k in zip(names,V.KINDS)],"visual_evidence":[row("waveform.png")]}
  p=root/"report_manifest.json"; p.write_text(json.dumps(data)); return p,data
 def bundle_fixture(self,root):
  p,d=self.fixture(root); title="Audio eval 18000"; (root/"REPORT.md").write_text(title+" score 7 report.html"); (root/"report.html").write_text("<p>"+title+" score 7 audio.wav waveform.png spectrogram.png playback.html</p>")
  for key in d["outputs"]:
   n=d["outputs"][key]["path"]; d["outputs"][key]["sha256"]=hashlib.sha256((root/n).read_bytes()).hexdigest()
  d["bundle"]={"title":title,"primary_representation_id":"playback","representations":[{"id":"playback","format":"html","roles":["interactive"],"output":"html"},{"id":"summary","format":"md","roles":["summary","navigation"],"output":"markdown"}]}; p.write_text(json.dumps(d)); return p,d
 def test_legacy_classification(self):
  with tempfile.TemporaryDirectory() as td: self.assertEqual(V.verify(self.fixture(Path(td))[0])["bundle_classification"],"legacy/unspecified")
 def test_legacy_stdout_unchanged(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.fixture(Path(td)); old=sys.argv; out=sys.stdout
   try: sys.argv=["report-manifest-verify.py",str(p)]; sys.stdout=io.StringIO(); V.main(); self.assertEqual(sys.stdout.getvalue().strip(),'{"media": 4, "samples": 1}')
   finally: sys.argv=old; sys.stdout=out
 def test_classification_flag(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.fixture(Path(td)); old=sys.argv; out=sys.stdout
   try:
    sys.argv=["report-manifest-verify.py",str(p),"--classification"]; sys.stdout=io.StringIO(); V.main(); self.assertEqual(sys.stdout.getvalue().strip(),"legacy/unspecified"); sys.argv=["report-manifest-verify.py",str(self.bundle_fixture(Path(td))[0]),"--classification"]; sys.stdout=io.StringIO(); V.main(); self.assertEqual(sys.stdout.getvalue().strip(),"declared")
   finally: sys.argv=old; sys.stdout=out
 def test_bundle_audio_passes(self):
  with tempfile.TemporaryDirectory() as td: self.assertEqual(V.verify(self.bundle_fixture(Path(td))[0])["bundle_classification"],"declared")
 def test_bundle_removed_still_fails_legacy(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); del d["bundle"]; p.write_text(json.dumps(d))
   with self.assertRaisesRegex(ValueError,"^media link not bound in both outputs:"): V.verify(p)
 def test_equivalence_requires_shared_title(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); d["bundle"]["representations"][0]["roles"]=["interactive","canonical"]; d["bundle"]["representations"][1]["roles"]=["summary","canonical"]; d["bundle"]["representations"][0]["section_order"]=["intro"]; d["bundle"]["representations"][1]["section_order"]=["intro"]; d["bundle"]["equivalence_groups"]=[{"id":"all","representation_ids":["playback","summary"],"section_order":["intro"]}]; Path(td,"REPORT.md").write_text("score 7"); d["outputs"]["markdown"]["sha256"]=hashlib.sha256(Path(td,"REPORT.md").read_bytes()).hexdigest(); p.write_text(json.dumps(d)); self.assertRaisesRegex(ValueError,"equivalence group member missing the shared title",V.verify,p)
 def test_equivalence_requires_declared_section_order(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); d["bundle"]["representations"][0]["roles"]=["interactive","canonical"]; d["bundle"]["representations"][1]["roles"]=["summary","navigation","canonical"]; d["bundle"]["representations"][0]["section_order"]=["b","a"]; d["bundle"]["representations"][1]["section_order"]=["a","b"]; d["bundle"]["equivalence_groups"]=[{"id":"all","representation_ids":["playback","summary"],"section_order":["a","b"]}]; p.write_text(json.dumps(d)); self.assertRaisesRegex(ValueError,"equivalence group member must declare",V.verify,p)
 def test_equivalence_member_must_declare_sections(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); d["bundle"]["representations"][0]["roles"]=["interactive","canonical"]; d["bundle"]["representations"][1]["roles"]=["summary","navigation","canonical"]; d["bundle"]["representations"][1]["section_order"]=["a"]; d["bundle"]["equivalence_groups"]=[{"id":"all","representation_ids":["playback","summary"],"section_order":["a"]}]; p.write_text(json.dumps(d)); self.assertRaisesRegex(ValueError,"equivalence group member must declare",V.verify,p)
 def test_unknown_primary_fails(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); d["bundle"]["primary_representation_id"]="nope"; p.write_text(json.dumps(d)); self.assertRaises(ValueError,V.verify,p)
 def test_unclaimed_output_fails(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); d["bundle"]["representations"].pop(); p.write_text(json.dumps(d)); self.assertRaises(ValueError,V.verify,p)
 def test_two_canonicals_need_group(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); d["bundle"]["representations"][0]["roles"]=["interactive","canonical"]; d["bundle"]["representations"][1]["roles"]=["summary","navigation","canonical"]; p.write_text(json.dumps(d)); self.assertRaisesRegex(ValueError,"multiple canonical",V.verify,p)
 def test_output_and_file_are_exclusive(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); d["bundle"]["representations"][0]["file"]=d["outputs"]["html"]; p.write_text(json.dumps(d)); self.assertRaises(ValueError,V.verify,p); del d["bundle"]["representations"][0]["output"]; del d["bundle"]["representations"][0]["file"]; p.write_text(json.dumps(d)); self.assertRaises(ValueError,V.verify,p)
 def test_empty_or_null_bundle_is_not_legacy(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.fixture(Path(td)); d["bundle"]={}; p.write_text(json.dumps(d)); self.assertRaises(ValueError,V.verify,p); d["bundle"]=None; p.write_text(json.dumps(d)); self.assertRaises((ValueError,AttributeError),V.verify,p)
 def test_inline_file_alias_duplicates_output_path(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); alias=dict(d["outputs"]["html"]); alias["path"]="./report.html"; d["bundle"]["representations"].append({"id":"index","format":"html","roles":["navigation"],"file":alias}); p.write_text(json.dumps(d)); self.assertRaisesRegex(ValueError,"duplicates output path",V.verify,p)
 def test_schema_contract_binding(self):
  s=json.loads(Path(__file__).resolve().parents[1].joinpath("capabilities/report-manifest.schema.json").read_text()); b=s["properties"]["bundle"]; r=b["properties"]["representations"]["items"]; g=b["properties"]["equivalence_groups"]["items"]; self.assertEqual(set(b["properties"]),set(V.BUNDLE_KEYS)); self.assertEqual(set(r["properties"]),set(V.REPR_KEYS)); self.assertEqual(set(g["properties"]),set(V.GROUP_KEYS)); self.assertEqual(tuple(r["properties"]["roles"]["items"]["enum"]),V.ROLES); self.assertEqual(r["properties"]["format"]["pattern"],V.FORMAT_PAT); self.assertEqual(r["properties"]["id"]["pattern"],V.ID_PAT); self.assertNotIn("bundle",s["required"]); self.assertEqual(set(s["properties"]["outputs"]["required"]),{"markdown","html"}); self.assertFalse(s["properties"]["outputs"]["additionalProperties"])
 def test_third_representation_inline_file(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); Path(td,"index.html").write_text("Audio eval 18000 report.html"); d["bundle"]["representations"].append({"id":"index","format":"html","roles":["navigation"],"file":{"path":"index.html","sha256":hashlib.sha256(Path(td,"index.html").read_bytes()).hexdigest()}}); d["bundle"]["representations"][1]["roles"]=["summary"]; p.write_text(json.dumps(d)); self.assertEqual(V.verify(p)["media"],4); d["bundle"]["representations"][2]["file"]["sha256"]="0"*64; p.write_text(json.dumps(d)); self.assertRaises(ValueError,V.verify,p)
 def test_inline_file_cannot_duplicate_output_path(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); d["bundle"]["representations"].append({"id":"index","format":"html","roles":["navigation"],"file":d["outputs"]["html"]}); p.write_text(json.dumps(d)); self.assertRaisesRegex(ValueError,"duplicates output path",V.verify,p)
 def test_primary_must_carry_title(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); Path(td,"report.html").write_text("score 7 audio.wav waveform.png spectrogram.png playback.html"); d["outputs"]["html"]["sha256"]=hashlib.sha256(Path(td,"report.html").read_bytes()).hexdigest(); p.write_text(json.dumps(d)); self.assertRaisesRegex(ValueError,"bundle title missing from primary",V.verify,p)
 def test_navigation_must_link_primary(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.bundle_fixture(Path(td)); Path(td,"REPORT.md").write_text("Audio eval 18000 score 7"); d["outputs"]["markdown"]["sha256"]=hashlib.sha256(Path(td,"REPORT.md").read_bytes()).hexdigest(); p.write_text(json.dumps(d)); self.assertRaisesRegex(ValueError,"navigation representation",V.verify,p)
 def test_valid_and_hash(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.fixture(Path(td)); self.assertEqual(V.verify(p)["media"],4); Path(td,"audio.wav").write_text("changed"); self.assertRaises(ValueError,V.verify,p)
 def test_nested_html_media_links_resolve_from_html_folder(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); report=root/"html_report"; assets=report/"assets"; assets.mkdir(parents=True)
   names=("audio.wav","waveform.png","spectrogram.png","playback.html")
   for name in names: (assets/name).write_text(name)
   (assets/"other.wav").write_text("wrong existing media")
   title="Audio eval 18000"
   md=title+" score 7 html_report/index.html "+" ".join("html_report/assets/"+name for name in names)
   html_text='<html><body>'+title+' score 7 <audio src="assets/audio.wav"></audio><img src="assets/waveform.png"><img src="assets/spectrogram.png"><a href="assets/playback.html">playback</a></body></html>'
   (root/"REPORT.md").write_text(md); (report/"index.html").write_text(html_text)
   def row(path,kind=None):
    return {"path":path,"sha256":hashlib.sha256((root/path).read_bytes()).hexdigest(),**({"sample_id":"s1","kind":kind} if kind else {})}
   data={"schema_version":1,"outputs":{"markdown":row("REPORT.md"),"html":row("html_report/index.html")},"summary_stats":{"score":7},"house_parameters":{"sample_rate_hz":48000,"frequency_band_hz":[0,24000]},"media":[row("html_report/assets/"+name,kind) for name,kind in zip(names,V.KINDS)],"visual_evidence":[row("html_report/assets/waveform.png")]}
   manifest=root/"report_manifest.json"
   manifest.write_text(json.dumps(data)); self.assertEqual(V.verify(manifest)["media"],4)
   data["bundle"]={"title":title,"primary_representation_id":"playback","representations":[{"id":"playback","format":"html","roles":["canonical","interactive"],"output":"html"},{"id":"summary","format":"markdown","roles":["summary","navigation"],"output":"markdown"}]}
   manifest.write_text(json.dumps(data)); self.assertEqual(V.verify(manifest)["media"],4)
   for bad in ('assets/other.wav','../../outside.wav','assets/audio.wav?copy=1','assets/audio.wav#sample'):
    changed=html_text.replace('assets/audio.wav',bad)
    (report/"index.html").write_text(changed); data["outputs"]["html"]["sha256"]=row("html_report/index.html")["sha256"]
    manifest.write_text(json.dumps(data)); self.assertRaisesRegex(ValueError,"media link not bound",V.verify,manifest)
   (report/"index.html").write_text(html_text.replace('<html>','<html><base href="../">'))
   data["outputs"]["html"]["sha256"]=row("html_report/index.html")["sha256"]
   manifest.write_text(json.dumps(data)); self.assertRaisesRegex(ValueError,"media link not bound",V.verify,manifest)
   (report/"index.html").write_text(html_text); data["outputs"]["html"]["sha256"]=row("html_report/index.html")["sha256"]
   (assets/"audio.wav").unlink(); manifest.write_text(json.dumps(data)); self.assertRaisesRegex(ValueError,"missing report file",V.verify,manifest)
 def test_one_to_one(self):
  with tempfile.TemporaryDirectory() as td:
   p,d=self.fixture(Path(td)); d["media"].pop(); p.write_text(json.dumps(d)); self.assertRaises(ValueError,V.verify,p)

 def test_routed_cli_uses_declared_current_manifest_when_positional_is_omitted(self):
  import artifact_context
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); output=root/"cycle"/"artifacts"
   node={"id":"report","outputs":["report/report_manifest.json","report/REPORT.md"],
         "write_scope":["report/report_manifest.json","report/REPORT.md"]}
   context=artifact_context.ArtifactContext(root,{"route_id":"rt-report"},node,"report",None,
       root/"cycle",output,root,"route-lineage")
   old=sys.argv; stdout=sys.stdout
   try:
    sys.argv=["report-manifest-verify.py","--classification"]
    sys.stdout=io.StringIO()
    with mock.patch("artifact_context.resolve_context",return_value=context), \
         mock.patch.object(V,"verify",return_value={"bundle_classification":"declared"}) as verify:
     V.main()
    self.assertEqual(verify.call_args.args[0],str(output/"report/report_manifest.json"))
    self.assertEqual(sys.stdout.getvalue().strip(),"declared")
   finally:
    sys.argv=old; sys.stdout=stdout
if __name__=="__main__": unittest.main()
