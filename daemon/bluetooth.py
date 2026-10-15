"""Talk to a paired iPhone through BlueZ.

MAP (Message Access Profile) carries the texts. PBAP (Phone Book Access
Profile) carries the contacts. Both are public Bluetooth profiles. This
file never speaks Apple's private iMessage protocol and never sees an
Apple ID password.

The numeric comparison shown during pairing is a secret for that moment.
It is sent to the overlay and is not written to the log.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from pathlib import Path

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, dbus_property, method

from protocol import (
    address_from_thread,
    audio_profile_uuids,
    build_bmessage,
    classify_recipient,
    coerce_phone,
    connected_status,
    dominant_calling_code,
    extract_bmessage_peer,
    extract_bmessage_text,
    is_audio_profile,
    normalize_email,
    parse_vcards,
    message_has_file,
    present_message,
    public_error,
    thread_key,
    words_after_transfer,
)
from store import Store, runtime_dir

# iOS shows "Show Message Notifications" after it sees this ANCS service
# solicited. We advertise the UUID only. We do not subscribe to it, so
# other apps' notifications are never read.
_ANCS_UUID = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
_OBEX_CANDIDATES = (
    "/usr/lib/bluetooth/obexd",
    "/usr/libexec/bluetooth/obexd",
    "obexd",
)
# A timed-out download is not retried until this many seconds pass.
# Otherwise every poll spends five seconds re-fetching the same photo
# before it reaches the text that came after it.
_STUCK_GET_COOLDOWN = 30.0
# Our names on shared namespaces. The data directory uses the plugin id so
# it cannot collide with another program also called tmsg. D-Bus object
# path elements forbid hyphens, so the id's dashes become underscores here.
AGENT_PATH = "/io/github/j_c_m/tmsg/Messages/Agent"
ADVERT_PATH = "/io/github/j_c_m/tmsg/Messages/Advert"


def _unwrap(value):
    """dbus-next wraps property values in Variant. This returns the inner value."""
    if isinstance(value, Variant):
        return value.value
    return value


def _props_of(interfaces: dict, name: str) -> dict:
    raw = interfaces.get(name) or {}
    return {key: _unwrap(val) for key, val in raw.items()}


def _handle_from(path: str, fields: dict) -> str:
    """MAP handle for one text.

    BlueZ puts the handle in the object name, like .../message42.
    The property list often does not include it.
    """
    handle = fields.get("Handle")
    if handle not in (None, ""):
        return str(handle)
    match = re.search(r"message(\d+)$", path or "")
    return match.group(1) if match else ""


def _listing_entries(listing) -> list[tuple[str, dict]]:
    """Turn a ListMessages result into (object path, properties) rows.

    BlueZ returns a dictionary whose keys are object paths. dbus-next
    hands that back as pairs, not as a flat property bag.
    """
    if isinstance(listing, dict):
        pairs = list(listing.items())
    else:
        pairs = list(listing or [])
    rows = []
    for entry in pairs:
        if isinstance(entry, (list, tuple)) and len(entry) == 2 and isinstance(entry[1], dict):
            path, props = entry
            rows.append((str(path), {key: _unwrap(val) for key, val in props.items()}))
        elif isinstance(entry, dict):
            rows.append((str(entry.get("Path") or ""), {key: _unwrap(val) for key, val in entry.items()}))
    return rows


def _session_transfer_paths(session_path: str, objects: dict) -> list[str]:
    """Transfer objects owned by one OBEX session.

    The MAP and PBAP sessions each publish transfers under their own
    path. Only the MAP ones may be cancelled for a stuck text download.
    Message objects live under the same prefix but carry no Transfer1
    interface, so they are never picked up here.
    """
    prefix = (session_path or "").rstrip("/") + "/"
    paths = []
    for path, interfaces in (objects or {}).items():
        if not str(path).startswith(prefix):
            continue
        if not isinstance(interfaces, dict):
            continue
        if "org.bluez.obex.Transfer1" not in interfaces:
            continue
        paths.append(str(path))
    return paths


def _map_time(value: str) -> str:
    """MAP timestamps look like 20261002T103000. Store a sortable ISO string."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 14:
        return Store.now_stamp()
    return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}T{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"


class PairAgent(ServiceInterface):
    """BlueZ pairing agent. It shows a 6-digit code and waits for the overlay."""

    def __init__(self, on_code):
        super().__init__("org.bluez.Agent1")
        self._on_code = on_code
        self._pending: asyncio.Future | None = None

    def answer(self, accept: bool) -> None:
        pending = self._pending
        self._pending = None
        if pending is None or pending.done():
            return
        if accept:
            pending.set_result(None)
        else:
            pending.set_exception(DBusError("org.bluez.Error.Rejected", "user rejected"))

    def _begin_wait(self, code: str) -> asyncio.Future:
        if self._pending:
            self.answer(False)
        loop = asyncio.get_running_loop()
        self._pending = loop.create_future()
        self._on_code(code)
        return self._pending

    @method()
    def Release(self):
        self.answer(False)

    @method()
    def Cancel(self):
        self.answer(False)

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        raise DBusError("org.bluez.Error.Rejected", "numeric comparison only")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        self._on_code(str(pincode))

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        raise DBusError("org.bluez.Error.Rejected", "numeric comparison only")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        self._on_code(f"{int(passkey):06d}")

    @method()
    async def RequestConfirmation(self, device: "o", passkey: "u"):
        try:
            await asyncio.wait_for(self._begin_wait(f"{int(passkey):06d}"), timeout=90)
        except asyncio.TimeoutError as exc:
            raise DBusError("org.bluez.Error.Rejected", "timed out") from exc

    @method()
    async def RequestAuthorization(self, device: "o"):
        try:
            await asyncio.wait_for(self._begin_wait("YES"), timeout=90)
        except asyncio.TimeoutError as exc:
            raise DBusError("org.bluez.Error.Rejected", "timed out") from exc

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        # Pairing confirmation is RequestConfirmation. This call is the
        # later per-service allow. Headset and media profiles are refused.
        if is_audio_profile(str(uuid)):
            raise DBusError("org.bluez.Error.Rejected", "audio profiles are not used")
        return


class SolicitationAdvert(ServiceInterface):
    """A short Bluetooth announcement that asks iOS to show the message switches.

    It solicits the ANCS UUID and nothing else. There is no GATT subscription,
    so notification contents from other apps are never delivered here.
    """

    def __init__(self):
        super().__init__("org.bluez.LEAdvertisement1")

    @method()
    def Release(self):
        return

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def SolicitUUIDs(self) -> "as":
        return [_ANCS_UUID]

    @dbus_property(access=PropertyAccess.READ)
    def Discoverable(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def ManufacturerData(self) -> "a{qv}":
        # 0xffff is the Bluetooth test manufacturer id, not an Apple id.
        return {0xFFFF: Variant("ay", bytes([0x00]))}


class BluetoothSession:
    """One user's Bluetooth session: scan, pair, read, and send."""

    def __init__(self, store: Store, emit):
        self.store = store
        self.emit = emit
        self.state = "not_paired"
        self.detail = ""
        self.ready = False
        self._system: MessageBus | None = None
        self._session_bus: MessageBus | None = None
        self._agent: PairAgent | None = None
        self._agent_on = False
        self._advert_on = False
        self._advert_stop: asyncio.Task | None = None
        self._map_path = ""
        self._pbap_path = ""
        self._message_paths: dict[str, str] = {}
        # Preview rows without a MAP handle share a timestamp-based id.
        # The counter keeps two texts in the same second distinct.
        self._preview_seq = 0
        # Handles whose download timed out, mapped to the monotonic time
        # when they may be tried again.
        self._stuck_gets: dict[str, float] = {}
        self._obex_proc: subprocess.Popen | None = None
        self._announce_live = False
        self._listening = False
        self._contacts_watching = False
        self._link_ticks = 0
        self._last_link_try = 0.0
        self._reconnecting = False
        # These must stay referenced. If Python collects them, new-text
        # alerts from the phone are silently dropped.
        self._obex_root = None
        self._obex_manager = None
        self._bluez_manager = None
        self._tasks: set[asyncio.Task] = set()
        self.lock = asyncio.Lock()

    def _track(self, coro) -> asyncio.Task:
        """Keep a background task alive until it finishes."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self) -> None:
        self._system = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._session_bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self._agent = PairAgent(self._show_code)
        self._system.export(AGENT_PATH, self._agent)
        self._system.export(ADVERT_PATH, SolicitationAdvert())
        self.ready = True
        # A phone paired in the system Bluetooth settings is already known.
        # Show it immediately instead of waiting for a new scan.
        await self._adopt_paired_iphone()
        await self._refresh_presence()
        self.push_status()
        self._track(self._watch_audio())
        if self.store.settings()["phoneAddress"]:
            self._track(self._safe_reconnect())

    async def reconnect(self) -> None:
        """Try the message link again. Used after the iPhone switches are turned on."""
        await self._safe_reconnect()

    async def _safe_reconnect(self) -> None:
        if self._reconnecting:
            return
        self._reconnecting = True
        self._last_link_try = time.monotonic()
        try:
            async with self.lock:
                await self._connect_saved()
        except Exception as exc:
            self.detail = public_error(exc)
            self.push_status()
        finally:
            self._reconnecting = False

    def push_status(self) -> None:
        settings = self.store.settings()
        if not settings["phoneAddress"] and self.state not in ("pairing", "scanning"):
            self.state = "not_paired"
        self.emit(
            {
                "event": "status",
                "status": {
                    "state": self.state,
                    "phoneName": settings["phoneName"],
                    "phoneAddress": settings["phoneAddress"],
                    "ownNumber": settings["ownNumber"],
                    "unread": self.store.unread_count(),
                    "notifyBody": settings["notifyBody"],
                    "detail": self.detail,
                    "bluetoothReady": self.ready,
                },
            }
        )

    def _show_code(self, code: str) -> None:
        self.state = "pairing"
        self.detail = "Confirm this code on the iPhone too."
        self.emit({"event": "pair-code", "code": str(code)})
        self.push_status()

    def confirm(self, accept: bool) -> None:
        if self._agent:
            self._agent.answer(accept)

    async def _adopt_paired_iphone(self) -> None:
        """Remember an iPhone that BlueZ already has paired, and tell the overlay."""
        devices = await self._phone_dicts()
        self.emit({"event": "devices", "devices": devices})
        saved = self.store.settings()["phoneAddress"].upper()
        if saved:
            visible = any(device["address"].upper() == saved for device in devices)
            self.state = "phone_nearby"
            self.detail = "iPhone is paired." if visible else "The paired phone is not visible right now."
            return
        paired = [device for device in devices if device.get("paired")]
        if len(paired) != 1:
            return
        chosen = paired[0]
        self.store.set_phone(chosen["address"], chosen["name"] or "iPhone")
        # "connected" is reserved for a live message session. A Bluetooth
        # link alone means the phone is nearby and can be opened.
        self.state = "phone_nearby"
        self.detail = "iPhone is paired."

    async def list_phones(self) -> None:
        """Publish phones BlueZ already knows. This does not start discovery."""
        async with self.lock:
            self.emit({"event": "devices", "devices": await self._phone_dicts()})

    async def scan(self) -> None:
        async with self.lock:
            self.state = "scanning"
            self.detail = "Looking for nearby phones."
            self.push_status()
            try:
                # Paired phones are already in BlueZ. Publish them before
                # the discovery wait, or a connected iPhone looks missing.
                await self._adopt_paired_iphone()
                self.push_status()
                adapter = await self._adapter()
                try:
                    await adapter.call_set_discovery_filter({"Transport": Variant("s", "bredr")})
                except DBusError:
                    pass
                try:
                    await adapter.call_start_discovery()
                except DBusError:
                    # Already discovering is fine. The list below still updates.
                    pass
                await asyncio.sleep(8)
                try:
                    await adapter.call_stop_discovery()
                except DBusError:
                    pass
                await self._adopt_paired_iphone()
                if not self.store.settings()["phoneAddress"]:
                    self.state = "not_paired"
                    self.detail = "No phone found. Move it closer and scan again."
                else:
                    self.detail = "Select your iPhone."
            except Exception as exc:
                self.detail = public_error(exc)
                self.emit({"event": "error", "message": self.detail})
            self.push_status()

    async def pair(self, address: str) -> None:
        """Bond the phone, then open texts and contacts.

        Bonding never connects first. Holding a profile connection
        open makes iOS drop the link, while the message channel
        brings the link up on demand.
        """
        async with self.lock:
            self._cancel_advert_stop()
            try:
                await self._register_agent()
                device = await self._device(address)
                name = await self._device_name(device)
                try:
                    bonded = await device.get_paired()
                except Exception:
                    bonded = False
                if not bonded:
                    await device.call_pair()
                await self._untrust(address)
                self.store.set_phone(address, name)
                self.detail = "Paired. Waiting for the iPhone switches."
                self.state = "waiting_for_switches"
                self.push_status()
                await self._start_advert()
                self._schedule_advert_stop()
                await self._open_profiles(address, announce=False)
            except Exception as exc:
                self.detail = public_error(exc)
                self.state = "not_paired"
                self.emit({"event": "error", "message": self.detail})
                self.push_status()
            finally:
                await self._unregister_agent()

    def _cancel_advert_stop(self) -> None:
        task = self._advert_stop
        self._advert_stop = None
        if task and not task.done():
            task.cancel()

    def _schedule_advert_stop(self) -> None:
        """Drop the announcement if the message link is still down after a while."""
        self._cancel_advert_stop()

        async def _stop() -> None:
            try:
                await asyncio.sleep(120)
            except asyncio.CancelledError:
                return
            await self._stop_advert()

        self._advert_stop = self._track(_stop())

    async def send(self, address: str, body: str) -> str:
        async with self.lock:
            if not self._map_path:
                raise RuntimeError("The phone is not connected for messages yet.")
            own = self.store.settings()["ownNumber"]
            _, value = classify_recipient(address, own)
            # build happens in the caller via protocol so a bad body fails
            # before we touch Bluetooth. Re-check here as the gate.
            payload = build_bmessage(value, body, own)
            folder = runtime_dir()
            path = folder / "outgoing.bmsg"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            try:
                access = await self._iface("org.bluez.obex", self._map_path, "org.bluez.obex.MessageAccess1", self._session_bus)
                await access.call_push_message(str(path), "telecom/msg/outbox", {})
            finally:
                try:
                    path.unlink()
                except OSError:
                    pass
            label = self.store.label_for(value)
            row, _created = self.store.upsert_message(
                external_id=f"local-{time.time_ns()}",
                address=value,
                body=body,
                direction="out",
                timestamp=Store.now_stamp(),
                read=True,
                has_attachment=False,
                body_complete=True,
                peer_label=label,
            )
            self._emit_threads()
            return str(row["thread"]) if row else ""

    async def mark_read(self, thread: str) -> None:
        handles = self.store.mark_thread_read(thread)
        self._emit_threads()
        self.push_status()
        if not self._map_path:
            return
        for handle in handles:
            path = self._message_paths.get(handle)
            if not path:
                continue
            try:
                message = await self._iface("org.bluez.obex", path, "org.bluez.obex.Message1", self._session_bus)
                await message.set_read(True)
            except Exception:
                # Local read state still stands when the phone does not accept the update.
                continue

    async def fetch_thread(self, thread: str) -> None:
        """Download full text for messages we have only seen as a preview."""
        if not self._map_path:
            return
        async with self.lock:
            for handle in self.store.incomplete_in_thread(thread):
                await self._download_body(handle)

    async def _connect_saved(self) -> None:
        settings = self.store.settings()
        address = settings["phoneAddress"]
        if not address:
            return
        self.state = "phone_nearby"
        self.detail = "Reconnecting to the paired iPhone."
        self.push_status()
        await self._untrust(address)
        await self._drop_audio(address)
        await self._open_profiles(address, announce=False)

    async def _link_up(self, address: str) -> bool:
        try:
            device = await self._device(address)
            return bool(await device.get_connected())
        except Exception:
            return False

    async def _bring_up_link(self, address: str) -> None:
        """Connect the phone so it will offer the message service.

        iOS does not publish MAP until the radio link is up. Device.Connect
        is what completes that discovery. The agent rejects audio, and any
        audio profile that still attaches is dropped before we return.
        """
        if await self._link_up(address):
            await self._drop_audio(address)
            return
        started_agent = not self._agent_on
        try:
            await self._register_agent()
            device = await self._device(address)
            try:
                await device.call_connect()
            except Exception:
                pass
        finally:
            await self._drop_audio(address)
            if started_agent:
                await self._unregister_agent()

    async def _untrust(self, address: str) -> None:
        """Stop BlueZ from attaching every profile with no question."""
        try:
            device = await self._device(address)
            await device.set_trusted(False)
        except Exception:
            return

    async def _drop_audio(self, address: str) -> None:
        """Disconnect headset and media profiles if the phone opened them."""
        try:
            device = await self._device(address)
        except Exception:
            return
        for uuid in audio_profile_uuids():
            try:
                await device.call_disconnect_profile(uuid)
            except Exception:
                continue

    def _on_audio_added(self, path, interfaces) -> None:
        address = self.store.settings()["phoneAddress"]
        if not address:
            return
        needle = address.upper().replace(":", "_")
        if needle not in str(path).upper():
            return
        names = set(interfaces.keys()) if isinstance(interfaces, dict) else set()
        watched = {"org.bluez.MediaTransport1", "org.bluez.MediaControl1", "org.bluez.Headset1"}
        if names.isdisjoint(watched):
            return
        self._track(self._drop_audio(address))

    async def _watch_audio(self) -> None:
        """Disconnect audio if the phone starts it after the message link is up."""
        if not self._system:
            return
        try:
            intro = await self._system.introspect("org.bluez", "/")
            root = self._system.get_proxy_object("org.bluez", "/", intro)
            manager = root.get_interface("org.freedesktop.DBus.ObjectManager")
            self._bluez_manager = manager
            manager.on_interfaces_added(self._on_audio_added)
        except Exception:
            return

    async def _open_profiles(self, address: str, announce: bool, recover: bool = True) -> None:
        await self._ensure_obexd()
        # No eager connect: the OBEX session pages the phone on demand,
        # and a held profile connection makes iOS drop the link. When the
        # message channel reports the link down, the retry below brings
        # it up explicitly.
        await self._close_profiles()
        client = await self._iface("org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", self._session_bus)
        try:
            self._map_path = await client.call_create_session(address, {"Target": Variant("s", "map")})
        except Exception as exc:
            text = str(exc).lower()
            # One retry after the radio link is up. iOS hides MAP until then.
            retry = (
                "service record" in text
                or "0x43" in text
                or "busy" in text
                or "forbidden" in text
                or "connection refused" in text
                or "errno 111" in text
                or "host is down" in text
            )
            if recover and retry:
                await self._close_profiles()
                await asyncio.sleep(1.2)
                await self._bring_up_link(address)
                await self._open_profiles(address, announce, recover=False)
                return
            message = public_error(exc)
            if "not allowed" in message or "forbidden" in text or "0x43" in text:
                self.state = "waiting_for_switches"
            else:
                self.state = "phone_nearby"
            self.detail = message
            self.push_status()
            return
        self._listen_for_messages()
        await self._sync_inbox(announce)
        try:
            self._pbap_path = await client.call_create_session(address, {"Target": Variant("s", "pbap")})
            await self._pull_contacts()
        except Exception:
            # Contacts are optional. Texts still work when Sync Contacts is off.
            pass
        self.state = "connected"
        self.detail = connected_status(self.store.settings()["phoneName"])
        self._announce_live = True
        await self._stop_advert()
        await self._drop_audio(address)
        self._emit_threads()
        # People you have already texted are listed even when the phone
        # book download is still empty.
        self.emit({"event": "contacts", "contacts": self.store.search_contacts("")})
        self.push_status()
        if not self._contacts_watching:
            self._contacts_watching = True
            self._track(self._watch_contacts())

    async def _sync_inbox(self, announce: bool) -> None:
        await self._poll_inbox_listing(announce)
        # A new text can already be sitting on the bus as an alert, with
        # no inbox row. Pick those up too.
        await self._collect_live_messages(announce)

    async def _poll_inbox_listing(self, announce: bool, update: bool = True) -> int:
        """Download inbox rows the alert listener never announced.

        Returns how many rows became newly complete. Finished rows are
        skipped before reading so a poll never re-fetches old texts.
        """
        access = await self._iface("org.bluez.obex", self._map_path, "org.bluez.obex.MessageAccess1", self._session_bus)
        if update:
            try:
                await access.call_update_inbox()
            except Exception:
                pass
        try:
            listing = await access.call_list_messages(
                "telecom/msg/inbox",
                {"MaxCount": Variant("q", 40), "SubjectLength": Variant("y", 255)},
            )
        except Exception:
            listing = await access.call_list_messages("telecom/msg/inbox", {})
        entries = _listing_entries(listing)
        saved = 0
        for path, fields in entries:
            handle = _handle_from(path, fields)
            if path and handle:
                self._message_paths[handle] = path
            current = self.store.record(handle) if handle else None
            if current and current["body_complete"]:
                continue
            # The listing is a preview. The full text, and often the phone
            # number, are inside the downloaded message.
            self._ingest(fields, path=path, announce=announce)
            if handle:
                await self._download_body(handle, announce=announce, sent=bool(fields.get("Sent")))
                after = self.store.record(handle)
                if after and after["body_complete"] and (current is None or not current["body_complete"]):
                    saved += 1
        return saved

    async def _collect_live_messages(self, announce: bool) -> int:
        """Download message alerts BlueZ already has, skipping finished ones.

        Message objects from an earlier session linger under its own path
        and are still downloadable, so objects under any session count.
        """
        if not self._session_bus or not self._map_path:
            return 0
        saved = 0
        objects = await self._managed("org.bluez.obex", "/", self._session_bus)
        for path, interfaces in objects.items():
            if "/message" not in str(path):
                continue
            props = _props_of(interfaces, "org.bluez.obex.Message1")
            if not props:
                continue
            handle = _handle_from(str(path), props)
            if not handle:
                continue
            current = self.store.record(handle)
            if current and current["body_complete"]:
                continue
            self._message_paths[handle] = str(path)
            await self._download_body(handle, announce=announce, sent=bool(props.get("Sent")))
            after = self.store.record(handle)
            if after and (current is None or not current["body_complete"]):
                saved += 1
        return saved

    def _ingest(self, fields: dict, path: str, announce: bool) -> tuple[dict, bool] | None:
        handle = _handle_from(path, fields)
        if path and handle:
            self._message_paths[handle] = path
        sender = str(fields.get("SenderAddress") or fields.get("Sender") or "")
        # A brand-new text often arrives with no sender yet. The download
        # step fills that in. Skipping here would drop the text.
        if not sender:
            return None
        # A display name is not a sendable address. The download inserts the
        # row once the bMessage has a phone number or an email.
        if not thread_key(sender, self.store.settings()["ownNumber"]).startswith(("tel:", "email:")):
            return None
        sender_name = str(fields.get("Sender") or "")
        # A phone number is not a display name. Leave the label empty so the
        # contact cache can fill it in, and so a notification is not forced
        # to invent a name from digits.
        label = sender_name if sender_name and sender_name != sender else ""
        subject = str(fields.get("Subject") or "")
        attached = message_has_file(fields, subject)
        if present_message(subject, False) == "attachment omitted":
            subject = ""
        if handle:
            external_id = handle
        else:
            self._preview_seq += 1
            stamp = _map_time(str(fields.get("Timestamp") or ""))
            external_id = f"preview-{stamp}-{sender}-{self._preview_seq}"
        row, created = self.store.upsert_message(
            external_id=external_id,
            address=sender,
            body=subject,
            direction="out" if fields.get("Sent") else "in",
            timestamp=_map_time(str(fields.get("Timestamp") or "")),
            read=bool(fields.get("Read")),
            has_attachment=attached,
            body_complete=False,
            peer_label=label,
        )
        if row is None:
            return None
        if announce and created and row["direction"] == "in" and not row["read"]:
            self.emit({"event": "message", "message": row, "notify": True})
        return row, created

    async def _cancel_transfer(self, transfer_path: str) -> None:
        """Stop one OBEX transfer, then wait until its object is gone.

        A vanished object reads back as "complete". Either way the
        session is idle again before the next download starts.
        """
        if not transfer_path or not self._session_bus:
            return
        try:
            transfer = await self._iface(
                "org.bluez.obex", transfer_path, "org.bluez.obex.Transfer1", self._session_bus
            )
            await transfer.call_cancel()
        except Exception:
            pass
        await self._wait_transfer_rounds(transfer_path, 20)

    async def _cancel_map_transfers(self) -> None:
        """Stop every transfer on the message session, leaving contacts alone."""
        if not self._session_bus or not self._map_path:
            return
        try:
            objects = await self._managed("org.bluez.obex", "/", self._session_bus)
        except Exception:
            return
        for path in _session_transfer_paths(self._map_path, objects):
            await self._cancel_transfer(path)

    async def _pull_message_file(self, message_path: str, target: Path) -> tuple[str, str]:
        """Download one text file from the phone.

        Returns the bytes read and the transfer status. A photo download
        can run for minutes on the phone; this waits five seconds, then
        cancels so the session is idle before the next text.
        attachment=False: image bytes are never copied onto this computer.
        """
        message = await self._iface("org.bluez.obex", message_path, "org.bluez.obex.Message1", self._session_bus)
        result = await message.call_get(str(target), False)
        transfer = str(result[0]) if isinstance(result, (list, tuple)) and result else ""
        if not transfer:
            return await self._read_bmessage(target), "complete"
        status = await self._wait_transfer(transfer)
        if status != "timeout":
            return await self._read_bmessage(target), status
        # Read before cancelling: obexd may delete the partial file.
        before = await self._read_bmessage(target)
        await self._cancel_transfer(transfer)
        after = await self._read_bmessage(target)
        return (after if after else before), status

    async def _wait_transfer(self, transfer_path: str) -> str:
        """Message.Get answers before the file has any bytes in it.

        The returned object is a transfer. It stays "queued" until the
        phone finishes sending. When the object disappears, the file is ready.
        """
        return await self._wait_transfer_rounds(transfer_path, 50)

    async def _wait_transfer_rounds(self, transfer_path: str, rounds: int) -> str:
        """Wait until a transfer finishes. A vanished object means the file is ready."""
        for _ in range(rounds):
            try:
                intro = await self._session_bus.introspect("org.bluez.obex", transfer_path)
                obj = self._session_bus.get_proxy_object("org.bluez.obex", transfer_path, intro)
                status = _unwrap(
                    await obj.get_interface("org.freedesktop.DBus.Properties").call_get(
                        "org.bluez.obex.Transfer1", "Status"
                    )
                )
            except Exception:
                return "complete"
            if str(status) in ("complete", "error"):
                return str(status)
            await asyncio.sleep(0.1)
        return "timeout"

    async def _read_bmessage(self, target: Path) -> str:
        """Read a downloaded text, retrying while the file is still empty."""
        last = ""
        for _ in range(20):
            try:
                if target.exists() and target.stat().st_size > 0:
                    last = target.read_text(encoding="utf-8", errors="replace")
                    if "BEGIN:MSG" in last.upper():
                        return last
            except OSError:
                pass
            await asyncio.sleep(0.05)
        return last

    async def _download_body(self, handle: str, announce: bool = False, sent: bool | None = None) -> None:
        """Read one text from the phone and store the words plus the sender."""
        current = self.store.record(handle)
        if current and current["body_complete"]:
            return
        if self._stuck_gets.get(handle, 0.0) > time.monotonic():
            return
        path = self._message_paths.get(handle)
        if not path:
            return
        target = runtime_dir() / f"incoming-{handle}.bmsg"
        try:
            try:
                raw, status = await self._pull_message_file(path, target)
            except Exception:
                # A stuck photo GET fails the next download too. Clear
                # the session, then try this text once more.
                await self._cancel_map_transfers()
                raw, status = await self._pull_message_file(path, target)
        except Exception:
            return
        finally:
            try:
                target.unlink()
            except OSError:
                pass
        text = extract_bmessage_text(raw)
        current = self.store.record(handle)
        flagged = bool(current["has_attachment"]) if current else False
        decided = words_after_transfer(status, raw, text, flagged)
        if decided is None:
            if status == "timeout":
                now = time.monotonic()
                self._stuck_gets = {key: done for key, done in self._stuck_gets.items() if done > now}
                self._stuck_gets[handle] = now + _STUCK_GET_COOLDOWN
            return
        text, flagged = decided
        if sent is None:
            sent = current["direction"] == "out" if current else False
        peer = extract_bmessage_peer(raw, sent)
        address = address_from_thread(current["thread_key"]) if current else peer
        if not address:
            address = peer
        if not address or (not text and not flagged):
            return
        was_complete = bool(current["body_complete"]) if current else False
        label = current["peer_label"] if current else ""
        _row, created = self.store.upsert_message(
            external_id=handle,
            address=address,
            body=text,
            direction="out" if sent else "in",
            timestamp=current["timestamp"] if current else Store.now_stamp(),
            read=bool(current["read"]) if current else False,
            has_attachment=flagged,
            body_complete=True,
            peer_label=label,
        )
        # A preview row already exists when the listing named a sender, so
        # finishing the download must announce too, not just creating it.
        if _row and announce and not sent and (created or not was_complete):
            self.emit({"event": "message", "message": _row, "notify": True})

    async def _watch_contacts(self) -> None:
        """Keep asking for the phone book until the iPhone shares it.

        Sync Contacts can be turned on after this program has already
        connected. An empty book is retried. A book we already saved is
        left alone.
        """
        while self._session_bus:
            await asyncio.sleep(8)
            if self.store.contact_count() > 0 or not self.store.settings()["phoneAddress"]:
                continue
            try:
                async with self.lock:
                    await self._ensure_contacts()
            except Exception:
                continue

    async def _ensure_contacts(self) -> None:
        address = self.store.settings()["phoneAddress"]
        if not address:
            return
        if not self._pbap_path:
            client = await self._iface(
                "org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", self._session_bus
            )
            try:
                self._pbap_path = await client.call_create_session(address, {"Target": Variant("s", "pbap")})
            except Exception:
                return
        await self._pull_contacts()

    async def _pull_contacts(self) -> None:
        if not self._pbap_path:
            return
        phonebook = await self._iface(
            "org.bluez.obex", self._pbap_path, "org.bluez.obex.PhonebookAccess1", self._session_bus
        )
        target = runtime_dir() / "contacts.vcf"
        # Photos make the book huge, and a short wait then saves only the
        # first cards. Names and numbers are enough to pick a person.
        filters = {
            "Format": Variant("s", "vcard30"),
            "MaxCount": Variant("q", 65535),
            "Fields": Variant("as", ["FN", "N", "TEL", "EMAIL", "NICKNAME"]),
        }
        try:
            await phonebook.call_select("int", "pb")
            try:
                result = await phonebook.call_pull_all(str(target), filters)
            except Exception:
                result = await phonebook.call_pull_all(
                    str(target),
                    {"Format": Variant("s", "vcard30"), "MaxCount": Variant("q", 65535)},
                )
        except Exception:
            await self._drop_pbap()
            return
        transfer = str(result[0]) if isinstance(result, (list, tuple)) and result else ""
        # A full book can take longer than one text. A timed-out file is
        # only the first slice, so it must not replace the saved list.
        if transfer and await self._wait_transfer_rounds(transfer, 600) == "timeout":
            try:
                target.unlink()
            except OSError:
                pass
            return
        try:
            raw = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        except OSError:
            raw = ""
        finally:
            try:
                target.unlink()
            except OSError:
                pass
        if "BEGIN:VCARD" not in raw.upper():
            # The iPhone keeps an empty book on a session that started
            # before Sync Contacts was turned on. Drop it and open a new one.
            await self._drop_pbap()
            return
        cards = parse_vcards(raw)
        own = self.store.settings()["ownNumber"]
        if not own:
            code = dominant_calling_code([phone for card in cards for phone in card["phones"]])
            if code:
                own = "+" + code + "5555555555"
        rows = []
        for card in cards:
            name = card["name"] or "Unknown"
            for phone in card["phones"]:
                number = coerce_phone(phone, own) or ""
                if number:
                    rows.append({"name": name, "address": number, "kind": "tel"})
            for email in card["emails"]:
                cleaned = normalize_email(email)
                if cleaned:
                    rows.append({"name": name, "address": cleaned, "kind": "email"})
        if not rows:
            await self._drop_pbap()
            return
        self.store.replace_contacts(rows)
        self.store.apply_contact_names()
        self._emit_threads()
        self.emit({"event": "contacts", "contacts": self.store.search_contacts("")})

    async def _drop_pbap(self) -> None:
        """Close the phone-book link so the next try starts clean."""
        path = self._pbap_path
        self._pbap_path = ""
        if not path or not self._session_bus:
            return
        try:
            client = await self._iface(
                "org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", self._session_bus
            )
            await client.call_remove_session(path)
        except Exception:
            pass

    def _listen_for_messages(self) -> None:
        if not self._session_bus or self._listening:
            return
        self._listening = True
        self._track(self._subscribe_messages())

    async def _subscribe_messages(self) -> None:
        try:
            intro = await self._session_bus.introspect("org.bluez.obex", "/")
            root = self._session_bus.get_proxy_object("org.bluez.obex", "/", intro)
            manager = root.get_interface("org.freedesktop.DBus.ObjectManager")
            self._obex_root = root
            self._obex_manager = manager
            manager.on_interfaces_added(self._on_message_added)
        except Exception:
            self._listening = False
            return
        self._track(self._watch_messages())

    async def _watch_messages(self) -> None:
        """Look again every couple of seconds.

        The phone can announce a text before our alert listener is ready.
        The alert stays on the bus, so a later look still finds it.
        """
        while self._session_bus:
            await asyncio.sleep(2)
            self._link_ticks += 1
            if self._link_ticks >= 8:
                self._link_ticks = 0
                try:
                    await self._check_link()
                except Exception:
                    pass
            if not self._map_path:
                continue
            try:
                async with self.lock:
                    # Bus objects only. Querying the inbox listing from this
                    # loop coincided with lost alerts, and the listing has
                    # never returned a row on this phone, so it stays a
                    # connect-time sync step, not a poll.
                    saved = await self._collect_live_messages(self._announce_live)
            except Exception:
                # A dead session looks like a poll error. Recheck the link
                # on the next tick instead of waiting out the full interval.
                self._link_ticks = 8
                continue
            if saved:
                self._emit_threads()
                self.push_status()

    def _on_message_added(self, path, interfaces) -> None:
        props = _props_of(interfaces, "org.bluez.obex.Message1")
        if not props:
            return
        # Schedule the download. Doing it here, inside the D-Bus callback,
        # would stall the Bluetooth connection.
        self._track(self._capture_message(str(path), props))

    async def _capture_message(self, path: str, fields: dict) -> None:
        """Save a text the iPhone just announced."""
        handle = _handle_from(path, fields)
        if path and handle:
            self._message_paths[handle] = path
        announce = self._announce_live
        async with self.lock:
            self._ingest(fields, path=path, announce=announce)
            if handle:
                await self._download_body(handle, announce=announce, sent=bool(fields.get("Sent")))
        self._emit_threads()
        self.push_status()

    async def _close_profiles(self) -> None:
        if not self._session_bus:
            return
        client = None
        try:
            client = await self._iface("org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", self._session_bus)
        except Exception:
            return
        for path in (self._map_path, self._pbap_path):
            if not path:
                continue
            try:
                await client.call_remove_session(path)
            except Exception:
                pass
        self._map_path = ""
        self._pbap_path = ""

    async def _ensure_obexd(self) -> None:
        try:
            await self._session_bus.introspect("org.bluez.obex", "/org/bluez/obex")
            return
        except Exception:
            pass
        if self._obex_proc and self._obex_proc.poll() is None:
            await asyncio.sleep(0.4)
            return
        for candidate in _OBEX_CANDIDATES:
            program = candidate
            if candidate.startswith("/") and not Path(candidate).is_file():
                continue
            try:
                self._obex_proc = subprocess.Popen(
                    [program],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                break
            except FileNotFoundError:
                continue
        await asyncio.sleep(0.5)
        await self._session_bus.introspect("org.bluez.obex", "/org/bluez/obex")

    async def _adapter(self):
        objects = await self._managed("org.bluez", "/", self._system)
        for path, interfaces in objects.items():
            if "org.bluez.Adapter1" in interfaces:
                return await self._iface("org.bluez", path, "org.bluez.Adapter1", self._system)
        raise RuntimeError("No Bluetooth adapter is available.")

    async def _device(self, address: str):
        objects = await self._managed("org.bluez", "/", self._system)
        wanted = address.upper()
        for path, interfaces in objects.items():
            props = _props_of(interfaces, "org.bluez.Device1")
            if props and str(props.get("Address", "")).upper() == wanted:
                return await self._iface("org.bluez", path, "org.bluez.Device1", self._system)
        raise RuntimeError("That phone is no longer in the Bluetooth list. Scan again.")

    async def _device_name(self, device) -> str:
        try:
            name = await device.get_name()
        except Exception:
            name = ""
        return str(name or "iPhone")

    async def _phone_dicts(self) -> list[dict]:
        objects = await self._managed("org.bluez", "/", self._system)
        found = []
        for _path, interfaces in objects.items():
            props = _props_of(interfaces, "org.bluez.Device1")
            if not props:
                continue
            name = str(props.get("Name") or props.get("Alias") or "")
            icon = str(props.get("Icon") or "")
            if icon != "phone" and "iphone" not in name.lower():
                continue
            found.append(
                {
                    "address": str(props.get("Address") or ""),
                    "name": name or "Phone",
                    "paired": bool(props.get("Paired")),
                    "connected": bool(props.get("Connected")),
                }
            )
        return found

    async def _map_alive(self) -> bool:
        """True when the message session answers on the bus.

        Introspection alone is not enough: a dead session can leave a
        zombie object behind that has no message interface.
        """
        if not self._map_path or not self._session_bus:
            return False
        try:
            await self._iface("org.bluez.obex", self._map_path, "org.bluez.obex.MessageAccess1", self._session_bus)
            return True
        except Exception:
            return False

    async def _check_link(self) -> None:
        """Drop a stale "connected" state and try the phone again.

        The message session can die overnight while the status line still says
        connected. A dead session is cleared at once. A new attempt waits
        half a minute so an absent phone is not dialed continuously.
        """
        if self._reconnecting or self.state in ("pairing", "scanning"):
            return
        if not self.store.settings()["phoneAddress"]:
            return
        radio = await self._link_up(self.store.settings()["phoneAddress"])
        if radio and await self._map_alive():
            return
        if self.state == "connected" or self._map_path or self._pbap_path:
            async with self.lock:
                await self._close_profiles()
                self.state = "phone_nearby"
                self.detail = "The paired phone is not connected."
                self.push_status()
        if time.monotonic() - self._last_link_try < 30:
            return
        self._track(self._safe_reconnect())

    async def _refresh_presence(self) -> None:
        settings = self.store.settings()
        if not settings["phoneAddress"]:
            self.state = "not_paired"
            self.detail = "Select your iPhone to pair."
            return
        try:
            device = await self._device(settings["phoneAddress"])
            connected = await device.get_connected()
        except Exception:
            self.state = "phone_nearby"
            self.detail = "The paired phone is not visible right now."
            return
        self.state = "connected" if connected and self._map_path else "phone_nearby"
        if connected and self._map_path:
            self.detail = connected_status(settings["phoneName"])
        elif not self.detail:
            self.detail = "Phone nearby."

    async def _register_agent(self) -> None:
        if self._agent_on:
            return
        manager = await self._iface("org.bluez", "/org/bluez", "org.bluez.AgentManager1", self._system)
        await manager.call_register_agent(AGENT_PATH, "DisplayYesNo")
        try:
            await manager.call_request_default_agent(AGENT_PATH)
        except Exception:
            pass
        self._agent_on = True

    async def _unregister_agent(self) -> None:
        if not self._agent_on:
            return
        try:
            manager = await self._iface("org.bluez", "/org/bluez", "org.bluez.AgentManager1", self._system)
            await manager.call_unregister_agent(AGENT_PATH)
        except Exception:
            pass
        self._agent_on = False

    async def _start_advert(self) -> None:
        if self._advert_on:
            return
        try:
            objects = await self._managed("org.bluez", "/", self._system)
        except Exception:
            return
        for path, interfaces in objects.items():
            if "org.bluez.LEAdvertisingManager1" not in interfaces:
                continue
            try:
                manager = await self._iface("org.bluez", path, "org.bluez.LEAdvertisingManager1", self._system)
                await manager.call_register_advertisement(ADVERT_PATH, {})
                self._advert_on = True
            except Exception:
                self.detail = "Paired, but the message switches may not appear until the phone is reopened in Bluetooth settings."
            return

    async def _stop_advert(self) -> None:
        if not self._advert_on:
            return
        try:
            objects = await self._managed("org.bluez", "/", self._system)
            for path, interfaces in objects.items():
                if "org.bluez.LEAdvertisingManager1" not in interfaces:
                    continue
                manager = await self._iface("org.bluez", path, "org.bluez.LEAdvertisingManager1", self._system)
                await manager.call_unregister_advertisement(ADVERT_PATH)
        except Exception:
            pass
        self._advert_on = False

    async def _managed(self, service: str, path: str, bus: MessageBus) -> dict:
        intro = await bus.introspect(service, path)
        root = bus.get_proxy_object(service, path, intro)
        manager = root.get_interface("org.freedesktop.DBus.ObjectManager")
        return await manager.call_get_managed_objects()

    async def _iface(self, service: str, path: str, interface: str, bus: MessageBus):
        intro = await bus.introspect(service, path)
        return bus.get_proxy_object(service, path, intro).get_interface(interface)

    def _emit_threads(self) -> None:
        self.emit({"event": "threads", "threads": self.store.threads()})

