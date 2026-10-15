"""Tests for MAP session helpers (no phone needed)."""

import sys
import tempfile
import unittest
from pathlib import Path

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
