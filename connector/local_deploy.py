"""Allowlisted Cloudflare Pages local manual and opt-in automatic deploy coordinator.

The browser submits a project ID only. There is no shell, custom command,
or custom checkout path. Automatic jobs also pin the GitHub HEAD observed by the
trusted WebManager page. Jobs use private managed Git clones under LOCALAPPDATA;
the user's D:\\sites working trees are untouched.
"""
from __future__ import annotations
import collections
import ctypes
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import deploy_notices
from pathlib import Path

DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WebManagerConnector"
WORKSPACES = DATA / "deploy-workspaces"
LOGS = DATA / "deploy-logs"
SHA = re.compile(r"^[0-9a-f]{40}$", re.I)
PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Project -> (GitHub repository, npm command, Pages publish folder)
# Values mirror the committed GitHub build contracts, not browser input.
SITES = {
    "webmanager": ("WebManager", "preflight", "dist"),
    "laminel": ("laminel", "build", "dist"),
    "zeus-helper": ("zeus-helper", "build", "public"),
    "site-ops": ("site-ops", "build", "public"),
    "aniimo-guide-gstn": ("aniimo-guide", "build", ".build/public"),
    "maple-price-board": ("maple-web", "build", "public"),
    "field-fix-guide": ("field-fix-guide", "build", "dist"),
    "scholarship": ("scholarship", "build", "dist"),
    "foreign-worker-check": ("foreign-worker-check", "build", "dist"),
    "freelancer-tax-check": ("freelancer-tax-check", "", "public"),
}
MAX_QUEUE = 6
PREFERENCES = DATA / "deploy-preferences.json"

def _load_concurrency():
    try:
        raw=json.loads(PREFERENCES.read_text(encoding="utf-8"))
        value=raw.get("maxConcurrent")
        if type(value) is int and 1 <= value <= 3:
            return value
    except (OSError,ValueError,AttributeError):
        pass
    return 1

MAX_CONCURRENT = _load_concurrency()
JOBS: dict[str,dict] = {}
ORDER: collections.deque[str] = collections.deque(maxlen=30)
QUEUE: collections.deque[str] = collections.deque()
LOCK = threading.RLock()
WORKERS: set[threading.Thread] = set()
PROCESSES: dict[str, subprocess.Popen] = {}
ACTIVE: set[str] = set()
RUNNING: set[str] = set()
IS_SHUTTING_DOWN = False
UPDATING = False

def _ts():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _safe_file(project):
    return WORKSPACES / project

def _clean_line(line):
    # Remove common token formats before logs can reach the browser.
    line = re.sub(r"(?i)(CLOUDFLARE_API_TOKEN|CF_API_TOKEN|GITHUB_TOKEN|Authorization)\s*[=:]\s*[^\s]+", r"\1=[hidden]", line)
    line = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~-]{12,}", r"\1[hidden]", line)
    return line[-550:]

def _log(job, text):
    item = _clean_line(str(text).rstrip())
    if not item:
        return
    with LOCK:
        job["lines"].append(item)
    # Keep runaway build output from consuming disk space on this PC.
    used=int(job.get("logBytes",0))
    if used>=2_000_000:
        return
    job["logBytes"]=used+len(item.encode("utf-8"))+1
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / (job["id"] + ".log")).open("a", encoding="utf-8") as handle:
        handle.write(item + "\n")

def _phase(job, phase, message):
    with LOCK:
        job["phase"] = phase
        job["message"] = message
        job["updatedAt"] = _ts()
    _log(job, "[" + phase + "] " + message)
    if phase in ("success","failed"):
        try:
            deploy_notices.record(job["project"],"success" if phase=="success" else "failure",
                str(job.get("sha") or job.get("expectedSha") or ""),str(job["id"]),"pc")
        except (OSError,ValueError):
            pass

def _executable(name):
    value = shutil.which(name)
    if not value:
        raise RuntimeError(name + " 명령을 찾지 못했습니다. Git / Node.js / Wrangler 로그인을 확인하세요.")
    return value

# Built-in process sampler: no external package or extra ZIP member required.
def _system_memory():
    if os.name != "nt":
        return {"usedMB": None, "totalMB": None, "percent": None}
    try:
        class Memory(ctypes.Structure):
            _fields_ = [
                ("length",ctypes.c_uint32),("load",ctypes.c_uint32),
                ("total",ctypes.c_uint64),("available",ctypes.c_uint64),
                ("pageTotal",ctypes.c_uint64),("pageAvailable",ctypes.c_uint64),
                ("virtualTotal",ctypes.c_uint64),("virtualAvailable",ctypes.c_uint64),
                ("extended",ctypes.c_uint64)
            ]
        info=Memory()
        info.length=ctypes.sizeof(Memory)
        k=ctypes.WinDLL("kernel32",use_last_error=True)
        k.GlobalMemoryStatusEx.argtypes=[ctypes.POINTER(Memory)]
        k.GlobalMemoryStatusEx.restype=ctypes.c_int
        if not k.GlobalMemoryStatusEx(ctypes.byref(info)) or not info.total:
            return {"usedMB": None, "totalMB": None, "percent": None}
        return {"usedMB": round((info.total-info.available)/1048576),
                "totalMB": round(info.total/1048576), "percent":int(info.load)}
    except (OSError,AttributeError,ValueError):
        return {"usedMB": None, "totalMB": None, "percent": None}


def _windows_processes():
    class ProcessEntry(ctypes.Structure):
        _fields_=[
            ("dwSize",ctypes.c_ulong),("cntUsage",ctypes.c_ulong),
            ("th32ProcessID",ctypes.c_ulong),("th32DefaultHeapID",ctypes.c_size_t),
            ("th32ModuleID",ctypes.c_ulong),("cntThreads",ctypes.c_ulong),
            ("th32ParentProcessID",ctypes.c_ulong),("pcPriClassBase",ctypes.c_long),
            ("dwFlags",ctypes.c_ulong),("szExeFile",ctypes.c_wchar*260)
        ]
    k=ctypes.WinDLL("kernel32",use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes=[ctypes.c_ulong,ctypes.c_ulong]
    k.CreateToolhelp32Snapshot.restype=ctypes.c_void_p
    k.Process32FirstW.argtypes=[ctypes.c_void_p,ctypes.POINTER(ProcessEntry)]
    k.Process32FirstW.restype=ctypes.c_int
    k.Process32NextW.argtypes=[ctypes.c_void_p,ctypes.POINTER(ProcessEntry)]
    k.Process32NextW.restype=ctypes.c_int
    k.CloseHandle.argtypes=[ctypes.c_void_p]
    k.CloseHandle.restype=ctypes.c_int
    snap=k.CreateToolhelp32Snapshot(0x00000002,0)
    if not snap or snap==ctypes.c_void_p(-1).value:
        return {}
    found={}
    try:
        entry=ProcessEntry()
        entry.dwSize=ctypes.sizeof(ProcessEntry)
        ok=k.Process32FirstW(snap,ctypes.byref(entry))
        while ok:
            found[int(entry.th32ProcessID)]=(int(entry.th32ParentProcessID),str(entry.szExeFile))
            ok=k.Process32NextW(snap,ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return found


def _windows_working_set(pid):
    class Counters(ctypes.Structure):
        _fields_=[
            ("cb",ctypes.c_ulong),("PageFaultCount",ctypes.c_ulong),
            ("PeakWorkingSetSize",ctypes.c_size_t),("WorkingSetSize",ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage",ctypes.c_size_t),("QuotaPagedPoolUsage",ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage",ctypes.c_size_t),("QuotaNonPagedPoolUsage",ctypes.c_size_t),
            ("PagefileUsage",ctypes.c_size_t),("PeakPagefileUsage",ctypes.c_size_t)
        ]
    k=ctypes.WinDLL("kernel32",use_last_error=True)
    k.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
    k.OpenProcess.restype=ctypes.c_void_p
    k.CloseHandle.argtypes=[ctypes.c_void_p]
    k.CloseHandle.restype=ctypes.c_int
    api=ctypes.WinDLL("psapi",use_last_error=True)
    api.GetProcessMemoryInfo.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong]
    api.GetProcessMemoryInfo.restype=ctypes.c_int
    handle=k.OpenProcess(0x0410,0,int(pid))
    if not handle:handle=k.OpenProcess(0x1010,0,int(pid))
    if not handle:return None
    try:
        counters=Counters()
        counters.cb=ctypes.sizeof(Counters)
        if api.GetProcessMemoryInfo(handle,ctypes.byref(counters),counters.cb):
            return int(counters.WorkingSetSize)
    finally:
        k.CloseHandle(handle)
    return None


def _linux_processes():
    found={}
    for folder in Path("/proc").iterdir():
        if not folder.name.isdigit():continue
        try:
            raw=(folder/"status").read_text(encoding="utf-8",errors="replace")
            parent=re.search(r"^PPid:\s*(\d+)",raw,re.M)
            name=re.search(r"^Name:\s*(.*)",raw,re.M)
            mem=re.search(r"^VmRSS:\s*(\d+)",raw,re.M)
            found[int(folder.name)]=(int(parent.group(1)) if parent else 0,
                name.group(1) if name else "",int(mem.group(1))*1024 if mem else None)
        except (OSError,ValueError):
            continue
    return found


def _process_memory_collect(root_pid):
    """Return sampled working set for the root process and its living descendants."""
    try:
        if os.name=="nt":
            processes=_windows_processes()
            if not processes:return {"memoryMB":None,"processes":[]}
            descendants={int(root_pid)}
            # PID parent traversal is bounded; no sampling other processes' memory.
            for _ in range(20):
                next_set=descendants|{pid for pid,(ppid,_) in processes.items() if ppid in descendants}
                if next_set==descendants:break
                descendants=next_set
            details=[]
            for pid in descendants:
                if pid not in processes:continue
                usage=_windows_working_set(pid)
                if usage is not None:
                    details.append({"pid":pid,"name":processes[pid][1],"memoryMB":round(usage/1048576,1)})
        elif Path("/proc").is_dir():
            processes=_linux_processes()
            descendants={int(root_pid)}
            for _ in range(20):
                next_set=descendants|{pid for pid,entry in processes.items() if entry[0] in descendants}
                if next_set==descendants:break
                descendants=next_set
            details=[{"pid":pid,"name":processes[pid][1],"memoryMB":round(processes[pid][2]/1048576,1)}
                     for pid in descendants if pid in processes and processes[pid][2] is not None]
        else:return {"memoryMB":None,"processes":[]}
        return {"memoryMB":round(sum(x["memoryMB"] for x in details),1) if details else None,
                "processes":sorted(details,key=lambda x:-x["memoryMB"])[:8]}
    except (OSError,AttributeError,ValueError):
        return {"memoryMB":None,"processes":[]}

class resource_monitor:
    system_memory=staticmethod(_system_memory)
    collect=staticmethod(_process_memory_collect)


def _invoke(job, argv, cwd, timeout=1800, env=None):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    command_label=" ".join([Path(str(argv[0])).name, *[str(v) for v in argv[1:] if not str(v).startswith("https://")]])
    _log(job, "$ " + command_label)
    # Read-only diagnostics: no new build, job cancellation or RAM restriction.
    baseline=resource_monitor.system_memory()
    process = subprocess.Popen([str(v) for v in argv],cwd=str(cwd),shell=False,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
        text=True,encoding="utf-8",errors="replace",bufsize=1,
        creationflags=flags,env=env)
    with LOCK:
        PROCESSES[job["id"]] = process
    started=time.monotonic()
    peak_process=None
    peak_processes=[]
    peak_system=baseline.get("usedMB")
    peak_percent=baseline.get("percent")
    last_sample=-2.0
    try:
        # Use a reader thread so silent or unresponsive child jobs also time out.
        lines: collections.deque[str] = collections.deque(maxlen=300)
        def reader():
            if process.stdout:
                for line in process.stdout:
                    lines.append(line)
                    _log(job,line)
        reader_thread=threading.Thread(target=reader,daemon=True)
        reader_thread.start()
        while process.poll() is None:
            elapsed=time.monotonic()-started
            if elapsed-last_sample>=1.0:
                last_sample=elapsed
                try:
                    resource=resource_monitor.collect(process.pid)
                    if resource["memoryMB"] is not None and (peak_process is None or resource["memoryMB"]>peak_process):
                        peak_process=resource["memoryMB"]
                        peak_processes=resource["processes"]
                    system=resource_monitor.system_memory()
                    if system["usedMB"] is not None and (peak_system is None or system["usedMB"]>peak_system):
                        peak_system=system["usedMB"]
                    if system["percent"] is not None and (peak_percent is None or system["percent"]>peak_percent):
                        peak_percent=system["percent"]
                except Exception:
                    pass
            if IS_SHUTTING_DOWN:
                _stop_process(process)
                raise RuntimeError("연결 프로그램이 종료되어 작업을 중단했습니다.")
            if time.monotonic()-started>timeout:
                _stop_process(process)
                raise RuntimeError("실행 제한 시간을 초과했습니다. 배포 기록을 확인하세요.")
            time.sleep(0.3)
        reader_thread.join(timeout=1)
        if process.returncode:
            raise RuntimeError("명령 실행 실패 ("+str(process.returncode)+"): "+"".join(lines)[-800:])
        return "".join(lines)
    finally:
        elapsed=round(time.monotonic()-started,1)
        measurement={
            "stage":job["phase"],"command":command_label[:240],"seconds":elapsed,
            "peakProcessMB":peak_process,"peakSystemUsedMB":peak_system,
            "peakSystemPercent":peak_percent,"baselineSystemUsedMB":baseline.get("usedMB"),
            "topProcesses":peak_processes[:5]
        }
        with LOCK:
            resource=job["resources"]
            resource["commands"].append(measurement)
            resource["commands"]=resource["commands"][-24:]
            resource["seconds"]=round(resource.get("seconds",0)+elapsed,1)
            if peak_process is not None:
                previous=resource.get("peakProcessMB")
                resource["peakProcessMB"]=max(previous,peak_process) if previous is not None else peak_process
            if peak_system is not None:
                previous=resource.get("peakSystemUsedMB")
                resource["peakSystemUsedMB"]=max(previous,peak_system) if previous is not None else peak_system
            if peak_percent is not None:
                previous=resource.get("peakSystemPercent")
                resource["peakSystemPercent"]=max(previous,peak_percent) if previous is not None else peak_percent
        _log(job,"[측정] "+command_label[:95]+" · "+str(elapsed)+"초 · 프로세스 최대 "+
             (str(peak_process)+"MB" if peak_process is not None else "측정 불가")+
             " · PC RAM 최대 "+(str(peak_percent)+"%" if peak_percent is not None else "측정 불가"))
        with LOCK:
            if PROCESSES.get(job["id"]) is process:
                PROCESSES.pop(job["id"],None)
        if process.stdout:
            process.stdout.close()

def _stop_process(process):
    try:
        if process.poll() is None:
            if os.name=="nt":
                subprocess.run(["taskkill","/PID",str(process.pid),"/T","/F"],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
            else:
                process.terminate()
    except (OSError,subprocess.TimeoutExpired):
        pass

def _prepare(job, repo):
    project=job["project"]
    root=_safe_file(project)
    git=_executable("git")
    env=os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT":"0","GCM_INTERACTIVE":"Never","CI":"1","CLOUDFLARE_SEND_METRICS":"false"})
    WORKSPACES.mkdir(parents=True,exist_ok=True)
    _phase(job,"source","GitHub 최신 코드 확인")
    expected=f"https://github.com/ccqpqp/{repo}.git"
    if root.exists():
        if root.is_symlink() or not (root/".git").is_dir():
            raise RuntimeError("관리 작업 폴더에 예상하지 못한 파일이 있습니다. 폴더를 확인하세요: "+str(root))
        url=_invoke(job,[git,"remote","get-url","origin"],root,25).strip()
        if url.rstrip("/").removesuffix(".git").lower()!=expected.removesuffix(".git").lower():
            raise RuntimeError("기존 작업 폴더의 GitHub 저장소가 다릅니다: "+str(root))
    else:
        _invoke(job,[git,"clone","--depth","1","--branch","main",expected,str(root)],WORKSPACES,600,env)
    _invoke(job,[git,"fetch","--depth","1","origin","main"],root,300,env)
    sha=_invoke(job,[git,"rev-parse","FETCH_HEAD"],root,30).strip()
    if not SHA.fullmatch(sha):
        raise RuntimeError("GitHub 커밋 확인 실패")
    expected=job.get("expectedSha","")
    if expected and sha.lower()!=expected:
        raise RuntimeError("배포 전 확인 뒤 GitHub 최신 커밋이 변경됐습니다. 변경 내용을 다시 확인한 뒤 시도하세요.")
    _invoke(job,[git,"reset","--hard",sha],root,60,env)
    # The private managed workspace is never a user's development folder.
    with LOCK:
        job["sha"]=sha.lower()
    return root,env

def _has_deps(root):
    try:
        pkg=json.loads((root/"package.json").read_text(encoding="utf-8"))
    except (OSError,ValueError):
        return False
    return bool(pkg.get("dependencies") or pkg.get("devDependencies"))

def _run(job):
    project=job["project"]
    repo,script,output=SITES[project]
    root,env=_prepare(job,repo)
    if not (root/"package.json").is_file():
        raise RuntimeError("저장소에 package.json이 없습니다.")
    pkg=json.loads((root/"package.json").read_text(encoding="utf-8"))
    if script and not isinstance(pkg.get("scripts",{}).get(script),str):
        raise RuntimeError("GitHub에 공통 빌드 명령이 없습니다: npm run "+script)
    if script:
        npm=_executable("npm.cmd" if os.name=="nt" else "npm")
        if _has_deps(root):
            lock=root/"package-lock.json"
            if not lock.is_file():
                raise RuntimeError("의존성은 있지만 package-lock.json이 없어 자동 설치하지 않습니다.")
            fingerprint=hashlib.sha256(lock.read_bytes()).hexdigest()
            stamp=root/".webmanager-dependencies-sha"
            cached=stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else ""
            if cached!=fingerprint or not (root/"node_modules").is_dir():
                _phase(job,"install","처음 실행 또는 의존성 변경 · npm 설치")
                _invoke(job,[npm,"ci","--no-audit","--no-fund"],root,900,env)
                stamp.write_text(fingerprint,encoding="utf-8")
        _phase(job,"build","필수 빌드·검증 실행")
        _invoke(job,[npm,"run",script],root,1800,env)
    published=(root/Path(output)).resolve()
    if published==root or not published.is_relative_to(root) or not published.is_dir():
        raise RuntimeError("배포 출력 폴더가 올바르지 않습니다: "+output)
    if not (published/"index.html").is_file():
        raise RuntimeError("배포 출력 폴더에 index.html이 없습니다.")
    wrangler=_executable("npx.cmd" if os.name=="nt" else "npx")
    _phase(job,"upload","Cloudflare Pages 전송 중")
    # Only known directories/projects and an exact freshly fetched HEAD.
    _invoke(job,[wrangler,"--yes","wrangler@4.148.0","pages","deploy",output,
        "--project-name",project,"--branch","main",
        "--commit-hash",job["sha"],"--commit-dirty=false"],root,1200,env)
    _phase(job,"success","Cloudflare 업로드 명령이 성공했습니다. 운영 반영은 Pages에서 확인하세요.")

def _ram_percent():
    # Resource guard: on high RAM pressure do not launch second/third build.
    if os.name!="nt":
        return None
    try:
        class Memory(ctypes.Structure):
            _fields_=[("length",ctypes.c_uint32),("load",ctypes.c_uint32),
                     ("total",ctypes.c_uint64),("available",ctypes.c_uint64),
                     ("pageTotal",ctypes.c_uint64),("pageAvailable",ctypes.c_uint64),
                     ("virtualTotal",ctypes.c_uint64),("virtualAvailable",ctypes.c_uint64),
                     ("extended",ctypes.c_uint64)]
        info=Memory()
        info.length=ctypes.sizeof(info)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(info)):
            return int(info.load)
    except (OSError,AttributeError,ValueError):
        pass
    return None

def _await_capacity(job):
    # Always permit one deployment; above 84% memory, postpone additional
    # jobs. Other processes remain untouched. This is a soft admission gate.
    deadline=time.monotonic()+600
    warned=False
    while True:
        with LOCK:
            if IS_SHUTTING_DOWN:
                raise RuntimeError("연결 프로그램이 종료 중입니다.")
            ram=_ram_percent()
            if len(RUNNING)<MAX_CONCURRENT and (not RUNNING or ram is None or ram<84):
                RUNNING.add(job["id"])
                return
        if not warned:
            _phase(job,"queued","PC 메모리 사용률이 높아 추가 빌드를 잠시 대기합니다.")
            warned=True
        if time.monotonic()>deadline:
            raise RuntimeError("PC 메모리 부족으로 10분 동안 추가 빌드를 시작하지 못했습니다.")
        time.sleep(10)

def _worker_loop():
    current=threading.current_thread()
    while True:
        with LOCK:
            if IS_SHUTTING_DOWN or not QUEUE:
                WORKERS.discard(current)
                return
            identity=QUEUE.popleft()
            job=JOBS[identity]
            ACTIVE.add(identity)
        try:
            _await_capacity(job)
            _run(job)
        except Exception as exc:
            _phase(job,"failed",str(exc)[:350])
        finally:
            with LOCK:
                job["endedAt"]=_ts()
                RUNNING.discard(identity)
                ACTIVE.discard(identity)
            try:
                _archive(job)
            except OSError:
                pass

def plan(project):
    if not isinstance(project,str) or project not in SITES or not PROJECT.fullmatch(project):
        raise ValueError("지원하지 않는 배포 대상입니다.")
    repo=SITES[project][0]
    git=_executable("git")
    expected=f"https://github.com/ccqpqp/{repo}.git"
    env=os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT":"0","GCM_INTERACTIVE":"Never"})
    flags=getattr(subprocess,"CREATE_NO_WINDOW",0)
    try:
        result=subprocess.run([git,"ls-remote","--exit-code",expected,"refs/heads/main"],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",errors="replace",
            stdin=subprocess.DEVNULL,shell=False,timeout=35,creationflags=flags,env=env)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("GitHub 최신 커밋 확인 시간이 초과됐습니다.") from exc
    sha=(result.stdout.split() or [""])[0].lower()
    if result.returncode or not SHA.fullmatch(sha):
        raise ValueError("GitHub 최신 커밋을 확인할 수 없습니다.")
    return {"project":project,"repo":"ccqpqp/"+repo,"branch":"main","sha":sha}

def _start_workers_locked():
    target=min(MAX_CONCURRENT,len(QUEUE)+len(ACTIVE))
    while len(WORKERS)<target:
        worker=threading.Thread(target=_worker_loop,name="WebManagerLocalDeploy-"+str(len(WORKERS)+1),daemon=True)
        WORKERS.add(worker)
        worker.start()


def set_max_concurrent(value):
    if type(value) is not int or value not in (1,2,3):
        raise ValueError("동시 배포 개수는 1~3개 중 선택하세요.")
    global MAX_CONCURRENT
    with LOCK:
        DATA.mkdir(parents=True,exist_ok=True)
        temporary=PREFERENCES.with_suffix(".tmp")
        temporary.write_text(json.dumps({"maxConcurrent":value},ensure_ascii=False)+"\n",encoding="utf-8")
        os.replace(temporary,PREFERENCES)
        MAX_CONCURRENT=value
        if not IS_SHUTTING_DOWN:_start_workers_locked()
        return {"maxConcurrent":MAX_CONCURRENT}


def submit(project, expected_sha="", automatic=False):
    if not isinstance(project,str) or project not in SITES or not PROJECT.fullmatch(project):
        raise ValueError("지원하지 않는 배포 대상입니다.")
    expected_sha=str(expected_sha or "").lower()
    if expected_sha and not SHA.fullmatch(expected_sha):
        raise ValueError("자동배포 커밋 형식이 올바르지 않습니다.")
    if automatic and not expected_sha:
        raise ValueError("자동배포에는 확인된 GitHub 커밋이 필요합니다.")
    with LOCK:
        if IS_SHUTTING_DOWN:
            raise ValueError("로컬 연결 프로그램이 종료 중입니다.")
        if UPDATING:
            raise ValueError("연결 프로그램 업데이트 중입니다. 완료 후 배포하세요.")
        if any(JOBS[i]["project"]==project and JOBS[i]["phase"] not in ("success","failed")
               for i in JOBS):
            raise ValueError("이 사이트는 이미 배포 대기 또는 진행 중입니다.")
        if len(QUEUE)>=MAX_QUEUE:
            raise ValueError("로컬 배포 대기열이 가득 찼습니다.")
        identity=uuid.uuid4().hex
        job={"id":identity,"project":project,"phase":"queued",
             "sha":"","message":"로컬 배포 대기 중","startedAt":_ts(),
             "updatedAt":_ts(),"endedAt":None,"automatic":bool(automatic),
             "expectedSha":expected_sha,"lines":collections.deque(maxlen=90),
              "resources":{"peakProcessMB":None,"peakSystemUsedMB":None,
                "peakSystemPercent":None,"seconds":0,"commands":[]}}
        JOBS[identity]=job
        ORDER.appendleft(identity)
        if len(JOBS)>40:
            for key in list(JOBS):
                if key not in ORDER and key not in ACTIVE:
                    del JOBS[key]
        QUEUE.append(identity)
        _start_workers_locked()
        return _public(job)

def _public(job):
    return {key:val for key,val in job.items() if key not in ("lines","logBytes")} | {"log":list(job["lines"])[-70:]}

HISTORY_FILE=LOGS/"deployment-history.json"

def _history():
    try:
        data=json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data,list):
            return [row for row in data[:40] if isinstance(row,dict)]
    except (OSError,ValueError):
        pass
    return []

def _archive(job):
    with LOCK:
        record={k:v for k,v in _public(job).items() if k!="log"}
        prior=[row for row in _history() if row.get("id")!=record["id"]]
        updated=[record]+prior
        LOGS.mkdir(parents=True,exist_ok=True)
        temporary=HISTORY_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(updated[:40],ensure_ascii=False),encoding="utf-8")
        os.replace(temporary,HISTORY_FILE)

def full_log(identity):
    if not isinstance(identity,str) or not re.fullmatch(r"[0-9a-f]{32}",identity):
        raise ValueError("로그 작업 ID가 올바르지 않습니다.")
    with LOCK:
        if identity not in JOBS and not any(row.get("id")==identity for row in _history()):
            raise ValueError("이 작업의 기록을 찾지 못했습니다.")
    path=LOGS/(identity+".log")
    try:
        # Full retained log is capped by the existing 2MB on-disk limit.
        return {"id":identity,"text":path.read_text(encoding="utf-8")[:2_000_100]}
    except OSError:
        return {"id":identity,"text":""}

def status():
    with LOCK:
        return {"connected":True,"active":sorted(ACTIVE),"activeCount":len(RUNNING),"waitingForMemory":len(ACTIVE)-len(RUNNING),"queued":len(QUEUE),
                "jobs":[_public(JOBS[i]) for i in ORDER if i in JOBS][:20],
                "projects":list(SITES),"maxConcurrent":MAX_CONCURRENT,"automaticReady":True,
                 "history":_history()[:20]}

def shutdown():
    global IS_SHUTTING_DOWN
    with LOCK:
        IS_SHUTTING_DOWN=True
        QUEUE.clear()
        running=list(PROCESSES.values())
    for process in running:
        _stop_process(process)
