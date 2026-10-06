"""Background supervisor for the MobiRent farm agent.

Starts service_launcher.py, restarts it after an unexpected exit, and
refuses a second supervisor instance. Intended to run under pythonw.exe
from a Windows Scheduled Task (no NSSM, no console window).
"""
from __future__ import annotations

import ctypes
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "farm_agent.log"
STOP_FILE = LOG_DIR / "farm_agent.stop"
MUTEX_NAME = "Local\\MobiRentFarmAgentSupervisor"
CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = "8790"
POLL_SECONDS = 1.0
MIN_BACKOFF = 2.0
MAX_BACKOFF = 30.0

logger = logging.getLogger("farm_agent_supervisor")


def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False


def _agent_python() -> Path:
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file():
        return venv_python
    sibling = Path(sys.executable).with_name("python.exe")
    if sibling.is_file():
        return sibling
    return Path(sys.executable)


def _listen_target() -> tuple[str, int]:
    host = os.getenv("FARM_AGENT_LISTEN_HOST") or DEFAULT_HOST
    port_text = os.getenv("FARM_AGENT_LISTEN_PORT") or DEFAULT_PORT
    return host, int(port_text)


def _port_is_listening(host: str, port: int) -> bool:
    probes = [host]
    if host in {"0.0.0.0", "::"}:
        probes = ["127.0.0.1"]
    for address in probes:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex((address, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def _stop_requested() -> bool:
    return STOP_FILE.is_file()


def _acquire_mutex() -> int | None:
    handle = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return int(handle)


def _release_mutex(handle: int | None) -> None:
    if not handle:
        return
    ctypes.windll.kernel32.ReleaseMutex(handle)
    ctypes.windll.kernel32.CloseHandle(handle)


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def main() -> int:
    os.chdir(str(ROOT))
    load_dotenv(ENV_FILE, override=False)
    _configure_logging()
    STOP_FILE.unlink(missing_ok=True)

    mutex = _acquire_mutex()
    if mutex is None:
        logger.info("supervisor already running; exiting to avoid a duplicate")
        return 0

    host, port = _listen_target()
    python = _agent_python()
    launcher = ROOT / "service_launcher.py"
    logger.info(
        "startup supervisor pid=%s python=%s host=%s port=%s cwd=%s",
        os.getpid(),
        python.name,
        host,
        port,
        ROOT,
    )

    backoff = MIN_BACKOFF
    try:
        while not _stop_requested():
            if _port_is_listening(host, port):
                logger.info("farm agent already listening on %s:%s; waiting", host, port)
                while _port_is_listening(host, port) and not _stop_requested():
                    time.sleep(POLL_SECONDS)
                if _stop_requested():
                    break
                continue

            if not launcher.is_file():
                logger.error("launcher missing: %s", launcher.name)
                return 1

            log_handle = LOG_FILE.open("a", encoding="utf-8")
            try:
                logger.info("starting farm agent host=%s port=%s", host, port)
                child = subprocess.Popen(
                    [str(python), str(launcher)],
                    cwd=str(ROOT),
                    env=os.environ.copy(),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                )
                logger.info("farm agent child pid=%s", child.pid)
                while child.poll() is None:
                    if _stop_requested():
                        logger.info("shutdown requested; stopping child pid=%s", child.pid)
                        _terminate(child)
                        logger.info("shutdown")
                        return 0
                    time.sleep(POLL_SECONDS)
                code = child.returncode
            finally:
                log_handle.close()

            if _stop_requested():
                logger.info("shutdown after child exit code=%s", code)
                return 0
            logger.warning("restart after crash exit_code=%s backoff=%.0fs", code, backoff)
            time.sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2)
        logger.info("shutdown")
        return 0
    finally:
        _release_mutex(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
