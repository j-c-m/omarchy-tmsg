"""Tests for MAP session helpers (no phone needed)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bluetooth import ADVERT_PATH, AGENT_PATH, BluetoothSession, _session_transfer_paths
from store import Store


class SessionTransferTests(unittest.TestCase):
    def test_a_transfer_on_the_map_session_is_kept(self):
        objects = {
            "/org/bluez/obex/session1/transfer1": {"org.bluez.obex.Transfer1": {}},
        }
        self.assertEqual(
            _session_transfer_paths("/org/bluez/obex/session1", objects),
            ["/org/bluez/obex/session1/transfer1"],
        )

    def test_a_message_object_and_a_pbap_transfer_are_not(self):
        objects = {
            "/org/bluez/obex/session1/message42": {"org.bluez.obex.Message1": {}},
            "/org/bluez/obex/session2/transfer1": {"org.bluez.obex.Transfer1": {}},
        }
        self.assertEqual(_session_transfer_paths("/org/bluez/obex/session1", objects), [])


class DbusPathTests(unittest.TestCase):
    def test_agent_paths_share_plugin_namespace(self):
        for path in (AGENT_PATH, ADVERT_PATH):
            self.assertTrue(path.startswith("/io/github/j_c_m/tmsg/"))
            # Hyphens are illegal in a D-Bus object path element.
            self.assertNotIn("-", path)
            self.assertTrue(all(part for part in path.split("/")[1:]))


class PairBondTests(unittest.IsolatedAsyncioTestCase):
    async def _session(self, directory: str) -> tuple:
        from store import Store

        store = Store(path=Path(directory) / "messages.db")
        events: list[dict] = []
        session = BluetoothSession(store, events.append)
        device = mock.AsyncMock()
        device.get_paired.return_value = False
        session._device = mock.AsyncMock(return_value=device)
        session._device_name = mock.AsyncMock(return_value="Ada iPhone")
        for name in ("_register_agent", "_unregister_agent", "_untrust", "_start_advert", "_open_profiles"):
            setattr(session, name, mock.AsyncMock())
        session._schedule_advert_stop = mock.Mock()
        return session, store, events, device

    async def test_pair_bonds_without_connecting(self):
        directory = tempfile.mkdtemp()
        session, store, _events, device = await self._session(directory)
        try:
            await session.pair("AA:BB:CC:DD:EE:FF")
            device.call_pair.assert_awaited_once()
            device.call_connect.assert_not_awaited()
            self.assertEqual(store.settings()["phoneAddress"], "AA:BB:CC:DD:EE:FF")
            self.assertEqual(session.state, "waiting_for_switches")
        finally:
            store.close()


class IngestPreviewTests(unittest.TestCase):
    def test_two_previews_in_one_second_stay_distinct(self):
        directory = tempfile.mkdtemp()
        store = Store(path=Path(directory) / "messages.db")
        try:
            session = BluetoothSession(store, lambda event: None)
            fields = {"SenderAddress": "+15551234567", "Timestamp": "20261002T103000"}
            session._ingest(dict(fields), path="", announce=False)
            session._ingest(dict(fields), path="", announce=False)
            self.assertEqual(len(store.messages_for("tel:+15551234567")), 2)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
