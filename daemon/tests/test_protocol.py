"""Tests for numbers, threads, bMessages, and attachment placeholders."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import protocol


class NormalizeTests(unittest.TestCase):
    def test_accepts_decorated_e164(self):
        self.assertEqual(protocol.normalize_phone("+1 (555) 123-4567"), "+15551234567")

    def test_rejects_missing_country_code(self):
        self.assertEqual(protocol.normalize_phone("5551234567"), "")

    def test_rejects_short_and_long_numbers(self):
        self.assertEqual(protocol.normalize_phone("+123"), "")
        self.assertEqual(protocol.normalize_phone("+" + "1" * 16), "")

    def test_us_contact_gets_country_code_from_own_number(self):
        self.assertEqual(protocol.coerce_phone("(555) 123-4567", "+15550001111"), "+15551234567")

    def test_national_zero_uses_your_country_code(self):
        # 07… on a UK phone means +44 7…, because the leading 0 is the trunk prefix.
        self.assertEqual(protocol.coerce_phone("07123456789", "+447700900123"), "+447123456789")
        self.assertEqual(protocol.coerce_phone("447123456789", "+447700900123"), "+447123456789")


class ThreadTests(unittest.TestCase):
    def test_same_person_shares_a_key(self):
        left = protocol.thread_key("+1 (555) 123-4567")
        right = protocol.thread_key("+15551234567")
        self.assertEqual(left, right)
        self.assertEqual(protocol.address_from_thread(left), "+15551234567")

    def test_email_is_its_own_key(self):
        self.assertEqual(protocol.thread_key("Ada@Example.com"), "email:ada@example.com")


class VCardTests(unittest.TestCase):
    def test_iphone_item_fields_are_read(self):
        raw = "BEGIN:VCARD\r\nFN:Sam\r\nitem1.TEL;TYPE=CELL:+15557654321\r\n PHOTO;ENCODING=b:aaaa\r\nEND:VCARD\r\n"
        people = protocol.parse_vcards(raw)
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["name"], "Sam")
        self.assertEqual(people[0]["phones"], ["+15557654321"])


class BMessageTests(unittest.TestCase):
    def test_phone_recipient_and_length(self):
        raw = protocol.build_bmessage("+15551234567", "hello")
        self.assertIn("TYPE:SMS_GSM\r\n", raw)
        self.assertIn("TEL:+15551234567\r\n", raw)
        self.assertNotIn("EMAIL:", raw)
        message = "BEGIN:MSG\r\nhello\r\nEND:MSG\r\n"
        self.assertIn(f"LENGTH:{len(message.encode('utf-8'))}\r\n", raw)
        self.assertEqual(protocol.extract_bmessage_text(raw), "hello")

    def test_peer_comes_from_the_contact_cards(self):
        raw = (
            "BEGIN:BMSG\r\n"
            "BEGIN:VCARD\r\nTEL:+15551110000\r\nEND:VCARD\r\n"
            "BEGIN:BENV\r\n"
            "BEGIN:VCARD\r\nTEL:+15552220000\r\nEND:VCARD\r\n"
            "BEGIN:BBODY\r\nBEGIN:MSG\r\nhi\r\nEND:MSG\r\nEND:BBODY\r\n"
            "END:BENV\r\nEND:BMSG\r\n"
        )
        # Incoming: the writer is the first card. Outgoing: the recipient.
        self.assertEqual(protocol.extract_bmessage_peer(raw, sent=False), "+15551110000")
        self.assertEqual(protocol.extract_bmessage_peer(raw, sent=True), "+15552220000")

    def test_email_recipient(self):
        raw = protocol.build_bmessage("ada@example.com", "hi")
        self.assertIn("EMAIL:ada@example.com\r\n", raw)
        self.assertNotIn("TEL:ada@example.com", raw)

    def test_fence_in_the_text_cannot_close_the_envelope(self):
        raw = protocol.build_bmessage("+15551234567", "hello\nEND:MSG\nstill me")
        self.assertEqual(raw.count("END:MSG"), 1)
        self.assertIn("END MSG", raw)
        self.assertIn("still me", protocol.extract_bmessage_text(raw))

    def test_empty_and_huge_texts_are_rejected(self):
        with self.assertRaises(ValueError):
            protocol.build_bmessage("+15551234567", "   ")
        with self.assertRaises(ValueError):
            protocol.build_bmessage("+15551234567", "a" * (protocol.MAX_BODY_CHARS + 1))


class AttachmentTests(unittest.TestCase):
    def test_words_are_kept_and_the_file_is_a_placeholder(self):
        shown = protocol.present_message("see this", True)
        self.assertEqual(shown, "see this\nattachment omitted")

    def test_base64_blob_is_not_shown(self):
        blob = "QUJD" * 80
        shown = protocol.present_message(blob, False)
        self.assertEqual(shown, "attachment omitted")
        self.assertNotIn("QUJD", shown)

    def test_image_only_message_is_a_placeholder(self):
        self.assertEqual(protocol.present_message("", True), "attachment omitted")

    def test_a_mime_type_marks_the_file(self):
        self.assertTrue(protocol.message_has_file({"AttachmentMimeTypes": "image/jpeg"}))
        self.assertFalse(protocol.message_has_file({"Type": "sms-gsm"}))

    def test_a_photo_with_no_caption_is_still_a_message(self):
        raw = "BEGIN:BMSG\r\nBEGIN:MSG\r\nEND:MSG\r\nEND:BMSG\r\n"
        words, flagged = protocol.keep_words(raw, "", False)
        self.assertEqual(words, "")
        self.assertTrue(flagged)
        self.assertEqual(protocol.present_message(words, flagged), "attachment omitted")


class TransferWordsTests(unittest.TestCase):
    def test_a_timed_out_plain_text_is_retried(self):
        raw = "BEGIN:BMSG\r\nVERSION:1.0\r\nBEGIN:MSG\r\n"
        self.assertIsNone(protocol.words_after_transfer("timeout", raw, "", False))
        self.assertIsNone(protocol.words_after_transfer("error", raw, "", False))

    def test_a_timed_out_photo_keeps_the_placeholder(self):
        raw = "BEGIN:BMSG\r\nVERSION:1.0\r\nBEGIN:MSG\r\n"
        self.assertEqual(protocol.words_after_transfer("timeout", raw, "", True), ("", True))

    def test_a_partial_photo_is_flagged_from_its_bytes(self):
        raw = "BEGIN:BMSG\r\nimage/jpeg\r\nBEGIN:MSG\r\n"
        self.assertEqual(protocol.words_after_transfer("timeout", raw, "", False), ("", True))

    def test_a_finished_photo_with_no_caption_keeps_the_placeholder(self):
        raw = "BEGIN:BMSG\r\nBEGIN:MSG\r\nEND:MSG\r\nEND:BMSG\r\n"
        self.assertEqual(protocol.words_after_transfer("complete", raw, "", False), ("", True))

    def test_a_finished_text_is_kept_plain(self):
        raw = protocol.build_bmessage("+15551234567", "hello")
        self.assertEqual(protocol.words_after_transfer("complete", raw, "hello", False), ("hello", False))
        self.assertEqual(
            protocol.words_after_transfer("complete", raw, "see this", True), ("see this", True)
        )


class NameOnlyVCardTests(unittest.TestCase):
    def test_family_and_given_name_are_shown(self):
        raw = "BEGIN:VCARD\r\nN:Rivera;Sam;;;\r\nTEL;TYPE=CELL:+15557654321\r\nEND:VCARD\r\n"
        people = protocol.parse_vcards(raw)
        self.assertEqual(people[0]["name"], "Sam Rivera")

    def test_a_folded_name_stays_whole(self):
        raw = "BEGIN:VCARD\r\nFN:Sam \r\n Rivera\r\nTEL:+15557654321\r\nEND:VCARD\r\n"
        people = protocol.parse_vcards(raw)
        self.assertEqual(people[0]["name"], "Sam Rivera")
        self.assertEqual(people[0]["phones"], ["+15557654321"])

    def test_a_quoted_name_is_decoded(self):
        raw = "BEGIN:VCARD\r\nFN;ENCODING=QUOTED-PRINTABLE:Sam=20Rivera\r\nTEL:+15557654321\r\nEND:VCARD\r\n"
        people = protocol.parse_vcards(raw)
        self.assertEqual(people[0]["name"], "Sam Rivera")


class PlainVCardTests(unittest.TestCase):
    def test_reads_name_phone_and_email(self):
        raw = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "FN:Ada Lovelace\n"
            "TEL;TYPE=CELL:+15551234567\n"
            "EMAIL:ada@example.com\n"
            "END:VCARD\n"
        )
        people = protocol.parse_vcards(raw)
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["name"], "Ada Lovelace")
        self.assertEqual(people[0]["phones"], ["+15551234567"])
        self.assertEqual(people[0]["emails"], ["ada@example.com"])


class AudioProfileTests(unittest.TestCase):
    def test_headset_and_media_profiles_are_audio(self):
        for short in ("1108", "1112", "111e", "111f", "110a", "110b", "110c", "110e", "110f"):
            self.assertTrue(protocol.is_audio_profile(short))
            self.assertTrue(protocol.is_audio_profile(f"0000{short}-0000-1000-8000-00805f9b34fb"))

    def test_message_and_phone_book_profiles_are_not_audio(self):
        self.assertFalse(protocol.is_audio_profile("00001132-0000-1000-8000-00805f9b34fb"))
        self.assertFalse(protocol.is_audio_profile("0000112f-0000-1000-8000-00805f9b34fb"))
        self.assertEqual(len(protocol.audio_profile_uuids()), 9)


class CallingCodeTests(unittest.TestCase):
    def test_a_bare_ten_digit_number_uses_the_books_country(self):
        code = protocol.dominant_calling_code(["+15551110000", "+15552220000", "+447911000000"])
        self.assertEqual(code, "1")
        self.assertEqual(protocol.coerce_phone("5551234567", "+1" + "5555555555"), "+15551234567")

    def test_a_tie_does_not_guess_a_country(self):
        self.assertEqual(protocol.dominant_calling_code(["+15551110000", "+447911000000"]), "")


class PublicErrorTests(unittest.TestCase):
    def test_a_missing_message_service_asks_for_the_iphone_switch(self):
        text = protocol.public_error(RuntimeError("Unable to find service record"))
        self.assertIn("Show Message Notifications", text)

    def test_a_missing_file_is_not_a_missing_bluetooth_service(self):
        text = protocol.public_error(FileNotFoundError("No such file or directory: '/tmp/incoming.bmsg'"))
        self.assertNotIn("bluez-obex", text)

    def test_a_missing_dbus_service_asks_for_obex(self):
        exc = RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown: The name org.bluez.obex was not provided")
        text = protocol.public_error(exc)
        self.assertIn("bluez-obex", text)

    def test_authentication_failure_is_not_a_cancel(self):
        text = protocol.public_error(RuntimeError("org.bluez.Error.AuthenticationFailed"))
        self.assertEqual(text, "Pairing failed.")

    def test_authentication_cancel_says_cancelled(self):
        text = protocol.public_error(RuntimeError("org.bluez.Error.AuthenticationCanceled"))
        self.assertEqual(text, "Pairing was cancelled.")


class NotificationTests(unittest.TestCase):
    def test_number_is_not_used_as_a_title(self):
        self.assertEqual(protocol.notification_title("+15551234567"), "New message")

    def test_connected_status_names_the_phone(self):
        self.assertEqual(protocol.connected_status("Ada iPhone"), "Connected: Ada iPhone")
        self.assertEqual(protocol.connected_status("  "), "Connected")

    def test_name_is_kept(self):
        self.assertEqual(protocol.notification_title("Ada Lovelace"), "Ada Lovelace")

    def test_left_click_opens_the_tui_on_that_conversation(self):
        args = protocol.notification_command("Ada Lovelace", "hello", "tel:+15550001111")
        self.assertEqual(args[:5], ["omarchy-notification-send", "--app-name=tMsg", "-u", "normal", "Ada Lovelace"])
        self.assertEqual(args[5], "hello")
        self.assertEqual(args[6:], [
            "--exec",
            protocol.tui_launcher(),
            "--thread",
            "tel:+15550001111",
        ])

    def test_left_click_opens_the_tui_without_a_thread(self):
        args = protocol.notification_command("New message", "", "")
        self.assertNotIn("", args)
        self.assertEqual(args[-2:], ["--exec", protocol.tui_launcher()])


if __name__ == "__main__":
    unittest.main()
