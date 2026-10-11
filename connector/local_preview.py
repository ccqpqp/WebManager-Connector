"""Token-protected, loopback-only build preview for allowlisted Pages projects.

Preview builds use their own managed workspaces and never invoke Wrangler or
write to a user's checkout. A successful build is served with noindex headers.
"""
from __future__ import annotations
import datetime
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError
import local_deploy
import preview_mapping

DATA=local_deploy.DATA
WORKSPACES=DATA / "preview-workspaces"
SHA=re.compile(r"^[0-9a-f]{40}$",re.I)
TOKEN=re.compile(r"^[A-Za-z0-9_-]{32}$")
MAX_FILE=30_000_000
TTL_SECONDS=2*60*60
LOCK=threading.RLock()
JOBS={}
SESSIONS={}
LIVE_URLS={
    "webmanager":"https://webmanager-2nv.pages.dev",
    "laminel":"https://gamestn.com",
    "zeus-helper":"https://zeusguide.kr",
    "site-ops":"https://site-ops-8kf.pages.dev",
}
SHELL_JS=Path(__file__).with_name("preview_shell.js")
SHELL_CSS=Path(__file__).with_name("preview_shell.css")

def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _phase(job,phase,message):
    with LOCK:
        job["phase"]=phase
        job["message"]=str(message)[:300]
        job["updatedAt"]=_now()

def _executable(name):
    value=shutil.which(name)
    if not value:raise RuntimeError(name+" 실행 파일을 찾을 수 없습니다.")
    return value

def _failure_summary(output):
    lines=[line.strip() for line in str(output or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("Error: ["):
            return line.removeprefix("Error: ")[:240]
    for line in reversed(lines):
        if ("Error:" in line or "실패" in line or "failed" in line.lower()) and not line.startswith("at "):
            return line[:240]
    return "빌드 검사에서 오류가 발생했습니다. 상세 기록에서 마지막 검사 결과를 확인하세요."

def _run(job,args,cwd,timeout):
    label=" ".join(str(x) for x in args[:3])
    with LOCK:job["log"]=(job.get("log",[])+["[실행] "+label])[-40:]
    env=os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT":"0","GCM_INTERACTIVE":"Never","CI":"1","CLOUDFLARE_SEND_METRICS":"false"})
    flags=getattr(subprocess,"CREATE_NO_WINDOW",0)
    try:
        result=subprocess.run(args,cwd=str(cwd),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,shell=False,timeout=timeout,creationflags=flags,env=env,
            text=True,encoding="utf-8",errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("미리보기 준비 시간이 초과됐습니다: "+label) from exc
    output="\n".join(line[-500:] for line in result.stdout.splitlines()[-18:])
    with LOCK:job["log"]=(job.get("log",[])+([output] if output else []))[-40:]
    if result.returncode:raise RuntimeError("미리보기 빌드 실패 · "+_failure_summary(output))
    return result.stdout.strip()

def _safe_workspace(project):
    root=(WORKSPACES/project).resolve()
    if not root.is_relative_to(WORKSPACES.resolve()):raise RuntimeError("미리보기 작업 폴더 오류")
    return root

def _build(job):
    project=job["project"]
    repo,script,output=local_deploy.SITES[project]
    root=_safe_workspace(project)
    expected="https://github.com/ccqpqp/"+repo+".git"
    git=_executable("git")
    WORKSPACES.mkdir(parents=True,exist_ok=True)
    _phase(job,"source","GitHub 미리보기 코드를 확인하는 중")
    if root.exists():
        if root.is_symlink() or not (root/".git").is_dir():raise RuntimeError("미리보기 전용 작업 폴더를 확인하세요")
        origin=_run(job,[git,"remote","get-url","origin"],root,20)
        if origin.rstrip("/").removesuffix(".git").lower()!=expected.removesuffix(".git").lower():
            raise RuntimeError("미리보기 저장소 연결이 다릅니다")
    else:
        _run(job,[git,"clone","--depth","1","--branch","main",expected,str(root)],WORKSPACES,600)
    _run(job,[git,"fetch","--depth","1","origin","main"],root,300)
    sha=_run(job,[git,"rev-parse","FETCH_HEAD"],root,30).lower()
    if not SHA.fullmatch(sha) or sha!=job["sha"]:raise RuntimeError("확인 뒤 GitHub 최신 커밋이 변경됐습니다. 다시 미리보기를 실행하세요.")
    published=(root/Path(output)).resolve()
    marker=root/".webmanager-preview-sha"
    if marker.is_file() and marker.read_text(encoding="utf-8",errors="ignore").strip()==sha and published.is_dir() and (published/"index.html").is_file():
        _phase(job,"build","같은 커밋의 기존 미리보기 빌드 재사용")
        return published
    _run(job,[git,"reset","--hard",sha],root,60)
    package_file=root/"package.json"
    if not package_file.is_file():raise RuntimeError("저장소에 package.json이 없습니다.")
    package=json.loads(package_file.read_text(encoding="utf-8"))
    if script and not isinstance(package.get("scripts",{}).get(script),str):raise RuntimeError("공통 빌드 명령을 확인할 수 없습니다: npm run "+script)
    dependencies=bool(package.get("dependencies") or package.get("devDependencies"))
    npm=_executable("npm.cmd" if os.name=="nt" else "npm")
    if dependencies:
        if not (root/"package-lock.json").is_file():raise RuntimeError("의존성은 있지만 package-lock.json이 없어 미리보기를 만들지 않습니다.")
        _phase(job,"install","미리보기 의존성 설치 중")
        _run(job,[npm,"ci","--no-audit","--no-fund"],root,900)
    if script:
        _phase(job,"build","운영 배포 없이 미리보기 빌드·검증 중")
        _run(job,[npm,"run",script],root,1800)
    if published==root or not published.is_relative_to(root) or not published.is_dir():raise RuntimeError("미리보기 출력 폴더가 올바르지 않습니다: "+output)
    if not (published/"index.html").is_file():raise RuntimeError("미리보기 출력 폴더에 index.html이 없습니다.")
    marker.write_text(sha+"\n",encoding="utf-8")
    return published

def _cleanup():
    now=time.time()
    for token,row in list(SESSIONS.items()):
        if row["expires"]<=now:SESSIONS.pop(token,None)
    if len(JOBS)>30:
        finished=[key for key,row in JOBS.items() if row.get("phase") in ("ready","failed")]
        for key in finished[:-20]:JOBS.pop(key,None)

def _worker(job,port):
    try:
        published=_build(job)
        root=_safe_workspace(job["project"])
        mapping=preview_mapping.update(job["project"],job["sha"],root,published)
        token=secrets.token_urlsafe(24)
        session={"token":token,"project":job["project"],"sha":job["sha"],"root":str(published),
                 "expires":time.time()+TTL_SECONDS,"liveUrl":LIVE_URLS.get(job["project"],"")}
        with LOCK:
            _cleanup();SESSIONS[token]=session
            job["token"]=token;job["url"]="http://127.0.0.1:"+str(port)+"/preview/"+token+"/";job["mapping"]=mapping
        _phase(job,"ready","미리보기 준비 완료 · 운영 사이트는 변경되지 않았습니다")
    except Exception as exc:
        _phase(job,"failed",str(exc)[:300])

def start(project,sha,port):
    if project not in local_deploy.SITES or not local_deploy.PROJECT.fullmatch(str(project or "")):raise ValueError("지원하지 않는 미리보기 대상입니다.")
    sha=str(sha or "").lower()
    if not SHA.fullmatch(sha):raise ValueError("확인된 GitHub 커밋이 필요합니다.")
    if not isinstance(port,int) or not 1<=port<=65535:raise ValueError("로컬 미리보기 포트를 확인할 수 없습니다.")
    if local_deploy.UPDATING:raise ValueError("PC 연결 프로그램 업데이트가 끝난 뒤 미리보기를 실행하세요.")
    deployment=local_deploy.status()
    if deployment.get("activeCount") or deployment.get("queued"):raise ValueError("진행 중인 로컬 배포가 끝난 뒤 미리보기를 실행하세요.")
    with LOCK:
        _cleanup()
        for job in JOBS.values():
            if job["project"]==project and job["sha"]==sha and job["phase"] in ("queued","source","install","build","ready"):
                if job["phase"]=="ready" and job.get("token") in SESSIONS:SESSIONS[job["token"]]["expires"]=time.time()+TTL_SECONDS
                return dict(job)
        identity=uuid.uuid4().hex
        job={"id":identity,"project":project,"sha":sha,"phase":"queued","message":"미리보기 대기 중",
             "createdAt":_now(),"updatedAt":_now(),"url":"","log":[]}
        JOBS[identity]=job
    threading.Thread(target=_worker,args=(job,port),name="WebManagerPreview",daemon=True).start()
    return dict(job)

def status(identity):
    if not re.fullmatch(r"[0-9a-f]{32}",str(identity or "")):raise ValueError("미리보기 작업 번호를 확인하세요.")
    with LOCK:
        row=JOBS.get(identity)
        if not row:raise ValueError("미리보기 작업을 찾을 수 없습니다.")
        return dict(row)

def active():
    with LOCK:return any(row.get("phase") in ("queued","source","install","build") for row in JOBS.values())

def _inject_preview_guard(data,row,token):
    text=data.decode("utf-8",errors="replace")
    live=html.escape(str(row.get("liveUrl","")).rstrip("/"),quote=True)
    prefix="/preview-site/"+token
    guard="""<meta name="robots" content="noindex,nofollow,noarchive"><script data-wm-preview-guard>
(()=>{'use strict';
for(const key of ['gstn_stats_excluded','zeus_analytics_exclude','siteops_visit_excluded','fw_analytics_exclude','freelancer_tax_admin_exclude'])try{localStorage.setItem(key,'1')}catch{}
try{document.cookie='gstn_stats_excluded=1; Path=/; SameSite=Strict; Max-Age=7200'}catch{}
const originalFetch=window.fetch?.bind(window);
if(originalFetch)window.fetch=(input,init={})=>{const method=String(init.method||input?.method||'GET').toUpperCase();return ['GET','HEAD'].includes(method)?originalFetch(input,init):Promise.resolve(new Response(null,{status:204,statusText:'Preview write blocked'}))};
try{navigator.sendBeacon=()=>true}catch{}
const xhrOpen=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(method,url,...rest){const write=!['GET','HEAD'].includes(String(method||'GET').toUpperCase());return xhrOpen.call(this,write?'GET':method,write?'/__wm-preview-write-blocked__':url,...rest)};
const live='__LIVE__',prefix='__PREFIX__';
document.addEventListener('click',event=>{const link=event.target.closest?.('a[href]');if(!link||!live)return;try{const target=new URL(link.href,location.href);if(target.origin===new URL(live).origin)link.href=prefix+target.pathname+target.search+target.hash}catch{}},true);
})();
</script>""".replace("__LIVE__",live).replace("__PREFIX__",prefix)
    if re.search(r"<head(?:\s[^>]*)?>",text,re.I):
        text=re.sub(r"<head(\s[^>]*)?>",lambda m:m.group(0)+guard,text,count=1,flags=re.I)
    else:text=guard+text
    return text.encode("utf-8")

def _preview_file(root,request_path):
    raw=unquote(str(request_path or "")).replace("\\","/").lstrip("/")
    parts=PurePosixPath(raw).parts
    if any(part in (".","..") for part in parts):raise ValueError("잘못된 미리보기 경로")
    base=Path(root).resolve();candidate=(base/Path(*parts)).resolve()
    if not candidate.is_relative_to(base):raise ValueError("잘못된 미리보기 경로")
    if candidate.is_dir():candidate=candidate/"index.html"
    if not candidate.is_file() and not Path(raw).suffix:candidate=base/"index.html"
    if not candidate.is_file() or candidate.is_symlink() or candidate.stat().st_size>MAX_FILE:raise FileNotFoundError("미리보기 파일 없음")
    return candidate

def _cookie_token(handler):
    for item in handler.headers.get("Cookie","").split(";"):
        name,sep,value=item.strip().partition("=")
        if sep and name=="wm_preview" and TOKEN.fullmatch(value):return value
    return ""

def _session(token):
    with LOCK:
        _cleanup();row=SESSIONS.get(token)
        return dict(row) if row else None

def _send(handler,status,data,content_type="text/plain; charset=utf-8",cookie="",csp=""):
    handler.send_response(status);handler.send_header("Content-Type",content_type)
    handler.send_header("Content-Length",str(len(data)));handler.send_header("Cache-Control","private, no-store")
    handler.send_header("X-Robots-Tag","noindex, nofollow, noarchive");handler.send_header("X-Content-Type-Options","nosniff")
    handler.send_header("Referrer-Policy","no-referrer")
    if cookie:handler.send_header("Set-Cookie","wm_preview="+cookie+"; HttpOnly; SameSite=Strict; Path=/; Max-Age="+str(TTL_SECONDS))
    if csp:handler.send_header("Content-Security-Policy",csp)
    handler.end_headers();handler.wfile.write(data)

def _wrapper(row):
    project=html.escape(row["project"]);sha=html.escape(row["sha"][:12]);token=row["token"]
    live=html.escape(row.get("liveUrl","") or "",quote=True)
    live_frame="/preview-live/"+token+"/" if live else ""
    template="""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive"><title>WebManager 미리보기</title><link rel="stylesheet" href="/preview-shell.css"></head><body data-live-frame="__LIVE_FRAME__"><div class="bar"><strong>__PROJECT__ 미리보기 <small>__SHA__</small></strong><button id="desktop" class="on">PC</button><button id="mobile">모바일</button><button id="compare">운영 화면 나란히</button><button id="diff" disabled>변경 표시</button></div><div id="diffState" class="note">운영 배포와 분리된 미리보기입니다. 방문·행동 통계와 데이터 변경 요청을 차단합니다.</div><div id="stage" class="stage"><div id="previewPane" class="pane preview"><iframe id="previewFrame" src="/preview-site/__TOKEN__/" title="수정본 미리보기"></iframe></div><div id="livePane" class="pane live"><iframe id="liveFrame" sandbox="allow-same-origin" title="현재 운영 화면"></iframe></div></div><script src="/preview-shell.js"></script></body></html>"""
    return template.replace("__PROJECT__",project).replace("__SHA__",sha).replace("__TOKEN__",token).replace("__LIVE_FRAME__",live_frame).encode("utf-8")

class _SameOriginRedirect(HTTPRedirectHandler):
    def __init__(self,origin):super().__init__();self.origin=origin
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        parsed=urlsplit(newurl)
        if parsed.scheme+"://"+parsed.netloc!=self.origin:raise HTTPError(newurl,403,"cross-origin redirect blocked",headers,fp)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def _live_snapshot(row,path=""):
    live=row.get("liveUrl","").rstrip("/")
    parsed=urlsplit(live)
    if parsed.scheme!="https" or not parsed.netloc:raise ValueError("운영 화면 주소 없음")
    origin=parsed.scheme+"://"+parsed.netloc
    target=urljoin(live+"/",str(path or "").lstrip("/"))
    request=Request(target,headers={"User-Agent":"WebManagerPreview/2","Accept":"text/html"},method="GET")
    try:
        with build_opener(_SameOriginRedirect(origin)).open(request,timeout=15) as response:
            final_url=response.geturl();final=urlsplit(final_url)
            if final.scheme+"://"+final.netloc!=origin:raise ValueError("운영 화면이 다른 주소로 이동했습니다")
            if "text/html" not in str(response.headers.get("Content-Type","")).lower():raise ValueError("운영 HTML을 비교할 수 없습니다")
            data=response.read(4_000_001)
    except (HTTPError,URLError,TimeoutError) as exc:raise ValueError("운영 화면을 불러오지 못했습니다") from exc
    if len(data)>4_000_000:raise ValueError("운영 화면 비교 크기 제한")
    text=data.decode("utf-8",errors="replace")
    text=re.sub(r"<script\b[^>]*>[\s\S]*?</script\s*>","",text,flags=re.I)
    text=re.sub(r"<meta\b[^>]*http-equiv=[\"']?refresh[\"']?[^>]*>","",text,flags=re.I)
    addition='<base href="'+html.escape(final_url,quote=True)+'"><meta name="robots" content="noindex,nofollow,noarchive">'
    if re.search(r"<head(?:\s[^>]*)?>",text,re.I):text=re.sub(r"<head(\s[^>]*)?>",lambda match:match.group(0)+addition,text,count=1,flags=re.I)
    else:text=addition+text
    return text.encode("utf-8")

def handle(handler):
    path=urlsplit(handler.path).path
    if path=="/preview-shell.js":
        try:data=SHELL_JS.read_bytes()
        except OSError:return _send(handler,404,b"Preview shell missing") or True
        return _send(handler,200,data,"text/javascript; charset=utf-8",csp="default-src 'none'") or True
    if path=="/preview-shell.css":
        try:data=SHELL_CSS.read_bytes()
        except OSError:return _send(handler,404,b"Preview shell missing") or True
        return _send(handler,200,data,"text/css; charset=utf-8",csp="default-src 'none'") or True
    if path.startswith("/preview/"):
        parts=path.split("/");token=parts[2] if len(parts)>2 else "";row=_session(token)
        if not row:return _send(handler,404,b"Preview expired") or True
        csp="default-src 'self'; frame-src 'self' https:; style-src 'self'; script-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'"
        return _send(handler,200,_wrapper(row),"text/html; charset=utf-8",token,csp) or True
    if path.startswith("/preview-live/"):
        parts=path.split("/");token=parts[2] if len(parts)>2 else "";row=_session(token)
        if not row:return _send(handler,404,b"Preview expired") or True
        try:data=_live_snapshot(row,"/".join(parts[3:]))
        except ValueError as exc:return _send(handler,502,str(exc).encode("utf-8")) or True
        csp="default-src 'none'; style-src 'unsafe-inline' https:; img-src https: data:; font-src https: data:; script-src 'none'; connect-src 'none'; form-action 'none'; frame-ancestors 'self'; base-uri https:"
        return _send(handler,200,data,"text/html; charset=utf-8",csp=csp) or True
    if path.startswith("/preview-site/"):
        parts=path.split("/");token=parts[2] if len(parts)>2 else "";row=_session(token)
        if not row:return _send(handler,404,b"Preview expired") or True
        relative="/".join(parts[3:])
    else:
        token=_cookie_token(handler);row=_session(token)
        if not row or path.startswith("/v1/"):return False
        relative=path.lstrip("/")
    try:file=_preview_file(row["root"],relative);data=file.read_bytes()
    except (ValueError,OSError):return _send(handler,404,b"Preview file not found") or True
    mime=mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    csp=""
    if file.suffix.lower() in (".html",".htm"):
        data=_inject_preview_guard(data,row,token);mime="text/html; charset=utf-8"
        csp="default-src 'self' https: data: blob:; script-src 'self' https: 'unsafe-inline' 'unsafe-eval'; style-src 'self' https: 'unsafe-inline'; img-src 'self' https: data: blob:; font-src 'self' https: data:; connect-src 'self' https:; form-action 'none'; object-src 'none'; base-uri 'self'"
    elif mime.startswith("text/") or file.suffix.lower() in (".js",".mjs",".css",".json",".svg"):mime=mime+"; charset=utf-8"
    _send(handler,200,data,mime,csp=csp)
    return True
