"""Semantic Windows UI Automation helpers.

Uses the Windows accessibility tree when an application exposes one, so
actions can target labels and control roles instead of screen coordinates.
"""
import time
import re


def _desktop():
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise RuntimeError(
            "Semantic UI automation needs pywinauto. Run: pip install -r requirements.txt"
        ) from exc
    return Desktop(backend="uia")


def _title_tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", value.casefold())
        if token not in {"memory", "usage", "mb"}
    }


def _controls(window):
    controls = [window]
    try:
        controls.extend(window.descendants())
    except Exception:
        pass
    return controls


def _matches(control, target: str, control_type: str | None = None) -> bool:
    wanted = target.strip().casefold()
    if not wanted:
        return False
    try:
        info = control.element_info
        name = str(info.name or "").casefold()
        automation_id = str(info.automation_id or "").casefold()
        current_type = str(info.control_type or "").casefold()
        if control_type and current_type != control_type.casefold():
            return False
        if wanted in name or wanted in automation_id:
            return True
        aliases = {
            "message...": ("message", "send message", "type a message"),
            "message": ("message", "send message", "type a message"),
            "search": ("search", "search or start"),
        }
        return any(alias in name for alias in aliases.get(wanted, ()))
    except Exception:
        return False


def _find(target: str, window_title: str | None = None, control_type: str | None = None):
    desktop = _desktop()
    windows = desktop.windows()
    if window_title:
        wanted_window = window_title.casefold()
        windows = [
            window for window in windows
            if wanted_window in str(window.window_text()).casefold()
        ]
    for window in windows:
        for control in _controls(window):
            if _matches(control, target, control_type):
                return control
    raise RuntimeError(f"Could not find a UI control named '{target}'.")


def focus_window(title: str) -> dict:
    if not title or not title.strip():
        raise ValueError("focus_window requires a window title")
    wanted = title.strip().casefold()
    for window in _desktop().windows():
        if wanted in str(window.window_text()).casefold():
            try:
                window.restore()
            except Exception:
                pass
            window.set_focus()
            time.sleep(0.3)
            return {"ok": True, "action": "focus_window", "window": window.window_text()}
    # Browser tabs are accessibility controls inside the browser window, not
    # top-level Windows windows. Activate a matching tab when the model
    # supplies a tab title such as "Instagram - Messages".
    for window in _desktop().windows():
        try:
            for control in window.descendants():
                info = control.element_info
                if info.control_type != "TabItem":
                    continue
                tab_title = str(info.name or "").casefold()
                if wanted in tab_title or _title_tokens(wanted) <= _title_tokens(tab_title):
                    control.click_input()
                    window.set_focus()
                    time.sleep(0.3)
                    return {
                        "ok": True,
                        "action": "focus_window",
                        "window": window.window_text(),
                        "tab": info.name,
                    }
        except Exception:
            continue
    raise RuntimeError(f"Could not find a visible window named '{title}'.")


def focus_browser_tab(target: str) -> bool:
    """Activate a Chrome/Edge tab whose title contains the requested site."""
    wanted = _title_tokens(target)
    if not wanted:
        return False
    for window in _desktop().windows():
        try:
            for control in window.descendants():
                info = control.element_info
                if info.control_type != "TabItem":
                    continue
                title = str(info.name or "")
                tokens = _title_tokens(title)
                if wanted <= tokens or any(token in tokens for token in wanted):
                    control.click_input()
                    window.set_focus()
                    time.sleep(0.2)
                    return True
        except Exception:
            continue
    return False


def click_text(target: str, window_title: str | None = None, control_type: str | None = None) -> dict:
    control = _find(target, window_title, control_type)
    try:
        control.click_input()
    except Exception:
        control.invoke()
    time.sleep(0.25)
    return {"ok": True, "action": "click_text", "target": target}


def click_exact_text(target: str, window_title: str | None = None) -> dict:
    """Click an exact visible label, preferring selectable result controls."""
    wanted = " ".join(target.casefold().split())
    if not wanted:
        raise ValueError("click_exact_text requires a non-empty label")
    windows = _desktop().windows()
    if window_title:
        title = window_title.casefold()
        windows = [window for window in windows if title in str(window.window_text()).casefold()]
    matches = []
    for window in windows:
        for control in _controls(window):
            try:
                info = control.element_info
                name = " ".join(str(info.name or "").casefold().split())
                if name == wanted:
                    matches.append((str(info.control_type or ""), control))
            except Exception:
                continue
    if not matches:
        raise RuntimeError(f"Could not find an exact UI control named '{target}'.")
    preferred = {"ListItem": 0, "DataItem": 1, "TreeItem": 2, "Button": 3, "Hyperlink": 4}
    matches.sort(key=lambda item: preferred.get(item[0], 5))
    control_type, control = matches[0]
    try:
        control.click_input()
    except Exception:
        try:
            control.invoke()
        except Exception as exc:
            raise RuntimeError(f"Could not select the exact result '{target}'.") from exc
    time.sleep(0.15)
    return {"ok": True, "action": "click_exact_text", "target": target, "control_type": control_type}


def type_into(
    target: str,
    text: str,
    window_title: str | None = None,
    replace: bool = False,
) -> dict:
    if not text:
        raise ValueError("type_into requires non-empty text")
    try:
        control = _find(target, window_title)
    except RuntimeError as exc:
        if window_title and "instagram" in window_title.casefold():
            raise RuntimeError(
                "Instagram's web page controls are not exposed to Windows UI Automation "
                "in this browser session. Use the screen view so Cipher can target the "
                "visible Instagram controls, or reopen Instagram in a supported browser "
                "automation session."
            ) from exc
        raise
    try:
        control.click_input()
    except Exception:
        control.set_focus()
    try:
        import pyperclip
        from pywinauto.keyboard import send_keys
        if replace:
            send_keys("^a")
        pyperclip.copy(text)
        send_keys("^v")
    except ImportError:
        if replace:
            control.type_keys("^a")
        control.type_keys(text, with_spaces=True)
    time.sleep(0.25)
    return {"ok": True, "action": "type_into", "target": target, "length": len(text)}
