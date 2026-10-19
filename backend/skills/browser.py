import re
import time
import webbrowser
from urllib.parse import quote_plus, urlparse

import httpx

# Name the user can say -> URL to open. Add more here as needed.
SITES = {
    "gmail": "https://mail.google.com",
    "email": "https://mail.google.com",
    "spotify": "https://open.spotify.com",
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "google": "https://www.google.com",
    "gemini": "https://gemini.google.com",
    "amazon": "https://www.amazon.com",
    "netflix": "https://www.netflix.com",
    "calendar": "https://calendar.google.com",
    "maps": "https://maps.google.com",
    "drive": "https://drive.google.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "linkedin": "https://www.linkedin.com",
    "reddit": "https://www.reddit.com",
    "x": "https://x.com",
    "twitter": "https://x.com",
    "tiktok": "https://www.tiktok.com",
    "discord": "https://discord.com/app",
    "telegram": "https://web.telegram.org",
    "slack": "https://app.slack.com/client",
    "teams": "https://teams.microsoft.com",
    "notion": "https://www.notion.so",
    "trello": "https://trello.com",
    "canva": "https://www.canva.com",
    "chatgpt": "https://chatgpt.com",
    "docs": "https://docs.google.com",
    "sheets": "https://sheets.google.com",
}

SITE_DETAILS = {
    "gmail": "email: search, read, compose drafts",
    "youtube": "video: search, open results, playback controls",
    "google": "search: web searches and result navigation",
    "instagram": "social: messages and profile navigation",
    "facebook": "social: search, posts, and messages",
    "linkedin": "professional network: search, profiles, messages",
    "reddit": "forums: search, posts, and comments",
    "discord": "chat: servers, channels, and messages",
    "telegram": "chat: contacts and messages",
    "teams": "work chat: teams, channels, and messages",
    "slack": "work chat: channels and messages",
    "notion": "notes and workspace pages",
    "trello": "project boards and cards",
    "canva": "design editor and templates",
    "chatgpt": "AI chat",
    "docs": "document editing",
    "sheets": "spreadsheet editing",
}

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def list_sites() -> list[str]:
    return sorted(SITES.keys())


def catalog() -> dict:
    return {name: {"url": SITES[name], "capabilities": SITE_DETAILS.get(name, "browser navigation")}
            for name in sorted(SITES)}


def _activate_existing_tab(target: str) -> bool:
    try:
        from . import ui_automation
        return ui_automation.focus_browser_tab(target)
    except (ImportError, RuntimeError, OSError):
        return False


def _navigate_existing_tab(url: str, target: str) -> bool:
    if not _activate_existing_tab(target):
        return False
    try:
        from . import screen_control
        pyautogui = screen_control._pyautogui()
        pyautogui.hotkey("ctrl", "l")
        screen_control.type_text(url)
        pyautogui.press("enter")
        return True
    except (ImportError, RuntimeError, OSError):
        return False


def _open_in_foreground(url: str, target: str) -> dict:
    if not webbrowser.open(url, new=0):
        return {"error": f"Default browser did not accept {url}."}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _activate_existing_tab(target):
            return {"ok": True, "opened": url, "foreground": True}
        time.sleep(0.25)
    return {
        "error": f"Browser accepted {url}, but Cipher could not bring its tab to the foreground.",
    }


def open_target(target: str) -> dict:
    """
    `target` is either a whitelisted site name (see SITES) or a raw
    http(s) URL. Anything else — file://, javascript:, data:, etc. —
    is rejected so this can't be used to open local files or run
    script URLs.
    """
    target = (target or "").strip()
    key = target.lower()

    if key in SITES:
        url = SITES[key]
    elif _URL_RE.match(target):
        url = target
    else:
        return {
            "error": f"'{target}' isn't a known site and isn't an http(s) URL.",
            "known_sites": list_sites(),
        }

    try:
        tab_target = key
        if _URL_RE.match(target):
            tab_target = urlparse(target).netloc.split(".")[0]
        if _activate_existing_tab(tab_target):
            return {"ok": True, "opened": url, "focused_existing": True}
        return _open_in_foreground(url, tab_target)
    except Exception as e:
        return {"error": str(e)}


def youtube_search_and_open(query: str, result_index: int | None = None) -> dict:
    """Open a YouTube search, or a specific numbered result when requested."""
    query = (query or "").strip()
    if not query:
        return {"error": "Tell me what to search for on YouTube."}
    if result_index is None:
        url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        if _navigate_existing_tab(url, "youtube"):
            return {"ok": True, "opened": url, "focused_existing": True}
        return _open_in_foreground(url, "youtube")
    if result_index < 1 or result_index > 10:
        return {"error": "YouTube result_index must be between 1 and 10."}
    try:
        html = httpx.get(
            f"https://www.youtube.com/results?search_query={quote_plus(query)}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        ).text
        ids = []
        for video_id in re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html):
            if video_id not in ids:
                ids.append(video_id)
        if len(ids) < result_index:
            return {"error": f"YouTube returned fewer than {result_index} playable results."}
        video_id = ids[result_index - 1]
        url = f"https://www.youtube.com/watch?v={video_id}"
        if _navigate_existing_tab(url, "youtube"):
            return {"ok": True, "opened": url, "focused_existing": True, "title": f"result {result_index} for {query}"}
        if not webbrowser.open(url, new=0):
            return {"error": f"Default browser did not accept {url}."}
        return {"ok": True, "opened": url, "title": f"result {result_index} for {query}"}
    except httpx.HTTPError as exc:
        return {"error": f"YouTube search failed: {exc}"}


def spotify_search_and_open(query: str, result_index: int | None = None) -> dict:
    """Open a Spotify search in the existing Spotify tab when possible."""
    query = (query or "").strip()
    if not query:
        return {"error": "Tell me what to search for on Spotify."}
    url = f"https://open.spotify.com/search/{quote_plus(query)}"
    if _navigate_existing_tab(url, "spotify"):
        return {
            "ok": True,
            "opened": url,
            "focused_existing": True,
            "result_index": result_index,
        }
    opened = _open_in_foreground(url, "spotify")
    if opened.get("error"):
        return opened
    return {
        **opened,
        "result_index": result_index,
        "note": "Spotify search opened; result selection may require the logged-in Spotify UI.",
    }


def open_and_type(target: str, text: str, delay_sec: float = 3.0) -> dict:
    """Open a known site, wait for its window, then type into the active window."""
    from . import screen_control

    target = (target or "").strip().lower()
    text = (text or "").strip()
    if target not in SITES:
        return {"error": f"'{target}' is not a supported site."}
    if not text:
        return {"error": "Nothing to type."}
    opened = open_target(target)
    if opened.get("error"):
        return opened
    time.sleep(max(0.5, min(float(delay_sec), 10.0)))
    try:
        typed = screen_control.type_text(text)
    except Exception as exc:
        return {"error": f"Opened {target}, but typing failed: {exc}"}
    if typed.get("error"):
        return typed
    return {"ok": True, "opened": opened["opened"], "typed_length": len(text)}
