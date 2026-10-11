"""Conservative source-map foundation for future visual editing.

Only explicit source hints are trusted. Generated markup is inventoried and cached
by content hash, but never guessed back to a source file.
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
import local_deploy

STORE=local_deploy.DATA / "preview-mappings"
SAFE_ID=re.compile(r"^[A-Za-z][A-Za-z0-9_:.-]{0,159}$")

class Scanner(HTMLParser):
    def __init__(self,root):
        super().__init__(convert_charrefs=True);self.root=root;self.elements=[]
    def handle_starttag(self,tag,attrs):
        values=dict(attrs);identity=values.get("id","")
        if not SAFE_ID.fullmatch(identity):return
        item={"selector":"#"+identity,"tag":tag,"editable":False,"confidence":"unresolved"}
        source=values.get("data-source-file","").replace("\\","/").lstrip("/")
        try:line=int(values.get("data-source-line","0"))
        except (TypeError,ValueError):line=0
        if source and not any(part in (".","..") for part in Path(source).parts):
            candidate=(self.root/source).resolve()
            if candidate.is_relative_to(self.root) and candidate.is_file():
                item.update({"sourceFile":source,"sourceLine":line or None,"editable":True,"confidence":"explicit"})
        self.elements.append(item)

def _load(path):
    try:
        value=json.loads(path.read_text(encoding="utf-8"));return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}

def update(project,sha,root,published):
    root=Path(root).resolve();published=Path(published).resolve();STORE.mkdir(parents=True,exist_ok=True)
    target=STORE/(project+".json");old=_load(target);prior={x.get("outputFile"):x for x in old.get("pages",[]) if isinstance(x,dict)}
    pages=[];reused=0;analyzed=0
    for file in sorted(published.rglob("*.html"))[:500]:
        if file.is_symlink() or not file.resolve().is_relative_to(published):continue
        data=file.read_bytes();fingerprint=hashlib.sha256(data).hexdigest();name=file.relative_to(published).as_posix()
        if prior.get(name,{}).get("sha256")==fingerprint:
            pages.append(prior[name]);reused+=1;continue
        parser=Scanner(root)
        try:parser.feed(data.decode("utf-8",errors="replace"))
        except ValueError:pass
        pages.append({"outputFile":name,"sha256":fingerprint,"elements":parser.elements});analyzed+=1
    payload={"schema":1,"project":project,"commit":sha,"generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "policy":"explicit-source-hints-only","pages":pages}
    temporary=target.with_suffix(".tmp");temporary.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8");os.replace(temporary,target)
    return {"pages":len(pages),"analyzed":analyzed,"reused":reused,
            "editable":sum(1 for page in pages for item in page.get("elements",[]) if item.get("editable"))}
