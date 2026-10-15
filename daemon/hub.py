"""One daemon, many UI clients.

The shell overlay keeps the original stdin and stdout. A terminal UI
connects to the Unix socket and speaks the same JSON lines.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

def socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/io.github.j-c-m.tmsg-{os.getuid()}"
    return os.path.join(runtime, "io.github.j-c-m.tmsg", "messagesd.sock")


class Hub:
    """Broadcast events and accept command lines from socket clients."""

    def __init__(self) -> None:
        self.writers: list[asyncio.StreamWriter] = []
        self.on_line = None
        self.server: asyncio.Server | None = None
        # The event loop keeps only a weak reference to tasks. Without
        # this set, a drain can be garbage-collected before it finishes.
        self._drains: set[asyncio.Task] = set()

    def publish(self, event: dict) -> None:
        text = json.dumps(event, ensure_ascii=False) + "\n"
        sys.stdout.write(text)
        sys.stdout.flush()
        data = text.encode()
        for writer in list(self.writers):
            if writer.is_closing():
                self._drop(writer)
                continue
            writer.write(data)
            try:
                task = asyncio.get_running_loop().create_task(self._drain(writer))
            except RuntimeError:
                pass
            else:
                self._drains.add(task)
                task.add_done_callback(self._drains.discard)

    async def _drain(self, writer: asyncio.StreamWriter) -> None:
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            self._drop(writer)

    def _drop(self, writer: asyncio.StreamWriter) -> None:
        if writer in self.writers:
            self.writers.remove(writer)

    async def listen(self, path: str, on_line) -> asyncio.Server:
        """Replace a stale socket and serve. `on_line` receives one command string."""
        self.on_line = on_line
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.server = await asyncio.start_unix_server(self._client, path=path)
        os.chmod(path, 0o600)
        return self.server

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text and self.on_line is not None:
                    await self.on_line(text)
        finally:
            self._drop(writer)
            writer.close()
