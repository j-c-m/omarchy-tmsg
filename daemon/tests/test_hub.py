"""The daemon socket broadcasts events and accepts command lines."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hub import Hub, socket_path


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_client_receives_events_and_sends_commands(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "messagesd.sock")
        seen = []

        async def on_line(text: str) -> None:
            seen.append(json.loads(text))

        hub = Hub()
        await hub.listen(path, on_line)
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b'{"cmd":"threads"}\n')
        await writer.drain()
        for _ in range(20):
            if seen:
                break
            await asyncio.sleep(0.01)
        with open(os.devnull, "w") as quiet:
            old = sys.stdout
            sys.stdout = quiet
            try:
                hub.publish({"event": "threads", "threads": []})
            finally:
                sys.stdout = old
        await asyncio.sleep(0.05)
        line = await asyncio.wait_for(reader.readline(), timeout=1)
        self.assertEqual(json.loads(line), {"event": "threads", "threads": []})
        for _ in range(20):
            if seen:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(seen, [{"cmd": "threads"}])
        writer.close()
        await writer.wait_closed()
        hub.server.close()
        await hub.server.wait_closed()

    def test_socket_path_uses_plugin_namespace(self):
        self.assertIn("io.github.j-c-m.tmsg", socket_path())


if __name__ == "__main__":
    unittest.main()
