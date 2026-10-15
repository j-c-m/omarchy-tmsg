"""One terminal screen.

A second launch hands the conversation to the screen that is already
open and focuses that window. It does not start another one.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess


def socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/io.github.j-c-m.tmsg-{os.getuid()}"
    return os.path.join(runtime, "io.github.j-c-m.tmsg", "tui.sock")


def terminal_program() -> str:
    root = os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy"
    return os.path.join(root, "bin", "omarchy-launch-terminal")


def launcher_script() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "bin", "tmsg")


def terminal_command(thread: str, standalone: bool) -> list[str]:
    """Argv for a terminal whose command is the shell launcher.

    The launcher is a shell script. Running it with Python exits at once,
    and the new window closes.
    """
    command = [terminal_program(), launcher_script(), "--inside"]
    if standalone:
        command.append("--standalone")
    if thread:
        command.extend(["--thread", thread])
    return command


def exec_in_terminal(thread: str, standalone: bool) -> None:
    """Replace this process with a terminal running the screen."""
    command = terminal_command(thread, standalone)
    os.execvp(command[0], command)


def ancestor_pids(pid: int | None = None) -> list[int]:
    """This process, then each parent, stopping at init."""
    current = os.getpid() if pid is None else pid
    found: list[int] = []
    seen: set[int] = set()
    while current > 1 and current not in seen:
        seen.add(current)
        found.append(current)
        current = _parent_pid(current)
    return found


def address_for_pids(clients: list[dict], pids: list[int]) -> str:
    """Hyprland address of the nearest ancestor that owns a window."""
    by_pid: dict[int, dict] = {}
    for client in clients:
        try:
            by_pid[int(client.get("pid") or 0)] = client
        except (TypeError, ValueError):
            continue
    for pid in pids:
        client = by_pid.get(pid)
        address = str(client.get("address") or "") if client else ""
        if address:
            return address
    return ""


def focus_address(address: str) -> None:
    if not address:
        return
    subprocess.run(
        ["hyprctl", "dispatch", "focuswindow", f"address:{address}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def focus_this_window() -> None:
    focus_address(address_for_pids(_hypr_clients(), ancestor_pids()))


def hand_off(thread: str) -> bool:
    """True when the screen that is already open took this launch."""
    return _forward(socket_path(), thread)


def claim(thread: str) -> socket.socket | None:
    """Listen for later launches. None means a screen already took this one."""
    path = socket_path()
    if _forward(path, thread):
        return None
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(path)
    except OSError:
        sock.close()
        _forward(path, thread)
        return None
    os.chmod(path, 0o600)
    sock.listen(8)
    sock.setblocking(False)
    return sock


def release(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass
    try:
        os.unlink(socket_path())
    except FileNotFoundError:
        pass


def _forward(path: str, thread: str) -> bool:
    """True when a running screen accepted the launch."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        sock.connect(path)
    except OSError:
        sock.close()
        return False
    try:
        message = json.dumps({"thread": thread}) + "\n"
        sock.sendall(message.encode())
        sock.shutdown(socket.SHUT_WR)
        try:
            sock.recv(64)
        except OSError:
            pass
    finally:
        sock.close()
    return True


def _parent_pid(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("PPid:"):
            return int(line.split()[1])
    return 0


def _hypr_clients() -> list[dict]:
    try:
        raw = subprocess.check_output(
            ["hyprctl", "clients", "-j"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError):
        return []
    return data if isinstance(data, list) else []
