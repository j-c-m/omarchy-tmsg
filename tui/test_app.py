"""The terminal UI asks the daemon to open a conversation and mark it read."""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from textual.pilot import Pilot  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("textual is not installed")

from tui.app import HINT, Composer, TmsgApp, ThreadList, Transcript, render_message


def _background_columns(text, prefix: str) -> list[list[int]]:
    """Columns of each line whose style starts with prefix. Drops the trailing blank line."""
    rows: list[list[int]] = []
    start = 0
    plain = text.plain
    while start < len(plain):
        end = plain.find("\n", start)
        if end == -1:
            end = len(plain)
        columns = []
        for index in range(start, end):
            if any(str(span.style).startswith(prefix) and span.start <= index < span.end for span in text.spans):
                columns.append(index - start)
        rows.append(columns)
        if end == len(plain):
            break
        start = end + 1
    if rows and not rows[-1] and plain.endswith("\n"):
        rows.pop()
    return rows
from tui.render import link_spans


class QueueClient:
    def __init__(self) -> None:
        self.sent = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def send(self, command: dict) -> None:
        self.sent.append(command)

    async def lines(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def push(self, event: dict) -> None:
        await self.queue.put(event)

    def close(self) -> None:
        self.closed = True


class ThemeTests(unittest.TestCase):
    def test_the_screen_uses_terminal_ansi_colors(self):
        app = TmsgApp(client=QueueClient())
        self.assertTrue(app.ansi_color)
        self.assertEqual(app.theme, "ansi-dark")


class LinkTests(unittest.TestCase):
    def test_a_bare_host_is_a_link(self):
        spans = link_spans("Stop him: voterep.co/j237n today")
        self.assertEqual(spans, [(10, 26, "https://voterep.co/j237n")])

    def test_a_rendered_message_keeps_the_link_target(self):
        text = render_message({"body": "see https://example.com/a now", "direction": "in", "timestamp": "2026-10-02T12:00:00"})
        styles = [str(span.style) for span in text.spans]
        self.assertTrue(any("https://example.com/a" in style for style in styles))

    def test_a_sent_message_is_on_the_right_without_you(self):
        text = render_message(
            {"body": "hi", "direction": "out", "timestamp": "2026-10-02T12:04:00"},
            width=10,
        )
        self.assertNotIn("you", text.plain)
        lines = text.plain.splitlines()
        self.assertTrue(any(line.endswith("12:04") for line in lines))
        self.assertTrue(all(len(line) <= 10 for line in lines))
        self.assertTrue(any("hi" in line and line.startswith(" ") for line in lines))
        sent = [str(span.style) for span in text.spans]
        self.assertTrue(sent)
        self.assertTrue(all("on blue" in style and "bright_black" not in style for style in sent))
        self.assertEqual(_background_columns(text, "black on blue"), [list(range(5, 10)), list(range(5, 10))])

    def test_a_received_message_is_black_on_bright_black(self):
        text = render_message({"body": "hello", "direction": "in", "timestamp": "2026-10-02T12:00:00"})
        styles = [str(span.style) for span in text.spans]
        self.assertTrue(styles)
        self.assertTrue(all(style.startswith("black on bright_black") for style in styles))
        wide = render_message(
            {"body": "hi", "direction": "in", "timestamp": "2026-10-02T12:00:00"},
            width=20,
        )
        self.assertEqual(_background_columns(wide, "black on bright_black"), [list(range(5)), list(range(5))])


class OpenTests(unittest.IsolatedAsyncioTestCase):
    async def test_j_moves_the_contact_while_the_transcript_is_focused(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({
                "event": "threads",
                "threads": [
                    {"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"},
                    {"key": "tel:+15550002222", "label": "Bo", "unread": 0, "preview": "yo"},
                ],
            })
            await pilot.pause()
            app.query_one("#transcript").focus()
            await pilot.press("j")
            await pilot.pause()
            self.assertEqual(app.query_one("#threads").highlighted, 1)
            self.assertEqual(app.open_key, "tel:+15550002222")
            await pilot.press("k")
            await pilot.pause()
            self.assertEqual(app.query_one("#threads").highlighted, 0)

    async def test_j_types_inside_a_reply(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({
                "event": "threads",
                "threads": [
                    {"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"},
                    {"key": "tel:+15550002222", "label": "Bo", "unread": 0, "preview": "yo"},
                ],
            })
            await pilot.pause()
            await pilot.press("r")
            await pilot.press("j")
            await pilot.pause()
            self.assertIn("j", app.query_one("#composer", Composer).text)
            self.assertEqual(app.query_one("#threads").highlighted, 0)

    async def test_moving_onto_a_conversation_marks_it_read_and_loads_it(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({
                "event": "threads",
                "threads": [
                    {"key": "tel:+15550001111", "label": "Ada", "unread": 1, "preview": "hi"},
                    {"key": "tel:+15550002222", "label": "Bo", "unread": 0, "preview": "yo"},
                ],
            })
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            await client.push({
                "event": "messages",
                "thread": "tel:+15550002222",
                "messages": [
                    {"body": "yo https://example.com/a", "direction": "in", "timestamp": "2026-10-02T12:00:00"},
                ],
            })
            await pilot.pause()
        commands = [item["cmd"] for item in client.sent]
        self.assertIn("messages", commands)
        self.assertIn("mark-read", commands)
        marked = [item for item in client.sent if item["cmd"] == "mark-read"]
        self.assertEqual(marked[-1]["thread"], "tel:+15550002222")

    async def test_a_notification_thread_opens_that_conversation(self):
        client = QueueClient()
        app = TmsgApp(client=client, thread="tel:+15550002222")
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({
                "event": "threads",
                "threads": [
                    {"key": "tel:+15550001111", "label": "Ada", "unread": 1, "preview": "hi"},
                    {"key": "tel:+15550002222", "label": "Bo", "unread": 1, "preview": "yo"},
                ],
            })
            await pilot.pause()
            self.assertEqual(app.open_key, "tel:+15550002222")
        marked = [item for item in client.sent if item["cmd"] == "mark-read"]
        self.assertEqual(marked[-1]["thread"], "tel:+15550002222")

    async def test_a_summon_focuses_this_screen_and_opens_the_conversation(self):
        from tui import instance

        focused: list[bool] = []
        original = instance.focus_this_window
        instance.focus_this_window = lambda: focused.append(True)
        client = QueueClient()
        app = TmsgApp(client=client)
        try:
            async with app.run_test(size=(100, 24)) as pilot:
                await client.push({
                    "event": "threads",
                    "threads": [
                        {"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"},
                        {"key": "tel:+15550002222", "label": "Bo", "unread": 1, "preview": "yo"},
                    ],
                })
                await pilot.pause()
                app.summon("tel:+15550002222")
                await pilot.pause()
                self.assertEqual(app.open_key, "tel:+15550002222")
                self.assertEqual(focused, [True])
        finally:
            instance.focus_this_window = original

    async def test_t_toggles_notification_text(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "connected", "phoneName": "Ada iPhone", "notifyBody": False}})
            await client.push({
                "event": "threads",
                "threads": [{"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"}],
            })
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            self.assertTrue(app.notify_body)
        toggles = [item for item in client.sent if item["cmd"] == "set-notify-body"]
        self.assertEqual(toggles, [{"cmd": "set-notify-body", "enabled": True}])

    async def test_contacts_list_is_twenty_characters_wide(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "threads", "threads": [{"key": "tel:+15550001111", "label": "Ada", "unread": 0}]})
            await pilot.pause()
            status = app.query_one("#status")
            hint = app.query_one("#hint")
            self.assertEqual(status.styles.padding.left, 1)
            self.assertEqual(hint.styles.padding.right, 1)
            self.assertEqual(status.styles.background.ansi, 8)
            self.assertEqual(status.styles.color.ansi, 15)
            self.assertEqual(hint.styles.background.ansi, 8)
            self.assertEqual(hint.styles.color.ansi, 15)
            top_gap, bottom_gap = app.query(".bar-gap")
            self.assertEqual(top_gap.region.height, 1)
            self.assertEqual(bottom_gap.region.height, 1)
            self.assertEqual(top_gap.styles.background.ansi, 0)
            self.assertEqual(bottom_gap.styles.background.ansi, 0)
            self.assertEqual(top_gap.region.y, status.region.bottom)
            body = app.query_one("#body")
            self.assertEqual(body.region.y, top_gap.region.bottom)
            self.assertEqual(bottom_gap.region.y, body.region.bottom)
            self.assertEqual(hint.region.y, bottom_gap.region.bottom)
            threads = app.query_one("#threads")
            self.assertEqual(threads.region.width, 23)
            self.assertEqual(threads.styles.background.ansi, 0)
            self.assertEqual(threads.styles.border.right[0], "solid")
            self.assertEqual(threads.styles.border.right[1].ansi, 8)
            self.assertFalse(threads.styles.border.left[0])
            self.assertNotEqual(app.query_one("#transcript").styles.background.ansi, 8)
            await pilot.press("n")
            await client.push({
                "event": "contacts",
                "contacts": [{"name": "A very long contact name indeed", "address": "+15550001111", "kind": "tel"}],
            })
            await pilot.pause()
            panel = app.query_one("#threads")
            self.assertEqual(panel.region.width, 23)
            self.assertEqual(panel.content_region.width, 20)
            self.assertEqual(panel.get_option_at_index(0).prompt, "A very long contact ")

    async def test_resizing_the_window_reflows_the_conversation(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({
                "event": "threads",
                "threads": [
                    {"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"},
                    {"key": "tel:+15550002222", "label": "Bo", "unread": 0, "preview": "yo"},
                ],
            })
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            await client.push({
                "event": "messages",
                "thread": "tel:+15550002222",
                "messages": [
                    {"body": "hello " * 40, "direction": "in", "timestamp": "2026-10-02T12:00:00"},
                ],
            })
            await pilot.pause()
            log = app.query_one("#transcript", Transcript)
            wide_lines = len(log.lines)
            wide = log.scrollable_content_region.width
            self.assertGreater(wide_lines, 1)
            self.assertGreater(wide, 40)
            await pilot.resize_terminal(52, 24)
            narrow = log.scrollable_content_region.width
            self.assertLess(narrow, wide)
            self.assertGreater(len(log.lines), wide_lines)
            self.assertLessEqual(max(strip.cell_length for strip in log.lines), narrow)
            await pilot.resize_terminal(100, 24)
            self.assertEqual(len(log.lines), wide_lines)
            self.assertLessEqual(max(strip.cell_length for strip in log.lines), log.scrollable_content_region.width)

    async def test_reply_starts_at_one_line_and_grows_to_five(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({
                "event": "threads",
                "threads": [{"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"}],
            })
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            composer = app.query_one("#composer", Composer)
            self.assertEqual(composer.region.height, 3)
            self.assertEqual(composer.content_region.height, 1)
            width = composer.wrap_width
            self.assertGreater(width, 5)
            composer.insert("x" * (width + 1))
            await pilot.pause()
            self.assertGreater(composer.content_region.height, 1)
            self.assertLessEqual(composer.region.height, Composer.MAX_LINES + 2)
            composer.insert("x" * (width * 8))
            await pilot.pause()
            self.assertEqual(composer.region.height, Composer.MAX_LINES + 2)
            self.assertEqual(composer.content_region.height, Composer.MAX_LINES)
            self.assertGreater(composer.wrapped_document.height, Composer.MAX_LINES)
            composer.clear()
            await pilot.pause()
            self.assertEqual(composer.region.height, 3)


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_unpaired_phone_enters_setup_and_scans(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await pilot.pause()
            self.assertEqual(app.mode, "setup")
            self.assertEqual(app.setup_step, "scan")
        self.assertIn({"cmd": "scan"}, client.sent)

    async def test_enter_in_scan_sends_another_scan(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({"event": "devices", "devices": []})
            await pilot.pause()
            self.assertEqual(app.setup_step, "scan")
            await pilot.press("enter")
            await pilot.pause()
        scans = [item for item in client.sent if item["cmd"] == "scan"]
        self.assertGreaterEqual(len(scans), 2)

    async def test_phones_move_to_pick_and_enter_pairs(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({
                "event": "devices",
                "devices": [
                    {"address": "AA:BB:CC:DD:EE:FF", "name": "Ada iPhone", "paired": False},
                    {"address": "11:22:33:44:55:66", "name": "Other Phone", "paired": False},
                ],
            })
            await pilot.pause()
            self.assertEqual(app.setup_step, "pick")
            panel = app.query_one("#threads")
            self.assertEqual(panel.get_option_at_index(0).prompt, "Ada iPhone")
            await pilot.press("enter")
            await pilot.pause()
        self.assertEqual(
            [item for item in client.sent if item["cmd"] == "pair"],
            [{"cmd": "pair", "address": "AA:BB:CC:DD:EE:FF"}],
        )

    async def test_pairing_code_shows_big_and_y_confirms(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({"event": "pair-code", "code": "123456"})
            await client.push({"event": "status", "status": {"state": "pairing", "detail": "Confirm this code on the iPhone too."}})
            await pilot.pause()
            self.assertEqual(app.setup_step, "code")
            self.assertIn("123 456", app.setup_panel)
            self.assertIn("iPhone", app.setup_panel)
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(app.pair_code, "")
        self.assertIn({"cmd": "confirm", "accept": True}, client.sent)

    async def test_switches_step_names_both_switches(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({"event": "status", "status": {"state": "waiting_for_switches", "detail": "Paired. Waiting for the iPhone switches."}})
            await pilot.pause()
            self.assertEqual(app.setup_step, "switches")
            self.assertIn("Show Notifications", app.setup_panel)
            self.assertIn("Sync Contacts", app.setup_panel)
            self.assertIn("on its own", app.setup_panel)

    async def test_connected_leaves_setup(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await pilot.pause()
            self.assertEqual(app.mode, "setup")
            await client.push({
                "event": "threads",
                "threads": [{"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"}],
            })
            await client.push({"event": "status", "status": {"state": "connected", "phoneName": "Ada iPhone"}})
            await pilot.pause()
            self.assertEqual(app.mode, "threads")
            self.assertEqual(str(app.query_one("#status").content), "Connected: Ada iPhone")

    async def test_connected_clears_panel_without_a_note(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({"event": "status", "status": {"state": "waiting_for_switches", "detail": "Paired."}})
            await pilot.pause()
            self.assertIn("Setup 4 of 4", app.setup_panel)
            await client.push({
                "event": "threads",
                "threads": [{"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"}],
            })
            await client.push({"event": "status", "status": {"state": "connected", "phoneName": "Ada iPhone"}})
            await pilot.pause()
            self.assertEqual(app.mode, "threads")
            log = app.query_one("#transcript", Transcript)
            panel = "\n".join(strip.text for strip in log.lines)
            self.assertNotIn("Setup 4 of 4", panel)
            self.assertEqual(panel, "")

    async def test_connected_without_contacts_nudges_switches(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({
                "event": "threads",
                "threads": [{"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"}],
            })
            await client.push({
                "event": "status",
                "status": {"state": "connected", "phoneName": "Ada iPhone", "contactCount": 0},
            })
            await pilot.pause()
            self.assertIn("Sync Contacts", str(app.query_one("#status").content))

    async def test_connected_with_contacts_shows_plain_status(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({
                "event": "threads",
                "threads": [{"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"}],
            })
            await client.push({
                "event": "status",
                "status": {"state": "connected", "phoneName": "Ada iPhone", "contactCount": 2},
            })
            await pilot.pause()
            self.assertEqual(str(app.query_one("#status").content), "Connected: Ada iPhone")

    async def test_escape_leaves_setup_and_stays_out(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await pilot.pause()
            self.assertEqual(app.mode, "setup")
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.mode, "threads")
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await pilot.pause()
            self.assertEqual(app.mode, "threads")

    async def test_enter_in_switches_reinvites_saved_phone(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({
                "event": "status",
                "status": {"state": "waiting_for_switches", "phoneAddress": "AA:BB:CC:DD:EE:FF", "detail": "Paired."},
            })
            await pilot.pause()
            self.assertEqual(app.setup_step, "switches")
            await pilot.press("enter")
            await pilot.pause()
        self.assertIn(
            {"cmd": "pair", "address": "AA:BB:CC:DD:EE:FF"},
            client.sent,
        )

    async def test_switches_panel_names_time_window(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({"event": "status", "status": {"state": "waiting_for_switches", "detail": "Paired."}})
            await pilot.pause()
            self.assertIn("two minutes", app.setup_panel)

    async def test_accept_answers_at_once_in_setup(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({"event": "pair-code", "code": "123456"})
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(app.setup_step, "confirming")
            self.assertIn("Finishing pairing", app.setup_panel)
            self.assertEqual(app.pair_code, "")
        self.assertIn({"cmd": "confirm", "accept": True}, client.sent)

    async def test_reject_answers_at_once_and_returns_to_pick(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({
                "event": "devices",
                "devices": [{"address": "AA:BB:CC:DD:EE:FF", "name": "Ada iPhone", "paired": False}],
            })
            await client.push({"event": "pair-code", "code": "123456"})
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(app.setup_step, "pick")
            self.assertIn("Code rejected.", app.setup_panel)
        self.assertIn({"cmd": "confirm", "accept": False}, client.sent)

    async def test_error_while_confirming_returns_to_pick(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({"event": "pair-code", "code": "123456"})
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            await client.push({"event": "error", "message": "Pairing failed."})
            await pilot.pause()
            self.assertEqual(app.setup_step, "pick")
            self.assertIn("Pairing failed.", app.setup_panel)

    async def test_error_keeps_setup_with_retry_hint(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({
                "event": "devices",
                "devices": [{"address": "AA:BB:CC:DD:EE:FF", "name": "Ada iPhone", "paired": False}],
            })
            await pilot.pause()
            await client.push({"event": "error", "message": "Pairing failed."})
            await pilot.pause()
            self.assertEqual(app.mode, "setup")
            self.assertIn("Pairing failed.", app.setup_panel)


    async def test_a_phone_filter_hides_other_phones(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "status", "status": {"state": "not_paired", "detail": ""}})
            await client.push({
                "event": "devices",
                "devices": [
                    {"address": "AA:BB:CC:DD:EE:FF", "name": "Ada iPhone", "paired": False},
                    {"address": "11:22:33:44:55:66", "name": "Other Phone", "paired": False},
                ],
            })
            await pilot.pause()
            self.assertEqual(app.setup_step, "pick")
            await pilot.press("slash")
            await pilot.press("o", "t", "h")
            await pilot.pause()
            panel = app.query_one("#threads")
            self.assertEqual(panel.option_count, 1)
            self.assertEqual(panel.get_option_at_index(0).prompt, "Other Phone")

    async def test_a_pairing_code_accepts_rejects_and_cancels(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({"event": "pair-code", "code": "123456"})
            await client.push({"event": "status", "status": {"state": "pairing", "detail": "Confirm this code on the iPhone too."}})
            await pilot.pause()
            self.assertEqual(str(app.query_one("#status").content), "Pairing code 123456")
            self.assertEqual(str(app.query_one("#hint").content), "y accept    n reject    esc cancel")
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(app.pair_code, "")
            await client.push({"event": "pair-code", "code": "654321"})
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await client.push({"event": "pair-code", "code": "111222"})
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        confirms = [item for item in client.sent if item["cmd"] == "confirm"]
        self.assertEqual(confirms, [
            {"cmd": "confirm", "accept": True},
            {"cmd": "confirm", "accept": False},
        ])
        self.assertIn({"cmd": "cancel"}, client.sent)
        self.assertEqual(app.pair_code, "")


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_dropped_daemon_reconnects_and_reloads(self):
        from unittest import mock

        first = QueueClient()
        second = QueueClient()
        attempts: list[bool] = []

        async def fake_connect(standalone: bool = False):
            attempts.append(standalone)
            if len(attempts) < 3:
                raise OSError("daemon is restarting")
            return second

        app = TmsgApp(client=first)
        threads = [{"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"}]
        with (
            mock.patch("tui.app.connect", fake_connect),
            mock.patch("tui.app.RECONNECT_DELAY", 0),
        ):
            async with app.run_test(size=(100, 24)) as pilot:
                await first.push({"event": "threads", "threads": threads})
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.open_key, "tel:+15550001111")
                await first.push(None)  # daemon died; the stream ends
                for _ in range(200):
                    if app.client is second:
                        break
                    await asyncio.sleep(0.01)
                self.assertIs(app.client, second)
                await second.push({"event": "threads", "threads": threads})
                await pilot.pause()
                self.assertEqual(app.threads, threads)
        # Two failures retried, then the fresh connection resubscribed.
        self.assertEqual(attempts, [False, False, False])
        self.assertTrue(first.closed)
        commands = [(item["cmd"], item.get("thread", "")) for item in second.sent]
        self.assertIn(("status", ""), commands)
        self.assertIn(("threads", ""), commands)
        self.assertIn(("messages", "tel:+15550001111"), commands)


class ComposerTests(unittest.IsolatedAsyncioTestCase):
    async def _open_reply(self, app, client, pilot):
        await client.push({
            "event": "threads",
            "threads": [{"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"}],
        })
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        return app.query_one("#composer", Composer)

    async def test_shift_enter_inserts_a_newline_and_enter_sends(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            composer = await self._open_reply(app, client, pilot)
            await pilot.press("h", "i")
            await pilot.pause()
            await pilot.press("shift+enter")
            await pilot.pause()
            await pilot.press("y", "o")
            await pilot.pause()
            self.assertEqual(composer.text, "hi\nyo")
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(composer.display)
            self.assertEqual(composer.text, "")
            self.assertTrue(composer.has_focus)
        sends = [item for item in client.sent if item["cmd"] == "send"]
        self.assertEqual(sends[-1]["body"], "hi\nyo")

    async def test_send_keeps_the_box_active_until_escape(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            composer = await self._open_reply(app, client, pilot)
            await pilot.press("h", "i")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(composer.text, "")
            self.assertTrue(composer.has_focus)
            await pilot.press("y", "o")
            await pilot.pause()
            self.assertEqual(composer.text, "yo")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(composer.text, "")
            self.assertTrue(composer.has_focus)
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(app.query_one("#threads", ThreadList).has_focus)
        sends = [item for item in client.sent if item["cmd"] == "send"]
        self.assertEqual([item["body"] for item in sends], ["hi", "yo"])

    async def test_ctrl_j_inserts_a_newline_where_shift_enter_is_missing(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            composer = await self._open_reply(app, client, pilot)
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("ctrl+j")
            await pilot.pause()
            self.assertEqual(composer.text, "a\n")
            self.assertTrue(composer.display)

    async def test_a_too_long_message_is_blocked_with_a_count(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            composer = await self._open_reply(app, client, pilot)
            composer.insert("x" * 1001)
            await pilot.pause()
            self.assertEqual(str(composer.border_subtitle), "1001/1000")
            await pilot.press("enter")
            await pilot.pause()
            self.assertNotIn("send", [item["cmd"] for item in client.sent])
            self.assertTrue(composer.display)
            self.assertIn("too long", str(app.query_one("#hint").content))

    async def test_composer_shows_title_hint_and_counter(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            composer = await self._open_reply(app, client, pilot)
            self.assertEqual(str(composer.border_title), "Reply")
            self.assertEqual(str(composer.border_subtitle), "0/1000")
            self.assertEqual(
                str(app.query_one("#hint").content),
                "enter send    shift+enter newline    esc cancel",
            )
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(composer.display)
            self.assertEqual(composer.text, "")
            self.assertTrue(app.query_one("#threads", ThreadList).has_focus)
            self.assertEqual(str(app.query_one("#hint").content), HINT)

    async def test_reply_targets_the_open_conversation(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({
                "event": "threads",
                "threads": [{"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"}],
            })
            await pilot.pause()
            self.assertEqual(app.open_key, "tel:+15550001111")
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("y", "o")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        sends = [item for item in client.sent if item["cmd"] == "send"]
        self.assertEqual(
            sends[-1], {"cmd": "send", "thread": "tel:+15550001111", "body": "yo"}
        )

    async def test_send_with_cleared_open_key_falls_back_to_highlight(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await client.push({
                "event": "threads",
                "threads": [{"key": "tel:+15550001111", "label": "Ada", "unread": 0, "preview": "hi"}],
            })
            await pilot.pause()
            app.open_key = ""
            composer = app.query_one("#composer", Composer)
            composer.focus()
            await pilot.pause()
            composer.insert("yo")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.open_key, "tel:+15550001111")
        sends = [item for item in client.sent if item["cmd"] == "send"]
        self.assertEqual(
            sends[-1], {"cmd": "send", "thread": "tel:+15550001111", "body": "yo"}
        )
        loads = [item for item in client.sent if item["cmd"] == "messages"]
        self.assertIn({"cmd": "messages", "thread": "tel:+15550001111"}, loads)

    async def test_send_with_no_conversation_keeps_the_text(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            composer = app.query_one("#composer", Composer)
            composer.focus()
            await pilot.pause()
            composer.insert("hello?")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertNotIn("send", [item["cmd"] for item in client.sent])
            self.assertEqual(composer.text, "hello?")
            self.assertIn("open a conversation", str(app.query_one("#hint").content))

    async def test_setup_never_sends_to_a_stale_thread(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            app.open_key = "tel:+15550001111"
            app.mode = "setup"
            composer = app.query_one("#composer", Composer)
            composer.focus()
            await pilot.pause()
            composer.insert("hi")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertNotIn("send", [item["cmd"] for item in client.sent])
            self.assertEqual(composer.text, "hi")

    async def test_a_new_message_titles_the_box_differently(self):
        client = QueueClient()
        app = TmsgApp(client=client)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("n")
            await pilot.pause()
            await client.push({
                "event": "contacts",
                "contacts": [{"name": "Ada", "address": "+15550001111", "kind": "tel"}],
            })
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            composer = app.query_one("#composer", Composer)
            self.assertTrue(composer.display)
            self.assertEqual(str(composer.border_title), "New message")


if __name__ == "__main__":
    unittest.main()
