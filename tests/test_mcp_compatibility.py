"""MCP migration regressions; device operations are mocked, never executed."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp import MCPError

from ensp_mcp_server import config, runtime, server as application, topology_context
from ensp_mcp_server.tools import cli


TOOL_NAMES = {
    "register_device", "unregister_device", "list_devices", "auto_discover",
    "execute_cli", "mcp_execute", "push_config", "mcp_push_config",
    "multi_device_push_config", "save_config", "get_running_config",
    "ping_from_device", "traceroute_from_device", "health_check",
    "mcp_health_check", "multi_health_check", "discover_topology",
    "find_topo_files", "sync_topology_devices", "get_topology_context",
}
PROMPT_NAMES = {"ensp_ops_playbook", "ensp_senior_network_engineer"}
ROOT = Path(__file__).resolve().parent.parent


class InProcessCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Every test has an isolated topology file and runtime log store.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.topology_file = Path(directory.name) / "topology.json"
        self.enterContext(patch.object(topology_context, "TOPOLOGY_JSON", self.topology_file))
        self.enterContext(patch.object(runtime, "runtime", runtime.RuntimeStore()))
        self.client = await self.enterAsyncContext(Client(application.server, timeout=10))

    async def test_catalog_preserves_public_contract(self):
        self.assertEqual(self.client.protocol_version, "2026-07-28")
        catalog = {tool.name: tool for tool in await self.client.list_tools()}
        self.assertEqual(set(catalog), TOOL_NAMES)
        self.assertEqual(
            catalog["execute_cli"].input_schema["required"], ["device_ip", "command"]
        )
        self.assertEqual(catalog["register_device"].input_schema["required"], ["name", "port"])
        self.assertEqual(catalog["ping_from_device"].input_schema["properties"]["count"]["default"], 5)
        self.assertEqual(
            catalog["multi_device_push_config"].input_schema["properties"]["devices"]["items"]["required"],
            ["device_ip", "commands"],
        )
        self.assertEqual({prompt.name for prompt in await self.client.list_prompts()}, PROMPT_NAMES)
        resources = {str(resource.uri): resource for resource in await self.client.list_resources()}
        self.assertEqual(set(resources), {"ensp://topology/current", "ensp://devices/registered"})
        self.assertEqual(resources["ensp://topology/current"].mime_type, "application/json")

    async def test_device_aliases_and_optional_defaults(self):
        cases = [
            ("execute_cli", ("execute_cli", "mcp_execute"),
             {"device_ip": "R1", "command": "display version"}, ("R1", "display version")),
            ("push_config", ("push_config", "mcp_push_config"),
             {"device_ip": "R1", "commands": ["sysname R1"]}, ("R1", ["sysname R1"])),
            ("health_check", ("health_check", "mcp_health_check"),
             {"device_ip": "R1"}, ("R1",)),
        ]
        for function, names, arguments, expected in cases:
            with self.subTest(function=function), patch.object(application, function, return_value="设备正常") as handler:
                for name in names:
                    result = await self.client.call_tool(name, arguments)
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.content[0].text, "设备正常")
                    handler.assert_called_with(*expected)
                self.assertEqual(handler.call_count, len(names))
        with patch.object(application, "ping_from_device", return_value="ok") as handler:
            await self.client.call_tool("ping_from_device", {"device_ip": "R1", "target_ip": "192.0.2.1"})
            handler.assert_called_once_with("R1", "192.0.2.1", 5, source_ip=None)

    async def test_invalid_arguments_never_reach_device_handlers(self):
        cases = [
            ("register_device", {"name": "R1", "port": "2000"}),
            ("execute_cli", {"device_ip": "R1"}),
            ("push_config", {"device_ip": "R1", "commands": [42]}),
            ("multi_device_push_config", {"devices": [{"device_ip": "R1"}]}),
        ]
        handler = Mock(return_value="must not run")
        with patch.dict(application.TOOL_DISPATCH, {name: handler for name, _ in cases}):
            for name, arguments in cases:
                with self.subTest(tool=name):
                    result = await self.client.call_tool(name, arguments, raise_on_error=False)
                    self.assertTrue(result.is_error)
                    self.assertIn("Invalid", result.content[0].text)
        handler.assert_not_called()

    async def test_handler_failure_sets_error_flag_and_finishes_runtime_request(self):
        handler = Mock(side_effect=RuntimeError("模拟连接失败"))
        with patch.dict(application.TOOL_DISPATCH, {"list_devices": handler}):
            with self.assertLogs("ensp_mcp.server", level="ERROR"):
                result = await self.client.call_tool("list_devices", raise_on_error=False)
        self.assertTrue(result.is_error)
        self.assertIn("模拟连接失败", result.content[0].text)
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["active_requests"], [])
        self.assertEqual(snapshot["recent_requests"][-1]["status"], "error")
        self.assertTrue(any(event["type"] == "tool_error" for event in snapshot["recent_events"]))

    async def test_topology_file_failures_set_mcp_error_flag(self):
        cases = {"broken.topo": b"<topo>", "undecodable.topo": b"\xff",
                 "broken.json": b"{", "array.json": b"[]", "unsupported.txt": b"test",
                 "missing.topo": None}
        for filename, data in cases.items():
            path = self.topology_file.parent / filename
            if data is not None:
                path.write_bytes(data)
            with self.subTest(file=filename):
                with self.assertLogs("ensp_mcp.server", level="ERROR"):
                    result = await self.client.call_tool(
                        "discover_topology", {"file_path": str(path)}, raise_on_error=False
                    )
                self.assertTrue(result.is_error, result.content[0].text)
                self.assertEqual(runtime.snapshot()["recent_requests"][-1]["status"], "error")

    async def test_actual_cli_failures_set_mcp_error_flag(self):
        cases = [
            ("execute_cli", {"device_ip": "R1", "command": "display clock"}),
            ("push_config", {"device_ip": "R1", "commands": ["sysname R1"]}),
            ("mcp_push_config", {"device_ip": "R1", "commands": ["sysname R1"]}),
            ("multi_device_push_config", {"devices": [{"device_ip": "R1", "commands": ["sysname R1"]}]}),
            ("get_running_config", {"device_ip": "R1"}),
            ("save_config", {"device_ip": "R1"}),
            ("ping_from_device", {"device_ip": "R1", "target_ip": "192.0.2.1"}),
            ("traceroute_from_device", {"device_ip": "R1", "target_ip": "192.0.2.1"}),
        ]
        with patch.object(cli, "_connect", side_effect=TimeoutError("simulated timeout")):
            for name, args in cases:
                with self.subTest(tool=name), self.assertLogs("ensp_mcp.server", level="ERROR"):
                    result = await self.client.call_tool(name, args, raise_on_error=False)
                self.assertTrue(result.is_error)
                self.assertIn("simulated timeout", result.content[0].text)
                self.assertEqual(runtime.snapshot()["recent_requests"][-1]["status"], "error")

    async def test_device_rejected_commands_set_mcp_error_flag(self):
        cases = [
            ("execute_cli", {"device_ip": "R1", "command": "display invalid"}),
            ("mcp_execute", {"device_ip": "R1", "command": "display invalid"}),
            ("push_config", {"device_ip": "R1", "commands": ["invalid"]}),
            ("multi_device_push_config", {"devices": [{"device_ip": "R1", "commands": ["invalid"]}]}),
            ("get_running_config", {"device_ip": "R1"}),
            ("save_config", {"device_ip": "R1"}),
            ("ping_from_device", {"device_ip": "R1", "target_ip": "invalid"}),
            ("traceroute_from_device", {"device_ip": "R1", "target_ip": "invalid"}),
        ]
        with patch.object(cli, "_connect") as connect:
            console = connect.return_value.__enter__.return_value
            for output in (" ^\nError: Unrecognized command found at '^' position.",
                           " ^\nError: Wrong parameter found at '^' position."):
                console.send_command.return_value = output
                console.send_config_set.return_value = output
                console.save_config.return_value = output
                for name, args in cases:
                    with self.subTest(tool=name, output=output):
                        with self.assertLogs("ensp_mcp.server", level="ERROR"):
                            result = await self.client.call_tool(name, args, raise_on_error=False)
                        self.assertTrue(result.is_error, result.content[0].text)
                        self.assertIn("Error:", result.content[0].text)

    async def test_error_word_in_normal_cli_output_is_not_failure(self):
        output = "description Error: investigation\nInput errors: 0\nError packets: 0"
        with patch.object(cli, "_connect") as connect:
            connect.return_value.__enter__.return_value.send_command.return_value = output
            result = await self.client.call_tool("execute_cli", {"device_ip": "R1", "command": "display interface"})
        self.assertFalse(result.is_error)
        self.assertIn(output, result.content[0].text)

    async def test_health_check_with_rejected_clock_command_is_failure(self):
        with patch.object(cli, "_resolve_device", return_value=("127.0.0.1", 2000)), \
             patch.object(cli, "socket"), patch.object(cli, "HuaweiConsole") as connect:
            connect.return_value.__enter__.return_value.send_command.return_value = "Error: Unrecognized command"
            with self.assertLogs("ensp_mcp.server", level="ERROR"):
                result = await self.client.call_tool("health_check", {"device_ip": "R1"}, raise_on_error=False)
        self.assertTrue(result.is_error)

    async def test_rejected_section_is_not_reported_as_configuration(self):
        with patch.object(cli, "_connect") as connect:
            connect.return_value.__enter__.return_value.send_command.return_value = (
                "Error: Unrecognized command found at '^' position."
            )
            with self.assertLogs("ensp_mcp.server", level="ERROR"):
                result = await self.client.call_tool(
                    "get_running_config", {"device_ip": "R1", "section": "interface"}, raise_on_error=False
                )
        self.assertTrue(result.is_error)
        self.assertIn("section", result.content[0].text)

    async def test_failed_health_checks_set_mcp_error_flag(self):
        with patch.object(cli, "_resolve_device", return_value=("127.0.0.1", 2000)), \
             patch.object(cli, "socket") as socket_module:
            socket_module.socket.return_value.connect.side_effect = OSError("simulated refusal")
            for name, args in (("health_check", {"device_ip": "R1"}),
                               ("multi_health_check", {"device_names": ["R1"]})):
                with self.subTest(tool=name), self.assertLogs("ensp_mcp.server", level="ERROR"):
                    result = await self.client.call_tool(name, args, raise_on_error=False)
                self.assertTrue(result.is_error)

    async def test_blocking_handler_keeps_runtime_context_in_worker_thread(self):
        loop_thread = threading.get_ident()

        def handler(arguments):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            runtime.publish("console_test", text="设备回显")
            return 42

        with patch.dict(application.TOOL_DISPATCH, {"health_check": handler}):
            result = await self.client.call_tool("health_check", {"device_ip": "R1"})
        self.assertEqual(result.content[0].text, "42")
        snapshot = runtime.snapshot()
        request = snapshot["recent_requests"][-1]
        event = next(event for event in snapshot["recent_events"] if event["type"] == "console_test")
        self.assertEqual(request["status"], "done")
        self.assertEqual(event["request_id"], request["request_id"])
        self.assertEqual(event["device"], "R1")
        self.assertEqual(event["tool"], "health_check")

    async def test_topology_resource_and_prompts_refresh_between_requests(self):
        empty = await self.client.read_resource("ensp://topology/current")
        self.assertEqual(json.loads(empty[0].text)["devices"], [])
        for device in ("R1", "R2"):
            payload = {"devices": [{"name": device, "model": "AR2220", "comPort": 2000}], "links": []}
            self.topology_file.write_text(json.dumps(payload), encoding="utf-8")
            contents = await self.client.read_resource("ensp://topology/current")
            self.assertEqual(contents[0].mime_type, "application/json")
            self.assertEqual(json.loads(contents[0].text), payload)
            context = await self.client.call_tool("get_topology_context")
            self.assertIn(device, context.content[0].text)
            listing = await self.client.list_resources()
            topology = next(resource for resource in listing if str(resource.uri) == "ensp://topology/current")
            self.assertIn("1 devices", topology.description)
            for name in PROMPT_NAMES:
                prompt = await self.client.get_prompt(name)
                self.assertEqual(len(prompt.messages), 2)
                self.assertEqual([message.role for message in prompt.messages], ["user", "user"])
                self.assertIn(device, prompt.messages[1].content.text)
                self.assertIn("latest uploaded topology", prompt.description)

    async def test_registry_resource_and_unknown_components(self):
        with patch.object(application, "list_devices", return_value="R1:2000"):
            contents = await self.client.read_resource("ensp://devices/registered")
        self.assertEqual(contents[0].text, "R1:2000")
        self.assertEqual(contents[0].mime_type, "text/plain")
        with self.assertRaises(MCPError):
            await self.client.read_resource("ensp://missing/resource")
        with self.assertRaises(MCPError):
            await self.client.get_prompt("missing_prompt")
        missing = await self.client.call_tool("missing_tool", raise_on_error=False)
        self.assertTrue(missing.is_error)


class StdioCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def _check_protocol(self, mode: str, expected: str):
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "ensp_mcp_server.server"],
            cwd=str(ROOT),
            env={"FASTMCP_LOG_LEVEL": "ERROR", "ENSP_LOG_LEVEL": "ERROR"},
            keep_alive=False,
        )
        async with Client(transport, mode=mode, timeout=20, init_timeout=20) as client:
            self.assertEqual(client.protocol_version, expected)
            self.assertEqual({tool.name for tool in await client.list_tools()}, TOOL_NAMES)
            result = await client.call_tool("get_topology_context")
            self.assertFalse(result.is_error)
            self.assertEqual(len(await client.read_resource("ensp://topology/current")), 1)
            self.assertEqual(len((await client.get_prompt("ensp_ops_playbook")).messages), 2)

    async def test_latest_protocol_over_real_stdio(self):
        await self._check_protocol("2026-07-28", "2026-07-28")

    async def test_legacy_protocol_over_real_stdio(self):
        await self._check_protocol("legacy", "2025-11-25")


class AutoDiscoverEncodingTests(unittest.TestCase):
    """tasklist/netstat emit OEM code page bytes; decoding must not crash."""

    def setUp(self):
        self.enterContext(patch.dict(config._device_registry, {}, clear=True))

    def test_console_bytes_decode_without_utf8_crash(self):
        # "系统 Development" in cp936 contains bytes invalid as UTF-8.
        raw = "系统  Device1  2000  LISTENING".encode("cp936")
        self.assertEqual(config._decode_console_bytes(raw), "系统  Device1  2000  LISTENING")

    def test_auto_discover_survives_gbk_tasklist_output(self):
        tasklist = "映像名称 PID 会话名\r\neNSP_Client.exe          22284 Console                    1     67,060 K\r\n".encode("cp936")

        def fake_run(command, **kwargs):
            if command[0] == "tasklist":
                return subprocess.CompletedProcess(command, 0, stdout=tasklist, stderr=b"")
            return subprocess.CompletedProcess(
                command, 0, stdout=b"  TCP    0.0.0.0:2000    0.0.0.0:0    LISTENING    22284\r\n", stderr=b""
            )

        with patch.object(config.subprocess, "run", side_effect=fake_run), \
             patch.object(config, "_get_sysname_from_port", return_value=None), \
             patch.object(config, "_save_registry"):
            report = config.auto_discover_devices()
        self.assertIn("自动发现完成", report)
        self.assertIn("2000", report)


if __name__ == "__main__":
    unittest.main()
