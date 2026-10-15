"""A second launch focuses the screen that is already open."""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest

from tui.instance import address_for_pids, ancestor_pids, claim, release, socket_path, terminal_command


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


if __name__ == "__main__":
    unittest.main()
