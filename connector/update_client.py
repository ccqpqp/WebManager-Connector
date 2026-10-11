"""Verified, path-independent updates from the dedicated connector repository.

The connector never pulls arbitrary repositories or shell commands. Downloads are
checked against the official manifest and installation targets are fixed filenames.
Local token, logs, and settings under LOCALAPPDATA are never replaced.
"""
from __future__ import annotations
import base64
import hashlib
import io
import json
import datetime
import threading
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

ORIGIN = "https://ccqpqp.github.io/WebManager-Connector"
ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WebManagerConnector"
FILES = (
    "agent.py", "auto_local.py", "local_deploy.py", "local_preview.py", "preview_mapping.py", "preview_shell.js", "preview_shell.css", "pages_deploy.py", "deploy_notices.py",
    "update_client.py", "WebManager-Tray.ps1", "WebManager-Start.vbs",
    "실행.cmd", "connector-version.json",
)
HEX = re.compile(r"[0-9a-f]{64}")
PROGRESS = DATA / "connector-update-progress.json"
AGENT_STARTED_AT = datetime.datetime.now(datetime.timezone.utc).isoformat()
STATUS_LOCK = threading.RLock()
UPDATE_BUSY = False

def _write_status(phase, message, target=""):
    with STATUS_LOCK:
        DATA.mkdir(parents=True, exist_ok=True)
        previous = _read_status()
        payload = {
            "phase": phase, "message": str(message)[:300],
            "target": target or previous.get("target",""),
            "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        tmp = PROGRESS.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload,ensure_ascii=False),encoding="utf-8")
        os.replace(tmp,PROGRESS)

def _read_status():
    try:
        value=json.loads(PROGRESS.read_text(encoding="utf-8"))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        return {}

def status():
    with STATUS_LOCK:
        result=_read_status()
        phase=result.get("phase","idle")
        target=result.get("target","")
        if phase=="restarting" and target and _current()==target and RUNNING_VERSION==target:
            result={**result,"phase":"complete","message":"연결 프로그램 업데이트가 설치되었습니다. PC 연결을 확인하세요."}
        running=RUNNING_VERSION
        installed=_current()
        return {**result,"busy":UPDATE_BUSY,
            "current":installed[:12] or "기존 버전",
            "runningVersion":running,"installedVersion":installed,
            "runningMatchesInstalled":bool(running) and running==installed,
            "agentPid":os.getpid(),"agentStartedAt":AGENT_STARTED_AT}

def start(status_func, restart):
    global UPDATE_BUSY
    with STATUS_LOCK:
        if UPDATE_BUSY:
            return {"accepted":True,"alreadyRunning":True}
        jobs=status_func()
        if jobs.get("active") or jobs.get("queued") or jobs.get("activeCount"):
            raise ValueError("현재 배포를 마친 후 업데이트를 시작하세요.")
        UPDATE_BUSY=True
        _write_status("checking","공식 버전 정보를 확인하고 있습니다.",target="")

    def worker():
        global UPDATE_BUSY
        try:
            outcome=install(status_func(), progress=_write_status)
            if outcome.get("updated"):
                _write_status("restarting","설치가 완료됐습니다. PC 연결 프로그램을 재시작하고 있습니다.")
                restart()
            elif RUNNING_VERSION != _current():
                _write_status("restarting","설치된 파일과 실행 중인 버전이 다릅니다. PC 연결 프로그램을 다시 시작합니다.")
                restart()
            else:
                _write_status("complete","설치와 실행 중인 프로그램 모두 최신 버전입니다.")
        except Exception as exc:
            _write_status("failed",str(exc)[:300])
        finally:
            with STATUS_LOCK:
                UPDATE_BUSY=False
            try:
                import local_deploy
                local_deploy.UPDATING=False
            except (ImportError,AttributeError):
                pass
    threading.Thread(target=worker,name="WebManagerConnectorUpdate",daemon=True).start()
    return {"accepted":True,"alreadyRunning":False}


def schedule_tray_restart():
    """Restart just this installation's Windows tray after a verified update.

    Runs outside the old Python service and only terminates a PowerShell
    process whose command line contains the exact installed tray path.
    The tray owns the singleton mutex, so no duplicate stays running.
    """
    if os.name != "nt":
        return False
    tray=ROOT/"WebManager-Tray.ps1"
    launcher=ROOT/"WebManager-Start.vbs"
    if not tray.is_file() or not launcher.is_file():
        raise ValueError("트레이 설치 파일이 없습니다. 업데이트를 확인하세요.")
    DATA.mkdir(parents=True,exist_ok=True)
    def literal(value):
        return "'"+str(value).replace("'","''")+"'"
    code=r"""
$ErrorActionPreference='Stop'
$trayFile=__TRAY__
$launcher=__LAUNCH__
$logFile=__LOG__
try {
    Start-Sleep -Milliseconds 1100
    $matching=@(Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction Stop |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine.IndexOf($trayFile,[System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $_.CommandLine.IndexOf('-File',[System.StringComparison]::OrdinalIgnoreCase) -ge 0
        })
    if ($matching.Count -gt 1) {
        throw '동일 설치 경로의 트레이 프로그램이 둘 이상이라 자동 종료하지 않았습니다.'
    }
    if ($matching.Count -eq 1) {
        $oldId=[int]$matching[0].ProcessId
        Stop-Process -Id $oldId -Force -ErrorAction Stop
        for($i=0;$i -lt 40;$i++){
            if (-not (Get-Process -Id $oldId -ErrorAction SilentlyContinue)) {break}
            Start-Sleep -Milliseconds 200
        }
    }
    $wscript=Join-Path $env:SystemRoot 'System32\wscript.exe'
    if (-not (Test-Path -LiteralPath $wscript)) {throw 'Windows Script Host를 찾지 못했습니다.'}
    Start-Process -FilePath $wscript -ArgumentList ('//B "'+$launcher+'" background') -WindowStyle Hidden
    ('PC 트레이 자동 재실행 요청 · '+[datetime]::Now.ToString('s')) |
       Set-Content -LiteralPath $logFile -Encoding UTF8
} catch {
    ('PC 트레이 자동 재실행 실패 · '+$_.Exception.Message) |
       Set-Content -LiteralPath $logFile -Encoding UTF8
}
"""
    code=(code.replace("__TRAY__",literal(tray))
              .replace("__LAUNCH__",literal(launcher))
              .replace("__LOG__",literal(DATA/"tray-restart-result.log")))
    encoded=base64.b64encode(code.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.Popen(
            ["powershell.exe","-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
             "-WindowStyle","Hidden","-EncodedCommand",encoded],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=(getattr(subprocess,"CREATE_NO_WINDOW",0) |
                           getattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0)))
    except OSError as exc:
        raise ValueError("PC 트레이 자동 재실행을 시작하지 못했습니다: "+str(exc)) from exc
    return True



def install_location():
    """Read-only installation path of the actual running connector."""
    tray=ROOT/"WebManager-Tray.ps1"
    launcher=ROOT/"WebManager-Start.vbs"
    try:
        import deploy_notices
        health=deploy_notices.tray_health()
    except (ImportError,OSError,ValueError):
        health={"running":False,"current":False}
    try:
        last=(DATA/"tray-restart-result.log").read_text(encoding="utf-8-sig")[-300:]
    except OSError:
        last=""
    return {"folder":str(ROOT),"launcher":str(launcher),
            "filesPresent":tray.is_file() and launcher.is_file(),
            "agentPid":os.getpid(),"tray":health,"lastRestart":last}


def open_install_folder():
    if os.name!="nt":
        raise ValueError("설치 폴더 열기는 Windows에서만 지원합니다.")
    if not ROOT.is_dir():
        raise ValueError("연결 프로그램 설치 폴더를 찾지 못했습니다.")
    os.startfile(str(ROOT))
    return {"opened":True,"folder":str(ROOT)}


def create_desktop_shortcut():
    """Create only a shortcut to our own fixed launcher, never caller paths."""
    if os.name!="nt":
        raise ValueError("바탕화면 바로가기는 Windows에서만 지원합니다.")
    launcher=ROOT/"WebManager-Start.vbs"
    if not launcher.is_file():
        raise ValueError("실행 파일을 찾지 못했습니다: WebManager-Start.vbs")
    def literal(value):
        return "'"+str(value).replace("'","''")+"'"
    script=r"""
$ErrorActionPreference='Stop'
$desktop=[Environment]::GetFolderPath('DesktopDirectory')
if (-not $desktop) {throw 'Windows 바탕화면 경로를 확인할 수 없습니다.'}
$target=__LAUNCH__
$wscript=Join-Path $env:SystemRoot 'System32\wscript.exe'
$lnk=Join-Path $desktop 'WebManager PC 연결.lnk'
$wsh=New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $lnk) {
    $old=$wsh.CreateShortcut($lnk)
    if ($old.TargetPath -and ($old.TargetPath -ne $wscript -or
        $old.Arguments.IndexOf($target,[System.StringComparison]::OrdinalIgnoreCase) -lt 0)) {
        throw '다른 바탕화면 바로가기가 있어 덮어쓰지 않았습니다.'
    }
}
$link=$wsh.CreateShortcut($lnk)
$link.TargetPath=$wscript
$link.Arguments='//B "'+$target+'"'
$link.WorkingDirectory=Split-Path -Parent $target
$link.Description='WebManager PC 연결 프로그램'
$link.Save()
Write-Output $lnk
"""
    script=script.replace("__LAUNCH__",literal(launcher))
    encoded=base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive",
                               "-ExecutionPolicy","Bypass","-EncodedCommand",encoded],
                              capture_output=True,text=True,timeout=15,
                              creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise ValueError("바탕화면 바로가기를 만드는 중 오류가 발생했습니다.") from exc
    if result.returncode:
        raise ValueError("바탕화면 바로가기 만들기 실패 · "+
                         (result.stderr.strip() or "권한 및 Windows 설정 확인")[-250:])
    return {"created":True,"location":result.stdout.strip()[-500:]}


def _download(path, limit):
    req = urllib.request.Request(ORIGIN + path, headers={"User-Agent": "WebManagerConnector/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            if not response.geturl().startswith(ORIGIN + "/"):
                raise ValueError("공식 배포 주소 외부로 이동할 수 없습니다.")
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code==404:
            raise ValueError("연결 프로그램 전용 저장소에 업데이트 파일이 없습니다.") from exc
        raise ValueError("업데이트 서버 오류 (HTTP "+str(exc.code)+")") from exc
    except urllib.error.URLError as exc:
        raise ValueError("업데이트 서버에 연결할 수 없습니다. 인터넷 연결을 확인하세요.") from exc
    if len(data) > limit:
        raise ValueError("다운로드 크기 제한을 초과했습니다.")
    return data

def _manifest():
    try:
        row = json.loads(_download("/connector-manifest.json", 8192))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("업데이트 버전 정보가 올바르지 않습니다.") from exc
    if (not isinstance(row, dict) or row.get("schema") != 1 or
        not isinstance(row.get("version"), str) or not HEX.fullmatch(row["version"]) or
        not isinstance(row.get("archiveSha256"), str) or
        not HEX.fullmatch(row["archiveSha256"])):
        raise ValueError("공식 업데이트 정보가 올바르지 않습니다.")
    return row

def _current():
    try:
        row=json.loads((ROOT/"connector-version.json").read_text(encoding="utf-8"))
        value=row.get("hash", "")
        return value if isinstance(value, str) and HEX.fullmatch(value) else ""
    except (ValueError,OSError,AttributeError):
        return ""

RUNNING_VERSION = _current()

def check():
    row=_manifest()
    installed=_current()
    running=RUNNING_VERSION
    latest=row["version"]
    return {"available":installed!=latest,
            "updateNeeded":installed!=latest,
            "restartNeeded":installed==latest and running!=latest,
            "runningCurrent":running==latest,
            "runningVersion":running,
            "installedVersion":installed,
            "latestVersion":latest,
            "current":installed[:12] or "기존 버전",
            "latest":latest[:12],
            "agentPid":os.getpid(),
            "agentStartedAt":AGENT_STARTED_AT}

def install(jobs, progress=None):
    if jobs.get("active") or jobs.get("queued") or jobs.get("activeCount"):
        raise ValueError("진행 중이거나 대기 중인 배포를 완료한 다음 업데이트하세요.")
    row=_manifest()
    if progress:progress("downloading","새 버전 파일을 다운로드하고 있습니다.",row["version"])
    if _current()==row["version"]:
        return {"updated":False}
    data=_download("/WebManager-Connector.zip", 6_000_000)
    if progress:progress("verifying","파일의 무결성과 버전을 확인하고 있습니다.",row["version"])
    if hashlib.sha256(data).hexdigest()!=row["archiveSha256"]:
        raise ValueError("다운로드 검증 실패 · 기존 버전을 유지합니다.")
    prefix="WebManager-Connector/"
    expected={prefix+name for name in FILES}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos=archive.infolist()
            if (len(infos)!=len(FILES) or
                {i.filename for i in infos}!=expected or
                any(i.is_dir() or i.file_size>1_000_000 for i in infos)):
                raise ValueError("프로그램 패키지의 파일 목록이 예상과 다릅니다.")
            extracted={i.filename[len(prefix):]:archive.read(i) for i in infos}
        packaged=json.loads(extracted["connector-version.json"])
    except (zipfile.BadZipFile,KeyError,UnicodeDecodeError) as exc:
        raise ValueError("압축 파일이 올바르지 않습니다.") from exc
    if not isinstance(packaged,dict) or packaged.get("hash")!=row["version"]:
        raise ValueError("패키지 버전이 공식 정보와 일치하지 않습니다.")
    if progress:progress("installing","검증된 연결 프로그램 파일을 설치 중입니다.",row["version"])
    DATA.mkdir(parents=True,exist_ok=True)
    backup=DATA/"connector-previous"
    with tempfile.TemporaryDirectory(prefix="wm-update-",dir=DATA) as tmp:
        stage=Path(tmp)
        for name in FILES:
            (stage/name).write_bytes(extracted[name])
        if backup.exists():shutil.rmtree(backup)
        backup.mkdir()
        for name in FILES:
            if (ROOT/name).exists():
                shutil.copy2(ROOT/name,backup/name)
        touched=[]
        try:
            for name in FILES:
                target=ROOT/name
                temporary=ROOT/(name+".wmnext")
                shutil.copy2(stage/name,temporary)
                os.replace(temporary,target)
                touched.append(name)
        except OSError as exc:
            for name in reversed(touched):
                original=backup/name
                if original.exists():
                    shutil.copy2(original,ROOT/name)
                else:
                    (ROOT/name).unlink(missing_ok=True)
            raise ValueError("프로그램 폴더 쓰기 실패 · 기존 파일을 복구했습니다.") from exc
        finally:
            for name in FILES:
                (ROOT/(name+".wmnext")).unlink(missing_ok=True)
    return {"updated":True,"version":row["version"][:12]}
