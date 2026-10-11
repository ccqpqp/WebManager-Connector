"""WebManager background GitHub monitor. Runs entirely on the user's PC.

Reads the private settings BRANCH through local Git credentials. The monitor
never needs an open browser and never starts a deployment on first observation.
Only allowlisted local projects are eligible. SHA attempts are persisted before
queueing to prevent replay after restarts/crashes. No secrets are logged.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import local_deploy

BASE = local_deploy.DATA
CONFIG_REPO = BASE / "auto-settings-repo"
LEDGER_FILE = BASE / "auto-local-ledger.json"
SETTINGS_REPO = "https://github.com/ccqpqp/WebManager.git"
SETTINGS_BRANCH = "webmanager-settings"
SETTINGS_FILE = "config/deployment-modes.json"
INTERVAL_SECONDS = 60
STABILITY_SECONDS = 3 * 60
MAX_CHECK_SECONDS = 35
LOCK = threading.RLock()
STOP = threading.Event()
THREAD: threading.Thread | None = None
STATE: dict = {
    "running": False, "enabled": [], "lastCheck": None,
    "lastError": "", "lastTrigger": None, "nextCheckSeconds": INTERVAL_SECONDS,
    "source": "PC 백그라운드", "browserRequired": False,
    "settlingSeconds": STABILITY_SECONDS, "lastPending": None,
}

def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _age(value):
    try:
        started=datetime.datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc)-started).total_seconds()
    except (TypeError,ValueError):
        return 0

def _git(*args: str, cwd: Path | None = None, timeout: int = MAX_CHECK_SECONDS):
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
                "GIT_CONFIG_NOSYSTEM": "0"})
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(["git", "-c", "credential.interactive=never", *args],
            cwd=str(cwd) if cwd else None, env=env, shell=False,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=flags)
    except FileNotFoundError as exc:
        raise RuntimeError("Git이 설치되어 있지 않거나 PATH에서 찾을 수 없습니다.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub 연결 시간 초과 · PC 인터넷/Git 인증 확인") from exc
    if proc.returncode:
        # Do not copy stderr: git credential helpers may leak sensitive data.
        raise RuntimeError("비공개 GitHub 설정 브랜치 접근 실패 · Windows Git 인증 상태 확인")
    return proc.stdout.strip()

def _read_settings():
    # Never modify user's development repositories or their package-lock.json.
    BASE.mkdir(parents=True, exist_ok=True)
    if CONFIG_REPO.exists():
        if CONFIG_REPO.is_symlink() or not (CONFIG_REPO / ".git").is_dir():
            raise RuntimeError("자동배포 설정 작업 폴더를 확인하세요.")
        origin = _git("remote", "get-url", "origin", cwd=CONFIG_REPO, timeout=10)
        if origin.rstrip("/").removesuffix(".git").lower() != SETTINGS_REPO.removesuffix(".git").lower():
            raise RuntimeError("자동배포 설정 폴더의 GitHub 주소가 일치하지 않습니다.")
    else:
        _git("clone", "--depth", "1", "--single-branch",
             "--branch", SETTINGS_BRANCH, SETTINGS_REPO, str(CONFIG_REPO),
             cwd=BASE, timeout=90)
    _git("fetch", "--depth", "1", "origin", SETTINGS_BRANCH,
         cwd=CONFIG_REPO, timeout=40)
    raw = _git("show", "FETCH_HEAD:" + SETTINGS_FILE, cwd=CONFIG_REPO, timeout=10)
    try:
        values = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("GitHub 자동배포 설정 파일이 올바르지 않습니다.") from exc
    if values.get("version") != 1 or not isinstance(values.get("modes"), dict):
        raise RuntimeError("GitHub 자동배포 설정 버전이 일치하지 않습니다.")
    return sorted(project for project, mode in values["modes"].items()
                  if project in local_deploy.SITES and mode == "local")

def _ledger():
    try:
        data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
        if data.get("version") == 1 and isinstance(data.get("projects"), dict):
            return data["projects"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}

def _persist(data):
    BASE.mkdir(parents=True, exist_ok=True)
    temporary = LEDGER_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": 1, "projects": data},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, LEDGER_FILE)

def _record(project, value):
    with LOCK:
        record = _ledger()
        record[project] = value
        _persist(record)

def _sync_result():
    # Job completion is informational. Its SHA is already marked as attempted,
    # so a crash or failure cannot restart an identical SHA indefinitely.
    jobs = local_deploy.status().get("jobs", [])
    with LOCK:
        ledger = _ledger()
        updated = False
        for job in jobs:
            if not job.get("automatic"):
                continue
            project = job.get("project")
            row = ledger.get(project)
            if not isinstance(row, dict) or row.get("job") != job.get("id"):
                continue
            new_status = job.get("phase", "")
            if new_status in ("success", "failed") and row.get("status") != new_status:
                row["status"] = new_status
                row["updatedAt"] = _now()
                updated = True
        if updated:
            _persist(ledger)

def _set_state(**fields):
    with LOCK:
        STATE.update(fields)

def _scan():
    enabled = _read_settings()
    _set_state(enabled=enabled, lastCheck=_now(), lastError="")
    _sync_result()
    if not enabled:
        return
    existing = _ledger()
    inflight = local_deploy.status()
    active_jobs = {j["project"] for j in inflight["jobs"]
                   if j.get("phase") in ("queued", "source", "install", "build", "upload")}
    slots = max(0, int(inflight.get("maxConcurrent", 1)) - len(active_jobs))
    for project in enabled:
        if STOP.is_set():
            break
        if project in active_jobs or slots <= 0:
            continue
        try:
            sha = local_deploy.plan(project)["sha"].lower()
        except (ValueError, RuntimeError, OSError) as exc:
            _set_state(lastError=project + " · GitHub 최신 코드 조회 실패: " +
                       str(exc)[:110])
            continue
        previous = existing.get(project)
        if not isinstance(previous, dict):
            # First observation is baseline only. No surprise deploy on enabling.
            _record(project, {"sha": sha, "status": "baseline",
                              "updatedAt": _now()})
            continue
        if previous.get("sha") == sha:
            if previous.get("candidateSha"):
                clean={key:value for key,value in previous.items()
                       if key not in ("candidateSha","candidateSince")}
                _record(project,clean)
            continue
        # A push is not deployed immediately. Wait until the same GitHub HEAD has
        # remained unchanged for a few checks, so several consecutive commits
        # become one deployment of only the final revision.
        if previous.get("candidateSha") != sha:
            pending={**previous,"candidateSha":sha,"candidateSince":_now()}
            _record(project,pending);existing[project]=pending
            _set_state(lastPending={"project":project,"sha":sha[:12],
                                    "at":pending["candidateSince"]})
            continue
        if _age(previous.get("candidateSince")) < STABILITY_SECONDS:
            continue
        # Persist before queueing. Even if agent exits here, same SHA is not
        # retried automatically. Manual deploy is always available.
        entry = {"sha": sha, "status": "attempted", "updatedAt": _now()}
        _record(project, entry)
        existing[project] = entry
        try:
            job = local_deploy.submit(project, expected_sha=sha, automatic=True)
        except (ValueError, RuntimeError) as exc:
            _record(project, {**entry, "status": "rejected",
                              "reason": str(exc)[:110]})
            _set_state(lastError=project + " · 자동배포 요청 거절: " + str(exc)[:110])
            continue
        _record(project, {**entry, "job": job["id"], "status": "queued"})
        _set_state(lastTrigger={"project": project, "sha": sha[:12],
                                "at": _now()}, lastError="")
        slots -= 1
        active_jobs.add(project)

def _loop():
    _set_state(running=True, lastError="")
    try:
        while not STOP.is_set():
            try:
                _scan()
            except (RuntimeError, ValueError, OSError) as exc:
                _set_state(lastCheck=_now(), lastError=str(exc)[:170])
            # Every minute, independent of browser visibility or login.
            if STOP.wait(INTERVAL_SECONDS):
                break
    finally:
        _set_state(running=False)

def start():
    global THREAD
    with LOCK:
        if THREAD is not None and THREAD.is_alive():
            return
        STOP.clear()
        THREAD = threading.Thread(target=_loop, name="WebManagerBackgroundAuto",
                                  daemon=True)
        THREAD.start()

def stop():
    STOP.set()
    thread = THREAD
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=3)

def status():
    with LOCK:
        return dict(STATE)
