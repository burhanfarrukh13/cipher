"""
Cipher core — local personal assistant.

Architecture (Qureshi Sahib's notes):
    1. Every chat message flows through this router.
    2. First we check for the unlock phrase (if Cipher is in restricted mode).
    3. Then we try the whitelisted command router (volume, open app, weather,
       places, etc) — fast and exact.
    4. If no whitelist hit, we ALWAYS run a live web search and feed the
       results to Ollama — the model evaluates what the user actually wants,
       can answer conversationally, or (in unrestricted mode) can request a
       PowerShell command to run.
    5. In unrestricted mode, Ollama can emit a fenced command block like
         ```powershell
         Get-Process
         ```
       and we execute it and feed the result back through.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000
Then open frontend/index.html.
"""
import os
import base64
import io
from dotenv import load_dotenv
load_dotenv()

import httpx
import json
import re
import time
import uuid
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from skills import tasks, smart_home, pc_control, browser, face_auth, weather, web_search, places, executor, screen_control, ui_automation

app = FastAPI(title="Cipher")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "").strip()
OLLAMA_URL = os.getenv(
    "OLLAMA_URL",
    "https://ollama.com/api/chat" if OLLAMA_API_KEY else "http://localhost:11434/api/chat",
)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:31b")

USER_NAME = os.getenv("USER_NAME", "").strip()
USER_HONORIFIC = os.getenv("USER_HONORIFIC", "").strip()

# --- Face-lock session ---
# The face session is the natural key to unrestricted mode: when the
# camera recognizes you, both the chat UI and the executor unlock for
# the same 30-minute sliding window. Walk away or sit idle → both
# lock back down. The unlock-phrase path is kept as a backup.
SESSION_TIMEOUT_SEC = 30 * 60
FACE_LOCK_ENABLED = os.getenv("CIPHER_FACE_LOCK_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
_session_state = {"verified_at": 0.0, "last_activity": 0.0}
_pending_confirmation: list[dict] | None = None
_active_app: str | None = None
_MEMORY_FILE = Path(__file__).resolve().parent / "data" / "chat_memory.json"
_FACTS_FILE = Path(__file__).resolve().parent / "data" / "long_term_memory.json"
_TASK_STATE_FILE = Path(__file__).resolve().parent / "data" / "task_state.json"
_MEMORY_LIMIT = 40


def _load_memory() -> list[dict]:
    try:
        data = json.loads(_MEMORY_FILE.read_text()) if _MEMORY_FILE.exists() else []
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _remember(role: str, content: str) -> None:
    memory = _load_memory()
    memory.append({"role": role, "content": content[:4000], "at": time.time()})
    _MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MEMORY_FILE.write_text(json.dumps(memory[-_MEMORY_LIMIT:], indent=2))


def _load_facts() -> dict:
    try:
        data = json.loads(_FACTS_FILE.read_text()) if _FACTS_FILE.exists() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_fact(category: str, key: str, value: str) -> dict:
    facts = _load_facts()
    facts.setdefault(category, {})[key.lower().strip()] = value.strip()
    _FACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _FACTS_FILE.write_text(json.dumps(facts, indent=2, ensure_ascii=False))
    return facts


def _normalize_relation(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", str(value or "").lower())
    modifiers = {"elder", "younger", "old", "new", "best", "dear", "my"}
    remaining = [word for word in words if word not in modifiers]
    return remaining[-1] if remaining else (words[-1] if words else "")


def _memory_items() -> list[dict]:
    items = _load_facts().get("memories", [])
    return [item for item in items if isinstance(item, dict) and item.get("content")]


def _save_memory_item(content: str) -> dict:
    normalized = " ".join(content.casefold().split())
    if not normalized:
        raise ValueError("A memory cannot be empty.")
    if len(content) > 4000:
        raise ValueError("A single long-term memory must be 4,000 characters or less.")
    if re.search(r"\b(?:password|passcode|api[ _-]?key|secret|private key|recovery code)\b", content, re.IGNORECASE):
        raise ValueError("For your security, Cipher will not store passwords, API keys, or recovery secrets in long-term memory.")

    facts = _load_facts()
    items = facts.setdefault("memories", [])
    now = time.time()
    for item in items:
        if " ".join(str(item.get("content", "")).casefold().split()) == normalized:
            item["updated_at"] = now
            _FACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _FACTS_FILE.write_text(json.dumps(facts, indent=2, ensure_ascii=False))
            return item
    item = {
        "id": uuid.uuid4().hex,
        "content": content.strip(),
        "created_at": now,
        "updated_at": now,
    }
    items.append(item)
    _FACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _FACTS_FILE.write_text(json.dumps(facts, indent=2, ensure_ascii=False))
    return item


def _remember_user_facts(message: str) -> dict | None:
    match = re.match(
        r"\s*(?:hey\s+cipher[, ]+)?(?:please\s+)?(?:remember|save|store|note)\s+(?:that\s+)?(?P<fact>.+?)\s*[.!?]*\s*$",
        message,
        re.IGNORECASE,
    )
    if not match:
        return None
    content = match.group("fact").strip().strip("\"'")
    content = re.sub(r"\s+", " ", content)
    if not content:
        return None
    contact_match = re.fullmatch(
        r"[\"']?(?P<contact>.+?)[\"']?\s+(?:is|as)\s+my\s+(?P<relation>[a-z][a-z0-9]*(?:\s+[a-z][a-z0-9]*){0,3})",
        content,
        re.IGNORECASE,
    )
    if contact_match:
        contact = contact_match.group("contact").strip().strip("\"'")
        relation = re.sub(r"\s+", " ", contact_match.group("relation").strip().lower())
        _save_fact("contacts", relation, contact)
        normalized = _normalize_relation(relation)
        if normalized and normalized != relation:
            _save_fact("contacts", normalized, contact)
    item = _save_memory_item(content)
    return {"id": item["id"], "content": item["content"], "updated": item.get("created_at") != item.get("updated_at")}


def _forget_memory(query: str) -> list[dict]:
    facts = _load_facts()
    items = facts.get("memories", [])
    wanted = " ".join(query.casefold().split()).strip(" .!?\"'")
    if wanted in {"everything", "everything you remember", "all", "all memories"} or wanted.startswith("everything you remember ") or wanted.startswith("all memories "):
        facts["memories"] = []
        facts["contacts"] = {}
        _FACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FACTS_FILE.write_text(json.dumps(facts, indent=2, ensure_ascii=False))
        return list(items)
    matches = [
        item for item in items
        if wanted and (
            wanted == " ".join(str(item.get("content", "")).casefold().split())
            or wanted in " ".join(str(item.get("content", "")).casefold().split())
        )
    ]
    contacts = facts.get("contacts", {})
    removed_contacts = [
        (key, str(contact)) for key, contact in contacts.items()
        if wanted and (wanted in key.casefold() or wanted in str(contact).casefold())
    ]
    if matches:
        removed_ids = {item.get("id") for item in matches}
        facts["memories"] = [item for item in items if item.get("id") not in removed_ids]
    for key, _contact in removed_contacts:
        del contacts[key]
    if matches or removed_contacts:
        _FACTS_FILE.write_text(json.dumps(facts, indent=2, ensure_ascii=False))
    return matches or [{"content": f"{key}: {contact}"} for key, contact in removed_contacts]


def _relevant_memory_items(query: str, limit: int = 30) -> list[dict]:
    items = _memory_items()
    query_terms = set(re.findall(r"[a-z0-9]{2,}", query.casefold()))
    if not query_terms:
        return items[-limit:]
    ranked = []
    for index, item in enumerate(items):
        content = str(item.get("content", ""))
        terms = set(re.findall(r"[a-z0-9]{2,}", content.casefold()))
        overlap = len(query_terms & terms)
        if overlap:
            ranked.append((overlap, index, item))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    selected = [row[2] for row in ranked[:limit]]
    selected_ids = {item.get("id") for item in selected}
    for item in reversed(items):
        if len(selected) >= limit:
            break
        if item.get("id") not in selected_ids:
            selected.append(item)
            selected_ids.add(item.get("id"))
    return selected


def _contact_for_alias(alias: str) -> str:
    cleaned = str(alias or "").strip()
    normalized = cleaned.lower()
    contacts = _load_facts().get("contacts", {})
    relation = _normalize_relation(normalized.removeprefix("my "))
    for key, contact in contacts.items():
        if normalized in {key, f"my {key}"} or relation == _normalize_relation(key):
            return str(contact).strip()
    return cleaned


def _contact_from_request(request_context: str) -> str:
    """Resolve an explicitly named contact or saved relationship from the request."""
    text = str(request_context or "")
    contacts = _load_facts().get("contacts", {})
    saved_names = sorted(set(map(str, contacts.values())), key=len, reverse=True)
    for contact in saved_names:
        if re.search(rf"(?<!\w){re.escape(contact)}(?!\w)", text, re.IGNORECASE):
            return contact

    matches = []
    for relation, contact in contacts.items():
        pattern = rf"\b(?:my\s+)?{re.escape(relation)}\b"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            matches.append((match.start(), len(relation), str(contact).strip()))
    if not matches:
        for relation, contact in contacts.items():
            normalized = _normalize_relation(relation)
            if normalized:
                match = re.search(rf"\bmy\s+{re.escape(normalized)}\b", text, re.IGNORECASE)
                if match:
                    matches.append((match.start(), len(normalized), str(contact).strip()))
    return max(matches, key=lambda item: (item[1], -item[0]))[2] if matches else ""


def _generate_external_message(request_context: str, contact: str) -> tuple[str, str]:
    """Turn the user's intent into a short sendable message using Ollama."""
    prompt = (
        "Write the exact message to send to this contact.\n"
        f"Contact: {contact}\n"
        f"User's request and context: {request_context}\n\n"
        "Return only the message text. Do not include quotes, labels, Markdown, "
        "explanations, or placeholders. Preserve the user's requested tone and "
        "purpose (for example reminder, goodbye, apology, congratulations, or "
        "greeting). Keep it natural and under 700 characters."
    )
    text, error = _call_ollama([
        {
            "role": "system",
            "content": (
                "You write concise personal messages for the user. Never invent "
                "specific dates, promises, or facts that are not in the request."
            ),
        },
        {"role": "user", "content": prompt},
    ], timeout=60)
    if error:
        return "", error
    message = re.sub(r"^```(?:text)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    message = re.sub(r"^(?:message|text)\s*:\s*", "", message, flags=re.IGNORECASE)
    message = message.strip().strip("\"'")
    if not message:
        return "", "Ollama returned an empty message."
    return message[:700], ""


def _complete_message_actions(actions: list[dict], request_context: str) -> None:
    """Resolve aliases and generate missing external messages before execution."""
    for action in actions:
        name = str(action.get("action", "")).strip().lower()
        if name not in {"whatsapp_message", "instagram_message"}:
            continue
        requested_contact = _contact_from_request(request_context)
        action["contact"] = requested_contact or _contact_for_alias(action.get("contact", ""))
        if str(action.get("message", "")).strip() or action.get("_message_error"):
            continue
        message, error = _generate_external_message(request_context, str(action["contact"]))
        if error:
            action["_message_error"] = f"Could not write the message with Ollama: {error}"
        else:
            action["message"] = message


def _load_task_state() -> dict:
    try:
        data = json.loads(_TASK_STATE_FILE.read_text()) if _TASK_STATE_FILE.exists() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_task_state(**values) -> dict:
    state = _load_task_state()
    state.update(values)
    state["updated_at"] = time.time()
    _TASK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TASK_STATE_FILE.write_text(json.dumps(state, indent=2))
    return state


def _clear_task_state() -> None:
    _TASK_STATE_FILE.unlink(missing_ok=True)


def _read_local_file(path_value: str) -> dict:
    path = Path(str(path_value or "").strip()).expanduser()
    if not path.is_absolute():
        return {"error": "File paths must be absolute, for example C:\\Users\\user\\Documents\\notes.txt"}
    if not path.exists():
        return {"error": f"File not found: {path}"}
    if not path.is_file():
        return {"error": f"That path is not a file: {path}"}
    try:
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            return {"error": "This appears to be a binary file; text reading was not attempted."}
        text = data.decode("utf-8", errors="replace")
        truncated = len(text) > 50000
        return {
            "ok": True,
            "action": "read_file",
            "path": str(path),
            "content": text[:50000],
            "truncated": truncated,
        }
    except OSError as exc:
        return {"error": f"Could not read {path}: {exc}"}


def _collect_project_context(path_value: str) -> dict:
    root = Path(str(path_value or "").strip()).expanduser()
    if not root.is_absolute():
        return {"error": "Project paths must be absolute, for example C:\\Users\\user\\source"}
    if not root.exists():
        return {"error": f"Project folder not found: {root}"}
    if not root.is_dir():
        return {"error": f"That path is not a project folder: {root}"}

    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".idea", ".next"}
    preferred_names = {
        "readme.md", "howtosetup.md", "pyproject.toml", "requirements.txt",
        "package.json", "tsconfig.json", "dockerfile", ".env.example",
    }
    extensions = {".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".toml", ".yaml", ".yml", ".md", ".txt"}
    files = []
    try:
        for candidate in root.rglob("*"):
            if not candidate.is_file() or any(part in ignored for part in candidate.parts):
                continue
            if candidate.name.lower() in preferred_names or candidate.suffix.lower() in extensions:
                files.append(candidate)
    except OSError as exc:
        return {"error": f"Could not inspect project folder: {exc}"}

    files.sort(key=lambda item: (item.name.lower() not in preferred_names, len(item.parts), str(item).lower()))
    selected = files[:80]
    tree = [str(item.relative_to(root)) for item in selected]
    sections = []
    total_chars = 0
    for item in selected:
        if total_chars >= 120000:
            break
        try:
            raw = item.read_bytes()
            if b"\x00" in raw[:8192]:
                continue
            text = raw.decode("utf-8", errors="replace")
            remaining = 120000 - total_chars
            excerpt = text[:min(12000, remaining)]
            sections.append(f"\n--- {item.relative_to(root)} ---\n{excerpt}")
            total_chars += len(excerpt)
        except OSError:
            continue
    return {
        "ok": True,
        "root": str(root),
        "file_count": len(files),
        "files": tree,
        "content": "".join(sections),
        "truncated": len(files) > len(selected) or total_chars >= 120000,
    }


def _mark_verified() -> None:
    now = time.time()
    _session_state["verified_at"] = now
    _session_state["last_activity"] = now
    # Face verified → grant executor the same 30-min window. So when
    # you unlock by walking in front of the camera, unrestricted mode
    # comes on automatically. No need to say a magic phrase.
    executor.unlock_session(SESSION_TIMEOUT_SEC)


def _touch_activity() -> None:
    _session_state["last_activity"] = time.time()
    # Each chat message also slides the executor window forward. If
    # you're actively chatting, you're definitely at the computer.
    if executor.is_unlocked():
        executor.unlock_session(SESSION_TIMEOUT_SEC)


def _is_verified() -> bool:
    if _session_state["verified_at"] == 0.0:
        return False
    return (time.time() - _session_state["last_activity"]) < SESSION_TIMEOUT_SEC


def _face_lock_active() -> bool:
    return FACE_LOCK_ENABLED and face_auth.is_trained()


def _is_local_computer_task(message: str) -> bool:
    """Avoid live search for requests whose purpose is local UI control."""
    return bool(re.search(
        r"\b(?:open|close|launch|focus|click|type|write|send|message|search|"
        r"select|press|enter|whatsapp|instagram|facebook|youtube|browser|"
        r"screen|desktop|app|application|file|folder|read|resume|continue|"
        r"project|codebase|code|implement|improve|fix|modify|test|audit)\b",
        message or "",
        re.IGNORECASE,
    ))


def _is_project_implementation_task(message: str, task_state: dict | None = None) -> bool:
    state = task_state or {}
    return bool(
        state.get("project_root")
        and (
            state.get("status") == "implementation_requested"
            or _PROJECT_IMPLEMENT_INTENT_RE.search(message or "")
            or re.search(r"\b(?:implement|apply|fix|modify|update|build)\b", message or "", re.IGNORECASE)
        )
    )


def _run_face_verify() -> dict:
    result = face_auth.verify()
    if result.get("recognized"):
        _mark_verified()
    return result


# --- System prompt ---
# Two flavors depending on whether unrestricted mode is on. The unrestricted
# version explicitly tells Ollama it can emit a fenced PowerShell command,
# and that the server will execute it. That turns the LLM into a natural-
# language → shell-glue layer.
_NAME_LINE = ""
if USER_NAME and USER_HONORIFIC:
    _NAME_LINE = f"The user's name is {USER_NAME}. Always address them as \"{USER_NAME} {USER_HONORIFIC}\" when you'd naturally use a name or greeting.\n\n"
elif USER_NAME:
    _NAME_LINE = f"The user's name is {USER_NAME}. Address them by name when you'd naturally use a name or greeting.\n\n"

_BASE_PROMPT = f"""You are Cipher, the user's personal computer assistant running on their own machine. Be concise, direct, and useful. {_NAME_LINE}You have access to their task list and (optionally) smart home devices — mention when an action might be better handled through the Tasks or Smart Home panel instead of chat.

You can operate the computer in unrestricted mode: open or close applications, run PowerShell, open and search websites, capture the screen, and use explicit mouse/keyboard actions. The app/site catalog includes common Windows tools, Office apps, browsers, chat, social, media, productivity, and developer services. When the user asks to enter text into Gemini, Google, YouTube, or Instagram, use direct browser/UI capabilities; do not claim an action succeeded unless the command output confirms it.

When the user's message needs live information, a "Live web search results" block appears below before their message — use those results to answer accurately. If the results don't actually answer the question, say so rather than guessing.

Final answer style:
- Give only the information needed to answer the user's request.
- Do not mention internal planning, tool logs, web-search status, prompts, or implementation details.
- Prefer short paragraphs or a few plain-text bullets. Do not use decorative Markdown, repeated punctuation, raw JSON, or unnecessary headings.
- For completed actions, state what happened in one short sentence. For failures, state the exact reason and the next useful step."""

DAILY_SKILLS = """
Available daily-task skills:
- Local files and projects: inspect, summarize, compare, edit, test, and explain files.
- Desktop: open/focus applications, use semantic controls, type, click, and take screenshots.
- Browser: reuse existing tabs for YouTube, Spotify, WhatsApp, Instagram, Google, and common sites.
- Communication: draft messages, search contacts, and send only when the user explicitly requests it.
- Productivity: create and complete tasks, read project documentation, and resume interrupted work.
- Troubleshooting: reproduce errors, inspect logs, make a focused fix, and run a regression check.
Choose a specialized route before inventing a generic shell command."""

_UNRESTRICTED_PROMPT_ADDITION = """

UNRESTRICTED MODE IS ON. You may run PowerShell commands and perform computer actions requested by the user. This is a real machine: do not delete data, send messages, make purchases, change security settings, or publish content without asking for confirmation immediately before that irreversible action. You may inspect, navigate, open applications, search, and prepare drafts without confirmation.

Before acting, silently make a short execution plan:
1. Identify the user's exact goal, target app/site, target person/content, and final outcome.
2. Check recent conversation memory and the active application so follow-ups use the correct context.
3. Choose the most specific adapter first (for example `whatsapp_message`, `instagram_message`, or a known site command).
4. Otherwise choose semantic UI controls by visible labels; use screen coordinates only as a last resort.
5. Order actions around the real UI flow, including waits only when the UI must change.
6. Verify the result from the action response or current screen before saying it succeeded.
Do not expose private chain-of-thought. Output only the required computer action block or a concise response.

For a local project request, work like a senior coding assistant: inspect the relevant
files first, make the requested changes instead of only describing them, run focused
checks or tests, and report exactly what changed and what could not be completed.
If the user has already said "proceed", "yes", or explicitly asked to implement,
do not ask for permission again. Continue in bounded steps until the requested work
is complete or a concrete blocker is reached. Never claim a change was made unless
the command result confirms it.

For desktop tasks, prefer a structured action plan in exactly one fenced block:
```computer
{"actions":[{"action":"focus_window","title":"WhatsApp"},{"action":"click_text","target":"Search","window":"WhatsApp"},{"action":"type_into","target":"Search","text":"Azka CA Lhr","window":"WhatsApp"},{"action":"hotkey","keys":["ENTER"]}]}
```
For `open_app`, use `name`, `app`, or `value` for the application name. Supported actions are open_url, open_app, focus_window, click_text, type_into, click, type, hotkey, wait, screenshot, whatsapp_message, and instagram_message. Prefer semantic actions (`focus_window`, `click_text`, `type_into`) with visible labels over coordinates. For a browser tab, `focus_window` may use a short tab title such as `Instagram Messages`; the server activates the matching tab inside Chrome. Use at most 8 actions. The server observes the screen and can execute up to four short action rounds. Use a PowerShell block only when a computer action cannot accomplish the task.
When a matching browser tab is already open, focus and use it; do not open a duplicate tab. When an app-specific action exists, do not recreate it with generic clicks.
For WhatsApp and Instagram messages, generate the final, polished message directly in the specialized action's `message` field from the user's full context. Do not omit the message or use a generic placeholder; the server-side writer is a recovery path only when the action field is missing.
To read a file the user explicitly points to, use `read_file` with its absolute path. It returns bounded text and clearly reports binary or inaccessible files. To resume an interrupted task, use the saved task state and continue from the first incomplete step; do not repeat completed steps.

PowerShell execution rules:
- Emit one executable command per block, without explanatory comments.
- Use robust paths and quoting; prefer `-LiteralPath` for Windows files.
- For source edits, prefer a short Python script using pathlib and an exact
  old-text/new-text replacement over PowerShell `-replace` regular expressions.
  Read the file first, require the old text to occur exactly once, write it,
  then inspect the result.
- After an edit, inspect the changed file and run the smallest relevant test.
- If PowerShell reports a parser, regex, quoting, or path error, return a
  corrected command rather than repeating it.
- For Python tests, run from the project root, use the project's virtual
  environment when available, and set `PYTHONPATH` to the project root so
  package imports resolve consistently.

CRITICAL: To run a command, your reply MUST contain a fenced PowerShell block using triple backticks and the word `powershell`:

```powershell
Get-Process | Select-Object -First 10
```

DO NOT:
- Inline the command in single backticks (e.g. `Get-Process` alone) — that will be shown to the user as raw text and NOT executed.
- Describe running a command without including the fenced block.
- Include any commentary inside the fenced block (it gets executed as-is).
- Include more than one fenced PowerShell block per turn (only the first will run).

After the fenced block, you may add a short natural-language summary of what you did. The server will execute the block, give you the output, and you'll be asked to summarize the result for the user.

If the user's request is purely conversational (a question, a chat, a discussion), reply normally WITHOUT a fenced block. Only emit the fenced block when an action is actually needed."""


def _build_system_prompt() -> str:
    base = _BASE_PROMPT + DAILY_SKILLS
    if executor.is_unlocked():
        base += _UNRESTRICTED_PROMPT_ADDITION
    return base


# --- Whitelisted command routing ---
# Runs BEFORE the LLM. Clear matches only; otherwise falls through.
_VOLUME_TO_RE = re.compile(r"(?:set )?volume to (\d{1,3})\b", re.IGNORECASE)
_OPEN_RE = re.compile(r"^(?:open|launch|start)\s+(.+)$", re.IGNORECASE)
_CLOSE_RE = re.compile(r"^(?:close|quit|exit|kill|stop)\s+(.+)$", re.IGNORECASE)
_OPEN_FOLDER_RE = re.compile(r"^(?:open)\s+(?:the\s+)?(.+?)\s+folder$", re.IGNORECASE)
_WEATHER_RE = re.compile(r"weather\s*(?:like)?\s*(?:in|for|at|of)\s+(.+)$", re.IGNORECASE)
_WEATHER_HERE_RE = re.compile(r"\bweather\b", re.IGNORECASE)
_YOUTUBE_SEARCH_RE = re.compile(
    r"(?:open\s+youtube\s+and\s+)?(?:search|look)\s+(?:youtube\s+)?(?:for\s+)?(.+?)(?:\s+and\s+open\s+(?:the\s+)?(?:first|1st|second|2nd)\s+(?:video|result))?$",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(r"[\"']?(?P<path>[A-Za-z]:\\[^\"'\r\n]+?)[\"']?(?=\s|$)", re.IGNORECASE)
_PROJECT_REVIEW_INTENT_RE = re.compile(
    r"\b(?:check|review|analy[sz]e|inspect|audit|improve)\b.*?"
    r"\b(?:project|codebase|code|folder|directory)\b",
    re.IGNORECASE,
)
_PROJECT_IMPLEMENT_INTENT_RE = re.compile(
    r"\b(?:implement|apply|make|do|fix|change|modify|update|build)\b",
    re.IGNORECASE,
)
_YOUTUBE_RESULT_RE = re.compile(
    r"(?:open\s+)?youtube(?:\s+and)?\s+(?:search\s+)?(?:for\s+)?(?P<query>.+?)"
    r"\s+and\s+open\s+(?:the\s+)?(?P<ordinal>first|1st|second|2nd)\s+(?:video|result)$",
    re.IGNORECASE,
)
_YOUTUBE_SECOND_RE = re.compile(
    r"(?:open|play)\s+(?:the\s+)?(?:second|2nd)\s+(?:youtube\s+)?(?:video|result)\s+(?:for|about)\s+(.+)$",
    re.IGNORECASE,
)
_SPOTIFY_SEARCH_RE = re.compile(
    r"(?:open\s+)?spotify\s*[,;]?\s*(?:and\s+)?(?:search\s+)?(?:for\s+)?"
    r"(?P<query>.+?)\s*(?:,\s*|\s+and\s+open\s+(?:the\s+)?)"
    r"(?P<ordinal>first|1st|second|2nd)\s+(?:result|song|track)\.?\s*$",
    re.IGNORECASE,
)
_TYPE_IN_SITE_RE = re.compile(
    r"(?:open\s+)?(?P<site>gemini|google|youtube)\s+and\s+(?:type|enter|write)\s+['\"]?(?P<text>.+?)['\"]?$",
    re.IGNORECASE,
)
_FORECAST_RE = re.compile(
    r"forecast|will it rain|going to rain|gonna rain|rain (?:today|tomorrow|this week|later)",
    re.IGNORECASE,
)
_PLACE_TAIL_RE = re.compile(r"\b(?:and|tell|when|please|,|\?)\b.*$", re.IGNORECASE)
_LOCATION_RE = re.compile(
    r"where am i\b"
    r"|my (?:\w+\s+){0,3}location"
    r"|current location"
    r"|exact location",
    re.IGNORECASE,
)
_FILE_SUMMARY_RE = re.compile(
    r"\b(?:summari[sz]e|summarise|read|explain|analy[sz]e|review)\b"
    r".*?\bfile\b(?:\s+to\s+me|\s+for\s+me)?\s*"
    r"(?P<path>[\"']?[A-Za-z]:\\[^\"'\r\n]+?[\"']?)\s*$",
    re.IGNORECASE,
)
_TOWN_RE = re.compile(
    r"which (?:town|city|area|neighbou?rhood) (?:am i|do i live)"
    r"|what (?:town|city|area|neighbou?rhood) am i"
    r"|my (?:town|neighbou?rhood|area)\b",
    re.IGNORECASE,
)


def _match_known(name: str, keys) -> str | None:
    name = name.strip().lower()
    if name in keys:
        return name
    matches = [k for k in keys if re.search(rf"\b{re.escape(k)}\b", name)]
    if matches:
        return max(matches, key=len)
    return None


def try_command(
    text: str,
    location: str | None = None,
    area: str | None = None,
    street: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> str | None:
    global _active_app
    t = text.strip()
    lower = t.lower()

    if lower in ("lock screen", "lock the screen", "lock my screen"):
        result = pc_control.dispatch("lock_screen")
        return _format_result(result, "Locking the screen.")
    if lower in ("mute", "mute volume", "mute the volume"):
        return _format_result(pc_control.dispatch("mute"), "Muted.")
    if lower in ("unmute", "unmute volume", "unmute the volume"):
        return _format_result(pc_control.dispatch("unmute"), "Unmuted.")
    if lower in ("volume up", "turn up the volume", "turn the volume up", "increase volume"):
        return _format_result(pc_control.dispatch("volume_up"), "Turning the volume up.")
    if lower in ("volume down", "turn down the volume", "turn the volume down", "decrease volume"):
        return _format_result(pc_control.dispatch("volume_down"), "Turning the volume down.")

    if lower in ("what's on my screen", "what is on my screen", "show me my screen", "take a screenshot"):
        result = screen_control.capture()
        if result.get("error"):
            return _format_result(result, "Taking a screenshot.")
        return f"Screenshot captured. Open {result['url']} to view it."

    file_summary = _FILE_SUMMARY_RE.search(t)
    if file_summary:
        if not executor.is_unlocked():
            return "Unlock unrestricted mode first so I can read local files."
        path = file_summary.group("path").strip().strip("\"'")
        result = _read_local_file(path)
        if result.get("error"):
            return f"Couldn't read that file: {result['error']}"
        return _summarize_local_file(result["path"], result["content"])

    project_review = _PROJECT_REVIEW_INTENT_RE.search(t)
    path_match = _WINDOWS_PATH_RE.search(t)
    if project_review and path_match:
        if not executor.is_unlocked():
            return "Unlock unrestricted mode first so I can inspect a local project."
        path = path_match.group("path").strip().strip("\"'")
        context = _collect_project_context(path)
        if context.get("error"):
            return f"Couldn't inspect that project: {context['error']}"
        _save_task_state(
            goal=t,
            project_root=context["root"],
            status="implementation_requested" if _PROJECT_IMPLEMENT_INTENT_RE.search(t) else "reviewed",
            discovered_files=context["file_count"],
            completed_actions=[],
        )
        if _PROJECT_IMPLEMENT_INTENT_RE.search(t):
            return None
        prompt = (
            "Review this local software project and give the user a concrete, prioritized "
            "improvement report. Use only the supplied files; do not claim tests or files "
            "were changed. Include: (1) what the project does, (2) important architecture "
            "and workflow observations, (3) specific problems or risks with file paths, "
            "(4) prioritized improvements with rationale, and (5) a short recommended "
            "implementation order. If evidence is insufficient, say what should be inspected "
            "next. Keep it concise but substantive.\n\n"
            f"Project root: {context['root']}\n"
            f"Discovered text/source files: {context['file_count']}\n"
            f"Selected files:\n{json.dumps(context['files'], indent=2)}\n"
            f"Project contents:\n{context['content']}"
        )
        review = _ask_model(prompt, [], None, None, None, None)
        if review.startswith("["):
            return f"Project files were collected, but the review failed: {review}"
        return review

    type_in_site = _TYPE_IN_SITE_RE.match(t)
    if type_in_site:
        if not executor.is_unlocked():
            return "Unlock unrestricted mode first so I can control the active browser window."
        site = type_in_site.group("site")
        text = type_in_site.group("text").strip().strip("'\"")
        result = browser.open_and_type(site, text)
        if result.get("error"):
            return f"Couldn't do that: {result['error']}"
        return f"Opened {site} and typed your query into the active window."

    youtube_second = _YOUTUBE_SECOND_RE.search(t)
    if youtube_second:
        result = browser.youtube_search_and_open(youtube_second.group(1), result_index=2)
        return _format_browser_result(result, "Opening the second YouTube result.")

    spotify_search = _SPOTIFY_SEARCH_RE.search(t)
    if spotify_search and "spotify" in lower:
        ordinal = (spotify_search.group("ordinal") or "").lower()
        result_index = 1 if ordinal in {"first", "1st"} else 2 if ordinal in {"second", "2nd"} else None
        result = browser.spotify_search_and_open(
            spotify_search.group("query").strip().strip("\"',."),
            result_index=result_index,
        )
        return _format_browser_result(
            result,
            "Opening the first Spotify result." if result_index == 1 else "Opening Spotify search.",
        )

    youtube_result = _YOUTUBE_RESULT_RE.search(t)
    if youtube_result:
        ordinal = youtube_result.group("ordinal").lower()
        result_index = 1 if ordinal in {"first", "1st"} else 2
        result = browser.youtube_search_and_open(
            youtube_result.group("query").strip().strip("\"'"),
            result_index=result_index,
        )
        return _format_browser_result(
            result,
            f"Opening the {ordinal} YouTube result.",
        )

    youtube_search = _YOUTUBE_SEARCH_RE.search(t)
    if youtube_search and ("youtube" in lower or "video" in lower):
        query = youtube_search.group(1).strip()
        wants_second = bool(re.search(r"\b(?:second|2nd)\b", lower))
        wants_first = bool(re.search(r"\b(?:first|1st)\b", lower))
        result_index = 2 if wants_second else 1 if wants_first else None
        result = browser.youtube_search_and_open(query, result_index=result_index)
        return _format_browser_result(result, "Opening YouTube.")

    m = _VOLUME_TO_RE.search(lower)
    if m:
        level = m.group(1)
        return _format_result(pc_control.dispatch("set_volume", level), f"Setting volume to {level}.")

    m = _OPEN_FOLDER_RE.match(lower)
    if m:
        name = m.group(1).strip()
        folder_key = _match_known(name, pc_control.FOLDERS)
        if folder_key:
            return _format_result(pc_control.dispatch("open_folder", folder_key), f"Opening the {folder_key} folder.")

    m = _OPEN_RE.match(t)
    if m and not re.search(r"\b(?:write|type|message|send|click|search|then|and)\b", lower):
        name = m.group(1).strip().lower()
        site_key = _match_known(name, browser.SITES)
        if site_key:
            _active_app = site_key
            return _format_result(browser.open_target(site_key), f"Opening {site_key} in your browser.")
        app_key = _match_known(name, pc_control.APPS)
        if app_key:
            _active_app = app_key
            return _format_result(pc_control.dispatch("open_app", app_key), f"Opening {app_key}.")
        folder_key = _match_known(name, pc_control.FOLDERS)
        if folder_key:
            return _format_result(pc_control.dispatch("open_folder", folder_key), f"Opening the {folder_key} folder.")

    m = _CLOSE_RE.match(t)
    if m:
        name = m.group(1).strip().lower()
        app_key = _match_known(name, pc_control.APPS)
        if app_key:
            return _format_result(pc_control.dispatch("close_app", app_key), f"Closing {app_key}.")

    if _TOWN_RE.search(lower):
        if area:
            return f"You're in {area}, within {location or 'your current city'}."
        if location:
            return (f"The most specific I've got is {location} — your browser/reverse-geocoding "
                     "service isn't returning anything more granular than city-level for this spot.")
        return ("I don't have your location — make sure you allowed the location "
                "permission prompt in the browser tab, then reload the page.")

    if _LOCATION_RE.search(lower):
        if street or area or location:
            bits = ', '.join(p for p in (street, area, location) if p)
            return f"Right now you're at {bits}."
        return ("I don't have your location — make sure you allowed the location "
                "permission prompt in the browser tab, then reload the page.")

    nearby_query = places.extract_nearby_query(t)
    if nearby_query:
        if lat is None or lon is None:
            return ("I don't have your precise location yet — grant location access in the browser, then reload.")
        places_result = places.find_nearby(lat, lon, nearby_query)
        if places_result.get("error"):
            pass  # fall through to LLM
        else:
            return _format_places(places_result)

    m = _WEATHER_RE.search(lower)
    wants_forecast = bool(_FORECAST_RE.search(lower))
    if m:
        place = _PLACE_TAIL_RE.sub("", m.group(1).strip()).strip()
        weather_result = weather.get_weather(place)
        forecast_result = weather.get_forecast(place) if wants_forecast else None
        return _format_weather_short(weather_result, forecast_result)
    if _WEATHER_HERE_RE.search(lower):
        weather_result = weather.get_weather(location, lat=lat, lon=lon)
        forecast_result = weather.get_forecast(location, lat=lat, lon=lon) if wants_forecast else None
        return _format_weather_short(weather_result, forecast_result)

    return None


def _format_result(result: dict, success_message: str) -> str:
    if result.get("error"):
        return f"Couldn't do that: {result['error']}"
    return success_message


def _summarize_local_file(path: str, content: str) -> str:
    """Summarize already-read local text without invoking web search."""
    prompt = (
        f"Summarize the following local file for the user. Mention the file name, "
        f"its purpose, and the main sections or takeaways. Do not invent details.\n"
        f"File: {path}\n\n"
        f"FILE CONTENT:\n{content}"
    )
    reply = _ask_model(prompt, [], None, None, None, None)
    if reply.startswith("[Ollama ") or reply.startswith("[Can't reach Ollama") or reply.startswith("[Ollama connection"):
        return f"File read successfully, but summarization failed: {reply}"
    return reply


def _format_browser_result(result: dict, success_message: str) -> str:
    if result.get("error"):
        return f"Couldn't do that: {result['error']}"
    return f"{success_message} {result.get('title', result.get('opened', ''))}".strip()


def _format_weather_short(weather_result: dict, forecast_result: dict | None = None) -> str:
    if weather_result.get("error"):
        return f"Couldn't do that: {weather_result['error']}"

    sentence = f"The weather of {weather_result['location']} is {weather_result['temperature_c']}°C"

    if forecast_result is None:
        return sentence + f", {weather_result['condition']}."

    if forecast_result.get("error"):
        return sentence + f". Couldn't get the forecast: {forecast_result['error']}"

    _DAY_LABELS = ["today", "tomorrow", "the day after tomorrow"]
    _RAIN_NOTABLE_PCT = 30
    notable = []
    for i, d in enumerate(forecast_result.get("days", [])[:3]):
        pct = d.get("rain_chance_pct")
        if pct is not None and pct >= _RAIN_NOTABLE_PCT:
            label = _DAY_LABELS[i] if i < len(_DAY_LABELS) else f"in {i} days"
            notable.append(f"{label} with {pct}% chance")

    if not notable:
        return sentence + " and no significant rain is expected over the next few days."
    return sentence + " and is expected to rain " + " or ".join(notable) + "."


def _format_places(result: dict) -> str:
    if result.get("error"):
        return f"Couldn't do that: {result['error']}"
    lines = []
    for p in result["places"]:
        extra = f" — {p['cuisine']}" if p.get("cuisine") else ""
        addr = f" — {p['address']}" if p.get("address") else ""
        lines.append(f"- {p['name']} ({p['distance_m']}m away){extra}{addr}")
    return "Here's what's nearby:\n" + "\n".join(lines)


# --- Block-level messaging for the frontend ---
# The frontend reads "tool_log" from the response and shows each entry as
# a dim "› ... " line in the chat. Lets the user see what Cipher did
# behind the scenes (web search, command runs, etc) without it cluttering
# the main reply.
class ChatResponse:
    def __init__(self, reply: str = "", tool_log: list[str] | None = None,
                 locked: bool = False, mode: str | None = None):
        self.reply = reply
        self.tool_log = tool_log or []
        self.locked = locked
        self.mode = mode

    def to_dict(self) -> dict:
        out = {"reply": self.reply, "tool_log": self.tool_log, "locked": self.locked}
        if self.mode is not None:
            out["mode"] = self.mode
        return out


# --- LLM layer ---
# Extracts a single ```powershell ... ``` block from an LLM response.
# We deliberately only allow ONE block per turn — limits blast radius.
_PWSH_BLOCK_RE = re.compile(r"```(?:powershell|pwsh|ps1)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)
_COMPUTER_BLOCK_RE = re.compile(r"```(?:computer|json)\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
ALLOW_AUTOMATED_SEND = os.getenv("CIPHER_ALLOW_AUTOMATED_SEND", "true").strip().lower() in {"1", "true", "yes", "on"}
# Inline backticks containing a command (e.g. "`Get-Process | ...`").
# The model often writes commands this way in chat replies.
_INLINE_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
# Phrases the model uses to introduce a command ("you should run:",
# "let me run:", "I'll run:", "running:").
_RUN_INTRO_RE = re.compile(
    r"(?:you\s+should\s+run|let\s+me\s+run|i'?ll\s+run|running|run\s+this|try\s+running|use\s+this\s+command|execute)\s*[:\-]?\s*[`\"]?",
    re.IGNORECASE,
)
# A PowerShell command almost always starts with a verb-noun cmdlet
# (Get-Process, Get-Service, etc) or a built-in (Get-ChildItem, dir,
# ls, etc). Used to decide if a line *looks* like a command.
_CMDLET_PREFIXES = (
    "get-", "set-", "new-", "remove-", "start-", "stop-", "select-",
    "where-", "foreach-", "if-", "show-", "find-", "test-", "invoke-",
    "measure-", "sort-", "group-", "format-", "out-", "write-",
    "read-", "copy-", "move-", "rename-", "clear-", "add-", "update-",
    "disable-", "enable-", "restart-", "resolve-", "register-",
    "unregister-", "import-", "export-", "convert-", "compare-",
    "restart-service", "stop-service", "start-service",
    "ls", "dir", "pwd", "whoami", "ipconfig", "systeminfo",
    "tasklist", "taskkill", "netstat", "ping", "tracert", "nslookup",
    "gpupdate", "gpresult", "sc", "net", "wevtutil", "wmic", "chkdsk",
)
# Don't treat these as commands even though they look like PowerShell.
_NOT_COMMANDS = {
    "true", "false", "null", "$true", "$false", "$null",
}


def _extract_computer_actions(text: str) -> list[dict] | None:
    """Extract a bounded structured desktop-action plan from the model."""
    match = _COMPUTER_BLOCK_RE.search(text or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None
    actions = payload.get("actions") if isinstance(payload, dict) else payload
    if not isinstance(actions, list) or not actions or len(actions) > 8:
        return None
    return [action for action in actions if isinstance(action, dict)]


def _run_computer_actions(actions: list[dict]) -> list[dict]:
    global _active_app
    results = []
    for action_index, action in enumerate(actions):
        name = str(action.get("action", "")).strip().lower()
        if name in {"whatsapp_message", "instagram_message"}:
            action["contact"] = _contact_for_alias(action.get("contact", ""))
        if name == "send":
            results.append({
                "action": name,
                "result": {
                    "confirmation_required": True,
                    "error": "Sending or submitting requires explicit confirmation.",
                },
            })
            break
        try:
            if name == "open_url":
                result = browser.open_target(str(action.get("url", "")))
            elif name == "open_app":
                app_name = action.get("name") or action.get("app") or action.get("value")
                _active_app = str(app_name or "").strip().lower() or _active_app
                result = pc_control.dispatch("open_app", str(app_name or ""))
            elif name == "click":
                result = screen_control.click(action.get("x"), action.get("y"))
            elif name == "type":
                result = screen_control.type_text(action.get("text", ""))
            elif name == "focus_window":
                result = ui_automation.focus_window(str(action.get("title", "")))
            elif name == "click_text":
                result = ui_automation.click_text(
                    str(action.get("target", "")),
                    str(action.get("window", "")) or None,
                    str(action.get("control_type", "")) or None,
                )
            elif name == "type_into":
                result = ui_automation.type_into(
                    str(action.get("target", "")),
                    str(action.get("text", "")),
                    str(action.get("window", "")) or None,
                )
            elif name == "read_file":
                result = _read_local_file(str(action.get("path", "")))
            elif name == "whatsapp_message":
                if action.get("_message_error"):
                    result = {"error": action["_message_error"]}
                elif not action.get("contact"):
                    result = {"error": "No WhatsApp contact was provided and no matching memory alias was found."}
                elif not str(action.get("message", "")).strip():
                    result = {"error": "No message was generated for the WhatsApp action; nothing was sent."}
                else:
                    result = screen_control.whatsapp_message(
                        str(action.get("contact", "")),
                        str(action.get("message", "")),
                        send=ALLOW_AUTOMATED_SEND,
                    )
            elif name == "instagram_message":
                if action.get("_message_error"):
                    result = {"error": action["_message_error"]}
                elif not action.get("contact"):
                    result = {"error": "No Instagram contact was provided and no matching memory alias was found."}
                elif not str(action.get("message", "")).strip():
                    result = {"error": "No message was generated for the Instagram action; nothing was sent."}
                else:
                    result = screen_control.instagram_message(
                        str(action.get("contact", "")),
                        str(action.get("message", "")),
                        send=ALLOW_AUTOMATED_SEND,
                    )
            elif name == "hotkey":
                is_enter = any(str(key).upper() in {"ENTER", "RETURN"} for key in action.get("keys", []))
                has_follow_up_typing = any(
                    str(next_action.get("action", "")).lower() == "type"
                    for next_action in actions[action_index + 1:]
                )
                if is_enter and not has_follow_up_typing and not ALLOW_AUTOMATED_SEND:
                    results.append({
                        "action": name,
                        "result": {
                            "confirmation_required": True,
                            "error": "Pressing Enter to send or submit requires explicit confirmation.",
                        },
                    })
                    break
                result = screen_control.hotkey(action.get("keys", []))
            elif name == "wait":
                delay = max(0.1, min(float(action.get("seconds", 1)), 10))
                time.sleep(delay)
                result = {"ok": True, "action": "wait", "seconds": delay}
            elif name == "screenshot":
                result = screen_control.capture()
            else:
                result = {"error": f"Unsupported computer action '{name}'."}
        except (TypeError, ValueError, RuntimeError) as exc:
            result = {"error": str(exc)}
        results.append({"action": name, "result": result})
        if result.get("error"):
            break
        if result.get("ok") and name not in {"wait", "screenshot"}:
            _save_task_state(
                goal=_load_task_state().get("goal", ""),
                current_app=_active_app,
                completed_actions=results,
                last_action=name,
            )
    return results


def _looks_like_command(text: str) -> bool:
    """Heuristic: does this look like a single-line PowerShell command?"""
    t = text.strip().strip("`'\"\t ")
    if not t or t.lower() in _NOT_COMMANDS:
        return False
    if len(t) > 1000:  # sanity cap, no real command is this long inline
        return False
    first = t.split(maxsplit=1)[0].lower() if t else ""
    return any(first == p or first.startswith(p) for p in _CMDLET_PREFIXES)


def _extract_powershell(text: str) -> str | None:
    """
    Returns a single PowerShell command if the model clearly intended to
    run one, otherwise None. Tries, in order:
      1. Triple-backtick fence (```powershell ... ```)
      2. Inline backticks containing a command (`Get-Process ...`)
      3. Lines introduced by "run:" / "I'll run:" / etc, where the
         trailing content looks like a command
      4. A line that simply looks like a command (last resort)
    """
    if not text:
        return None

    # 1. Triple-backtick PowerShell fence. Never treat a computer/json
    # action plan as shell, even if the model omitted the language label.
    powershell_text = _COMPUTER_BLOCK_RE.sub("", text)
    matches = _PWSH_BLOCK_RE.findall(powershell_text)
    if matches:
        cmd = matches[0].strip()
        if cmd:
            return _normalize_powershell_command(cmd)

    # 2. Inline backticks
    for m in _INLINE_BACKTICK_RE.finditer(text):
        candidate = m.group(1).strip()
        if _looks_like_command(candidate):
            return _normalize_powershell_command(candidate)

    # 3. "run this:" / "I'll run" intro
    for line in text.splitlines():
        if _RUN_INTRO_RE.search(line):
            # Strip the intro phrase and any backticks/quotes
            tail = _RUN_INTRO_RE.split(line, maxsplit=1)[-1]
            tail = tail.strip().strip("`'\"\t ")
            # If the tail is multi-line, take only the first line
            tail = tail.splitlines()[0].strip() if tail else ""
            if _looks_like_command(tail):
                return _normalize_powershell_command(tail)

    # 4. Last resort: a single-line reply that is itself a command
    stripped = text.strip().strip("`'\"\t ")
    if "\n" not in stripped and _looks_like_command(stripped):
        return _normalize_powershell_command(stripped)

    return None


def _normalize_powershell_command(command: str) -> str:
    """Remove presentation-only lines before sending model output to PowerShell."""
    lines = []
    for line in (command or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _needs_command_repair(command: str) -> bool:
    """Detect model commands likely to fail before touching the project."""
    lowered = (command or "").lower()
    return (
        "-replace" in lowered
        and ("get-content" in lowered or "set-content" in lowered)
    )


def _is_recoverable_command_error(result: dict) -> bool:
    text = f"{result.get('stderr', '')} {result.get('error', '')}".lower()
    return any(marker in text for marker in (
        "parsererror",
        "invalidregularexpression",
        "regular expression pattern",
        "unexpected token",
        "missing argument",
        "cannot bind argument",
        "quoted string",
        "pathnotfound",
        "cannot find path",
        "modulenotfounderror",
        "no module named",
    ))


def _repair_python_test_command(command: str, project_root: str | None) -> str | None:
    """Run Python tests from the project root with its import path available."""
    if not project_root or not re.search(r"\b(?:python|py)\b.*\b(?:pytest|unittest)\b", command, re.IGNORECASE):
        return None
    match = re.search(r"\b(?:python(?:\.exe)?|py)\b\s+(?P<args>.+)$", command, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    root = str(project_root).replace("'", "''")
    args = match.group("args").strip()
    return (
        f"Set-Location -LiteralPath '{root}'; "
        "$env:PYTHONPATH=(Get-Location).Path; "
        "$python = Join-Path (Get-Location) 'venv\\Scripts\\python.exe'; "
        f"if (Test-Path -LiteralPath $python) {{ & $python {args} }} "
        f"else {{ python {args} }}"
    )


def _call_ollama(messages: list[dict], timeout: int = 120) -> tuple[str, str]:
    """
    Calls Ollama. Returns (reply_text, error). On error, reply_text is "".
    """
    if OLLAMA_MODEL.startswith("gemma4") and not OLLAMA_API_KEY and "ollama.com" not in OLLAMA_URL:
        return "", (
            "gemma4:31b is an Ollama Cloud model, but OLLAMA_API_KEY is not configured. "
            "Set OLLAMA_API_KEY and OLLAMA_URL=https://ollama.com/api/chat in backend/.env, then restart Cipher."
        )
    try:
        headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}
        resp = httpx.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            headers=headers,
            timeout=timeout,
        )
    except httpx.ConnectError:
        return "", "Can't reach Ollama. Is it running? (`ollama serve`)"
    except Exception as e:
        return "", f"Ollama connection failed: {e}"
    if resp.status_code != 200:
        return "", f"Ollama error {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    return data.get("message", {}).get("content", ""), ""


def _ask_model(user_message: str, history: list[dict], search_block: str | None,
               location: str | None, area: str | None, street: str | None,
               exec_result: dict | None = None) -> str:
    """
    Build the chat payload, call Ollama, return the model's text reply.
    If `exec_result` is provided we append a follow-up message to the
    conversation so the model can see the command output and summarize.
    """
    messages = [{"role": "system", "content": _build_system_prompt()}]

    if location:
        loc_bits = ", ".join(p for p in (street, area, location) if p)
        messages.append({"role": "system", "content": f"The user's current location is: {loc_bits}."})

    if search_block:
        messages.append({"role": "system", "content": search_block})

    remembered = [
        {"role": item.get("role", "user"), "content": item.get("content", "")}
        for item in _load_memory()
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    if remembered:
        messages.append({
            "role": "system",
            "content": "Recent local conversation memory (use it to resolve follow-up references):\n"
            + json.dumps(remembered[-20:]),
        })
    facts = _load_facts()
    if facts:
        memories = _relevant_memory_items(user_message)
        messages.append({
            "role": "system",
            "content": (
                "Long-term user facts and contact aliases. Use these only when relevant; "
                "do not invent or assume details. Resolve contact aliases from contacts.\n"
                + json.dumps({"contacts": facts.get("contacts", {}), "memories": memories}, ensure_ascii=False)
            ),
        })
    if _active_app:
        messages.append({
            "role": "system",
            "content": f"The last application opened by the user was {_active_app}. "
            "Treat follow-ups like 'search for...', 'find...', or 'write this' as referring to it unless the user names another app.",
        })
    saved_task = _load_task_state()
    if saved_task:
        messages.append({
            "role": "system",
            "content": (
                "Persistent task state (continue from incomplete work and do not "
                f"repeat completed actions): {json.dumps(saved_task)}"
            ),
        })
        if saved_task.get("project_root") and re.search(
            r"\b(?:implement|change|fix|improve|modify|test|continue)\b",
            user_message,
            re.IGNORECASE,
        ):
            project_context = _collect_project_context(saved_task["project_root"])
            if project_context.get("ok"):
                messages.append({
                    "role": "system",
                    "content": (
                        "Current project context for the approved follow-up:\n"
                        f"Root: {project_context['root']}\n"
                        f"Files: {json.dumps(project_context['files'])}\n"
                        f"Relevant contents:\n{project_context['content']}"
                    ),
                })
        if _is_project_implementation_task(user_message, saved_task):
            messages.append({
                "role": "system",
                "content": (
                    "PROJECT IMPLEMENTATION MODE IS ACTIVE. Do not return a review "
                    "report or ask whether to proceed. Start or continue the work. "
                    "First diagnose the relevant files and project state; then make "
                    "one small verified change; then inspect it and run a focused "
                    "check. Return exactly one executable PowerShell block for the "
                    "next step, unless the approved task is genuinely complete. "
                    "Never use fragile PowerShell -replace for source code; prefer "
                    "a short Python pathlib exact-text edit with an occurrence check."
                ),
            })
    if executor.is_unlocked():
        messages.append({
            "role": "system",
            "content": (
                "Planning context: first resolve the target application or website "
                "from the user's wording and recent memory. Prefer an existing "
                "matching browser tab or visible app. Use the narrowest available "
                "specialized action, then semantic UI controls, then coordinates."
            ),
        })

    messages += history[-20:]
    messages.append({"role": "user", "content": user_message})

    if executor.is_unlocked():
        try:
            image = screen_control._pyautogui().screenshot()
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=60)
            messages.append({
                "role": "user",
                "content": "Current screen image. Use it to choose coordinates and verify the requested computer task.",
                "images": [base64.b64encode(buffer.getvalue()).decode("ascii")],
            })
        except Exception:
            pass

    if exec_result is not None:
        # Feed the command output back so the model can turn it into a
        # natural-language answer rather than dumping raw PowerShell at the user.
        if exec_result.get("computer_actions") is not None:
            messages.append({
                "role": "system",
                "content": (
                    "Computer action results:\n"
                    f"{json.dumps(exec_result['computer_actions'], indent=2)}\n"
                    "Summarize what actually succeeded. If an action failed, state which one and why."
                ),
            })
        elif exec_result.get("workflow_results") is not None:
            messages.append({
                "role": "system",
                "content": (
                    "Implementation workflow results:\n"
                    f"{json.dumps(exec_result, indent=2)}\n"
                    "Summarize only verified changes, inspections, and test results. "
                    "Do not claim that a change happened unless the command output shows it."
                ),
            })
        elif exec_result.get("command_result") is not None:
            messages.append({
                "role": "system",
                "content": (
                    "This is an active implementation workflow. The last command result is:\n"
                    f"{json.dumps(exec_result, indent=2)}\n"
                    "Continue the user's approved work. Inspect or modify the project and "
                    "run the next focused check if needed. Return exactly one fenced "
                    "PowerShell command when another step is needed; otherwise return a "
                    "brief completion summary. Do not ask whether to proceed again."
                ),
            })
        elif exec_result.get("preflight_rejected_command") is not None:
            messages.append({
                "role": "system",
                "content": (
                    "The proposed command was rejected before execution because it is "
                    "fragile or unsafe for this project edit:\n"
                    f"{json.dumps(exec_result, indent=2)}\n"
                    "Return exactly one corrected executable PowerShell block. Prefer "
                    "a short Python pathlib script with an exact old-text/new-text "
                    "replacement and a check that the old text occurs exactly once. "
                    "Do not ask for permission again."
                ),
            })
        elif exec_result.get("error"):
            messages.append({
                "role": "system",
                "content": f"The command failed: {exec_result['error']}\nIf it was blocked, tell the user. If it errored, explain what happened and (only if it helps) suggest a fix.",
            })
        else:
            messages.append({
                "role": "system",
                "content": (
                    f"Command output (exit {exec_result.get('exit_code', '?')}, "
                    f"{exec_result.get('duration_ms', '?')}ms):\n"
                    f"```\n{exec_result.get('stdout', '')}\n{exec_result.get('stderr', '')}\n```\n"
                    "Summarize the result for the user concisely."
                ),
            })

    text, err = _call_ollama(messages)
    if err:
        return f"[{err}]"
    return text


def _run_command_workflow(
    goal: str,
    first_command: str,
    history: list[dict],
    search_block: str | None,
    location: str | None,
    area: str | None,
    street: str | None,
    tool_log: list[str],
    max_steps: int = 8,
) -> tuple[list[dict], str]:
    """Iteratively inspect, change, and verify a requested local task."""
    results = []
    command = first_command
    seen: set[str] = set()
    for step in range(max_steps):
        signature = command.strip()
        if signature in seen:
            tool_log.append("stopped: the same command was proposed again")
            break
        seen.add(signature)
        tool_log.append(f"step {step + 1}/{max_steps}: running {command[:120]}{'…' if len(command) > 120 else ''}")
        if _needs_command_repair(command):
            repair = _ask_model(
                goal,
                history,
                search_block,
                location,
                area,
                street,
                exec_result={
                    "workflow_step": step + 1,
                    "workflow_goal": goal,
                    "preflight_rejected_command": command,
                    "reason": (
                        "The command uses a fragile PowerShell -replace regex for a source edit. "
                        "Replace it with a short Python pathlib exact-text edit that checks for "
                        "one match, then inspect the file."
                    ),
                },
            )
            repaired_command = _extract_powershell(repair)
            if repaired_command and repaired_command.strip() != command.strip():
                tool_log.append("repairing: replaced a fragile source-edit command")
                command = repaired_command
            else:
                tool_log.append("stopped: the proposed source edit was unsafe to repair")
                break
        result = executor.run_powershell(command)
        if result.get("exit_code", 0) != 0 and "modulenotfounderror" in (
            f"{result.get('stderr', '')} {result.get('stdout', '')}"
        ).lower():
            project_root = _load_task_state().get("project_root")
            repaired_test = _repair_python_test_command(command, project_root)
            if repaired_test and repaired_test.strip() != command.strip():
                tool_log.append("retrying: rerunning tests from the project root with PYTHONPATH configured")
                command = repaired_test
                result = executor.run_powershell(command)
        if result.get("exit_code", 0) != 0 and _is_recoverable_command_error(result):
            repair = _ask_model(
                goal,
                history,
                search_block,
                location,
                area,
                street,
                exec_result={
                    "workflow_step": step + 1,
                    "workflow_goal": goal,
                    "failed_command": command,
                    "command_result": result,
                },
            )
            repaired_command = _extract_powershell(repair)
            if repaired_command and repaired_command.strip() != command.strip():
                tool_log.append("retrying: corrected a command syntax, quoting, regex, or path error")
                command = repaired_command
                result = executor.run_powershell(command)
        results.append({"command": command, "result": result})
        _save_task_state(
            status="blocked" if result.get("blocked") else "in_progress",
            completed_actions=results,
            last_command=command,
            workflow_step=step + 1,
        )
        if result.get("blocked"):
            tool_log.append(f"blocked: {result['error']}")
            break
        if result.get("error") or result.get("exit_code", 0) != 0:
            tool_log.append(f"stopped: command failed ({result.get('error') or result.get('stderr', '')[:180]})")
            break
        tool_log.append(f"done (exit {result.get('exit_code', 0)}, {result.get('duration_ms', 0)}ms)")
        follow_up = _ask_model(
            (
                f"Continue the approved project task: {goal}\n"
                "Use this workflow phase order: diagnose and inspect first; make the "
                "smallest coherent change; inspect the changed files; run focused "
                "tests or compile checks; then report verified results. Do not ask "
                "for permission again. Return one executable PowerShell block for "
                "the next phase, or a concise completion summary when finished."
            ),
            history,
            search_block,
            location,
            area,
            street,
            exec_result={
                "workflow_step": step + 1,
                "workflow_goal": goal,
                "command": command,
                "command_result": result,
                "completed_steps": results,
            },
        )
        next_command = _extract_powershell(follow_up)
        if not next_command:
            _save_task_state(
                status="completed",
                completed_actions=results,
                last_command=command,
                workflow_step=step + 1,
            )
            return results, follow_up
        command = next_command
    return results, ""


# --- The main chat handler ---
_MESSAGE_REC = re.compile(r"^\s*(?:hey cipher|okay cipher|ok cipher,|cipher,)\s*(.+)$", re.IGNORECASE)
# Very small talk. ALL other messages now go through the full pipeline
# (web search + maybe a command run). "hi" / "thanks" / "ok" don't need
# a web search.
_SMALL_TALK = {
    "hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay",
    "cool", "nice", "bye", "goodbye", "good morning", "good night",
    "how are you", "what's up", "sup",
}


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = Field(default_factory=list)
    location: str | None = None
    area: str | None = None
    street: str | None = None
    lat: float | None = None
    lon: float | None = None


@app.on_event("shutdown")
def close_whatsapp_browser():
    screen_control.close_whatsapp_browser()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "smart_home_configured": smart_home.is_configured(),
        "unrestricted_unlocked": executor.is_unlocked(),
        "face_verified": _is_verified(),
        "face_lock_active": _face_lock_active(),
        "active_app": _active_app,
        "memory_messages": len(_load_memory()),
        "task_state": _load_task_state(),
    }


@app.delete("/memory")
def clear_memory():
    global _active_app
    _MEMORY_FILE.unlink(missing_ok=True)
    _clear_task_state()
    _active_app = None
    return {"ok": True, "cleared": True}


class MemoryFactRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


@app.get("/memory/facts")
def memory_facts():
    return {"facts": _load_facts(), "count": len(_memory_items())}


@app.post("/memory/facts")
def add_memory_fact(req: MemoryFactRequest):
    try:
        item = _save_memory_item(req.content.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "fact": item}


@app.delete("/memory/facts/{fact_id}")
def delete_memory_fact(fact_id: str):
    facts = _load_facts()
    items = facts.get("memories", [])
    kept = [item for item in items if item.get("id") != fact_id]
    if len(kept) == len(items):
        raise HTTPException(status_code=404, detail="Long-term memory item not found.")
    facts["memories"] = kept
    _FACTS_FILE.write_text(json.dumps(facts, indent=2, ensure_ascii=False))
    return {"ok": True, "deleted": fact_id}


@app.delete("/memory/facts")
def clear_memory_facts():
    _FACTS_FILE.unlink(missing_ok=True)
    return {"ok": True, "cleared": True}


@app.get("/task/state")
def task_state():
    return {"task": _load_task_state()}


@app.post("/chat")
def chat(req: ChatRequest):
    global _pending_confirmation, _active_app
    raw = req.message.strip()
    lower = raw.lower()

    # --- Unlock-phrase check (always, even when locked) ---
    # Backup way to enable unrestricted mode. The primary way is the
    # camera recognizing your face — see _mark_verified() in main.py.
    # If the user sends the unlock phrase, it extends the executor
    # window for 30 minutes. Useful for testing without a camera.
    if not executor.is_unlocked() and os.getenv("CIPHER_UNLOCK_PHRASE", "").strip():
        if executor.try_unlock(raw):
            return ChatResponse(
                reply="Unrestricted mode is ON (via unlock phrase). I can now run any PowerShell command. Lasts 30 min — your next face-verify or chat message will extend it.",
                mode="unrestricted",
            ).to_dict()

    # --- Lock / unlock commands ---
    if lower in ("lock it", "lock yourself", "go safe", "safe mode", "disable unrestricted", "lock the executor"):
        return ChatResponse(
            reply="Permanent unrestricted mode is enabled, so the executor stays available. "
                  "Set CIPHER_ALWAYS_UNRESTRICTED=false and restart Cipher to restore locking.",
            mode="unrestricted",
        ).to_dict()

    # --- Face-verify phrases always run ---
    if lower in ("who am i", "who am i?", "verify me", "scan my face", "recognize me"):
        result = _run_face_verify()
        if result.get("error"):
            return ChatResponse(
                reply=f"Couldn't do that: {result['error']}",
                locked=_face_lock_active() and not _is_verified(),
            ).to_dict()
        if not result.get("recognized"):
            return ChatResponse(reply="I don't recognize you. Staying locked.", locked=_face_lock_active()).to_dict()
        return ChatResponse(reply=f"Welcome back, {result['name']}. Unlocked.").to_dict()

    # --- Lock screen ---
    if _face_lock_active() and not _is_verified():
        return ChatResponse(
            reply="Locked. Say \"who am I\" while looking at the camera to unlock.",
            locked=True,
        ).to_dict()

    _touch_activity()
    _remember("user", raw)

    task_state = _load_task_state()
    if lower in {"continue", "continue the task", "resume", "resume the task", "what was i doing"}:
        if not task_state:
            return ChatResponse(reply="There is no interrupted task to resume.").to_dict()
        message = (
            "Resume the interrupted task from its saved state. "
            f"Saved state: {json.dumps(task_state)}"
        )
    elif task_state and lower.startswith(("continue ", "resume ")):
        message = (
            f"{message}\nSaved task state from the previous attempt: "
            f"{json.dumps(task_state)}. Continue from the first incomplete step."
        )

    if lower in {"confirm", "confirm send", "yes send it", "send it"}:
        if not _pending_confirmation:
            return ChatResponse(reply="There is no pending send or submit action to confirm.").to_dict()
        pending = _pending_confirmation
        _pending_confirmation = None
        results = _run_computer_actions(pending)
        if any(item["result"].get("confirmation_required") for item in results):
            _pending_confirmation = pending
            return ChatResponse(reply="I still need confirmation before that action.").to_dict()
        return ChatResponse(
            reply="Confirmed. " + json.dumps(results),
            tool_log=["executed the confirmed send/submit action"],
        ).to_dict()

    # Strip a wake-style prefix if present ("hey cipher, what's the weather")
    # so the routed command patterns still match.
    m = _MESSAGE_REC.match(raw)
    message = m.group(1).strip() if m else raw
    try:
        remembered_fact = _remember_user_facts(message)
    except ValueError as exc:
        return ChatResponse(reply=str(exc)).to_dict()
    if remembered_fact:
        _remember("assistant", f"Saved long-term memory: {remembered_fact['content']}")
        return ChatResponse(
            reply=f"I will remember: {remembered_fact['content']}",
            tool_log=["saved long-term memory"],
        ).to_dict()
    forget_match = re.match(r"\s*(?:please\s+)?(?:forget|remove from memory|delete from memory)\s+(?:that\s+)?(.+?)\s*[.!?]*\s*$", message, re.IGNORECASE)
    if forget_match:
        query = forget_match.group(1).strip()
        deleted = _forget_memory(query)
        if deleted:
            return ChatResponse(
                reply="Forgot: " + "; ".join(item.get("content", "") for item in deleted),
                tool_log=[f"removed {len(deleted)} long-term memory item(s)"],
            ).to_dict()
        return ChatResponse(reply=f"I couldn't find a saved memory matching: {query}").to_dict()
    if re.search(r"\b(?:what|which)\s+(?:do\s+)?you\s+remember\b|\blist\s+(?:my\s+)?(?:saved\s+)?memories\b", message, re.IGNORECASE):
        memories = _memory_items()
        contacts = _load_facts().get("contacts", {})
        lines = [f"- {item['content']}" for item in memories]
        lines.extend(f"- {name}: {contact}" for name, contact in contacts.items())
        return ChatResponse(reply="I have no long-term memories saved." if not lines else "Here is what I remember:\n" + "\n".join(lines)).to_dict()

    # --- 1. Whitelisted command router ---
    tool_log: list[str] = []
    command_reply = try_command(message, req.location, req.area, req.street, req.lat, req.lon)
    if command_reply is not None:
        return ChatResponse(reply=command_reply, tool_log=tool_log).to_dict()

    # --- 2. Live web search (ALWAYS, except small talk) ---
    search_block = None
    if (
        lower.rstrip("!.?") not in _SMALL_TALK
        and len(message.strip()) > 2
        and not (executor.is_unlocked() and _is_local_computer_task(message))
    ):
        tool_log.append("searching the web…")
        search_block = web_search.build_context_block(message)
        if not search_block:
            tool_log.append("no web results found")

    # --- 3. Ask Ollama ---
    if not executor.is_unlocked():
        # Plain conversational reply. Web search gave context, model answers.
        reply = _ask_model(message, req.history, search_block, req.location, req.area, req.street)
        return ChatResponse(reply=reply, tool_log=tool_log).to_dict()

    # --- 4. Let the model pick a command to run ---
    # First pass: ask the model what (if anything) to run.
    tool_log.append("analyzing request and planning the safest action")
    tool_log.append("asking what to do (unrestricted mode)")
    existing_task = _load_task_state()
    _save_task_state(
        goal=raw,
        current_app=_active_app,
        completed_actions=[],
        project_root=existing_task.get("project_root"),
        status=(
            "implementation_requested"
            if existing_task.get("status") == "implementation_requested"
            else "in_progress"
        ),
    )
    first_prompt = message
    if _is_project_implementation_task(message, existing_task):
        first_prompt = (
            f"Implement the approved changes in project {existing_task.get('project_root')}.\n"
            f"User request: {message}\n"
            "Begin with diagnosis and inspection. Return exactly one executable "
            "PowerShell block for the first step. Do not return only a report and "
            "do not ask for permission."
        )
    first = _ask_model(first_prompt, req.history, search_block, req.location, req.area, req.street)
    actions = _extract_computer_actions(first)
    if actions:
        action_results = []
        executed_plan_signatures: set[str] = set()
        for step in range(4):
            plan_signature = json.dumps(actions, sort_keys=True, ensure_ascii=True)
            if plan_signature in executed_plan_signatures:
                tool_log.append("stopped: the model repeated the same computer action plan")
                break
            executed_plan_signatures.add(plan_signature)
            tool_log.append(f"observing screen and executing step {step + 1}")
            _complete_message_actions(actions, message)
            step_results = _run_computer_actions(actions)
            action_results.extend(step_results)
            if any(item["result"].get("confirmation_required") for item in step_results):
                confirmation_index = next(
                    index for index, item in enumerate(step_results)
                    if item["result"].get("confirmation_required")
                )
                _pending_confirmation = [actions[confirmation_index]]
                tool_log.append("paused: confirmation required before sending/submitting")
                break
            if any(
                item["result"].get("error")
                and not item["result"].get("confirmation_required")
                for item in step_results
            ):
                break
            if any(
                item["action"] in {"whatsapp_message", "instagram_message"}
                and item["result"].get("sent") is True
                for item in step_results
            ):
                tool_log.append("stopped: the messaging action reported the message was sent")
                break
            next_prompt = (
                f"The previous computer actions completed: {json.dumps(step_results)}. "
                "Re-evaluate the goal against the new current screen image. "
                "If the user's task is not finished, return the next computer action "
                "block only. If finished, return a brief summary. Do not repeat an "
                "action that already succeeded."
            )
            follow_up = _ask_model(
                next_prompt,
                req.history,
                search_block,
                req.location,
                req.area,
                req.street,
                exec_result={"computer_actions": step_results},
            )
            actions = _extract_computer_actions(follow_up)
            if not actions:
                break
        for item in action_results:
            result = item["result"]
            if result.get("error"):
                tool_log.append(f"{item['action']}: {result['error']}")
            else:
                tool_log.append(f"{item['action']}: done")
        failed = next(
            (
                item for item in action_results
                if item["result"].get("error")
                and not item["result"].get("confirmation_required")
            ),
            None,
        )
        if failed:
            return ChatResponse(
                reply=f"I could not complete the computer task: {failed['result']['error']}",
                tool_log=tool_log,
            ).to_dict()
        if any(item["result"].get("confirmation_required") for item in action_results):
            return ChatResponse(
                reply=(
                    "The message is drafted and ready in the selected chat. "
                    "The message is ready for review before sending."
                ),
                tool_log=tool_log,
            ).to_dict()
        if any(
            item["action"] in {"whatsapp_message", "instagram_message"}
            and item["result"].get("sent") is True
            for item in action_results
        ):
            return ChatResponse(
                reply="The message was sent successfully.",
                tool_log=tool_log,
            ).to_dict()
        _clear_task_state()
        summary = _ask_model(
            message,
            req.history,
            search_block,
            req.location,
            req.area,
            req.street,
            exec_result={"computer_actions": action_results},
        )
        return ChatResponse(reply=summary, tool_log=tool_log).to_dict()
    cmd = _extract_powershell(first)

    if cmd is None:
        # No command found. Two sub-cases:
        # (a) the reply is conversational — just return it.
        # (b) the reply contains a backtick-quoted command that didn't
        #     match our cmdlet heuristic (e.g. model hallucinated a
        #     nonsense command). Either way, return the reply rather
        #     than executing garbage.
        cleaned = _PWSH_BLOCK_RE.sub("", _COMPUTER_BLOCK_RE.sub("", first)).strip()
        return ChatResponse(reply=cleaned or first, tool_log=tool_log).to_dict()

    workflow_results, workflow_reply = _run_command_workflow(
        message,
        cmd,
        req.history,
        search_block,
        req.location,
        req.area,
        req.street,
        tool_log,
    )
    final_result = workflow_results[-1]["result"] if workflow_results else {}
    if final_result.get("blocked") or final_result.get("error") or final_result.get("exit_code", 0) != 0:
        error = final_result.get("error") or final_result.get("stderr") or "The command failed."
        return ChatResponse(
            reply=f"I could not complete the requested work: {error}",
            tool_log=tool_log,
        ).to_dict()
    if workflow_reply and not _extract_powershell(workflow_reply):
        _clear_task_state()
        return ChatResponse(reply=workflow_reply, tool_log=tool_log).to_dict()
    summary = _ask_model(
        message,
        req.history,
        search_block,
        req.location,
        req.area,
        req.street,
        exec_result={
            "workflow_results": workflow_results,
            "status": "completed",
        },
    )
    _clear_task_state()
    return ChatResponse(reply=summary, tool_log=tool_log).to_dict()


# --- Tasks ---
@app.get("/tasks")
def get_tasks(include_done: bool = True):
    return tasks.list_tasks(include_done=include_done)


@app.post("/tasks")
def create_task(task: dict):
    return tasks.add_task(task.get("title", ""), task.get("due"))


@app.post("/tasks/{task_id}/complete")
def complete(task_id: str):
    result = tasks.complete_task(task_id)
    return result or {"error": "not found"}


@app.delete("/tasks/{task_id}")
def delete(task_id: str):
    ok = tasks.delete_task(task_id)
    return {"deleted": ok}


# --- Smart home ---
@app.post("/smart-home/action")
async def smart_home_action(action: dict):
    return await smart_home.call_service(action["domain"], action["service"], action["entity_id"])


@app.get("/smart-home/state/{entity_id}")
async def smart_home_state(entity_id: str):
    return await smart_home.get_state(entity_id)


# --- PC control ---
@app.post("/pc/action")
def pc_action(req: dict):
    return pc_control.dispatch(req.get("action"), req.get("value"))


@app.get("/pc/actions")
def pc_actions():
    return pc_control.list_actions()


# --- Browser ---
@app.post("/browser/open")
def browser_open(req: dict):
    return browser.open_target(req.get("target"))


@app.post("/browser/youtube")
def browser_youtube(req: dict):
    query = str(req.get("query", "")).strip()
    result_index = req.get("result_index")
    if result_index is not None:
        try:
            result_index = int(result_index)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="result_index must be an integer")
    return browser.youtube_search_and_open(query, result_index=result_index)


@app.get("/browser/sites")
def browser_sites():
    return {"sites": browser.list_sites(), "catalog": browser.catalog()}


@app.get("/apps/catalog")
def apps_catalog():
    return {"apps": pc_control.catalog(), "sites": browser.catalog()}


@app.post("/files/read")
def files_read(req: dict):
    if not executor.is_unlocked():
        raise HTTPException(status_code=423, detail="Unlock unrestricted mode before reading local files.")
    result = _read_local_file(req.get("path", ""))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/screen/screenshot")
def screen_screenshot():
    if _face_lock_active() and not _is_verified():
        raise HTTPException(status_code=423, detail="Unlock face verification before viewing the screen.")
    result = screen_control.capture()
    if result.get("error"):
        raise HTTPException(status_code=503, detail=result["error"])
    return FileResponse(result["path"], media_type="image/png", filename="cipher-screen.png")


@app.get("/screen/stream")
def screen_stream():
    if _face_lock_active() and not _is_verified():
        raise HTTPException(status_code=423, detail="Unlock face verification before viewing the screen.")
    return StreamingResponse(
        screen_control.mjpeg_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/screen/file/{filename}")
def screen_file(filename: str):
    if _face_lock_active() and not _is_verified():
        raise HTTPException(status_code=423, detail="Unlock face verification before viewing the screen.")
    safe_name = os.path.basename(filename)
    path = screen_control.SCREENSHOT_DIR / safe_name
    if path.parent != screen_control.SCREENSHOT_DIR or not path.is_file():
        raise HTTPException(status_code=404, detail="Screenshot not found.")
    return FileResponse(path, media_type="image/png", filename=safe_name)


@app.post("/screen/action")
def screen_action(req: dict):
    if not executor.is_unlocked():
        raise HTTPException(status_code=423, detail="Unlock unrestricted mode before controlling the screen.")
    action = str(req.get("action", "")).strip().lower()
    try:
        if action == "click":
            return screen_control.click(req.get("x"), req.get("y"))
        if action == "type":
            return screen_control.type_text(req.get("text", ""))
        if action == "hotkey":
            return screen_control.hotkey(req.get("keys", []))
        if action == "focus_window":
            return ui_automation.focus_window(req.get("title", ""))
        if action == "click_text":
            return ui_automation.click_text(
                req.get("target", ""),
                req.get("window"),
                req.get("control_type"),
            )
        if action == "type_into":
            return ui_automation.type_into(
                req.get("target", ""),
                req.get("text", ""),
                req.get("window"),
            )
        if action == "read_file":
            return _read_local_file(req.get("path", ""))
    except (TypeError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(
        status_code=400,
        detail="Supported actions: click, type, hotkey, focus_window, click_text, type_into, read_file",
    )


# --- Face auth ---
@app.post("/face/enroll")
def face_enroll(req: dict):
    return face_auth.enroll(req.get("name", ""), int(req.get("num_samples", 25)))


@app.post("/face/train")
def face_train():
    return face_auth.train()


@app.post("/face/verify")
def face_verify():
    return _run_face_verify()


@app.post("/face/lock")
def face_lock():
    """Manually lock the face session (and the executor) immediately."""
    _session_state["verified_at"] = 0.0
    _session_state["last_activity"] = 0.0
    executor.lock_session()
    return {"ok": True, "locked": True}


@app.get("/face/status")
def face_status():
    return {
        "trained": face_auth.is_trained(),
        "enrolled": face_auth.enrolled_names(),
        "lock_active": _face_lock_active(),
        "verified": _is_verified(),
    }


# --- Executor endpoints ---
@app.post("/executor/unlock")
def executor_unlock(req: dict):
    """Explicit unlock via API (alternative to saying the phrase in chat)."""
    if executor.try_unlock(req.get("phrase", "")):
        return {"ok": True, "unlocked": True}
    return {"ok": False, "unlocked": False, "error": "Wrong phrase, or none configured."}


@app.post("/executor/lock")
def executor_lock():
    executor.lock_session()
    return {"ok": True, "unlocked": False}


@app.post("/executor/run")
def executor_run(req: dict):
    return executor.run_powershell(req.get("command", ""), int(req.get("timeout_sec", 30)))


@app.get("/executor/log")
def executor_log():
    """Last 100 entries from the execution log. Use this to see what got run."""
    if not executor.LOG_FILE.exists():
        return {"log": []}
    try:
        log = json.loads(executor.LOG_FILE.read_text())
    except Exception:
        log = []
    return {"log": log[-100:]}
