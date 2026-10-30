"""A second launch focuses the screen that is already open."""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest

from types import SimpleNamespace
from unittest import mock

from tui import instance as instance_module
from tui.instance import (
    address_for_pids,
    ancestor_pids,
    claim,
    focus_command,
    focus_this_window,
    lua_focus_command,
    normalize_address,
    release,
    socket_path,
    terminal_command,
    workspace_for_address,
)


class InstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._runtime = tempfile.TemporaryDirectory()
        self._old = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self._runtime.name

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._old
        self._runtime.cleanup()

    def test_a_second_launch_reaches_the_first_screen(self) -> None:
        holder = claim("")
        self.assertIsNotNone(holder)
        got: list[str] = []

        def accept_one() -> None:
            assert holder is not None
            holder.setblocking(True)
            client, _addr = holder.accept()
            data = b""
            while b"\n" not in data:
                data += client.recv(256)
            got.append(json.loads(data.decode())["thread"])
            client.sendall(b'{"ok":true}\n')
            client.close()

        thread = threading.Thread(target=accept_one)
        thread.start()
        try:
            self.assertIsNone(claim("tel:+15550001111"))
        finally:
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(got, ["tel:+15550001111"])
        release(holder)
        self.assertFalse(os.path.exists(socket_path()))

    def test_a_dead_socket_does_not_block_a_new_screen(self) -> None:
        holder = claim("")
        assert holder is not None
        holder.close()
        self.assertTrue(os.path.exists(socket_path()))
        nxt = claim("")
        self.assertIsNotNone(nxt)
        release(nxt)

    def test_the_nearest_window_pid_is_the_one_to_focus(self) -> None:
        clients = [
            {"pid": 10, "address": "0xparent"},
            {"pid": 20, "address": "0xself"},
        ]
        self.assertEqual(address_for_pids(clients, [20, 10, 1]), "0xself")
        self.assertEqual(address_for_pids(clients, [11, 10]), "0xparent")
        self.assertEqual(address_for_pids(clients, [3]), "")

    def test_the_terminal_runs_the_shell_launcher(self) -> None:
        command = terminal_command("tel:+15550001111", False)
        self.assertTrue(command[1].endswith("/bin/tmsg"))
        self.assertEqual(command[2:], ["--inside", "--thread", "tel:+15550001111"])
        self.assertNotIn("python3", command)
        self.assertNotIn(sys.executable, command)

    def test_ancestor_pids_include_this_process(self) -> None:
        pids = ancestor_pids()
        self.assertEqual(pids[0], os.getpid())
        self.assertIn(os.getppid(), pids)

    def test_socket_path_uses_plugin_namespace(self) -> None:
        self.assertIn("io.github.j-c-m.tmsg", socket_path())

    def test_focus_switches_to_the_windows_workspace_first(self) -> None:
        self.assertEqual(
            focus_command("0xabc", "3"),
            ["hyprctl", "--batch", "dispatch workspace 3;dispatch focuswindow address:0xabc"],
        )

    def test_focus_without_a_workspace_stays_a_single_dispatch(self) -> None:
        self.assertEqual(
            focus_command("0xabc", ""),
            ["hyprctl", "dispatch", "focuswindow", "address:0xabc"],
        )

    def test_workspace_lookup_uses_the_matched_window(self) -> None:
        clients = [
            {"pid": 10, "address": "0xparent", "workspace": {"id": 1, "name": "1"}},
            {"pid": 20, "address": "0xself", "workspace": {"id": 3, "name": "3"}},
        ]
        self.assertEqual(workspace_for_address(clients, "0xself"), "3")
        self.assertEqual(workspace_for_address(clients, "0xmissing"), "")
        self.assertEqual(workspace_for_address(clients, ""), "")

    def test_special_workspaces_keep_the_old_dispatch(self) -> None:
        clients = [{"pid": 20, "address": "0xself", "workspace": {"id": -98, "name": "special"}}]
        self.assertEqual(workspace_for_address(clients, "0xself"), "")

    def test_focus_this_window_lands_on_another_workspace(self) -> None:
        clients = [{"pid": 20, "address": "0xself", "workspace": {"id": 3, "name": "3"}}]
        calls = self._run_focus(clients, returncodes=[0])
        self.assertEqual(
            calls,
            [["hyprctl", "dispatch", "hl.dsp.focus({ window = 'address:0xself' })"]],
        )

    def test_focus_falls_back_when_the_lua_form_is_unknown(self) -> None:
        clients = [{"pid": 20, "address": "0xself", "workspace": {"id": 3, "name": "3"}}]
        calls = self._run_focus(clients, returncodes=[1, 0])
        self.assertEqual(
            calls,
            [
                ["hyprctl", "dispatch", "hl.dsp.focus({ window = 'address:0xself' })"],
                ["hyprctl", "--batch", "dispatch workspace 3;dispatch focuswindow address:0xself"],
            ],
        )

    def test_focus_normalizes_a_bare_address(self) -> None:
        self.assertEqual(normalize_address("abc"), "0xabc")
        self.assertEqual(normalize_address("0xabc"), "0xabc")
        self.assertEqual(
            lua_focus_command("abc"),
            ["hyprctl", "dispatch", "hl.dsp.focus({ window = 'address:0xabc' })"],
        )

    def _run_focus(self, clients: list[dict], returncodes: list[int]) -> list[list[str]]:
        calls: list[list[str]] = []
        codes = list(returncodes)

        def fake_run(argv: list[str], **_: object) -> object:
            calls.append(list(argv))
            return SimpleNamespace(returncode=codes.pop(0) if codes else 0)

        with (
            mock.patch.object(instance_module, "_hypr_clients", return_value=clients),
            mock.patch.object(instance_module, "ancestor_pids", return_value=[20]),
            mock.patch.object(instance_module.subprocess, "run", side_effect=fake_run),
        ):
            focus_this_window()
        return calls


if __name__ == "__main__":
    unittest.main()
