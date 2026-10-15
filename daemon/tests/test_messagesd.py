"""Commands the terminal UI sends while pairing."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import messagesd


class DevicesCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_devices_asks_the_session_for_known_phones(self):
        class Session:
            def __init__(self) -> None:
                self.listed = 0

            async def list_phones(self) -> None:
                self.listed += 1

        session = Session()
        await messagesd.dispatch("devices", {}, None, session)
        self.assertEqual(session.listed, 1)

    async def test_devices_is_empty_without_bluetooth(self):
        events = []
        original = messagesd.emit
        messagesd.emit = events.append
        try:
            await messagesd.dispatch("devices", {}, None, None)
        finally:
            messagesd.emit = original
        self.assertEqual(events, [{"event": "devices", "devices": []}])


class StatusShapeTests(unittest.TestCase):
    def test_status_carries_contact_count(self):
        import tempfile

        from store import Store

        directory = tempfile.mkdtemp()
        store = Store(path=Path(directory) / "messages.db")
        try:
            self.assertEqual(messagesd.status_from_store(store, False, "")["contactCount"], 0)
        finally:
            store.close()
