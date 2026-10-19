@echo off
setlocal
cd /d "%~dp0"
title Cipher — close THIS window to stop everything

REM --- Backend: create venv if missing ---
if not exist "backend\venv\Scripts\activate.bat" (
    echo [setup] No venv found — creating one...
    python -m venv backend\venv
)

REM --- Always sync installed packages with requirements.txt.
REM     Using "python -m pip" (not pip.exe directly) since pip.exe is a
REM     launcher stub that can end up with a stale absolute path baked in
REM     if the venv folder is ever moved/renamed — python.exe doesn't have
REM     that problem. This is a fast no-op when everything's already
REM     satisfied, so it's cheap on normal runs and self-heals if
REM     requirements.txt ever gets a new dependency. ---
echo [setup] Syncing backend dependencies...
backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt --quiet

REM --- Backend + frontend both run as BACKGROUND jobs attached to THIS
REM     console (start /B — no separate window), instead of two separate
REM     "cmd /k" windows like before. Windows tears down everything still
REM     attached to a console when that console closes, so closing this
REM     one window now stops both servers too — no more hunting down two
REM     extra cmd windows separately. ---
echo [backend] Starting uvicorn on http://localhost:8000 ...
pushd backend
start /B "" venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
popd

echo [frontend] Starting local server on http://localhost:5500 ...
REM NOTE: system "python" currently points to a corrupted Python 3.13 install
REM (missing urllib/zipfile/xml/sqlite3 in its Lib folder), which crashes
REM http.server on startup. Using the healthy Python 3.14 install instead
REM (py -3.14) until 3.13 is repaired/reinstalled -- see chat for details.
pushd frontend
start /B "" py -3.14 -m http.server 5500
popd

REM --- Give both servers a moment to come up, then open the UI ---
timeout /t 3 /nobreak >nul
start "" "http://localhost:5500/index.html"

echo.
echo ============================================================
echo  Cipher is running.
echo  Backend:  http://localhost:8000
echo  Frontend: http://localhost:5500
echo.
echo  To stop Cipher completely: just close THIS window
echo  (or press Ctrl+C, then Y). That's the only window you need
echo  to close now — the browser tab you can leave open or close
echo  separately, it doesn't keep anything running by itself.
echo ============================================================
echo.

REM --- Keep this window (and therefore the background servers) alive
REM     until you close it. ---
:keepalive
timeout /t 3600 /nobreak >nul
goto keepalive
