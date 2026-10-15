"""Tests for the local database. They use a temporary directory, not your real texts."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.chmod(self._tmp.name, 0o700)
        self.db = store.Store(Path(self._tmp.name) / "messages.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_database_file_is_private(self):
        mode = os.stat(self.db.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_directories_use_plugin_namespace(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as runtime:
            env = {"XDG_DATA_HOME": data, "XDG_RUNTIME_DIR": runtime}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(store.data_dir().name, "io.github.j-c-m.tmsg")
                self.assertEqual(store.runtime_dir().name, "io.github.j-c-m.tmsg")

    def test_threads_group_and_present_attachments(self):
        self.db.set_own_number("+15550001111")
        created, is_new = self.db.upsert_message(
            external_id="map-1",
            address="+1 (555) 222-3333",
            body="hello",
            direction="in",
            timestamp="2026-10-02T10:00:00",
            read=False,
            has_attachment=True,
            body_complete=True,
            peer_label="Ada",
        )
        self.assertTrue(is_new)
        self.assertIn("attachment omitted", created["body"])
        self.assertNotIn("BEGIN:", created["body"])

        again, is_new = self.db.upsert_message(
            external_id="map-1",
            address="+15552223333",
            body="hello",
            direction="in",
            timestamp="2026-10-02T10:00:00",
            read=False,
            has_attachment=True,
            body_complete=True,
            peer_label="Ada",
        )
        self.assertFalse(is_new)
        self.assertEqual(again["id"], created["id"])

        self.db.upsert_message(
            external_id="local-1",
            address="+15552223333",
            body="reply",
            direction="out",
            timestamp="2026-10-02T10:05:00",
            read=True,
            has_attachment=False,
            body_complete=True,
        )
        threads = self.db.threads()
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["unread"], 1)
        self.assertEqual(threads[0]["label"], "Ada")
        self.db.mark_thread_read(threads[0]["key"])
        self.assertEqual(self.db.unread_count(), 0)

    def test_unread_conversations_sort_first(self):
        self.db.upsert_message(
            external_id="old-unread",
            address="+15551110000",
            body="older",
            direction="in",
            timestamp="2026-10-01T09:00:00",
            read=False,
            has_attachment=False,
            body_complete=True,
            peer_label="Older",
        )
        self.db.upsert_message(
            external_id="new-read",
            address="+15552220000",
            body="newer",
            direction="in",
            timestamp="2026-10-02T09:00:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="Newer",
        )
        labels = [row["label"] for row in self.db.threads()]
        self.assertEqual(labels, ["Older", "Newer"])

    def test_contact_search(self):
        self.db.replace_contacts(
            [
                {"name": "Ada Lovelace", "address": "+15551234567", "kind": "tel"},
                {"name": "Grace", "address": "grace@example.com", "kind": "email"},
            ]
        )
        found = self.db.search_contacts("ada")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["address"], "+15551234567")

    def test_one_person_is_one_row(self):
        self.db.replace_contacts(
            [
                {"name": "Sam Rivera", "address": "a@example.com", "kind": "email"},
                {"name": "Sam Rivera", "address": "b@example.com", "kind": "email"},
                {"name": "Ada", "address": "ada@example.com", "kind": "email"},
                {"name": "Ada", "address": "+15551234567", "kind": "tel"},
            ]
        )
        sam = self.db.search_contacts("sam")
        self.assertEqual(len(sam), 1)
        self.assertEqual(sam[0]["kind"], "email")
        ada = self.db.search_contacts("ada")
        self.assertEqual(len(ada), 1)
        self.assertEqual(ada[0]["address"], "+15551234567")

    def test_a_conversation_is_someone_you_can_text(self):
        self.db.upsert_message(
            external_id="map-9",
            address="+15557654321",
            body="hi",
            direction="in",
            timestamp="2026-10-02T11:00:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="Sam",
        )
        found = self.db.search_contacts("sam")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Sam")
        self.assertEqual(found[0]["address"], "+15557654321")

    def test_contact_name_replaces_a_bare_number(self):
        self.db.upsert_message(
            external_id="map-8",
            address="+15557654321",
            body="hi",
            direction="in",
            timestamp="2026-10-02T11:00:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="+15557654321",
        )
        self.db.replace_contacts(
            [{"name": "Sam", "address": "+15557654321", "kind": "tel"}]
        )
        self.assertEqual(self.db.apply_contact_names(), 1)
        self.assertEqual(self.db.threads()[0]["label"], "Sam")

    def test_an_email_joins_that_persons_phone_conversation(self):
        self.db.replace_contacts(
            [
                {"name": "Quinn", "address": "+15551110000", "kind": "tel"},
                {"name": "Quinn", "address": "quinn@example.com", "kind": "email"},
                {"name": "Ada", "address": "ada@example.com", "kind": "email"},
            ]
        )
        self.db.upsert_message(
            external_id="phone-1",
            address="+15551110000",
            body="from the phone",
            direction="in",
            timestamp="2026-10-02T11:00:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="Quinn",
        )
        self.db.upsert_message(
            external_id="mail-1",
            address="quinn@example.com",
            body="from email",
            direction="in",
            timestamp="2026-10-02T11:05:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="Quinn",
        )
        self.db.upsert_message(
            external_id="ada-1",
            address="ada@example.com",
            body="only email",
            direction="in",
            timestamp="2026-10-02T11:06:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="Ada",
        )
        labels = [row["label"] for row in self.db.threads()]
        self.assertEqual(labels, ["Ada", "Quinn"])
        quinn = self.db.threads()[1]
        self.assertTrue(quinn["key"].startswith("tel:"))
        bodies = [row["body"] for row in self.db.messages_for(quinn["key"])]
        self.assertEqual(bodies, ["from the phone", "from email"])

    def test_a_deleted_conversation_does_not_come_back(self):
        self.db.upsert_message(
            external_id="map-keep",
            address="+15550001111",
            body="keep me",
            direction="in",
            timestamp="2026-10-02T12:00:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="Keep",
        )
        self.db.upsert_message(
            external_id="map-gone",
            address="+15550002222",
            body="gone",
            direction="in",
            timestamp="2026-10-02T12:05:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="+15550002222",
        )
        self.assertEqual(self.db.delete_thread("tel:+15550002222"), 1)
        self.assertEqual([row["label"] for row in self.db.threads()], ["Keep"])
        again, created = self.db.upsert_message(
            external_id="map-gone",
            address="+15550002222",
            body="gone",
            direction="in",
            timestamp="2026-10-02T12:05:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="+15550002222",
        )
        self.assertIsNone(again)
        self.assertFalse(created)
        self.assertEqual([row["label"] for row in self.db.threads()], ["Keep"])
        fresh, created = self.db.upsert_message(
            external_id="map-new",
            address="+15550002222",
            body="a new text",
            direction="in",
            timestamp="2026-10-02T13:00:00",
            read=False,
            has_attachment=False,
            body_complete=True,
            peer_label="+15550002222",
        )
        self.assertTrue(created)
        self.assertEqual(fresh["thread"], "tel:+15550002222")

    def test_a_later_address_replaces_a_name_only_thread(self):
        self.db.upsert_message(
            external_id="map-raw",
            address="Ada",
            body="preview",
            direction="out",
            timestamp="2026-10-02T10:00:00",
            read=False,
            has_attachment=False,
            body_complete=False,
            peer_label="Ada",
        )
        row, is_new = self.db.upsert_message(
            external_id="map-raw",
            address="+15551234567",
            body="hello",
            direction="in",
            timestamp="2026-10-02T10:00:00",
            read=False,
            has_attachment=False,
            body_complete=True,
            peer_label="Ada",
        )
        self.assertFalse(is_new)
        self.assertEqual(row["thread"], "tel:+15551234567")
        self.assertEqual(row["direction"], "in")
        again, _ = self.db.upsert_message(
            external_id="map-raw",
            address="+15551234567",
            body="hello",
            direction="out",
            timestamp="2026-10-02T10:00:00",
            read=False,
            has_attachment=False,
            body_complete=True,
            peer_label="Ada",
        )
        self.assertEqual(again["direction"], "in")

    def test_own_number_fills_in_a_national_thread(self):
        self.db.upsert_message(
            external_id="map-nat",
            address="5551234567",
            body="hi",
            direction="in",
            timestamp="2026-10-02T10:00:00",
            read=True,
            has_attachment=False,
            body_complete=True,
        )
        self.assertTrue(self.db.threads()[0]["key"].startswith("raw:"))
        self.db.set_own_number("+15550001111")
        self.assertEqual(self.db.threads()[0]["key"], "tel:+15551234567")

    def test_same_second_preview_is_the_later_text(self):
        stamp = "2026-10-02T10:00:00"
        self.db.upsert_message(
            external_id="early",
            address="+15551110000",
            body="earlier",
            direction="in",
            timestamp=stamp,
            read=True,
            has_attachment=False,
            body_complete=True,
        )
        self.db.upsert_message(
            external_id="late",
            address="+15551110000",
            body="later",
            direction="in",
            timestamp=stamp,
            read=True,
            has_attachment=False,
            body_complete=True,
        )
        self.assertEqual(self.db.threads()[0]["preview"], "later")


if __name__ == "__main__":
    unittest.main()
