#!/usr/bin/env python3
"""Verify legacy media reports and self-contained report bundle manifests."""
import argparse, errno, hashlib, json, os, re, shutil, stat, subprocess, sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
KINDS=("audio","waveform","spectrogram","playback")
ROLES=("canonical","summary","interactive","navigation")
BUNDLE_KEYS=("title","primary_representation_id","representations","equivalence_groups")
REPR_KEYS=("id","format","roles","output","file","section_order")
GROUP_KEYS=("id","representation_ids","section_order")
ID_PAT="^[a-z0-9][a-z0-9-]*$"; FORMAT_PAT="^[a-z0-9][a-z0-9.+-]*$"
def check_file(root,row):
 p=(root/row["path"]).resolve()
 if root not in p.parents and p!=root: raise ValueError("report path escapes manifest root")
 if not p.is_file(): raise ValueError("missing report file: "+row["path"])
 if hashlib.sha256(p.read_bytes()).hexdigest()!=row["sha256"]: raise ValueError("hash mismatch: "+row["path"])
 return p
def load_bundle(root,data,texts):
 b=data["bundle"]; title=b.get("title")
 if not isinstance(title,str) or not title: raise ValueError("bundle title must be a non-empty string")
 reps={}; claimed=set(); outputs=data["outputs"]
 for row in b.get("representations",[]):
  if set(row)-set(REPR_KEYS): raise ValueError("invalid representation property")
  rid=row.get("id"); fmt=row.get("format"); roles=row.get("roles")
  if not isinstance(rid,str) or not re.fullmatch(ID_PAT,rid) or rid in reps: raise ValueError("invalid or duplicate representation id")
  if not isinstance(fmt,str) or not re.fullmatch(FORMAT_PAT,fmt): raise ValueError("invalid representation format: "+str(rid))
  if not isinstance(roles,list) or not roles or len(set(roles))!=len(roles) or any(role not in ROLES for role in roles): raise ValueError("invalid representation roles: "+rid)
  has_output="output" in row; has_file="file" in row
  if has_output==has_file: raise ValueError("representation requires exactly one output or file: "+rid)
  if has_output:
   key=row["output"]
   if key not in outputs: raise ValueError("representation output is not declared: "+rid)
   if key in claimed: raise ValueError("output claimed more than once: "+key)
   claimed.add(key); source=outputs[key]; p=check_file(root,source); path=source["path"]; text=texts[key]
  else:
   source=row["file"]; p=check_file(root,source); path=source["path"]
   if any(p==(root/outputs[key]["path"]).resolve() for key in outputs): raise ValueError("inline file duplicates output path: "+path)
   text=p.read_text(errors="replace")
  order=row.get("section_order"); reps[rid]=(set(roles),text,path,order)
 if claimed!=set(outputs): raise ValueError("every output must be claimed exactly once")
 pid=b.get("primary_representation_id")
 if pid not in reps: raise ValueError("unknown primary representation: "+str(pid))
 groups=[]; ids=set()
 for group in b.get("equivalence_groups",[]):
  if set(group)-set(GROUP_KEYS): raise ValueError("invalid equivalence group property")
  gid=group.get("id"); members=group.get("representation_ids"); order=group.get("section_order")
  if not isinstance(gid,str) or not re.fullmatch(ID_PAT,gid) or gid in ids: raise ValueError("invalid or duplicate equivalence group id")
  if not isinstance(members,list) or len(members)<2 or len(set(members))!=len(members) or any(member not in reps for member in members): raise ValueError("invalid equivalence group members")
  if not isinstance(order,list) or not order or len(set(order))!=len(order) or any(not isinstance(section,str) or not section for section in order): raise ValueError("invalid equivalence group section order")
  ids.add(gid); groups.append((set(members),order))
 return title,pid,reps,groups
def _verify_v1(path):
 path=Path(path).resolve(); root=path.parent; data=json.loads(path.read_text())
 if data.get("schema_version")!=1: raise ValueError("schema_version must be 1")
 house=data.get("house_parameters",{})
 if house.get("sample_rate_hz")!=48000 or house.get("frequency_band_hz")!=[0,24000]: raise ValueError("house parameters require 48kHz/full-band 0-24kHz")
 md=check_file(root,data["outputs"]["markdown"]); html=check_file(root,data["outputs"]["html"])
 md_text=md.read_text(errors="replace"); html_text=html.read_text(errors="replace")
 texts={"markdown":md_text,"html":html_text}; has_bundle="bundle" in data
 if has_bundle:
  title,pid,reps,eq_groups=load_bundle(root,data,texts); stat_ids={pid}|{rid for rid,(roles,*_) in reps.items() if roles&{"canonical","summary"}}; nav_ids={rid for rid,(roles,*_) in reps.items() if "navigation" in roles}; play_ids={rid for rid,(roles,*_) in reps.items() if "interactive" in roles}; canon_ids={rid for rid,(roles,*_) in reps.items() if "canonical" in roles}
  if title not in reps[pid][1]: raise ValueError("bundle title missing from primary representation: "+pid)
  for rid in nav_ids:
   if title not in reps[rid][1] or reps[pid][2] not in reps[rid][1]: raise ValueError("navigation representation must carry the bundle title and a link to the primary: "+rid)
  if not play_ids: raise ValueError("a media bundle requires one interactive representation")
 for key,value in data.get("summary_stats",{}).items():
  if has_bundle:
   for rid in stat_ids:
    if str(key) not in reps[rid][1] or str(value) not in reps[rid][1]: raise ValueError("summary stats missing from representation "+rid+": "+str(key))
  elif str(key) not in md_text or str(value) not in md_text or str(key) not in html_text or str(value) not in html_text: raise ValueError("summary stats missing from both outputs: "+key)
 groups={}
 for row in data.get("media",[]):
  if row.get("kind") not in KINDS: raise ValueError("invalid media kind")
  check_file(root,row); groups.setdefault(row.get("sample_id"),set()).add(row["kind"])
  link=row["path"]
  if has_bundle:
   for rid in play_ids:
    if link not in reps[rid][1]: raise ValueError("media link not bound in interactive representation "+rid+": "+link)
  elif link not in md_text or link not in html_text: raise ValueError("media link not bound in both outputs: "+link)
 if not groups or any(kinds!=set(KINDS) for kinds in groups.values()): raise ValueError("each sample requires 1:1 audio/waveform/spectrogram/playback")
 if not data.get("visual_evidence"): raise ValueError("visual evidence required")
 for row in data["visual_evidence"]: check_file(root,row)
 if has_bundle:
  for members,order in eq_groups:
   for rid in members:
    if reps[rid][3]!=order: raise ValueError("equivalence group member must declare the group section order: "+rid)
    if title not in reps[rid][1]: raise ValueError("equivalence group member missing the shared title: "+rid)
  if len(canon_ids)>1 and not any(canon_ids<=members for members,_ in eq_groups): raise ValueError("multiple canonical representations require one declared equivalence group")
 return {"samples":len(groups),"media":sum(map(len,groups.values())),"bundle_classification":"declared" if has_bundle else "legacy/unspecified"}


V2_KEYS={"schema_version","bundle_id","project","experiment_id","version","entrypoint","files","media"}
V2_FILE_KEYS={"path","sha256"}
V2_MEDIA_KEYS={"path","sha256","sample_id","kind"}
COMPONENT_PAT=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HASH_PAT=re.compile(r"^[0-9a-f]{64}$")
REMOTE_SCHEMES={"http","https","mailto"}
AUDIO_SUFFIXES={".wav",".mp3",".ogg"}
ACTIVE_TAGS={"script","iframe","object","embed","applet","frame","frameset","portal","base","form","button","input","select","textarea"}
TRANSIENT_ERRNOS={errno.EIO,errno.ESTALE,errno.ETIMEDOUT,errno.EAGAIN,errno.ENETDOWN,errno.ENETUNREACH}


class _HTMLLinks(HTMLParser):
 def __init__(self):
  super().__init__(convert_charrefs=True); self.links=[]; self.styles=[]; self.style_blocks=[]; self.dom_links=[]; self.active=[]; self._style_depth=0
 def handle_starttag(self,tag,attrs):
  tag=tag.lower()
  if tag in ACTIVE_TAGS: self.active.append("tag:"+tag)
  if tag=="style": self._style_depth+=1
  for key,value in attrs:
   key=key.lower()
   if key.startswith("on") or key=="srcdoc": self.active.append("attribute:"+key)
   if value is None: continue
   if value.strip().lower().startswith(("javascript:","vbscript:")): self.active.append("scheme:"+key)
   if tag=="meta" and key=="http-equiv" and value.strip().lower()=="refresh": self.active.append("meta-refresh")
   if key in {"href","src","poster"}:
    self.links.append((key,value)); self.dom_links.append((tag,key,value))
   elif key=="srcset":
    for candidate in _srcset_values(value):
     self.links.append(("asset",candidate)); self.dom_links.append((tag,key,candidate))
   elif key=="style": self.styles.append(value)
 def handle_endtag(self,tag):
  if tag.lower()=="style" and self._style_depth: self._style_depth-=1
 def handle_data(self,data):
  if self._style_depth: self.style_blocks.append(data)


def _srcset_values(value):
 values=[]
 for candidate in value.split(","):
  candidate=candidate.strip()
  if candidate: values.append(candidate.split()[0])
 return values


def _v2_relpath(raw,label):
 if not isinstance(raw,str) or not raw or "\\" in raw: raise ValueError(label+" must be a non-empty POSIX relative path")
 path=Path(raw)
 if path.is_absolute() or raw.startswith("/") or any(part in {"",".",".."} for part in path.parts): raise ValueError(label+" escapes bundle root: "+raw)
 return path


def _v2_file(root,row,label):
 if not isinstance(row,dict) or set(row)!=V2_FILE_KEYS: raise ValueError("invalid "+label+" record")
 rel=_v2_relpath(row.get("path"),label)
 if not isinstance(row.get("sha256"),str) or not HASH_PAT.fullmatch(row["sha256"]): raise ValueError("invalid sha256: "+str(row.get("path")))
 path=root/rel
 try:
  descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
 except OSError as error:
  if error.errno in TRANSIENT_ERRNOS: raise ValueError("bundle storage transient failure: "+str(rel))
  raise ValueError("missing or unsafe bundle file: "+str(rel))
 try:
  before=os.fstat(descriptor)
  if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: raise ValueError("missing or unsafe bundle file: "+str(rel))
  digest=hashlib.sha256()
  with os.fdopen(descriptor,"rb",closefd=False) as handle:
   for block in iter(lambda:handle.read(1024*1024),b""): digest.update(block)
  after=os.fstat(descriptor)
  if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
   raise ValueError("bundle file changed while hashing: "+str(rel))
  if digest.hexdigest()!=row["sha256"]: raise ValueError("hash mismatch: "+str(rel))
 finally: os.close(descriptor)
 return rel,path


def _link_target(root,document,value,attribute,inventory):
 raw=value.strip().strip("<>")
 if not raw or raw.startswith("#"): return
 split=urlsplit(raw)
 scheme=split.scheme.lower()
 if scheme:
  if scheme=="data" and attribute in {"src","poster","asset"}: return
  if scheme in REMOTE_SCHEMES and attribute=="href": return
  raise ValueError("non-self-contained resource link: "+raw)
 path_text=unquote(split.path)
 if not path_text: return
 if path_text.startswith("/") or "\\" in path_text: raise ValueError("bundle link escapes root: "+raw)
 target=(document.parent/Path(path_text)).resolve(strict=False)
 try: target.relative_to(root)
 except ValueError: raise ValueError("bundle link escapes root: "+raw)
 if target.is_dir(): target=target/"index.html"
 if target.is_symlink() or not target.is_file(): raise ValueError("missing internal link target: "+raw)
 rel=target.relative_to(root).as_posix()
 if rel not in inventory: raise ValueError("internal link target is not in bundle inventory: "+raw)
 return rel


def _css_links(text):
 text=re.sub(r"/\*.*?\*/","",text,flags=re.S)
 url_pattern=re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)",re.I)
 import_pattern=re.compile(r"@import\s+(['\"])(.*?)\1",re.I)
 return [value for _,value in url_pattern.findall(text)]+[value for _,value in import_pattern.findall(text)]


def _markdown_links(text):
 inline=re.compile(r"(!?)\[[^\]]*\]\(([^)\s]+)(?:\s+['\"][^)]*)?\)")
 definition=re.compile(r"^[ \t]{0,3}\[([^\]]+)\]:\s*(?:<([^>]+)>|(\S+))",re.M)
 references={}
 for label,angled,plain in definition.findall(text):
  references[" ".join(label.lower().split())]=angled or plain
 links=[("asset" if marker else "href",value) for marker,value in inline.findall(text)]
 full=re.compile(r"(!?)\[([^\]]+)\]\[([^\]]*)\]")
 used=set()
 for marker,text_label,raw_label in full.findall(text):
  label=" ".join((raw_label or text_label).lower().split())
  if label in references:
   used.add(label); links.append(("asset" if marker else "href",references[label]))
 shortcut=re.compile(r"(!?)\[([^\]]+)\](?![\[(])")
 for marker,raw_label in shortcut.findall(text):
  label=" ".join(raw_label.lower().split())
  if label in references and label not in used: links.append(("asset" if marker else "href",references[label]))
 for label,value in references.items():
  if label not in used: links.append(("href",value))
 return links


def _markdown_raw_html(text):
 lines=[]; fence=None
 for line in text.splitlines():
  stripped=line.lstrip()
  marker=stripped[:3] if stripped.startswith(("```","~~~")) else None
  if fence:
   if marker==fence: fence=None
   continue
  if marker:
   fence=marker; continue
  if line.startswith(("    ","\t")): continue
  lines.append(line)
 return re.sub(r"`+[^`\n]*`+","","\n".join(lines))


def _verify_html_markup(root,path,text,inventory):
 parser=_HTMLLinks(); parser.feed(text); parser.close()
 rel=path.relative_to(root).as_posix()
 if parser.active: raise ValueError("active HTML content forbidden: "+rel)
 for attr,value in parser.links: _link_target(root,path,value,attr,inventory)
 for style in parser.styles:
  for value in _css_links(style): _link_target(root,path,value,"asset",inventory)
 for block in parser.style_blocks:
  for value in _css_links(block): _link_target(root,path,value,"asset",inventory)


def _verify_links(root,files,inventory):
 for rel,path in files.items():
  suffix=path.suffix.lower()
  if suffix not in {".html",".htm",".md",".markdown",".css",".svg",".xml"}: continue
  text=path.read_text(encoding="utf-8")
  if suffix in {".html",".htm"}:
   _verify_html_markup(root,path,text,inventory)
  elif suffix in {".svg",".xml"}:
   parser=_HTMLLinks(); parser.feed(text); parser.close()
   if parser.active: raise ValueError("active HTML content forbidden: "+rel.as_posix())
   for attr,value in parser.links: _link_target(root,path,value,attr,inventory)
  elif suffix==".css":
   for value in _css_links(text): _link_target(root,path,value,"asset",inventory)
  else:
   for attr,value in _markdown_links(text): _link_target(root,path,value,attr,inventory)
   _verify_html_markup(root,path,_markdown_raw_html(text),inventory)


def _decode_media(path,kind):
 if kind in {"audio","waveform","spectrogram"}:
  if kind=="audio" and path.suffix.lower() not in AUDIO_SUFFIXES: raise ValueError("audio format must be WAV, MP3, or OGG: "+path.name)
  ffmpeg=shutil.which("ffmpeg")
  if not ffmpeg: raise ValueError("media decode failed: ffmpeg unavailable")
  try:
   result=subprocess.run(
    [ffmpeg,"-nostdin","-v","error","-xerror","-i",str(path),"-map","0:0","-f","null","-"],
    shell=False,timeout=10,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False,
   )
  except (OSError,subprocess.TimeoutExpired): raise ValueError("media decode failed: "+path.name)
  if result.returncode!=0: raise ValueError("media decode failed: "+path.name)
 elif kind=="playback":
  if path.suffix.lower() not in {".html",".htm"}: raise ValueError("playback must be HTML: "+path.name)
  try: parser=_HTMLLinks(); parser.feed(path.read_text(encoding="utf-8")); parser.close()
  except (UnicodeError,OSError): raise ValueError("playback decode failed: "+path.name)
  if parser.active: raise ValueError("active HTML content forbidden: "+path.name)


def _verify_playback_bindings(root,playback,rows,inventory):
 try:
  parser=_HTMLLinks(); parser.feed(playback.read_text(encoding="utf-8")); parser.close()
 except (UnicodeError,OSError): raise ValueError("playback decode failed: "+playback.name)
 bindings=set()
 for tag,attribute,value in parser.dom_links:
  rel=_link_target(root,playback,value,"asset" if attribute in {"src","srcset","poster"} else "href",inventory)
  if rel: bindings.add((tag,attribute,rel))
 for row in rows:
  if row["kind"]=="playback": continue
  expected=row["path"]
  if row["kind"]=="audio": allowed={"audio","video","source","a"}
  else: allowed={"img","source","a"}
  if not any(tag in allowed and rel==expected for tag,_,rel in bindings):
   raise ValueError("playback does not bind declared media: "+expected)


def _verify_v2(path):
 path=Path(path)
 if path.is_symlink(): raise ValueError("v2 manifest must not be a symlink")
 path=path.resolve(); root=path.parent
 if path.name!="report_manifest.json" or path.is_symlink() or not path.is_file(): raise ValueError("v2 manifest must be report_manifest.json")
 if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_nlink!=1: raise ValueError("v2 manifest must be a single-link regular file")
 data=json.loads(path.read_text(encoding="utf-8"))
 if set(data)!=V2_KEYS: raise ValueError("invalid v2 manifest properties")
 for key in ("project","experiment_id","version"):
  if not isinstance(data.get(key),str) or not COMPONENT_PAT.fullmatch(data[key]): raise ValueError("invalid "+key)
 if data.get("bundle_id")!=data["project"]+"/"+data["experiment_id"]: raise ValueError("bundle_id must equal project/experiment_id")
 if data.get("entrypoint")!="index.html": raise ValueError("entrypoint must be index.html")
 media_dir=root/"media"
 if media_dir.is_symlink() or not media_dir.is_dir(): raise ValueError("canonical media directory is required")
 rows=data.get("files")
 if not isinstance(rows,list) or len(rows)<2: raise ValueError("files must contain the complete bundle inventory")
 declared={}; paths={}
 for row in rows:
  rel,file_path=_v2_file(root,row,"file")
  key=rel.as_posix()
  if key in declared: raise ValueError("duplicate file inventory path: "+key)
  declared[key]=row["sha256"]; paths[rel]=file_path
 for required in ("index.html","REPORT.md"):
  if required not in declared: raise ValueError("missing canonical file inventory: "+required)
 actual=set()
 for candidate in root.rglob("*"):
  if candidate.is_symlink(): raise ValueError("symlink forbidden in report bundle: "+str(candidate.relative_to(root)))
  if candidate.is_file() and candidate!=path: actual.add(candidate.relative_to(root).as_posix())
 if actual!=set(declared):
  extra=sorted(actual-set(declared)); missing=sorted(set(declared)-actual)
  raise ValueError("bundle inventory mismatch: extra="+",".join(extra)+" missing="+",".join(missing))
 _verify_links(root,paths,set(declared))
 media=data.get("media")
 if not isinstance(media,list): raise ValueError("media must be an array")
 groups={}; media_paths=set()
 for row in media:
  if not isinstance(row,dict) or set(row)!=V2_MEDIA_KEYS: raise ValueError("invalid media record")
  kind=row.get("kind"); sample=row.get("sample_id")
  if kind not in KINDS or not isinstance(sample,str) or not COMPONENT_PAT.fullmatch(sample): raise ValueError("invalid media identity")
  rel=_v2_relpath(row.get("path"),"media")
  if rel.parts[0]!="media" or rel.as_posix() not in declared or declared[rel.as_posix()]!=row.get("sha256"): raise ValueError("media must bind to the file inventory: "+str(rel))
  if rel.as_posix() in media_paths: raise ValueError("duplicate media path: "+str(rel))
  media_paths.add(rel.as_posix()); groups.setdefault(sample,set()).add(kind); _decode_media(root/rel,kind)
 if groups:
  if any(kinds!=set(KINDS) for kinds in groups.values()): raise ValueError("each declared sample requires 1:1 audio/waveform/spectrogram/playback")
  for sample in groups:
   playback=next(root/Path(row["path"]) for row in media if row["sample_id"]==sample and row["kind"]=="playback")
   _verify_playback_bindings(root,playback,[row for row in media if row["sample_id"]==sample],set(declared))
 return {"samples":len(groups),"media":len(media),"bundle_id":data["bundle_id"],"version":data["version"],"entrypoint":"report/index.html","bundle_classification":"bundle/v2"}


def verify(path):
 path=Path(path)
 data=json.loads(path.read_text(encoding="utf-8"))
 version=data.get("schema_version")
 if version==1: return _verify_v1(path)
 if version==2: return _verify_v2(path)
 raise ValueError("schema_version must be 1 or 2")
def main():
 p=argparse.ArgumentParser(); p.add_argument("manifest"); p.add_argument("--classification",action="store_true"); a=p.parse_args(); result=verify(a.manifest)
 if a.classification: print(result["bundle_classification"])
 else:
  if result["bundle_classification"]=="legacy/unspecified": result.pop("bundle_classification")
  print(json.dumps(result,sort_keys=True))
if __name__=="__main__":
 try: main()
 except (ValueError,KeyError,TypeError,AttributeError,json.JSONDecodeError) as e: print("report-manifest-verify:",e,file=sys.stderr); raise SystemExit(65)
