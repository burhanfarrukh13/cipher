"""
Executor — runs arbitrary PowerShell commands on the user's machine.

Runs in unrestricted mode by default. A hard blocklist still protects
catastrophic system commands.

  1. UNRESTRICTED (you are present and verified)
     - Default way to unlock: the camera recognizes your face. The unlock
       lasts 30 minutes of activity, same as the face-lock session in
       main.py. Every chat message extends the window. When the timer
       runs out, it snaps back to safe automatically.
     - Backup way to unlock: send CIPHER_UNLOCK_PHRASE (from .env) as a
       chat message. Useful for testing without the camera, or if you
       want to share access with someone you trust.

Every command run in unrestricted mode is logged to
backend/data/exec_log.json with a timestamp, the command, exit code,
and a head of the output. Nothing runs silently.

Hard blocklist (even in unrestricted mode): catastrophic commands
(format-volume, rd /s /q C:\\, bcdedit, diskpart, reg delete hklm, etc)
are always refused. To override, edit this file.
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent.parent / "data" / "exec_log.json"
LOG_FILE.parent.mkdir(exist_ok=True)

# Blacklist of commands we will NEVER run, even with unrestricted mode on.
# These are the "you're about to nuke your machine" patterns. If you
# genuinely need one of these, edit this list — but at least you'll
# have to look it in the eye first.
_BLACKLIST_PATTERNS = [
    "format-volume",
    "format c:",
    "format d:",
    "rd /s /q c:\\",
    "rd /s /q d:\\",
    "rm -rf /",
    "remove-item -recurse -force c:\\",
    "remove-item -recurse -force d:\\",
    "bcdedit",
    "bootrec",
    "diskpart",
    "sfc /scannow",
    "reg delete hklm",
    "reg delete hkcr",
    "netsh advfirewall",
    "net user administrator",
    "cipher /w",
    "cleanmgr /sagerun",
    "powershell -enc",
    "invoke-expression",
    "iex ",
]


_session_unlocked_until = 0.0
_session_lock = threading.Lock()
ALWAYS_UNRESTRICTED = os.getenv("CIPHER_ALWAYS_UNRESTRICTED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}


def _is_unlocked_at(now: float) -> bool:
    return now < _session_unlocked_until


def is_unlocked() -> bool:
    """True when computer control is available."""
    if ALWAYS_UNRESTRICTED:
        return True
    with _session_lock:
        return _is_unlocked_at(time.time())


def unlock_session(duration_sec: int = 30 * 60) -> float:
    """
    Unlock for `duration_sec` from now. Re-calling extends the window.
    Returns the new expiry timestamp (seconds since epoch).
    """
    global _session_unlocked_until
    with _session_lock:
        _session_unlocked_until = time.time() + duration_sec
        return _session_unlocked_until


def lock_session() -> None:
    """Lock only when permanent unrestricted mode is disabled."""
    global _session_unlocked_until
    if ALWAYS_UNRESTRICTED:
        return
    with _session_lock:
        _session_unlocked_until = 0.0


def try_unlock(phrase: str) -> bool:
    """
    Compares the user's message against CIPHER_UNLOCK_PHRASE from .env.
    If they match, unlocks the session for 30 minutes from now. Returns
    True if the session is now unlocked.
    """
    expected = os.getenv("CIPHER_UNLOCK_PHRASE", "").strip()
    if not expected:
        return False  # never configured an unlock phrase → never unlockable
    if (phrase or "").strip().lower() == expected.lower():
        unlock_session()
        return True
    return False


def lock() -> None:
    """Backward-compat alias for lock_session()."""
    lock_session()


def _is_blacklisted(command: str) -> bool:
    c = (command or "").lower()
    return any(pat in c for pat in _BLACKLIST_PATTERNS)


def _log(entry: dict) -> None:
    log = []
    if LOG_FILE.exists():
        try:
            log = json.loads(LOG_FILE.read_text())
        except Exception:
            log = []
    log.append(entry)
    # Keep the log bounded — last 500 entries.
    log = log[-500:]
    LOG_FILE.write_text(json.dumps(log, indent=2))


def run_powershell(command: str, timeout_sec: int = 30) -> dict:
    """
    Runs an arbitrary PowerShell command. Returns
        {"ok": True, "stdout": ..., "stderr": ..., "exit_code": ..., "duration_ms": ...}
    or {"error": ..., "blocked": True/False}.

    Blacklisted commands are refused even in unrestricted mode.
    """
    if not is_unlocked():
        return {"error": "Computer control is locked. Enable unrestricted mode first."}

    command = (command or "").strip()
    if not command:
        return {"error": "Empty command."}

    if _is_blacklisted(command):
        _log({
            "at": datetime.utcnow().isoformat(),
            "command": command,
            "blocked": True,
            "reason": "blacklist",
        })
        return {"error": "Refused: that command is on the hard blocklist (it can wipe your system). Edit skills/executor.py if you really need it.", "blocked": True}

    t0 = time.time()
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout_sec,
        )
        duration_ms = int((time.time() - t0) * 1000)
        stdout = (result.stdout or "")[:4096]
        stderr = (result.stderr or "")[:4096]
        _log({
            "at": datetime.utcnow().isoformat(),
            "command": command,
            "blocked": False,
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "duration_ms": duration_ms,
            "stdout_head": stdout[:500],
            "stderr_head": stderr[:500],
        })
        return {
            "ok": result.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": result.returncode,
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - t0) * 1000)
        _log({"at": datetime.utcnow().isoformat(), "command": command, "timeout": True, "duration_ms": duration_ms})
        return {"error": f"Command timed out after {timeout_sec}s.", "timeout": True}
    except Exception as e:
        _log({"at": datetime.utcnow().isoformat(), "command": command, "error": str(e)})
        return {"error": f"Couldn't run command: {e}"}
