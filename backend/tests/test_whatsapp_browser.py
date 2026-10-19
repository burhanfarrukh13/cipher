import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from skills.whatsapp_browser import WhatsAppBrowserController


class WhatsAppBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.controller = WhatsAppBrowserController()
        self.page = MagicMock()
        self.page.bring_to_front = AsyncMock()

    async def test_login_required_is_reported_without_composing(self):
        with patch.object(
            self.controller,
            "_wait_for_search_or_login",
            new=AsyncMock(return_value=(None, True)),
        ):
            result = await self.controller._message(
                self.page, "Hashir Qureshi", "Hello", True
            )

        self.assertTrue(result["login_required"])
        self.assertIn("QR code", result["error"])
        self.page.get_by_role.assert_not_called()

    async def test_waits_for_search_field_to_finish_loading(self):
        search = MagicMock()
        with (
            patch.object(
                WhatsAppBrowserController,
                "_field",
                new=AsyncMock(side_effect=[None, None, search]),
            ),
            patch.object(
                WhatsAppBrowserController,
                "_login_required",
                new=AsyncMock(return_value=False),
            ),
        ):
            self.page.wait_for_timeout = AsyncMock()
            result = await WhatsAppBrowserController._wait_for_search_or_login(
                self.page
            )

        self.assertEqual(result, (search, False))
        self.assertEqual(self.page.wait_for_timeout.await_count, 2)

    async def test_missing_exact_result_stops_before_contact_selection(self):
        search = MagicMock()
        search.fill = AsyncMock()
        with (
            patch.object(
                self.controller,
                "_wait_for_search_or_login",
                new=AsyncMock(return_value=(search, False)),
            ),
            patch.object(
                self.controller,
                "_wait_for_contact_result",
                new=AsyncMock(return_value=(False, "No exact result")),
            ),
            patch.object(self.controller, "_select_contact_result") as select,
        ):
            result = await self.controller._message(
                self.page, "Hashir Qureshi", "Hello", True
            )

        self.assertEqual(result, {"error": "No exact result"})
        select.assert_not_called()
        search.fill.assert_awaited_once_with("Hashir Qureshi")

    async def test_chat_header_mismatch_stops_before_composing(self):
        search = MagicMock()
        search.fill = AsyncMock()
        self.page.wait_for_function = AsyncMock(
            side_effect=TimeoutError("header not found")
        )
        with (
            patch.object(
                self.controller,
                "_wait_for_search_or_login",
                new=AsyncMock(return_value=(search, False)),
            ),
            patch.object(
                self.controller,
                "_wait_for_contact_result",
                new=AsyncMock(return_value=(True, "")),
            ),
            patch.object(
                self.controller,
                "_select_contact_result",
                new=AsyncMock(return_value=(True, "")),
            ),
            patch.object(
                self.controller,
                "_header_matches",
                new=AsyncMock(return_value=False),
            ),
        ):
            result = await self.controller._message(
                self.page, "Hashir Qureshi", "Hello", True
            )

        self.assertIn("header", result["error"].lower())
        self.assertIn("Nothing was typed or sent", result["error"])
        self.page.get_by_role.assert_not_called()

    async def test_exact_unique_result_is_selected(self):
        row = MagicMock()
        row.is_visible = AsyncMock(return_value=True)
        row.click = AsyncMock()
        exact_text = MagicMock()
        list_rows = MagicMock()
        list_rows.filter.return_value = list_rows
        list_rows.count = AsyncMock(return_value=1)
        list_rows.nth.return_value = row
        grid_rows = MagicMock()
        grid_rows.filter.return_value = grid_rows
        grid_rows.count = AsyncMock(return_value=0)
        self.page.get_by_text.return_value = exact_text
        self.page.get_by_role.side_effect = [list_rows, grid_rows]

        selected, error = await WhatsAppBrowserController._select_contact_result(
            self.page, "Hashir Qureshi"
        )

        self.assertTrue(selected)
        self.assertEqual(error, "")
        row.click.assert_awaited_once()

    async def test_ambiguous_exact_results_are_not_selected(self):
        rows = [MagicMock(), MagicMock()]
        for row in rows:
            row.is_visible = AsyncMock(return_value=True)
            row.click = AsyncMock()
        candidate_rows = MagicMock()
        candidate_rows.filter.return_value = candidate_rows
        candidate_rows.count = AsyncMock(return_value=2)
        candidate_rows.nth.side_effect = rows
        empty_rows = MagicMock()
        empty_rows.filter.return_value = empty_rows
        empty_rows.count = AsyncMock(return_value=0)
        self.page.get_by_text.return_value = MagicMock()
        self.page.get_by_role.side_effect = [candidate_rows, empty_rows]

        selected, error = await WhatsAppBrowserController._select_contact_result(
            self.page, "Hashir Qureshi"
        )

        self.assertFalse(selected)
        self.assertIn("Multiple", error)
        for row in rows:
            row.click.assert_not_awaited()

    async def test_successful_send_requires_verified_outgoing_message(self):
        search = MagicMock()
        search.fill = AsyncMock()
        composer = MagicMock()
        composer.fill = AsyncMock()
        composer.inner_text = AsyncMock(side_effect=["Hello there", ""])
        composer.is_visible = AsyncMock(return_value=True)
        button = MagicMock()
        button.click = AsyncMock()
        self.page.get_by_role.return_value.first = button
        with (
            patch.object(
                self.controller,
                "_wait_for_search_or_login",
                new=AsyncMock(return_value=(search, False)),
            ),
            patch.object(
                self.controller,
                "_field",
                new=AsyncMock(return_value=composer),
            ),
            patch.object(
                self.controller,
                "_wait_for_contact_result",
                new=AsyncMock(return_value=(True, "")),
            ),
            patch.object(
                self.controller,
                "_select_contact_result",
                new=AsyncMock(return_value=(True, "")),
            ),
            patch.object(
                self.controller,
                "_header_matches",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                self.controller,
                "_visible",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                self.controller,
                "_outgoing_count",
                new=AsyncMock(side_effect=[0, 1]),
            ),
        ):
            result = await self.controller._message(
                self.page, "Hashir Qureshi", "Hello there", True
            )

        self.assertTrue(result["sent"])
        self.assertEqual(result["contact"], "Hashir Qureshi")
        composer.fill.assert_awaited_once_with("Hello there")
        button.click.assert_awaited_once()

    async def test_uncertain_delivery_is_never_reported_as_success(self):
        search = MagicMock()
        search.fill = AsyncMock()
        composer = MagicMock()
        composer.fill = AsyncMock()
        composer.inner_text = AsyncMock(return_value="Hello there")
        button = MagicMock()
        button.click = AsyncMock()
        self.page.get_by_role.return_value.first = button
        with (
            patch.object(
                self.controller,
                "_wait_for_search_or_login",
                new=AsyncMock(return_value=(search, False)),
            ),
            patch.object(
                self.controller,
                "_field",
                new=AsyncMock(return_value=composer),
            ),
            patch.object(
                self.controller,
                "_wait_for_contact_result",
                new=AsyncMock(return_value=(True, "")),
            ),
            patch.object(
                self.controller,
                "_select_contact_result",
                new=AsyncMock(return_value=(True, "")),
            ),
            patch.object(
                self.controller,
                "_header_matches",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                self.controller,
                "_visible",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                self.controller,
                "_outgoing_count",
                new=AsyncMock(return_value=0),
            ),
            patch("skills.whatsapp_browser.time.monotonic", side_effect=[0, 9]),
        ):
            result = await self.controller._message(
                self.page, "Hashir Qureshi", "Hello there", True
            )

        self.assertTrue(result["delivery_uncertain"])
        self.assertNotIn("sent", result)
        self.assertIn("Check this chat", result["error"])

    @unittest.skipUnless(os.name == "nt", "Windows uses a distinct Proactor loop")
    async def test_worker_uses_proactor_even_under_selector_policy(self):
        original_policy = asyncio.get_event_loop_policy()
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            loop = self.controller._new_worker_event_loop()
            try:
                self.assertIsInstance(loop, asyncio.ProactorEventLoop)
            finally:
                loop.close()
        finally:
            asyncio.set_event_loop_policy(original_policy)


if __name__ == "__main__":
    unittest.main()
