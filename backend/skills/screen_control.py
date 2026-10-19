"""Explicit, local screen capture and input helpers.

Input actions are called by the local assistant after request routing.
"""
import io
import os
import time
from datetime import datetime
from pathlib import Path

SCREENSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _foreground_window_bounds() -> tuple[int, int, int, int] | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        rect = wintypes.RECT()
        if not hwnd or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except (AttributeError, OSError):
        return None


def _pyautogui():
    try:
        import pyautogui
    except ImportError:
        raise RuntimeError("Screen control needs Pillow and pyautogui. Run: pip install -r requirements.txt")
    return pyautogui


def capture() -> dict:
    try:
        image = _pyautogui().screenshot()
        path = SCREENSHOT_DIR / f"screen-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        image.save(path, format="PNG")
        return {"ok": True, "path": str(path), "url": f"/screen/file/{path.name}"}
    except Exception as exc:
        return {"error": f"Couldn't capture the screen: {exc}"}


def mjpeg_stream(fps: int = 4):
    """Yield a live MJPEG stream for the authenticated local UI."""
    pyautogui = _pyautogui()
    delay = 1 / max(1, min(fps, 10))
    while True:
        image = pyautogui.screenshot()
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=70)
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Cache-Control: no-cache\r\n\r\n"
            + buffer.getvalue()
            + b"\r\n"
        )
        time.sleep(delay)


def click(x, y) -> dict:
    if x is None or y is None:
        raise ValueError("click requires numeric x and y coordinates")
    _pyautogui().click(int(x), int(y))
    return {"ok": True, "action": "click", "x": int(x), "y": int(y)}


def type_text(text) -> dict:
    if not isinstance(text, str) or not text:
        raise ValueError("type requires non-empty text")
    pyautogui = _pyautogui()
    try:
        import pyperclip
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
    except ImportError:
        pyautogui.write(text, interval=0.01)
    time.sleep(0.25)
    return {"ok": True, "action": "type", "length": len(text), "method": "paste"}


def hotkey(keys) -> dict:
    if not isinstance(keys, list) or not keys or not all(isinstance(key, str) for key in keys):
        raise ValueError("hotkey requires a non-empty list of key names")
    _pyautogui().hotkey(*keys)
    return {"ok": True, "action": "hotkey", "keys": keys}


def whatsapp_message(contact: str, message: str, send: bool = False) -> dict:
    """Send through an isolated browser profile with contact/header verification."""
    if not contact or not message:
        raise ValueError("whatsapp_message requires contact and message")
    from skills import whatsapp_browser
    return whatsapp_browser.send_message(contact, message, send)


def close_whatsapp_browser() -> None:
    from skills import whatsapp_browser
    whatsapp_browser.close()


def instagram_message(contact: str, message: str, send: bool = False) -> dict:
    """Start an Instagram DM using the page's accessible controls."""
    if not contact or not message:
        raise ValueError("instagram_message requires contact and message")
    from skills import ui_automation

    ui_automation.focus_window("Instagram")
    ui_automation.click_text("New message", window_title="Instagram")
    ui_automation.type_into("To:", contact, window_title="Instagram")
    time.sleep(0.6)
    ui_automation.click_text(contact, window_title="Instagram")
    time.sleep(0.3)
    ui_automation.click_text("Chat", window_title="Instagram")
    time.sleep(0.8)
    ui_automation.type_into("Message", message, window_title="Instagram")
    if send:
        _pyautogui().press("enter")
    return {
        "ok": True,
        "action": "instagram_message",
        "sent": send,
        "method": "windows_ui_automation",
    }
