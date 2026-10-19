# Cipher — your personal assistant

A local JARVIS-style assistant: chat, tasks, weather, places, PC control,
face-auth lock, and (optionally) full unrestricted execution — all running
on your machine, powered by Ollama.

## What's here

- `backend/main.py` — the router. Handles every chat message, runs the
  whitelist first, then web search, then Ollama, then (if unlocked) a
  PowerShell command execution.
- `backend/skills/` — one module per capability:
  - `tasks.py` — task list (plain JSON file, no DB).
  - `pc_control.py` — whitelisted PC actions (open/close apps, folders,
    volume, lock screen).
  - `browser.py` — whitelisted sites, existing-tab reuse, and YouTube search.
  - `weather.py` — Open-Meteo current conditions + forecast (free, keyless).
  - `places.py` — Overpass/Nominatim "near me" lookups (free, keyless).
  - `web_search.py` — DuckDuckGo via `ddgs`. Used to ground every LLM
    reply in current information.
  - `face_auth.py` — offline OpenCV Haar + LBPH face recognition.
  - `executor.py` — arbitrary PowerShell execution with a catastrophic-command blocklist.
  - `smart_home.py` — Home Assistant stub.
- `frontend/index.html` — single-page UI. No build step.

## Setup

```bash
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Then open `frontend/index.html`. Or just double-click `Cipher.bat` at the
project root — it does the same thing and handles the venv for you.

## Ollama Cloud or local Ollama

Cipher keeps the Ollama credential out of source control. To use Ollama Cloud,
set these values in `backend/.env` locally (do not paste the key into Python
files or commit it):

```dotenv
OLLAMA_API_KEY=your-ollama-cloud-key
OLLAMA_URL=https://ollama.com/api/chat
OLLAMA_MODEL=gemma4:31b
```

When `OLLAMA_API_KEY` is present, the backend sends it as a Bearer token and
defaults to `gemma4:31b`. Without a key, it uses the local
`http://localhost:11434/api/chat` endpoint with the same model name. The model
must be available in the selected Ollama environment; if it is not, pull it or
set `OLLAMA_MODEL` to an installed model.

If `OLLAMA_MODEL` starts with `gemma4` while the key is empty and the URL is
local, Cipher stops with a clear configuration message instead of sending the
cloud-only model name to localhost.

## How Cipher decides what to do

Every message goes through this pipeline:

1. **Unlock check** — if you haven't unlocked yet and the message matches
   `CIPHER_UNLOCK_PHRASE` from `.env`, Cipher flips into unrestricted mode.
2. **Whitelisted commands** — patterns like "open Chrome", "volume up",
   "weather in Multan", "cafes near me" are handled directly with no
   model call. Fast and exact.
3. **Live web search** — every other message (except small talk) runs a
   DuckDuckGo search and the results are dropped into the LLM's context.
4. **Ollama** — the model evaluates the request, sees the web results,
   and answers. It can also emit bounded PowerShell or computer-action
   steps when a local task requires them.
5. **Return** — anything the model did (whether conversational or a
   command run) gets shown in the chat as a `› ...` line so you can see
   what happened.

## Supported apps and websites

Cipher includes a catalog of common Windows applications and web services.
Use `GET /apps/catalog` to inspect names and capability descriptions. When a
matching browser tab is already open, Cipher activates that tab before
navigating instead of opening a separate site window. Desktop controls use
Windows accessibility labels where available, so tasks generally do not need
coordinates.

## Memory, resuming, and local files

Conversation memory is stored locally in `backend/data/chat_memory.json` and is
separate from universal long-term memory in `backend/data/long_term_memory.json`.
Explicit requests such as "remember that I prefer concise replies," "save that
my favorite color is blue," or "remember Azka CA Lhr is my buddy" are stored
persistently. Contact relationships create aliases, so "message my buddy" can
resolve to the saved contact. Ordinary statements are not silently promoted;
use "remember," "save," "store," or "note" to request persistence. Repeating
the same fact deduplicates it.

Ask "what do you remember about me?" to list saved facts, or say "forget [a
fact or phrase]" to remove matching memory. Inspect stored memory with
`GET /memory/facts`; add an item with `POST /memory/facts` and
`{"content":"I prefer concise replies"}`; delete an item with
`DELETE /memory/facts/{id}`; and clear all long-term memory with
`DELETE /memory/facts`. `DELETE /memory` clears conversation/task state only.
Long-term memories stay local, but relevant facts may be sent to the configured
Ollama model as context. Cipher refuses to store passwords, API keys, and
recovery secrets.
Relationships are not limited to a fixed list: phrases such as "elder brother,"
"manager," "cousin," or any other saved relationship are stored. Cipher also
creates a normalized alias where useful, so "my brother" can resolve a contact
saved as "my elder brother."
When a messaging request asks Cipher to write something, the original context
is sent to Ollama for message generation. If generation fails, the action is
stopped rather than sending a generic fallback.

### WhatsApp Web setup

WhatsApp messages use a dedicated visible Chrome profile at
`backend/data/whatsapp_profile`; Cipher does not attach to or change your normal
Chrome profile. The first WhatsApp task opens this separate browser window.
Scan the WhatsApp Web QR code with your phone once, then retry the task. The
profile keeps the linked session between Cipher restarts, so you generally do
not need to scan again unless WhatsApp logs it out.

Before typing or sending, Cipher requires one exact contact search result and
checks that the opened chat header matches that contact. If the result is
missing, ambiguous, or does not match, the message is not sent. If delivery
cannot be confirmed, check the WhatsApp chat before retrying so you do not send
a duplicate.

Cipher also saves the active computer task and completed action results in
`backend/data/task_state.json`. If a task is interrupted, say **resume** or
**continue** and Cipher will use that checkpoint to avoid repeating completed
steps.
The current checkpoint is also available at `GET /task/state`.

In unrestricted mode, you can point Cipher at a local text file using its
absolute path, or ask it to read a file through the `read_file` action. Text
is capped at 50,000 characters; binary, missing, and inaccessible files are
reported instead of being silently interpreted. The direct API is
`POST /files/read` with `{"path":"C:\\path\\to\\file.txt"}`.

## Unrestricted mode

Cipher runs in permanent unrestricted mode by default:

### Permanent computer control

Set `CIPHER_ALWAYS_UNRESTRICTED=true` in `.env` to keep computer control
available across restarts without an unlock phrase or face-authentication
gate. This is the default configuration.

To restore the old lock behavior, set
`CIPHER_ALWAYS_UNRESTRICTED=false` and restart the backend.

### What it can and can't do

- **Permanent unrestricted mode**: chat, web search, the whitelist (open apps,
  weather, places, volume, lock screen, browser bookmarks), arbitrary PowerShell,
  and explicit screen input actions (`click`, `type`, and `hotkey`) through the
  screen-control API. Every command run is logged to
  `backend/data/exec_log.json` with a timestamp, command, exit code,
  and a head of the output. View the last 100 entries at
  `GET /executor/log`.

The **See my screen continuously** button opens a live, local MJPEG preview and
can be stopped at any time. YouTube requests such
as “search YouTube for piano and open the second video” are handled directly:
Cipher searches YouTube and opens the requested numbered result. Destructive or
irreversible actions should still be confirmed immediately before execution.

For broader desktop work, Cipher can inspect the current screen and execute a
bounded action plan containing `open_url`, `open_app`, `click`, `type`,
`hotkey`, `wait`, and `screenshot`. It executes at most eight actions per
request and reports each result. This supports tasks across desktop apps, not
only browser text entry. Actions that delete data, send communications, make
purchases, change security settings, or publish content still require a
confirmation step.

For local project requests such as “review this project” or “implement these
changes”, Cipher uses a bounded inspect-change-verify workflow. It collects
relevant source and documentation files for a review, or continues approved
PowerShell steps for implementation and testing instead of stopping after one
directory listing. It retains the project root in task state so follow-up
requests such as “analyze the models and implement the findings” can continue
from the previous project context.

When a request combines review and implementation, the review route does not
stop the request: Cipher records the project context and continues into the
implementation workflow. Implementation phases are diagnosis and inspection,
smallest coherent change, changed-file inspection, focused verification, and
final reporting. A failed or blocked command remains in task state for
diagnosis and resume rather than being reported as completed.

Project implementation mode is explicit internally: the model receives the
current project snapshot, the saved workflow phase, and the last verified
command result. It must produce the next executable step rather than another
permission question or duplicate review. Internal reasoning is kept private;
the user receives concise progress, verified results, and actionable blockers.

The implementation workflow removes comment-only presentation lines from model
commands, uses `-LiteralPath` guidance for Windows files, retries a corrected
command after PowerShell parser, regex, quoting, or path errors, and preflights
fragile `Get-Content -replace` source edits in favor of exact-text Python
replacements. It stops on real execution failures and reports the verified
state instead of claiming success.

Python test failures caused by running from the wrong directory are diagnosed
automatically. For `ModuleNotFoundError` during `pytest` or `unittest`, Cipher
retries from the saved project root, sets `PYTHONPATH`, and uses the project's
`venv\Scripts\python.exe` when available before reporting the test as blocked.

For general desktop work, prefer semantic UI actions:
`focus_window`, `click_text`, and `type_into`. These use the Windows
accessibility tree to find controls by their visible label or automation ID,
so they do not require per-app coordinates. Coordinate clicks remain a
fallback for applications that expose no accessibility metadata.

For WhatsApp Web, the preferred action is `whatsapp_message` with `contact`
and `message` fields. Cipher activates the WhatsApp tab/window, uses accessible
search and composer controls when available, and otherwise calculates fallback
points relative to the current WhatsApp window bounds. After searching, it
clicks the exact matching contact result rather than pressing Enter into the
previously open chat; if it cannot identify that result, it stops without
composing or sending. The fallback adapts to resized or non-maximized windows;
it requires no user-provided coordinates and verifies WhatsApp is foreground
before typing. The semantic route reuses its
field lookup for clicking and typing, and waits only as long as needed for the
search/chat transition. App launches similarly wait for the app window and try
to restore and foreground it instead of returning while it runs behind other
windows. New browser navigation also waits for the matching tab and activates
it before reporting success. The model is instructed to compose the final
message in its action plan, avoiding a separate generation call when possible;
server-side generation remains as a quality-preserving recovery path.

Application actions accept `name`, `app`, or `value` as the application field,
so model-generated plans remain compatible across providers.

Typing uses clipboard paste on Windows so apostrophes and longer messages reach
the focused application reliably. `CIPHER_ALLOW_AUTOMATED_SEND=true` permits
the assistant to press Enter for the requested send action; set it to `false`
to require a separate confirmation again.

When an app is already open, `open_app` searches visible Windows titles and
foregrounds the existing window. Newly launched apps are restored and brought
to the foreground before the action reports success; if Windows does not expose
a foreground window, Cipher returns an explicit error. WhatsApp messaging uses
its specialized search-then-compose workflow and does not require fixed
coordinates or a full-screen window.

## Conversation memory

Cipher keeps the last 40 local chat messages in
`backend/data/chat_memory.json`, so follow-ups can refer to earlier requests
after a backend restart. It also remembers the last application opened during
the current backend session. For example:

```text
Open WhatsApp
Search for Azka CA Lhr
```

The second request is given the WhatsApp context. Delete the local memory with:

```powershell
curl.exe -X DELETE http://localhost:8000/memory
```

For example, in unrestricted mode you can say:

```text
Open Gemini and type "explain quantum computing simply"
```

Cipher opens Gemini, waits for the page to load, and types into the active
browser window. Keep the Gemini tab in the foreground while this runs; if
another window takes focus, Cipher reports the typing failure instead of
claiming it succeeded.
- **Hard blocklist** (in both modes): `format-volume`, `Format C:`,
  `rd /s /q C:\`, `bcdedit`, `diskpart`, `reg delete hklm`, and
  similar catastrophic patterns are always refused. To run them,
  edit `skills/executor.py` — at least you'll have to look it in the
  eye first.

### Removing the safety entirely

If you genuinely want unrestricted mode on by default with no
verification, you can:

1. Comment out the face-verify check in `main.py`'s `initFaceGate()`,
   OR
2. Set `CIPHER_UNLOCK_PHRASE=` to something simple and unlock once
   per backend restart.

The face gate is the default because (a) it ties "is the user here?"
to "can the assistant act", and (b) it auto-expires so you can't
accidentally leave your machine permanently unlocked.

## Voice

- **Speech-to-text** (mic button): browser's built-in `SpeechRecognition`.
- **Text-to-speech**: browser's built-in `speechSynthesis`. Cipher's
  replies are read aloud by your OS voice.

## Face recognition

```
curl.exe -X POST http://localhost:8000/face/enroll -H "Content-Type: application/json" -d '{"name":"Qureshi"}'
curl.exe -X POST http://localhost:8000/face/train
curl.exe http://localhost:8000/face/status
```

Once enrolled + trained, the frontend shows a lock screen on load that
scans your face and only reveals the chat UI if it recognizes you. The
unlock lasts 30 minutes of inactivity.

## Tasks, smart home

Both still work the same way: the Tasks panel in the UI uses
`/tasks`, and `POST /smart-home/action` calls Home Assistant if you set
`HA_URL` + `HA_TOKEN` in `.env`.
