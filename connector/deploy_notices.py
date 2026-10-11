"""Authenticated, bounded deployment notices for the Windows tray (no secrets or logs)."""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path

DATA=Path(os.environ.get("LOCALAPPDATA",str(Path.home()))) / "WebManagerConnector"
PATH=DATA/"deployment-notices.json"
DISPLAY=DATA/"notification-display.json"
TRAY_STATUS=DATA/"tray-status.json"
TRAY_SOURCE=Path(__file__).resolve().parent/"WebManager-Tray.ps1"

def tray_health():
    try:
        doc=json.loads(TRAY_STATUS.read_text(encoding="utf-8-sig"))
        if not isinstance(doc,dict):return {"running":False,"current":False}
        at=datetime.datetime.fromisoformat(str(doc.get("at","")).replace("Z","+00:00"))
        age=(datetime.datetime.now(datetime.timezone.utc)-at).total_seconds()
        if age<0 or age>50:return {"running":False,"current":False}
        expected=hashlib.sha256(TRAY_SOURCE.read_bytes()).hexdigest()
        return {"running":True,"current":str(doc.get("fileHash","")).lower()==expected,
                "pid":doc.get("pid") if type(doc.get("pid")) is int else None,
                "notificationsEnabled":doc.get("notificationsEnabled") is True}
    except (OSError,ValueError,TypeError):
        return {"running":False,"current":False}
LOCK=threading.RLock()
PROJECT=re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SHA=re.compile(r"^(?:[a-f0-9]{40})?$",re.I)
IDENT=re.compile(r"^[\w-]{0,120}$")

def items():
    try:
        raw=json.loads(PATH.read_text(encoding="utf-8"))
        if isinstance(raw,list):
            rows=[row for row in raw[-120:] if isinstance(row,dict)]
            tests=[];latest={}
            for row in rows:
                if row.get("source")=="test":tests.append(row)
                elif PROJECT.fullmatch(str(row.get("project",""))):latest[row["project"]]=row
            return (tests+list(latest.values()))[-120:]
    except (OSError,ValueError):
        pass
    return []

def record(project,event,sha="",identity="",source="pc"):
    if (not isinstance(project,str) or not PROJECT.fullmatch(project)
        or event not in ("success","failure","test") or not isinstance(sha,str)
        or not SHA.fullmatch(sha) or not isinstance(identity,str)
        or not IDENT.fullmatch(identity) or source not in ("pc","cloudflare","test")
        or (event=="test")!=(source=="test")):
        raise ValueError("배포 알림 데이터가 올바르지 않습니다.")
    if not sha and not identity:
        raise ValueError("배포 알림에는 커밋 또는 작업 ID가 필요합니다.")
    with LOCK:
        records=items()
        # The PC upload and Cloudflare watcher can report the same deployment
        # with different IDs. A site/revision/result is one user notification.
        marker=project+":"+event+":"+(sha.lower() or identity)
        existing=next((r for r in reversed(records) if r.get("key")==marker),None)
        if existing:return {"saved":False,"id":existing.get("id","")}
        if source!="test":
            # Do not make the tray walk through stale results. Keep only the
            # newest pending result for each site; tests stay independent.
            records=[r for r in records if r.get("source")=="test" or r.get("project")!=project]
        entry={
            "id":uuid.uuid4().hex,"key":marker,"project":project,
            "event":event,"sha":sha.lower(),"source":source,
            "createdAt":datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        records.append(entry)
        DATA.mkdir(parents=True,exist_ok=True)
        temporary=PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(records[-120:],ensure_ascii=False),encoding="utf-8")
        os.replace(temporary,PATH)
        return {"saved":True,"id":entry["id"]}

def list_notices():
    with LOCK:
        return {"items":items()}

def test_notice():
    return record("webmanager","test","","test-"+uuid.uuid4().hex,"test")

def display_status():
    try:
        row=json.loads(DISPLAY.read_text(encoding="utf-8-sig"))
        return ({**row,"tray":tray_health()} if isinstance(row,dict) else
                {"state":"waiting","message":"PC 트레이에서 표시 기록이 아직 없습니다.","tray":tray_health()})
    except (OSError,ValueError):
        return {"state":"waiting","message":"PC 트레이에서 표시 기록이 아직 없습니다.","tray":tray_health()}
