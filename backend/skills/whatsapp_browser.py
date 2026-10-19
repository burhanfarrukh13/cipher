"""Persistent, isolated Playwright session for verified WhatsApp Web messaging."""
import asyncio
from concurrent.futures import Future
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import re
import threading
import time

PROFILE_DIR = Path(__file__).resolve().parent.parent / "data" / "whatsapp_profile"
WHATSAPP_URL = "https://web.whatsapp.com/"
logger = logging.getLogger(__name__)


class WhatsAppBrowserController:
    def __init__(self):
        self._queue: Queue = Queue()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: str | None = None
        self._startup_stage = "Playwright initialization"
        self._closed = False

    def send_message(self, contact: str, message: str, send: bool) -> dict:
        if not contact.strip() or not message.strip():
            return {"error": "A contact and non-empty message are required."}
        if self._closed:
            return {"error": "WhatsApp automation is shutting down. Restart Cipher and retry."}
        if not self._thread or not self._thread.is_alive():
            self._ready.clear()
            self._startup_error = None
            self._thread = threading.Thread(target=self._run, name="whatsapp-playwright", daemon=True)
            self._thread.start()
        if not self._ready.wait(timeout=45):
            return {"error": "WhatsApp browser did not start within 45 seconds."}
        if self._startup_error:
            return {"error": self._startup_error}
        future: Future = Future()
        self._queue.put((contact, message, send, future))
        try:
            return future.result(timeout=55)
        except TimeoutError:
            return {
                "error": "WhatsApp did not finish within 55 seconds. Check its browser window before retrying.",
                "delivery_uncertain": send,
            }

    @staticmethod
    def _new_worker_event_loop():
        if os.name == "nt":
            return asyncio.ProactorEventLoop()
        return asyncio.new_event_loop()

    def _run(self) -> None:
        loop = self._new_worker_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:
            logger.exception("WhatsApp browser worker failed during %s", self._startup_stage)
            self._startup_error = (
                f"Could not start the isolated WhatsApp browser during {self._startup_stage}: "
                f"{type(exc).__name__}: {exc}"
            )
            self._ready.set()
            self._fail_queued_requests(self._startup_error)
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    async def _serve(self) -> None:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self._startup_stage = "Chrome launch"
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",
                headless=False,
                args=["--no-first-run"],
                viewport=None,
            )
            try:
                page = next(
                    (candidate for candidate in context.pages if "web.whatsapp.com" in candidate.url),
                    context.pages[0] if context.pages else await context.new_page(),
                )
                if "web.whatsapp.com" not in page.url:
                    self._startup_stage = "opening WhatsApp Web"
                    await page.goto(WHATSAPP_URL, wait_until="domcontentloaded", timeout=30000)
                self._startup_stage = "bringing WhatsApp Web to the foreground"
                await page.bring_to_front()
                self._startup_stage = "running WhatsApp actions"
                self._ready.set()
                while True:
                    try:
                        request = self._queue.get_nowait()
                    except Empty:
                        await asyncio.sleep(0.05)
                        continue
                    if request is None:
                        break
                    contact, message, send, future = request
                    if future.cancelled():
                        continue
                    try:
                        result = await self._message(page, contact, message, send)
                    except (PlaywrightError, TimeoutError) as exc:
                        result = {"error": f"WhatsApp browser automation failed: {exc}"}
                    except Exception as exc:
                        result = {"error": f"WhatsApp automation failed ({type(exc).__name__}): {exc}"}
                    if not future.done():
                        future.set_result(result)
            finally:
                await context.close()

    def _fail_queued_requests(self, error: str) -> None:
        while True:
            try:
                request = self._queue.get_nowait()
            except Empty:
                return
            if request is not None and not request[3].done():
                request[3].set_result({"error": error})

    @staticmethod
    async def _visible(locator) -> bool:
        try:
            return await locator.count() > 0 and await locator.first.is_visible()
        except Exception:
            return False

    @classmethod
    async def _wait_for_search_or_login(cls, page, timeout: float = 20):
        deadline = time.monotonic() + timeout
        while True:
            search = await cls._field(page, "Search or start a new chat")
            if search is not None:
                return search, False
            if await cls._login_required(page):
                return None, True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, False
            await page.wait_for_timeout(min(250, max(1, int(remaining * 1000))))

    @classmethod
    async def _field(cls, page, label: str):
        pattern = re.compile(re.escape(label), re.IGNORECASE)
        candidates = (
            page.get_by_role("textbox", name=pattern),
            page.get_by_placeholder(pattern),
            page.locator(f'[contenteditable="true"][aria-label*="{label}" i]'),
            page.locator(f'[contenteditable="true"][data-placeholder*="{label}" i]'),
        )
        for candidate in candidates:
            if await cls._visible(candidate):
                return candidate.first
        return None

    @staticmethod
    async def _login_required(page) -> bool:
        body = (await page.locator("body").inner_text(timeout=3000)).casefold()
        qr_canvas = await page.locator("canvas").count() > 0
        return qr_canvas or "scan the qr code" in body or "link with phone number" in body

    @staticmethod
    async def _select_contact_result(page, contact: str) -> tuple[bool, str]:
        exact_text = page.get_by_text(contact, exact=True)
        rows = []
        for role in ("listitem", "gridcell"):
            candidate_rows = page.get_by_role(role).filter(has=exact_text)
            rows.extend(
                candidate_rows.nth(index)
                for index in range(await candidate_rows.count())
            )
        visible_rows = [row for row in rows if await row.is_visible()]
        if len(visible_rows) == 1:
            await visible_rows[0].click()
            return True, ""
        if len(visible_rows) > 1:
            return False, f"Multiple WhatsApp search results exactly match '{contact}'. Specify a more distinctive contact name."

        exact_matches = [
            exact_text.nth(index) for index in range(await exact_text.count())
        ]
        visible_matches = [item for item in exact_matches if await item.is_visible()]
        if len(visible_matches) == 1:
            await visible_matches[0].click()
            return True, ""
        if len(visible_matches) > 1:
            return False, f"WhatsApp shows multiple exact matches for '{contact}'; nothing was sent."
        return False, f"WhatsApp did not show an exact search result for '{contact}'. Nothing was sent."

    @staticmethod
    async def _header_matches(page, contact: str) -> bool:
        expected = " ".join(contact.casefold().split())
        for header in await page.locator("header").all():
            if not await header.is_visible():
                continue
            lines = [
                " ".join(line.casefold().split())
                for line in (await header.inner_text()).splitlines()
            ]
            if expected in lines:
                return True
        return False

    async def _message(self, page, contact: str, message: str, send: bool) -> dict:
        await page.bring_to_front()
        search, login_required = await self._wait_for_search_or_login(page)
        if search is None:
            if login_required:
                return {
                    "error": "Sign in to the dedicated WhatsApp Web window by scanning its QR code, then retry the message.",
                    "login_required": True,
                }
            return {
                "error": "WhatsApp Web did not finish loading its contact search field within 20 seconds. Retry the task."
            }

        await search.fill(contact)
        result, reason = await self._wait_for_contact_result(page, contact)
        if not result:
            return {"error": reason}
        selected, reason = await self._select_contact_result(page, contact)
        if not selected:
            return {"error": reason}

        try:
            await page.wait_for_function(
                "contact => Array.from(document.querySelectorAll('header')).some(header => "
                "Array.from(header.innerText.split('\\n')).some(line => "
                "line.trim().toLocaleLowerCase() === contact.toLocaleLowerCase()))",
                arg=contact,
                timeout=7000,
            )
        except Exception:
            if not await self._header_matches(page, contact):
                return {"error": f"Selected chat header could not be verified as '{contact}'. Nothing was typed or sent."}
        if not await self._header_matches(page, contact):
            return {"error": f"Selected chat header does not match '{contact}'. Nothing was typed or sent."}

        composer = await self._field(page, "Type a message")
        if composer is None:
            return {"error": "The verified contact chat opened, but its message composer is unavailable."}
        await composer.fill(message)
        if " ".join((await composer.inner_text()).split()) != " ".join(message.split()):
            return {"error": "WhatsApp did not accept the complete message text; nothing was sent."}
        if not send:
            return {"ok": True, "action": "whatsapp_message", "sent": False, "ready": True, "contact": contact}

        before = await self._outgoing_count(page, message)
        send_button = page.get_by_role("button", name=re.compile(r"^send$", re.IGNORECASE))
        if await self._visible(send_button):
            await send_button.first.click()
        else:
            await composer.press("Enter")

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not await composer.is_visible():
                break
            if not (await composer.inner_text()).strip() and await self._outgoing_count(page, message) > before:
                return {"ok": True, "action": "whatsapp_message", "sent": True, "contact": contact}
            await page.wait_for_timeout(120)
        return {
            "error": "WhatsApp send outcome could not be verified. Check this chat before retrying to avoid a duplicate.",
            "delivery_uncertain": True,
            "contact": contact,
        }

    @staticmethod
    async def _wait_for_contact_result(page, contact: str) -> tuple[bool, str]:
        exact_text = page.get_by_text(contact, exact=True)
        try:
            await exact_text.first.wait_for(state="visible", timeout=8000)
        except Exception:
            return False, f"No exact WhatsApp search result appeared for '{contact}'. Nothing was sent."
        return True, ""

    @staticmethod
    async def _outgoing_count(page, message: str) -> int:
        for selector in (".message-out", '[data-testid="msg-container"]'):
            try:
                count = await page.locator(selector).filter(has_text=message).count()
                if count:
                    return count
            except Exception:
                continue
        return 0

    def close(self) -> None:
        if not self._thread or not self._thread.is_alive() or self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=5)


_controller = WhatsAppBrowserController()


def send_message(contact: str, message: str, send: bool = False) -> dict:
    return _controller.send_message(contact, message, send)


def close() -> None:
    _controller.close()
