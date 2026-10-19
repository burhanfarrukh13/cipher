"""
PC control skill.

Deliberately a WHITELIST of actions, not arbitrary command execution.
Nothing here accepts a free-form shell command from chat/API input —
every action is either a fixed function call or a lookup against a
dict you control below. To add a new capability, add an entry to
APPS/FOLDERS or a new explicit branch in dispatch(); never make
dispatch() call things by an arbitrary string name.

Windows-only for now (LockWorkStation, os.startfile, pycaw all assume
Windows). Guarded with IS_WINDOWS checks so it fails clearly instead
of crashing on other platforms.
"""
import ctypes
import os
import platform
import subprocess
import time
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# --- Apps: name the user can ask for -> command to launch it. ---
# Add more here as needed. Lookup is case-insensitive.
APPS = {
    "notepad": [r"C:\WINDOWS\system32\notepad.exe"],
    "calculator": [r"C:\WINDOWS\system32\calc.exe"],
    "calc": [r"C:\WINDOWS\system32\calc.exe"],
    "explorer": [r"C:\WINDOWS\explorer.exe"],
    "file explorer": [r"C:\WINDOWS\explorer.exe"],
    "paint": [r"C:\WINDOWS\system32\mspaint.exe"],
    "snipping tool": [r"C:\WINDOWS\system32\SnippingTool.exe"],
    "settings": ["ms-settings:"],
    "powershell": [r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe"],
    "terminal": ["wt.exe"],
    "visual studio code": [r"C:\Users\user\AppData\Local\Programs\Microsoft VS Code\Code.exe"],
    "vs code": [r"C:\Users\user\AppData\Local\Programs\Microsoft VS Code\Code.exe"],
    "code": [r"C:\Users\user\AppData\Local\Programs\Microsoft VS Code\Code.exe"],
    "teams": ["ms-teams:"],
    "mail": ["outlookmail:"],
    "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe"],
    "firefox": [r"C:\Program Files\Mozilla Firefox\firefox.exe"],
    "edge": [r"C:\Program Files (x86)\Microsoft\Copilot\Application\150.0.4078.65\msedge.exe"],
    "spotify": [r"C:\Users\user\AppData\Local\Microsoft\WindowsApps\Spotify.exe"],
    "whatsapp": ["whatsapp:"],
    "word": [r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"],
    "excel": [r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"],
    "powerpoint": [r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE"],
    "outlook": [r"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE"],
    "task manager": [r"C:\WINDOWS\system32\Taskmgr.exe"],
    "zoom": [r"C:\Users\user\AppData\Roaming\Zoom\bin\Zoom.exe"],
    "vlc": [r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"],
    "notion": [r"C:\Users\user\AppData\Local\Programs\Notion\Notion.exe"],
    "perplexity": [r"C:\Users\user\AppData\Local\Programs\Perplexity\Perplexity.exe"],
    "winrar": [r"C:\Program Files\WinRAR\WinRAR.exe"],
    "vmware": [r"D:\vm ware\vmware.exe"],
    "7-zip": [r"C:\Program Files\7-Zip\7zFM.exe"],
    "acrobat": [r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe"],
    "adobe acrobat": [r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe"],
    "bluestacks": [r"C:\Program Files\BlueStacks_nxt\HD-Player.exe"],
    "capcut": [r"C:\Users\user\AppData\Local\CapCut\Apps\CapCut.exe"],
    "cheat engine": [r"C:\Program Files\Cheat Engine\cheatengine-x86_64.exe"],
    "epic games": [r"D:\time pass\ms\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe"],
    "gameloop": [r"D:\TxGameAssistant\AppMarket\AppMarket.exe"],
    "google drive": [r"C:\Program Files\Google\Drive File Stream\128.0.0.0\GoogleDriveFS.exe"],
    "idm": [r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe"],
    "internet download manager": [r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe"],
    "itop screen recorder": [r"D:\i top\iTop Screen Recorder\iScrRec.exe"],
    "lively wallpaper": [r"D:\Lively Wallpaper\Lively.exe"],
    "onedrive": [r"C:\Program Files\Microsoft OneDrive\OneDrive.exe"],
    "remote desktop": [r"C:\WINDOWS\system32\mstsc.exe"],
    "remote desktop connection": [r"C:\WINDOWS\system32\mstsc.exe"],
    "autohotkey": [r"C:\Program Files\AutoHotkey\UX\AutoHotkeyUX.exe"],
    "turbovpn": [r"C:\Program Files (x86)\TurboVPN\TurboVPN.exe"],
    "hotspot shield": [r"C:\Program Files (x86)\Hotspot Shield\12.16.0\bin\hsscp.exe"],
}

APP_DETAILS = {
    "notepad": "text editing",
    "calculator": "calculations",
    "explorer": "files and folders",
    "paint": "basic image editing",
    "snipping tool": "screenshots and screen clips",
    "settings": "Windows settings",
    "powershell": "PowerShell terminal",
    "terminal": "Windows Terminal",
    "visual studio code": "code editing and terminal",
    "teams": "desktop work chat",
    "word": "document editing",
    "excel": "spreadsheet editing",
    "powerpoint": "presentations",
    "outlook": "email and calendar",
    "spotify": "music playback",
    "vlc": "local media playback",
    "zoom": "video meetings",
}


def catalog() -> dict:
    return {
        name: {"launch": APPS[name], "capabilities": APP_DETAILS.get(name, "open and focus")}
        for name in sorted(APPS)
    }

# --- Folders: name -> real path. NOT arbitrary paths from chat input. ---
FOLDERS = {
    "documents": os.path.expanduser("~/Documents"),
    "downloads": os.path.expanduser("~/Downloads"),
    "desktop": os.path.expanduser("~/Desktop"),
    "cipher": str(PROJECT_ROOT),
}


def list_actions() -> dict:
    """What's available — useful for a settings panel or for the LLM's context."""
    return {
        "open_app": sorted(APPS.keys()),
        "close_app": sorted(APPS.keys()),
        "open_folder": sorted(FOLDERS.keys()),
        "lock_screen": "no value needed",
        "set_volume": "value: integer 0-100",
        "volume_up": "no value needed (steps +10)",
        "volume_down": "no value needed (steps -10)",
        "mute": "no value needed",
        "unmute": "no value needed",
    }


def _activate_window(hwnd) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    user32 = ctypes.windll.user32
    foreground = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    attached = bool(foreground_thread and target_thread and foreground_thread != target_thread)
    try:
        if attached:
            user32.AttachThreadInput(foreground_thread, target_thread, True)
        user32.ShowWindow(hwnd, 9)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
        time.sleep(0.2)
        return user32.GetForegroundWindow() == hwnd
    finally:
        if attached:
            user32.AttachThreadInput(foreground_thread, target_thread, False)


def _window_for_process(process_id: int):
    user32 = ctypes.windll.user32
    found = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        current_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(current_pid))
        if current_pid.value == process_id and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def _foreground_title() -> str:
    if not IS_WINDOWS:
        return ""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd) if hwnd else 0
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def foreground_window_title() -> str:
    """Return the title of the currently foreground window."""
    return _foreground_title()


def open_app(name: str) -> dict:
    key = (name or "").strip().lower()
    if key not in APPS:
        return {"error": f"'{name}' isn't a whitelisted app.", "known_apps": sorted(APPS.keys())}
    try:
        if focus_existing_window(key):
            return {"ok": True, "opened": key, "focused_existing": True, "foreground": True}
        if not IS_WINDOWS:
            return {"error": "Opening and foregrounding apps is currently Windows-only."}
        launch = APPS[key]
        if len(launch) == 1 and launch[0].endswith(":"):
            os.startfile(launch[0])
            process = None
        else:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 1
            process = subprocess.Popen(launch, startupinfo=startup)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            hwnd = _window_for_process(process.pid) if process else None
            if hwnd and _activate_window(hwnd):
                return {"ok": True, "opened": key, "foreground": True}
            if focus_existing_window(key):
                return {"ok": True, "opened": key, "focused_existing": True, "foreground": True}
            time.sleep(0.25)
        return {
            "error": f"Started '{key}', but Windows did not expose a foreground window within 12 seconds.",
            "foreground_title": _foreground_title(),
        }
    except FileNotFoundError:
        return {"error": f"'{key}' is whitelisted but isn't installed/on PATH on this machine."}
    except Exception as e:
        return {"error": str(e)}


def focus_existing_window(name: str) -> bool:
    """Restore and foreground a visible top-level window whose title matches the app."""
    if not IS_WINDOWS:
        return False
    needle = "whatsapp" if name == "whatsapp" else name.casefold()
    user32 = ctypes.windll.user32
    found = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if needle in buffer.value.casefold():
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(callback, 0)
    return _activate_window(found[0]) if found else False


def close_app(name: str) -> dict:
    """Closes a whitelisted app by killing its process (matched by the
    exe filename from APPS, same whitelist open_app uses — no arbitrary
    process names accepted from chat input)."""
    key = (name or "").strip().lower()
    if key not in APPS:
        return {"error": f"'{name}' isn't a whitelisted app.", "known_apps": sorted(APPS.keys())}
    if not IS_WINDOWS:
        return {"error": "close_app currently uses taskkill, which is Windows-only."}
    exe_name = os.path.basename(APPS[key][0])
    try:
        result = subprocess.run(
            ["taskkill", "/IM", exe_name, "/F"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return {"ok": True, "closed": key}
        # taskkill exits non-zero (commonly 128) when no matching process is running.
        return {"error": f"'{key}' doesn't seem to be running."}
    except Exception as e:
        return {"error": str(e)}


def open_folder(name: str) -> dict:
    key = (name or "").strip().lower()
    if key not in FOLDERS:
        return {"error": f"'{name}' isn't a whitelisted folder.", "known_folders": sorted(FOLDERS.keys())}
    if not IS_WINDOWS:
        return {"error": "open_folder currently uses os.startfile, which is Windows-only."}
    path = FOLDERS[key]
    try:
        os.startfile(path)
        return {"ok": True, "opened": path}
    except Exception as e:
        return {"error": str(e)}


def lock_screen() -> dict:
    if not IS_WINDOWS:
        return {"error": "lock_screen is only implemented for Windows right now."}
    ctypes.windll.user32.LockWorkStation()
    return {"ok": True}


def _get_volume_interface():
    """Shared setup for the volume functions. Raises ImportError if pycaw isn't installed."""
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def set_volume(level) -> dict:
    """level: 0-100"""
    if not IS_WINDOWS:
        return {"error": "Volume control is only implemented for Windows right now."}
    try:
        volume = _get_volume_interface()
    except ImportError:
        return {"error": "Volume control needs extra packages. Run: pip install pycaw comtypes"}

    try:
        level = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return {"error": f"'{level}' isn't a valid volume level (expected 0-100)."}

    volume.SetMasterVolumeLevelScalar(level / 100.0, None)
    return {"ok": True, "volume": level}


def _adjust_volume(delta: int) -> dict:
    if not IS_WINDOWS:
        return {"error": "Volume control is only implemented for Windows right now."}
    try:
        volume = _get_volume_interface()
    except ImportError:
        return {"error": "Volume control needs extra packages. Run: pip install pycaw comtypes"}

    current = round(volume.GetMasterVolumeLevelScalar() * 100)
    new_level = max(0, min(100, current + delta))
    volume.SetMasterVolumeLevelScalar(new_level / 100.0, None)
    return {"ok": True, "volume": new_level}


def volume_up() -> dict:
    return _adjust_volume(10)


def volume_down() -> dict:
    return _adjust_volume(-10)


def set_mute(muted: bool) -> dict:
    if not IS_WINDOWS:
        return {"error": "Volume control is only implemented for Windows right now."}
    try:
        volume = _get_volume_interface()
    except ImportError:
        return {"error": "Volume control needs extra packages. Run: pip install pycaw comtypes"}
    volume.SetMute(1 if muted else 0, None)
    return {"ok": True, "muted": muted}


def dispatch(action: str, value=None) -> dict:
    """
    The one entry point main.py calls. `action` is matched against a
    fixed set of names below — there is no path from this function to
    executing an arbitrary string as code or as a shell command.
    """
    action = (action or "").strip().lower()

    if action == "open_app":
        return open_app(value)
    if action == "close_app":
        return close_app(value)
    if action == "open_folder":
        return open_folder(value)
    if action == "lock_screen":
        return lock_screen()
    if action == "set_volume":
        return set_volume(value)
    if action == "volume_up":
        return volume_up()
    if action == "volume_down":
        return volume_down()
    if action == "mute":
        return set_mute(True)
    if action == "unmute":
        return set_mute(False)

    return {
        "error": f"Unknown action '{action}'.",
        "known_actions": sorted(list_actions().keys()),
    }
