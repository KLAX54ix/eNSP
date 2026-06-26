"""
Lightweight Huawei console client for eNSP.
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
import weakref

from .runtime import publish, resolve_device_label

logger = logging.getLogger("ensp_mcp.console")

_MORE_RE = re.compile(r"----\s*More\s*----", re.IGNORECASE)
_ANSI_CSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(rb"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_IAC = 0xFF
_ACTIVE_CONSOLES: "weakref.WeakSet[HuaweiConsole]" = weakref.WeakSet()
_ACTIVE_CONSOLES_LOCK = threading.RLock()


def _strip_iac(data: bytes) -> bytes:
    """Remove Telnet IAC negotiation bytes."""
    result = bytearray()
    i = 0
    while i < len(data):
        if data[i] == _IAC and i + 2 < len(data):
            i += 3
        else:
            result.append(data[i])
            i += 1
    return bytes(result)


def _strip_terminal_escapes(data: bytes) -> bytes:
    data = _ANSI_OSC_RE.sub(b"", data)
    data = _ANSI_CSI_RE.sub(b"", data)
    return data.replace(b"\x1b", b"")


def _normalize_terminal_bytes(data: bytes) -> bytes:
    data = _strip_iac(data)
    data = _strip_terminal_escapes(data)
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _tail_text(data: bytes, limit: int = 768) -> str:
    return _normalize_terminal_bytes(data[-limit:]).decode("utf-8", errors="ignore")


def _last_non_empty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _clean_text(data: bytes) -> str:
    text = _normalize_terminal_bytes(data).decode("utf-8", errors="ignore")
    text = _MORE_RE.sub("", text)
    lines = [re.sub(r" {8,}", " ", line).rstrip() for line in text.splitlines()]
    return "\n".join(lines)


class HuaweiConsole:
    """Very small socket client for eNSP control ports."""

    def __init__(self, host: str, port: int, timeout: int = 15):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None

    @property
    def device_label(self) -> str:
        return resolve_device_label(self.host, self.port) or f"{self.host}:{self.port}"

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.host, self.port))
        time.sleep(0.3)
        self._sock.sendall(b"\r\n")
        self._read_until_prompt(timeout=5)
        with _ACTIVE_CONSOLES_LOCK:
            _ACTIVE_CONSOLES.add(self)
        publish(
            "device_connect",
            device=self.device_label,
            text=f"Connected to {self.device_label}",
        )
        logger.debug("Connected to %s:%d", self.host, self.port)

    def _prompt_tokens(self) -> tuple[str, ...]:
        candidates: list[str] = []
        for value in (self.device_label, self.host, f"{self.host}:{self.port}"):
            token = str(value or "").strip()
            if token and token not in candidates:
                candidates.append(token)
        return tuple(candidates)

    def _is_prompt_line(self, line: str) -> bool:
        stripped = str(line or "").strip()
        if len(stripped) < 3 or stripped[0] not in "<[" or stripped[-1] not in ">]":
            return False

        inner = stripped[1:-1].lstrip("~*")
        for token in self._prompt_tokens():
            if inner == token or inner.startswith(f"{token}-") or inner.startswith(f"{token}/"):
                return True
        return False

    def _tail_has_prompt(self, data: bytes) -> bool:
        return self._is_prompt_line(_last_non_empty_line(_tail_text(data)))

    def disconnect(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        with _ACTIVE_CONSOLES_LOCK:
            _ACTIVE_CONSOLES.discard(self)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()

    def send_command(self, command: str, timeout: int = 0) -> str:
        t = timeout or self.timeout
        self._send_line(command)
        output = self._read_until_prompt(timeout=t)
        lines = output.splitlines()
        if lines and lines[0].strip() == command.strip():
            lines = lines[1:]
        if lines and self._is_prompt_line(lines[-1].strip()):
            lines = lines[:-1]
        return "\n".join(lines)

    def send_config_set(self, commands: list[str]) -> str:
        all_output = []
        self._send_line("system-view")
        all_output.append(self._read_until_prompt(timeout=5))
        for cmd in commands:
            self._send_line(cmd)
            all_output.append(self._read_until_prompt(timeout=5))
        self._send_line("return")
        all_output.append(self._read_until_prompt(timeout=5))
        return "\n".join(all_output)

    def send_command_timing(self, command: str, delay: float = 2.0) -> str:
        self._send_line(command)
        time.sleep(delay)
        return self._read_available()

    def _send_line(self, text: str) -> None:
        if not self._sock:
            raise RuntimeError("Socket not connected")
        publish("command_send", device=self.device_label, text=text, payload={"command": text})
        self._sock.sendall((text + "\r\n").encode("utf-8"))

    def _publish_output_block(self, data: bytes) -> None:
        text = _clean_text(data)
        if not text.strip():
            return

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or self._is_prompt_line(stripped):
                continue
            publish("output", device=self.device_label, text=line)

    def _read_until_prompt(self, timeout: int = 15) -> str:
        if not self._sock:
            raise RuntimeError("Socket not connected")

        buf = b""
        pending = bytearray()
        deadline = time.monotonic() + timeout
        self._sock.settimeout(1.0)

        while time.monotonic() < deadline:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                chunk = _strip_iac(chunk)
                buf += chunk
                pending.extend(chunk)

                tail_text = _tail_text(buf)

                if _MORE_RE.search(tail_text):
                    self._sock.sendall(b" ")
                    continue

                if b"\n" in chunk:
                    self._publish_output_block(bytes(pending))
                    pending.clear()

                if self._tail_has_prompt(buf):
                    if pending:
                        self._publish_output_block(bytes(pending))
                        pending.clear()
                    break
            except socket.timeout:
                if self._tail_has_prompt(buf):
                    if pending:
                        self._publish_output_block(bytes(pending))
                        pending.clear()
                    break
                continue
            except OSError:
                break

        self._sock.settimeout(self.timeout)
        if pending:
            self._publish_output_block(bytes(pending))
        text = _clean_text(buf)
        return text

    def _read_available(self) -> str:
        if not self._sock:
            raise RuntimeError("Socket not connected")

        buf = b""
        pending = bytearray()
        self._sock.settimeout(0.5)
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                chunk = _strip_iac(chunk)
                buf += chunk
                pending.extend(chunk)
        except (socket.timeout, OSError):
            pass
        self._sock.settimeout(self.timeout)
        if pending:
            self._publish_output_block(bytes(pending))
        text = _clean_text(buf)
        return text


def disconnect_active_devices(device_names: list[str] | None = None) -> dict[str, object]:
    target_names = {str(name).strip() for name in (device_names or []) if str(name).strip()}
    disconnected: list[str] = []
    skipped: list[str] = []

    with _ACTIVE_CONSOLES_LOCK:
        consoles = list(_ACTIVE_CONSOLES)

    for console in consoles:
        label = console.device_label
        if target_names and label not in target_names:
            continue
        try:
            console.disconnect()
            disconnected.append(label)
        except Exception:
            skipped.append(label)

    return {
        "disconnected": disconnected,
        "skipped": skipped,
        "count": len(disconnected),
    }
