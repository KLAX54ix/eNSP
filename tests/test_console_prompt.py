"""Exercise VRP prompt handling through a local TCP console simulator."""

from __future__ import annotations

import socket
import threading
import time
import unittest
from unittest.mock import patch

from fastmcp import Client

from ensp_mcp_server import config, server as application
from ensp_mcp_server.console import HuaweiConsole


class SimulatedRouter:
    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(2)
        self.listener.settimeout(3)
        self.port = self.listener.getsockname()[1]
        self.errors: list[Exception] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @staticmethod
    def _reply(connection: socket.socket, body: str, prompt: str) -> None:
        # eNSP can emit a second prompt after the first has arrived.
        connection.sendall(f"{body}{prompt}".encode())
        time.sleep(0.02)
        connection.sendall(f"\r\n{prompt}".encode())

    def _serve(self) -> None:
        try:
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(3)
                with connection.makefile("rb") as incoming:
                    hostname = "Huawei"
                    system_view = False
                    while line := incoming.readline():
                        raw_command = line.decode("utf-8").rstrip("\r\n")
                        command = raw_command.strip()
                        prompt = f"[{hostname}]" if system_view else f"<{hostname}>"
                        if not command:
                            self._reply(connection, "\r\n", prompt)
                        elif command == "system-view":
                            system_view = True
                            self._reply(connection, f"{command}\r\n", f"[{hostname}]")
                        elif command.startswith("sysname "):
                            hostname = command.split(maxsplit=1)[1]
                            self._reply(connection, f"{command}\r\n", f"[{hostname}]")
                        elif command == "return":
                            system_view = False
                            self._reply(connection, f"{command}\r\n", f"<{hostname}>")
                        elif command == "no-prompt":
                            connection.sendall(b"no-prompt\r\npartial output")
                        elif command == "display page":
                            connection.sendall(b"display page\r\nfirst page\r\n---- More ----")
                            if connection.recv(1) == b" ":
                                self._reply(connection, "\r\nsecond page\r\n", prompt)
                        elif command == "display syslog":
                            # Prompt arrives first, then an asynchronous syslog
                            # line arrives after it (seen on S5700 after config
                            # changes). The prompt must still end the read.
                            connection.sendall(f"{command}\r\nvalue\r\n{prompt}".encode())
                            time.sleep(0.05)
                            connection.sendall(
                                b"\r\nOct  6 2026 16:00:00 Huawei DS/4/DATASYNC_CFGCHANGE: changed.\r\n"
                            )
                        elif command == "display brackets":
                            connection.sendall(b"display brackets\r\n<interface>\r\n")
                            time.sleep(0.03)
                            self._reply(connection, "second line\r\n", prompt)
                        else:
                            self._reply(connection, f"{command}\r\nRouter Software Version 1\r\n", prompt)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self.errors.append(exc)
        finally:
            self.listener.close()

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=3)


class ConsolePromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = SimulatedRouter()
        self.addCleanup(self.router.close)
        self.registry = patch.dict(config._device_registry, {"AR1": self.router.port}, clear=True)
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def test_default_prompt_is_learned_and_command_returns_without_delay(self) -> None:
        started = time.monotonic()
        with HuaweiConsole("127.0.0.1", self.router.port, timeout=2) as console:
            self.assertEqual(console.device_label, "AR1")
            self.assertEqual(console._learned_prompt, "Huawei")
            output = console.send_command("display version", timeout=2)
        self.assertEqual(output, "Router Software Version 1")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_sysname_change_and_system_view_prompts(self) -> None:
        with HuaweiConsole("127.0.0.1", self.router.port, timeout=2) as console:
            output = console.send_config_set(["sysname AR2", "display version"])
            self.assertEqual(console._learned_prompt, "AR2")
            self.assertIn("Router Software Version 1", output)
            self.assertEqual(console.send_command("display version"), "Router Software Version 1")

    def test_non_prompt_angle_brackets_do_not_end_output(self) -> None:
        with HuaweiConsole("127.0.0.1", self.router.port, timeout=2) as console:
            output = console.send_command("display brackets", timeout=2)
        self.assertIn("<interface>", output)
        self.assertIn("second line", output)

    def test_pagination_continues_until_final_prompt(self) -> None:
        with HuaweiConsole("127.0.0.1", self.router.port, timeout=2) as console:
            output = console.send_command("display page", timeout=2)
        self.assertIn("first page", output)
        self.assertIn("second page", output)
        self.assertNotIn("More", output)

    def test_missing_prompt_raises_explicit_timeout(self) -> None:
        with HuaweiConsole("127.0.0.1", self.router.port, timeout=2) as console:
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "partial output"):
                console.send_command("no-prompt", timeout=0.15)
            self.assertLess(time.monotonic() - started, 0.8)

    def test_async_syslog_after_prompt_does_not_time_out(self) -> None:
        started = time.monotonic()
        with HuaweiConsole("127.0.0.1", self.router.port, timeout=2) as console:
            output = console.send_command("display syslog", timeout=2)
        self.assertIn("value", output)
        self.assertLess(time.monotonic() - started, 1.5)


class ToolConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_execute_on_device_with_default_huawei_prompt(self) -> None:
        router = SimulatedRouter()
        self.addCleanup(router.close)
        with patch.dict(config._device_registry, {"AR1": router.port}, clear=True):
            async with Client(application.server, timeout=3) as client:
                result = await client.call_tool(
                    "mcp_execute", {"device_ip": "AR1", "command": "display version"}
                )
        self.assertFalse(result.is_error)
        self.assertIn("Router Software Version 1", result.content[0].text)


if __name__ == "__main__":
    unittest.main()
