"""Windows loopback connector: allowlisted local deployment only by explicit authenticated browser request."""
from __future__ import annotations
import argparse
import base64
import re
import json
import mimetypes
import os
import secrets
import threading
import uuid
import shutil
import ctypes
import time
import webbrowser
import local_deploy
import auto_local
import update_client
import deploy_notices
import local_preview
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote

def create_remote_link():
    import base64
    import json
    import os
    import re
    import secrets
    import socket
    import threading
    import time
    import uuid
    from pathlib import Path
    from urllib.parse import urlsplit, unquote
    from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
    from urllib.error import HTTPError

    BASE=Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'WebManagerConnector'
    CONFIG=BASE/'remote-link.json'
    ID=re.compile(r'^[a-f0-9]{32}$')
    TOKEN=re.compile(r'^[A-Za-z0-9_-]{43}$')
    PREVIEW=re.compile(r'^[A-Za-z0-9_-]{32}$')
    MAX_BYTES=8_500_000
    LOCK=threading.RLock()
    STOP=threading.Event()
    STATE={}
    LAST_CONTACT=0.0
    LAST_ERROR=''
    PORT=18765
    LOCAL_TOKEN=''
    THREAD=None
    ACTIVE=set()
    SEEN={}
    READ={
        '/v1/status','/v1/system/status','/v1/deploy/status','/v1/deploy/plan',
        '/v1/deploy/log','/v1/preview/status','/v1/folders','/v1/images','/v1/file',
        '/v1/workspace/status','/v1/workspace/list','/v1/workspace/file','/v1/trash',
        '/v1/update/check','/v1/update/status','/v1/connector/installation',
        '/v1/notifications','/v1/notifications/display',
    }
    WRITE={
        '/v1/preview/start','/v1/deploy/start','/v1/deploy/source','/v1/deploy/concurrency',
        '/v1/notifications','/v1/notifications/test','/v1/workspace/save',
        '/v1/workspace/configure','/v1/folders/add','/v1/folders/remove',
        '/v1/trash','/v1/restore','/v1/update/apply','/v1/connector/restart',
        '/v1/connector/open-folder','/v1/connector/desktop-shortcut',
    }
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            return None

    def valid_origin(value):
        if not isinstance(value,str):return False
        return bool(re.fullmatch(r'https://webmanager-pc-link\.[a-z0-9][a-z0-9-]*\.workers\.dev',value))

    def _write():
        BASE.mkdir(parents=True,exist_ok=True)
        temp=CONFIG.with_suffix('.tmp')
        with temp.open('w',encoding='utf-8') as handle:
            json.dump(STATE,handle,ensure_ascii=False,indent=2)
        try:os.chmod(temp,0o600)
        except OSError:pass
        os.replace(temp,CONFIG)

    def identity():
        with LOCK:
            if not STATE:
                try:
                    value=json.loads(CONFIG.read_text(encoding='utf-8'))
                    if not isinstance(value,dict) or not ID.fullmatch(value.get('id','')) or not TOKEN.fullmatch(value.get('token','')):
                        raise ValueError('PC 연결 정보 형식 오류')
                    if value.get('url') and not valid_origin(value['url']):raise ValueError('PC 연결 주소 오류')
                    STATE.update(value)
                except FileNotFoundError:
                    STATE.update(id=uuid.uuid4().hex,token=secrets.token_urlsafe(32),name=socket.gethostname()[:80],url='')
                    _write()
            return dict(STATE)

    def configure(data):
        nonlocal LAST_ERROR
        if not isinstance(data,dict) or set(data)!={'url','id'} or not valid_origin(data.get('url')):
            raise ValueError('공식 PC 연결 서버 주소만 사용할 수 있습니다')
        with LOCK:
            current=identity()
            if data['id']!=current['id']:raise ValueError('PC 등록 번호가 다릅니다')
            STATE['url']=data['url'];STATE['disabled']=False;STATE['disabledReason']='';_write();LAST_ERROR=''
        return status()

    def disable():
        with LOCK:
            identity();STATE['disabled']=True;STATE['disabledReason']='user';_write()
        return status()

    def status():
        with LOCK:
            current=identity()
            enabled=bool(current.get('url')) and not current.get('disabled',False)
            return {'id':current['id'],'name':current['name'],'registered':enabled,
                    'connected':enabled and time.time()-LAST_CONTACT<45,
                    'disabledByUser':current.get('disabledReason')=='user',
                    'disabledReason':str(current.get('disabledReason') or ''),
                    'lastContact':int(LAST_CONTACT*1000) if LAST_CONTACT else None,
                    'error':LAST_ERROR,'version':1}

    def _request(url,method='POST',data=None,headers=None,timeout=25,local=False):
        payload=None if data is None else json.dumps(data,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        request=Request(url,data=payload,method=method,headers={'Content-Type':'application/json',**(headers or {})})
        opener=build_opener(ProxyHandler({}),NoRedirect()) if local else build_opener(NoRedirect())
        try:response=opener.open(request,timeout=timeout)
        except HTTPError as error:response=error
        with response:
            size=int(response.headers.get('Content-Length','0'))
            if size>MAX_BYTES:raise ValueError('휴대폰 미리보기 파일은 8.5MB까지 지원합니다')
            raw=response.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES:raise ValueError('PC 응답 크기 제한을 초과했습니다')
            return response.status,response.headers,raw

    def validate_task(task):
        if not isinstance(task,dict) or not ID.fullmatch(str(task.get('id',''))):raise ValueError('PC 요청 번호 오류')
        expiry=task.get('expiresAt')
        if not isinstance(expiry,(int,float)) or not 0<expiry-time.time()*1000<=60000:raise ValueError('만료된 PC 요청')
        path=task.get('path');method=task.get('method')
        if not isinstance(path,str) or len(path)>4096 or not path.startswith('/') or path.startswith('//') or any(c in path for c in '\\\r\n#'):
            raise ValueError('PC 요청 경로 오류')
        parsed=urlsplit(path)
        if parsed.scheme or parsed.netloc:raise ValueError('외부 주소 요청 차단')
        if task.get('kind')=='api':
            if not (method=='GET' and parsed.path in READ or method=='POST' and parsed.path in WRITE):
                raise ValueError('허용되지 않은 PC 작업')
            if method=='GET' and task.get('body') is not None:raise ValueError('조회 요청에 본문을 사용할 수 없습니다')
            if method=='POST' and not isinstance(task.get('body'),dict):raise ValueError('PC 요청 본문 오류')
            if len(json.dumps(task.get('body')).encode('utf-8'))>8_500_000:raise ValueError('PC 요청 크기 초과')
        elif task.get('kind')=='preview':
            if method!='GET' or not PREVIEW.fullmatch(str(task.get('previewToken',''))):raise ValueError('미리보기 요청 오류')
            decoded=parsed.path
            for _ in range(3):decoded=unquote(decoded)
            if ':' in decoded or any(ord(c)<32 for c in decoded) or '\\' in decoded or any(p in ('.','..') for p in decoded.split('/')) or re.match(r'^/(?:v1|_wm)(?:/|$)',decoded,re.I):
                raise ValueError('미리보기 외부 경로 차단')
            explicit=re.match(r'^/preview(?:-site|-live)?/([A-Za-z0-9_-]{32})(?:/|$)',decoded)
            if explicit and explicit[1]!=task['previewToken']:raise ValueError('다른 미리보기 접근 차단')
        else:raise ValueError('허용되지 않은 PC 요청')
        return path

    def execute(task):
        path=validate_task(task)
        if task['kind']=='preview':
            status_code,headers,data=_request('http://127.0.0.1:'+str(PORT)+path,method='GET',
                headers={'Cookie':'wm_preview='+task['previewToken']},local=True)
            return {'id':task['id'],'status':status_code,'contentType':headers.get('Content-Type','application/octet-stream'),
                    'base64':base64.b64encode(data).decode('ascii')}
        status_code,headers,data=_request('http://127.0.0.1:'+str(PORT)+path,method=task['method'],data=task.get('body'),
            headers={'X-WebManager-Connector-Token':LOCAL_TOKEN},local=True)
        try:result=json.loads(data.decode('utf-8'))
        except (UnicodeDecodeError,ValueError):return {'id':task['id'],'status':502,'json':{'error':'PC 응답 형식 오류'}}
        return {'id':task['id'],'status':status_code,'json':result}

    def _work(task,current):
        try:
            try:result=execute(task)
            except Exception as exc:result={'id':task.get('id',''),'status':502,'json':{'error':str(exc)[:220]}}
            # Results can be retried; the local operation itself is never retried.
            for attempt in range(2):
                try:
                    code,_,_=_request(current['url']+'/_wm/result?device='+current['id'],data=result,
                        headers={'Authorization':'Bearer '+current['token']},timeout=10)
                    if code<500:break
                except (OSError,ValueError):pass
                if STOP.wait(1):break
        finally:
            with LOCK:ACTIVE.discard(task.get('id'))

    def _run():
        nonlocal LAST_CONTACT,LAST_ERROR
        failures=0
        while not STOP.is_set():
            try:
                current=identity()
                if not current.get('url') or current.get('disabled'):
                    STOP.wait(2);continue
                with LOCK:capacity=max(0,4-len(ACTIVE))
                if not capacity:STOP.wait(.2);continue
                code,_,raw=_request(current['url']+'/_wm/poll?device='+current['id'],data={'capacity':capacity},
                    headers={'Authorization':'Bearer '+current['token']},timeout=28)
                if code==401:
                    with LOCK:LAST_ERROR='PC에서 WebManager에 다시 접속해 연결하세요';STATE['disabled']=True;STATE['disabledReason']='auth';_write()
                    continue
                if code!=200:raise ValueError('PC 연결 서버 응답 오류 '+str(code))
                data=json.loads(raw.decode('utf-8'))
                if not isinstance(data.get('tasks'),list):raise ValueError('PC 연결 응답 형식 오류')
                LAST_CONTACT=time.time();LAST_ERROR='';failures=0
                for task in data['tasks'][:capacity]:
                    with LOCK:
                        for key,expiry in list(SEEN.items()):
                            if expiry<time.time():SEEN.pop(key,None)
                        identity_value=task.get('id','')
                        if identity_value in SEEN:continue
                        SEEN[identity_value]=time.time()+90;ACTIVE.add(identity_value)
                    threading.Thread(target=_work,args=(task,current),daemon=True,name='WebManagerRemoteTask').start()
            except (OSError,ValueError,KeyError,TypeError) as exc:
                LAST_ERROR=str(exc)[:180];failures+=1;STOP.wait(min(30,2**min(failures,5)))

    def start(port,token):
        nonlocal PORT,LOCAL_TOKEN,THREAD
        if not isinstance(port,int) or not 1024<=port<=65535:raise ValueError('PC 포트 오류')
        with LOCK:
            PORT=port;LOCAL_TOKEN=token
            if THREAD and THREAD.is_alive():return
            STOP.clear();THREAD=threading.Thread(target=_run,daemon=True,name='WebManagerRemoteLink');THREAD.start()

    def stop():
        STOP.set()

    from types import SimpleNamespace
    return SimpleNamespace(identity=identity, configure=configure, disable=disable, status=status,
                           start=start, stop=stop, validate_task=validate_task, execute=execute)

remote_link=create_remote_link()

BASE=Path(os.environ.get("LOCALAPPDATA",str(Path.home()))) / "WebManagerConnector"
SETTINGS=BASE / "settings.json"
TRASH=BASE / "trash"
TRASH_INDEX=BASE / "trash-index.json"
ALLOWED={".png",".jpg",".jpeg",".webp",".gif",".avif"}
LIMIT=200
MAX_FILE=6_000_000
MAX_BODY=4096
AGENT_START=time.monotonic()
_CPU_LAST=None

def system_health():
    """Low-cost local Windows health check, never executes commands."""
    global _CPU_LAST
    result={"pcOnline":True,"agentRunning":True,"cpuPercent":None,
            "ramPercent":None,"memoryTotalMB":None,"memoryUsedMB":None,
            "pcUptimeSec":None,"agentUptimeSec":round(time.monotonic()-AGENT_START)}
    if os.name!="nt":
        return result
    try:
        kernel32=ctypes.WinDLL("kernel32",use_last_error=True)
        class FileTime(ctypes.Structure):
            _fields_=[("low",ctypes.c_uint32),("high",ctypes.c_uint32)]
        idle,kernel,user=FileTime(),FileTime(),FileTime()
        kernel32.GetSystemTimes.argtypes=[ctypes.POINTER(FileTime)]*3
        kernel32.GetSystemTimes.restype=ctypes.c_int
        if kernel32.GetSystemTimes(ctypes.byref(idle),ctypes.byref(kernel),ctypes.byref(user)):
            to_num=lambda x:(x.high<<32)|x.low
            sample=(to_num(idle),to_num(kernel),to_num(user))
            if _CPU_LAST is not None:
                elapsed=(sample[1]-_CPU_LAST[1])+(sample[2]-_CPU_LAST[2])
                idle_delta=sample[0]-_CPU_LAST[0]
                if elapsed>0 and idle_delta>=0:
                    result["cpuPercent"]=round(max(0,min(100,100*(elapsed-idle_delta)/elapsed)),1)
            _CPU_LAST=sample
        class Memory(ctypes.Structure):
            _fields_=[("length",ctypes.c_uint32),("memoryLoad",ctypes.c_uint32),
                      ("totalPhys",ctypes.c_uint64),("availPhys",ctypes.c_uint64),
                      ("totalPageFile",ctypes.c_uint64),("availPageFile",ctypes.c_uint64),
                      ("totalVirtual",ctypes.c_uint64),("availVirtual",ctypes.c_uint64),
                      ("availExtendedVirtual",ctypes.c_uint64)]
        memory=Memory()
        memory.length=ctypes.sizeof(Memory)
        kernel32.GlobalMemoryStatusEx.argtypes=[ctypes.POINTER(Memory)]
        kernel32.GlobalMemoryStatusEx.restype=ctypes.c_int
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)) and memory.totalPhys>0:
            result["ramPercent"]=int(memory.memoryLoad)
            result["memoryTotalMB"]=round(memory.totalPhys/1048576)
            result["memoryUsedMB"]=round((memory.totalPhys-memory.availPhys)/1048576)
        kernel32.GetTickCount64.restype=ctypes.c_uint64
        result["pcUptimeSec"]=round(kernel32.GetTickCount64()/1000)
    except (OSError,AttributeError,ValueError):
        pass
    return result
# Trusted WebManager address. Local pairing never authorizes arbitrary domains.
WEBMANAGER_ORIGIN="https://webmanager-2nv.pages.dev"

def save(data):
    BASE.mkdir(parents=True,exist_ok=True)
    temp=SETTINGS.with_suffix(".tmp")
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    os.replace(temp,SETTINGS)

def initial():
    downloads=Path.home() / "Downloads"
    return {"token":secrets.token_urlsafe(32),"folders":[str(downloads.resolve())],"allowedOrigins":[]}

def load():
    if SETTINGS.exists():
        data=json.loads(SETTINGS.read_text(encoding="utf-8"))
    else:
        data=initial()
        save(data)
    return data

state=load()
lock=threading.Lock()

def resolve_folder(value):
    p=Path(value).expanduser().resolve(strict=True)
    if not p.is_dir(): raise ValueError("폴더가 아닙니다")
    return p

def folders():
    result=[]
    for entry in state["folders"]:
        try:
            p=resolve_folder(entry)
            result.append({"path":str(p),"name":p.name,"default":p==Path.home().joinpath("Downloads").resolve()})
        except (OSError,ValueError): pass
    return result

def images(folder):
    root=resolve_folder(folder)
    if str(root) not in [x["path"] for x in folders()]: raise PermissionError("등록되지 않은 폴더")
    results=[]
    for p in sorted(root.iterdir(),key=lambda p:p.name.casefold()):
        if len(results)>=LIMIT: break
        try:
            if not p.is_file() or p.is_symlink() or p.suffix.lower() not in ALLOWED: continue
            st=p.stat()
            if st.st_size>MAX_FILE:continue
            results.append({"name":p.name,"size":st.st_size,"mtime":int(st.st_mtime)})
        except OSError: continue
    return results

# WebManager original/converted outputs remain in their existing workspace.
# The browser can read/write only these two buckets and only with connector approval.
MAX_WRITE_BODY=8_500_000
MANAGED={"original":"original-images","webp":"converted-webp"}
def workspace_root():
    """Reuse existing original/WebP folders or create a private workspace.

    No disk-wide scan, no movement/deletion of existing files, no manual path
    needed on first use.
    """
    configured=str(state.get("workspaceRoot") or "").strip()
    candidates=[Path(configured)] if configured else []
    candidates += [
        Path("D:/1.program/WebManager"),Path("D:/sites/WebManager"),
        Path("D:/WebManager"),Path(__file__).resolve().parent.parent,
        Path.home()/"Documents"/"WebManager",Path.home()/"WebManager"
    ]
    for parent in (Path("D:/1.program"),Path("D:/sites")):
        try:
            if parent.is_dir() and not parent.is_symlink():
                candidates.extend(sorted(
                    (p for p in parent.iterdir() if p.is_dir() and not p.is_symlink()),
                    key=lambda p:p.name.casefold())[:80])
        except (OSError,ValueError):pass
    candidates.append(BASE/"image-workspace")
    checked=set()
    for root in candidates:
        try:
            p=root.resolve(strict=True)
            if str(p) in checked or p.is_symlink():continue
            checked.add(str(p))
            if p.is_dir() and all((p/name).is_dir() and not (p/name).is_symlink()
                                  for name in MANAGED.values()):
                if configured!=str(p):
                    state["workspaceRoot"]=str(p)
                    save(state)
                return p
        except (OSError,ValueError):pass
    managed=BASE/"image-workspace"
    (managed/MANAGED["original"]).mkdir(parents=True,exist_ok=True)
    (managed/MANAGED["webp"]).mkdir(parents=True,exist_ok=True)
    state["workspaceRoot"]=str(managed.resolve())
    save(state)
    return managed.resolve()

def configure_workspace(value):
    if not isinstance(value,str) or not value.strip():raise ValueError("기존 WebManager 작업 폴더를 입력하세요")
    root=Path(value).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():raise ValueError("폴더가 아닙니다")
    if not (root/"original-images").is_dir() or not (root/"converted-webp").is_dir():
        raise ValueError("original-images 와 converted-webp 폴더가 있는 기존 작업 폴더를 선택하세요")
    state["workspaceRoot"]=str(root)
    save(state)
    return str(root)

def managed_path(bucket,relative,existing=False):
    root=workspace_root()
    if root is None:raise ValueError("원본·WebP 작업 폴더를 먼저 연결하세요")
    if bucket not in MANAGED:raise ValueError("작업 폴더 종류 오류")
    if not isinstance(relative,str) or "\\" in relative or relative.startswith("/") or len(relative)>240:
        raise ValueError("잘못된 파일명입니다")
    parts=relative.split("/")
    if any(not p or p in (".","..") or ":" in p for p in parts):raise ValueError("잘못된 파일 경로입니다")
    base=(root/MANAGED[bucket]).resolve(strict=True)
    target=(base/Path(*parts)).resolve()
    if not target.is_relative_to(base):raise PermissionError("작업 폴더 범위를 벗어났습니다")
    if existing and (not target.is_file() or target.is_symlink()):raise ValueError("원본 파일을 찾을 수 없습니다")
    return target

def workspace_images(bucket):
    root=workspace_root()
    if root is None:return []
    if bucket not in MANAGED:raise ValueError("작업 폴더 종류 오류")
    base=(root/MANAGED[bucket]).resolve(strict=True)
    rows=[]
    for directory,dirs,files in os.walk(base,followlinks=False):
        here=Path(directory)
        depth=len(here.relative_to(base).parts)
        dirs[:]=sorted(d for d in dirs if depth<8 and not (here/d).is_symlink())
        for name in sorted(files):
            p=here/name
            try:
                if p.is_symlink() or p.suffix.lower() not in ALLOWED or not p.is_file() or p.stat().st_size>MAX_FILE:continue
                rows.append({"name":name,"path":p.relative_to(base).as_posix(),"size":p.stat().st_size})
                if len(rows)>=500:return rows
            except OSError:continue
    return rows

def write_workspace(bucket,name,encoded):
    import binascii
    if not isinstance(name,str) or "/" in name or "\\" in name or name in (".",".."):
        raise ValueError("파일 이름 오류")
    dest=managed_path(bucket,name)
    if dest.suffix.lower() not in ALLOWED:raise ValueError("이미지 형식이 아닙니다")
    if bucket=="webp" and dest.suffix.lower()!=".webp":raise ValueError("WebP 확장자 필요")
    if not isinstance(encoded,str) or len(encoded)>8_200_000:raise ValueError("이미지 크기 초과")
    try:data=base64.b64decode(encoded,validate=True)
    except (ValueError,binascii.Error):raise ValueError("이미지 데이터 오류")
    if not data or len(data)>MAX_FILE:raise ValueError("이미지 6MB 제한")
    ext=dest.suffix.lower()
    signature=(ext==".png" and data.startswith(b"\x89PNG\r\n\x1a\n")) or (
      ext in (".jpg",".jpeg") and data.startswith(b"\xff\xd8\xff")) or (
      ext==".webp" and data[:4]==b"RIFF" and data[8:12]==b"WEBP") or (
      ext==".gif" and data[:3]==b"GIF") or (
      ext==".avif" and b"ftypavif" in data[4:24])
    if not signature:raise ValueError("파일 형식과 확장자가 다릅니다")
    if dest.exists() or dest.is_symlink():raise FileExistsError("같은 이름의 파일이 이미 있습니다: "+name)
    with dest.open("xb") as handle:handle.write(data)
    return {"saved":name,"bucket":bucket,"size":len(data)}

def trash_rows():
    try:
        data=json.loads(TRASH_INDEX.read_text(encoding="utf-8"))
        if isinstance(data,list): return data
    except (OSError,ValueError): pass
    return []

def write_trash(rows):
    BASE.mkdir(parents=True,exist_ok=True)
    tmp=TRASH_INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8")
    os.replace(tmp,TRASH_INDEX)

def checked_file(folder,filename):
    if not filename or Path(filename).name!=filename or filename in (".",".."):
        raise ValueError("이미지 파일명 오류")
    root=resolve_folder(folder)
    if str(root) not in [x["path"] for x in folders()]:
        raise PermissionError("등록되지 않은 폴더")
    p=root / filename
    if p.is_symlink() or not p.is_file() or p.suffix.lower() not in ALLOWED or p.stat().st_size>MAX_FILE:
        raise ValueError("이미지에 접근할 수 없습니다")
    return p

def move_to_trash(folder,filename):
    original=checked_file(folder,filename)
    TRASH.mkdir(parents=True,exist_ok=True)
    identity=uuid.uuid4().hex
    target=TRASH / identity
    shutil.move(str(original),str(target))
    rows=trash_rows()
    rows.append({"id":identity,"folder":str(original.parent),"name":original.name})
    write_trash(rows)
    return identity

def restore_trash(identity):
    if not re.fullmatch(r"[0-9a-f]{32}",identity):raise ValueError("휴지통 항목 오류")
    rows=trash_rows()
    item=next((x for x in rows if x.get("id")==identity),None)
    if not item:raise ValueError("휴지통에서 찾을 수 없습니다")
    root=resolve_folder(item["folder"])
    if str(root) not in [x["path"] for x in folders()]:
        raise PermissionError("등록되지 않은 폴더")
    name=item["name"]
    if Path(name).name!=name:raise ValueError("잘못된 복구 파일명")
    dest=root/name
    if dest.exists():raise ValueError("같은 이름의 파일이 존재합니다. 덮어쓰기하지 않습니다")
    source=TRASH/identity
    if not source.is_file() or source.is_symlink():raise ValueError("휴지통 파일 없음")
    shutil.move(str(source),str(dest))
    write_trash([x for x in rows if x.get("id")!=identity])
    return str(dest)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):
        pass
    def send(self,status,obj):
        b=json.dumps(obj,ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.cors()
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Content-Length",str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def cors(self):
        origin=self.headers.get("Origin","")
        if origin and origin in state.get("allowedOrigins",[]):
            self.send_header("Access-Control-Allow-Origin",origin)
            self.send_header("Vary","Origin")
            self.send_header("Access-Control-Allow-Private-Network","true")
            self.send_header("Access-Control-Allow-Headers","X-WebManager-Connector-Token, Content-Type")
            self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
    def authorized(self):
        token=self.headers.get("X-WebManager-Connector-Token","")
        return bool(token) and secrets.compare_digest(token,state["token"])
    def origin_ok(self):
        origin=self.headers.get("Origin")
        return origin is None or origin in state.get("allowedOrigins",[])
    def dispatch_api(self,method):
        if not self.origin_ok():return self.send(403,{"error":"브라우저 연결은 아직 비활성화되어 있습니다"})
        if not self.authorized():return self.send(401,{"error":"로컬 연결 인증 실패"})
        u=urlsplit(self.path)
        q=parse_qs(u.query)
        try:
            if method=="GET" and u.path=="/v1/remote/identity":
                return self.send(200,remote_link.identity())
            if method=="GET" and u.path=="/v1/remote/status":
                return self.send(200,remote_link.status())
            if method=="POST" and u.path in ("/v1/remote/configure","/v1/remote/disable"):
                length=int(self.headers.get("Content-Length","0"))
                if not 0<length<=1024:return self.send(413,{"error":"요청 크기 초과"})
                body=json.loads(self.rfile.read(length).decode("utf-8"))
                if u.path.endswith("/disable"):
                    if body!={}:raise ValueError("연결 해제 입력 오류")
                    return self.send(200,remote_link.disable())
                return self.send(200,remote_link.configure(body))
            if method=="GET" and u.path=="/v1/deploy/plan":
                return self.send(200,local_deploy.plan(q.get("project",[""])[0]))
            if method=="GET" and u.path=="/v1/preview/status":
                return self.send(200,local_preview.status(q.get("id",[""])[0]))
            # The official manifest lookup may need a network request.
            # Never block the agent's health or deployment endpoints on it.
            if method=="GET" and u.path=="/v1/update/check":
                return self.send(200,update_client.check())
            if method=="GET" and u.path=="/v1/update/status":
                return self.send(200,update_client.status())
            if method=="GET" and u.path=="/v1/connector/installation":
                return self.send(200,update_client.install_location())
            with lock:
                if method=="GET" and u.path=="/v1/status":
                    return self.send(200,{"connected":True,"version":"0.7.0","folders":len(folders())})
                if method=="GET" and u.path=="/v1/system/status":
                    return self.send(200,system_health())
                if method=="GET" and u.path=="/v1/deploy/status":
                    return self.send(200,{**local_deploy.status(),"autoMonitor":auto_local.status()})
                if method=="GET" and u.path=="/v1/deploy/log":
                    return self.send(200,local_deploy.full_log(q.get("id",[""])[0]))
                if method=="GET" and u.path=="/v1/notifications":
                    return self.send(200,deploy_notices.list_notices())
                if method=="GET" and u.path=="/v1/notifications/display":
                    return self.send(200,deploy_notices.display_status())
                if method=="GET" and u.path=="/v1/folders":return self.send(200,{"folders":folders()})
                if method=="GET" and u.path=="/v1/workspace/status":
                    root=workspace_root()
                    return self.send(200,{"connected":root is not None,"root":str(root) if root else "",
                                          "automatic":True,"managed":bool(root and root==(BASE/"image-workspace").resolve()),
                                          "buckets":MANAGED})
                if method=="GET" and u.path=="/v1/workspace/list":
                    return self.send(200,{"images":workspace_images(q.get("bucket",["original"])[0])})
                if method=="GET" and u.path=="/v1/workspace/file":
                    name=q.get("path",[""])[0]
                    p=managed_path(q.get("bucket",["original"])[0],name,existing=True)
                    if p.suffix.lower() not in ALLOWED or p.stat().st_size>MAX_FILE:raise ValueError("이미지 접근 거부")
                    return self.send(200,{"name":p.name,"base64":base64.b64encode(p.read_bytes()).decode("ascii")})
                if method=="GET" and u.path=="/v1/trash":return self.send(200,{"items":trash_rows()[-100:]})
                if method=="GET" and u.path=="/v1/file":
                    folder=q.get("folder",[""])[0]
                    filename=q.get("name",[""])[0]
                    if not filename or Path(filename).name!=filename:raise ValueError("파일 이름 오류")
                    root=resolve_folder(folder)
                    if str(root) not in [x["path"] for x in folders()]:raise PermissionError("등록되지 않은 폴더")
                    p=root / filename
                    if p.is_symlink() or not p.is_file() or p.suffix.lower() not in ALLOWED or p.stat().st_size>MAX_FILE:raise ValueError("이미지 파일 접근 거부")
                    data=p.read_bytes()
                    return self.send(200,{"name":p.name,"base64":base64.b64encode(data).decode("ascii")})
                if method=="GET" and u.path=="/v1/images":
                    path=q.get("folder",[""])[0]
                    return self.send(200,{"images":images(path),"limited":True})
                if method=="POST" and u.path=="/v1/notifications/test":
                    length=int(self.headers.get("Content-Length","0"))
                    if length<=0 or length>MAX_BODY:return self.send(413,{"error":"요청 크기 초과"})
                    body=json.loads(self.rfile.read(length).decode("utf-8"))
                    if body!={}:raise ValueError("알림 테스트에는 추가 입력이 필요하지 않습니다.")
                    return self.send(200,deploy_notices.test_notice())
                if method=="POST" and u.path=="/v1/notifications":
                    length=int(self.headers.get("Content-Length","0"))
                    if length<=0 or length>MAX_BODY:return self.send(413,{"error":"요청 크기 초과"})
                    body=json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(body,dict) or set(body)!={"project","event","sha","id"}:
                        raise ValueError("배포 알림 입력 형식이 올바르지 않습니다.")
                    return self.send(200,deploy_notices.record(
                        body["project"],body["event"],body["sha"],body["id"],"cloudflare"))
                if method=="POST" and u.path=="/v1/deploy/source":
                    length=int(self.headers.get("Content-Length","0") or "0")
                    if not 0<length<=4096:return self.send(413,{"error":"설정 요청 크기 초과"})
                    body=json.loads(self.rfile.read(length))
                    if not isinstance(body,dict) or set(body)!={"project","repo","branch","script","output"}:
                        raise ValueError("배포 저장소 변경 정보가 올바르지 않습니다.")
                    return self.send(200,local_deploy.configure_source(
                        body["project"],body["repo"],body["branch"],body["script"],body["output"]))
                if method=="POST" and u.path=="/v1/deploy/concurrency":
                    length=int(self.headers.get("Content-Length","0"))
                    if length<=0 or length>MAX_BODY:return self.send(413,{"error":"요청 크기 초과"})
                    body=json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(body,dict) or set(body)!={"maxConcurrent"}:
                        raise ValueError("동시 배포 개수만 변경할 수 있습니다.")
                    return self.send(200,local_deploy.set_max_concurrent(body["maxConcurrent"]))
                if method=="POST" and u.path in (
                    "/v1/connector/open-folder","/v1/connector/desktop-shortcut",
                    "/v1/connector/restart"
                ):
                    length=int(self.headers.get("Content-Length","0"))
                    if length<=0 or length>MAX_BODY:return self.send(413,{"error":"요청 크기 초과"})
                    body=json.loads(self.rfile.read(length).decode("utf-8"))
                    if body!={}:raise ValueError("이 작업에는 추가 명령이나 경로가 필요하지 않습니다.")
                    if u.path=="/v1/connector/open-folder":
                        return self.send(200,update_client.open_install_folder())
                    if u.path=="/v1/connector/desktop-shortcut":
                        return self.send(200,update_client.create_desktop_shortcut())
                    if os.name!="nt":
                        raise ValueError("PC 연결 프로그램 재시작은 Windows에서만 지원합니다.")
                    with local_deploy.LOCK:
                        jobs=local_deploy.status()
                        if local_deploy.UPDATING or update_client.status().get("busy"):
                            raise ValueError("연결 프로그램 업데이트를 마친 뒤 재시작하세요.")
                        if jobs.get("active") or jobs.get("queued") or jobs.get("activeCount"):
                            raise ValueError("진행 중인 로컬 배포가 완료된 뒤 재시작하세요.")
                        update_client.schedule_tray_restart()
                        local_deploy.IS_SHUTTING_DOWN=True
                        auto_local.STOP.set()
                        threading.Thread(target=self.server.shutdown,daemon=True).start()
                        return self.send(202,{"accepted":True,"previousPid":os.getpid(),
                            "message":"PC 연결 프로그램과 트레이 재시작 요청 접수"})
                if method=="POST" and u.path=="/v1/update/apply":
                    length=int(self.headers.get("Content-Length","0"))
                    if length<=0 or length>MAX_BODY:return self.send(413,{"error":"요청 크기 초과"})
                    body=json.loads(self.rfile.read(length).decode("utf-8"))
                    if body != {}:raise ValueError("업데이트 요청에 추가 옵션을 사용할 수 없습니다.")
                    with local_deploy.LOCK:
                        if local_deploy.UPDATING and not update_client.status().get("busy"):
                            raise ValueError("이전 업데이트가 종료 처리 중입니다.")
                        if not local_deploy.UPDATING:
                            local_deploy.UPDATING=True
                        try:
                            def finish_update():
                                update_client.schedule_tray_restart()
                                auto_local.STOP.set()
                                local_deploy.IS_SHUTTING_DOWN=True
                                self.server.shutdown()
                            result=update_client.start(local_deploy.status,finish_update)
                        except Exception:
                            local_deploy.UPDATING=False
                            raise
                    return self.send(202,result)
                if method=="POST" and u.path=="/v1/deploy/start":
                    length=int(self.headers.get("Content-Length","0"))
                    if length<=0 or length>MAX_BODY:return self.send(413,{"error":"요청 크기 초과"})
                    body=json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(body,dict) or set(body) not in ({"project"},{"project","sha"}):
                        raise ValueError("배포 요청에는 등록된 사이트명과 확인된 커밋만 사용할 수 있습니다.")
                    # Browser can request explicit manual deploys only.
                    # The background monitor alone calls automatic submission.
                    return self.send(202,{"job":local_deploy.submit(body["project"],body.get("sha",""))})
                if method=="POST" and u.path=="/v1/preview/start":
                    length=int(self.headers.get("Content-Length","0"))
                    if length<=0 or length>MAX_BODY:return self.send(413,{"error":"요청 크기 초과"})
                    body=json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(body,dict) or set(body)!={"project","sha"}:
                        raise ValueError("미리보기에는 등록된 사이트명과 확인된 커밋만 사용할 수 있습니다.")
                    return self.send(202,{"job":local_preview.start(body["project"],body["sha"],self.server.server_address[1])})
                if method=="POST" and u.path in ("/v1/workspace/configure","/v1/workspace/save"):
                    length=int(self.headers.get("Content-Length","0"))
                    if length<=0 or length>MAX_WRITE_BODY:return self.send(413,{"error":"요청 크기 초과"})
                    body=json.loads(self.rfile.read(length).decode("utf-8"))
                    if u.path=="/v1/workspace/configure":
                        return self.send(200,{"root":configure_workspace(body.get("root",""))})
                    return self.send(200,write_workspace(str(body.get("bucket","")),str(body.get("name","")),body.get("base64","")))
                if method=="POST" and u.path in ("/v1/folders/add","/v1/folders/remove","/v1/trash","/v1/restore"):
                    length=int(self.headers.get("Content-Length","0"))
                    if length<=0 or length>MAX_BODY:return self.send(413,{"error":"요청 크기 초과"})
                    body=json.loads(self.rfile.read(length).decode("utf-8"))
                    if u.path=="/v1/trash":return self.send(200,{"id":move_to_trash(str(body.get("folder","")),str(body.get("name","")))})
                    if u.path=="/v1/restore":return self.send(200,{"restored":restore_trash(str(body.get("id","")))})
                    path=str(resolve_folder(body.get("path","")))
                    current=[str(resolve_folder(x)) for x in state["folders"] if Path(x).is_dir()]
                    if u.path.endswith("/add") and path not in current: current.append(path)
                    if u.path.endswith("/remove"): current=[p for p in current if p!=path]
                    state["folders"]=current
                    save(state)
                    return self.send(200,{"folders":folders()})
        except (ValueError,OSError,PermissionError,KeyError,json.JSONDecodeError) as exc:
            return self.send(400,{"error":str(exc)[:160]})
        return self.send(404,{"error":"지원하지 않는 작업"})
    def do_GET(self):
        if local_preview.handle(self):return
        self.dispatch_api("GET")
    def do_POST(self): self.dispatch_api("POST")
    def do_OPTIONS(self):
        if not self.origin_ok():return self.send(403,{"error":"허용되지 않은 웹 주소"})
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length","0")
        self.end_headers()

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--port",type=int,default=18765)
    p.add_argument("--origin",help="허용할 웹매니저 HTTPS 주소 등록")
    p.add_argument("--rotate-token",action="store_true")
    p.add_argument("--show-token",action="store_true")
    p.add_argument("--quick-start",action="store_true",help="Start locally and pair with the official WebManager in one click")
    p.add_argument("--no-browser",action="store_true",help="Background watchdog restart: never open browser automatically")
    a=p.parse_args()
    if a.rotate_token:
        state["token"]=secrets.token_urlsafe(32)
        save(state)
    if a.origin:
        origin=a.origin.rstrip("/")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{2,5})?",origin):raise SystemExit("HTTPS 웹 주소만 허용됩니다")
        if origin not in state["allowedOrigins"]:
            state["allowedOrigins"].append(origin)
            save(state)
    if a.show_token:
        print("연결 인증키:",state["token"],flush=True)
        return
    if a.origin or a.rotate_token:
        print("연결 설정 저장 완료. 이제 실행.cmd를 실행하세요.",flush=True)
        return
    if a.quick_start and WEBMANAGER_ORIGIN not in state.get("allowedOrigins",[]):
        state.setdefault("allowedOrigins",[]).append(WEBMANAGER_ORIGIN)
        save(state)
    if not 1024<=a.port<=65535:raise SystemExit("잘못된 포트")
    print("WebManager Local Connector 0.7.0",flush=True)
    print("설정:",SETTINGS,flush=True)
    print("기본 폴더:",Path.home()/"Downloads",flush=True)
    print("웹 연결 주소:",", ".join(state.get("allowedOrigins",[])) or "미등록",flush=True)
    if not a.quick_start:
        print("인증키 확인(고급): py -3 agent.py --show-token",flush=True)
    print("로컬에서만 접속: http://127.0.0.1:"+str(a.port),flush=True)
    try:server=ThreadingHTTPServer(("127.0.0.1",a.port),Handler)
    except OSError as exc:
        raise SystemExit("로컬 연결 프로그램이 이미 실행 중이거나 포트를 사용할 수 없습니다. 기존 실행창을 확인하세요. ("+str(exc)+")")
    # A connector updated by the previous agent may be launched by an older
    # tray process. Repair that first upgrade automatically, without a reinstall.
    # Current tray binaries are never restarted just to refresh the web page.
    def reconcile_tray_version():
        if os.name!="nt":return
        try:
            tray=deploy_notices.tray_health()
            progress=update_client._read_status()
            upgraded=(progress.get("phase")=="restarting" and
                      progress.get("target")==update_client.RUNNING_VERSION)
            if (tray.get("running") and tray.get("current") is False) or (
                    upgraded and not tray.get("current")):
                update_client.schedule_tray_restart()
        except (OSError,ValueError):
            pass
    reconcile_tray_version()
    # GitHub change monitoring runs on this PC, not in WebManager's browser.
    auto_local.start()
    remote_link.start(a.port,state["token"])
    # Updates are deliberately user initiated. Starting the connector must never
    # download, install, or restart it behind the user's back. The web screen can
    # perform a lightweight version check and exposes one explicit update button.
    if a.quick_start and not a.no_browser:
        # A URL fragment is not included in HTTP requests to Cloudflare Pages.
        # The site's JavaScript immediately removes it from the address bar and
        # sends the token only to the user's own 127.0.0.1 connector.
        url=WEBMANAGER_ORIGIN+"/#wm-connector="+state["token"]
        print("WebManager 브라우저 연결을 준비했습니다.",flush=True)
        print("브라우저가 열리지 않으면 WebManager에서 PC 연결 상태를 확인하세요.",flush=True)
        threading.Timer(0.7,lambda:webbrowser.open(url,new=2)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        remote_link.stop()
        auto_local.stop()
        local_deploy.shutdown()
        server.server_close()
if __name__=="__main__":main()