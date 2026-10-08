"""Regression coverage for destructive input, partial batches and save completion."""

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
import http.client
import http.server
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastmcp import Client

from ensp_mcp_server import config, server as application, web_ui
from ensp_mcp_server.console import HuaweiConsole
from ensp_mcp_server.exceptions import ConfigBatchError, ToolFailure
from ensp_mcp_server.tools import cli, topology


@contextmanager
def isolated_registry():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with patch.dict(config._device_registry, {}, clear=True), \
             patch.object(config, "_REGISTRY_FILE", str(root / "devices.json")), \
             patch.object(config, "_TOPOLOGY_MANIFEST_FILE", root / "manifest.json"), \
             patch.object(web_ui, "TOPO_JSON", root / "topology.json"):
            config.register_device("Manual", 2345, username="admin", password="secret")
            yield root


INVALID_TOPOLOGIES = [
    [], {}, {"devices": None}, {"devices": {}}, {"devices": [None]},
    {"devices": [{"name": "R1"}]}, {"devices": [{"name": " ", "comPort": 2000}]},
    {"devices": [{"name": "R1", "comPort": 65536}]},
    {"devices": [{"name": "R1", "comPort": True}]},
    {"devices": [{"name": "R1", "comPort": 1.5}]},
    {"devices": [{"name": "R1", "comPort": 2000}, {"name": "r1", "comPort": 2001}]},
    {"devices": [{"name": "A", "comPort": 2000}, {"name": "B", "comPort": 2000}]},
    {"devices": [], "links": None}, {"devices": [], "links": [42]},
]


class TopologySafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(isolated_registry())
        config.sync_topology_devices([{"name": "Managed", "comPort": 2000}])

    def state(self):
        return deepcopy(config._device_registry), {
            path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()
            and path.name != "input.json"
        }

    def test_invalid_json_cannot_delete_existing_devices_or_change_files(self):
        before = self.state()
        for payload in INVALID_TOPOLOGIES:
            with self.subTest(payload=payload):
                path = self.root / "input.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsInstance(config.sync_topology_devices_from_file(str(path)), ToolFailure)
                self.assertEqual(self.state(), before)

    def test_direct_sync_validates_entire_list_before_mutation(self):
        before = self.state()
        for payload in (None, {}, [{"name": "Good", "port": 2001}, {"name": "Bad", "port": -1}]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                config.sync_topology_devices(payload)
            self.assertEqual(self.state(), before)

    def test_repeat_sync_preserves_credentials_and_empty_topology_restores_manual(self):
        devices = [{"name": "Manual", "comPort": "2001"}, {"name": "PC", "comPort": 0}]
        config.sync_topology_devices(devices)
        self.assertEqual(config._device_registry["Manual"]["password"], "secret")
        repeated = config.sync_topology_devices(devices)
        self.assertEqual(repeated["count"], 0)
        config.sync_topology_devices([])
        self.assertEqual(config._device_registry, {
            "Manual": {"port": 2345, "username": "admin", "password": "secret", "_source": "manual"},
        })
        self.assertEqual(json.loads((self.root / "manifest.json").read_text())["managed_names"], [])

    def test_persistence_failure_restores_registry_manifest_and_snapshot(self):
        snapshot = self.root / "topology.json"
        snapshot.write_bytes(b'{"devices": [], "links": []}\n')
        before = self.state()
        original_write = config._write_json_atomic
        for failing_file in ("manifest.json", "topology.json"):
            def write(path, payload):
                if path.name == failing_file:
                    raise OSError("simulated disk failure")
                original_write(path, payload)

            with self.subTest(file=failing_file), patch.object(config, "_write_json_atomic", side_effect=write):
                payload = {"devices": [{"name": "New", "comPort": 2002}], "links": []}
                with self.assertRaisesRegex(OSError, "disk failure"):
                    config.sync_topology_devices(payload["devices"], snapshot=(snapshot, payload))
            self.assertEqual(self.state(), before)

    def test_invalid_manual_port_and_name_never_change_registry(self):
        before = self.state()
        for name, port in (("Bad", 0), ("Bad", 65536), ("Bad", True), ("Bad", 3.2), (" ", 2000), ("R\n1", 2000)):
            with self.subTest(name=name, port=port):
                self.assertIsInstance(config.register_device(name, port), ToolFailure)
                self.assertEqual(self.state(), before)

    def test_register_resolve_and_unregister_share_case_insensitive_identity(self):
        config.register_device("R1", 2001, username="operator", password="kept")
        result = config.register_device("r1", 2002)
        self.assertNotIsInstance(result, ToolFailure)
        self.assertIn("'R1'", result)
        self.assertNotIn("r1", config._device_registry)
        self.assertEqual(config._device_registry["R1"]["password"], "kept")
        self.assertEqual(config._resolve_device("r1"), ("127.0.0.1", 2002))
        self.assertEqual(config._resolve_device("R1"), ("127.0.0.1", 2002))
        self.assertIn("'R1' 已注销", config.unregister_device("r1"))
        self.assertNotIn("R1", config._device_registry)
        self.assertNotIn("R1", json.loads((self.root / "devices.json").read_text()))

    def test_unicode_casefold_registration_matches_resolution_and_removal(self):
        config.register_device("Straße", 2001)
        config.register_device("STRASSE", 2002)
        self.assertNotIn("STRASSE", config._device_registry)
        self.assertEqual(config._resolve_device("strasse"), ("127.0.0.1", 2002))
        self.assertIn("已注销", config.unregister_device("STRASSE"))
        self.assertNotIn("Straße", config._device_registry)

    def test_topology_case_changes_preserve_one_name_and_manual_restoration(self):
        config.register_device("R1", 2346, username="operator")
        config.sync_topology_devices([{"name": "r1", "comPort": 2001}])
        config.sync_topology_devices([{"name": "R1", "comPort": 2002}])
        self.assertNotIn("r1", config._device_registry)
        self.assertEqual(config._resolve_device("r1"), ("127.0.0.1", 2002))
        self.assertEqual(config._device_registry["R1"]["username"], "operator")
        self.assertEqual(json.loads((self.root / "manifest.json").read_text())["managed_names"], ["R1"])
        config.sync_topology_devices([])
        self.assertEqual(config._device_registry["R1"], {"port": 2346, "_source": "manual", "username": "operator"})

    def test_legacy_case_conflicts_are_rejected_until_exact_duplicate_is_removed(self):
        config._device_registry.update({"R1": 2001, "r1": 2002})
        config._save_registry()
        before = self.state()
        self.assertIsInstance(config.register_device("R1", 2003), ToolFailure)
        self.assertEqual(self.state(), before)
        with self.assertRaisesRegex(ValueError, "大小写冲突"):
            config._resolve_device("r1")
        config.unregister_device("r1")
        self.assertEqual(config._resolve_device("r1"), ("127.0.0.1", 2001))

    def test_duplicate_positive_ports_are_rejected_before_any_mutation(self):
        before = self.state()
        for second in ({"comPort": 2001}, {"comPort": "02001"}, {"port": 2001}):
            payload = [{"name": "A", "comPort": 2001}, {"name": "B", **second}]
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, "重复控制台端口 2001"):
                config.sync_topology_devices(payload)
            self.assertEqual(self.state(), before)

    def test_multiple_zero_ports_remain_valid(self):
        config.sync_topology_devices([
            {"name": "PC1", "comPort": 0}, {"name": "PC2", "comPort": "0"},
            {"name": "Server1", "port": 0}, {"name": "R1", "comPort": 2001},
        ])
        self.assertEqual([config._get_device_port(config._device_registry[name]) for name in ("PC1", "PC2", "Server1")], [0, 0, 0])
        self.assertEqual(config._resolve_device("r1"), ("127.0.0.1", 2001))

    def test_http_upload_rejects_invalid_payload_without_overwriting_snapshot(self):
        web_ui.TOPO_JSON.write_bytes(b'{"devices": [], "links": []}\n')
        before = self.state()
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), web_ui.UIRequestHandler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            for payload in INVALID_TOPOLOGIES:
                with self.subTest(payload=payload):
                    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
                    try:
                        connection.request("POST", "/api/topology", json.dumps(payload), {"Content-Type": "application/json"})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 400, response.read())
                        response.read()
                    finally:
                        connection.close()
                    self.assertEqual(self.state(), before)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_depth_limit_prunes_one_branch_and_keeps_siblings(self):
        deep = self.root / "a" / "deep" / "deeper"
        deep.mkdir(parents=True)
        (deep / "at-limit.topo").write_text("<topo/>")
        deeper = deep / "pruned"
        deeper.mkdir()
        (deeper / "too-deep.topo").write_text("<topo/>")
        sibling = self.root / "z"
        sibling.mkdir()
        (sibling / "should-be-found.topo").write_text("<topo/>")
        report = topology.find_topo_files(str(self.root))
        self.assertIn("should-be-found.topo", report)
        self.assertIn("at-limit.topo", report)
        self.assertNotIn("too-deep.topo", report)


class ScriptedSocket:
    """Records actual sent lines and delivers the device's scripted byte packets."""

    def __init__(self, script):
        self.script = script
        self.pending = deque()
        self.sent = []
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        command = data.decode().removesuffix("\r\n")
        self.sent.append(command)
        packets = self.script(command)
        self.pending.extend(packets)

    def recv(self, size):
        if self.pending:
            packet = self.pending.popleft()
            if isinstance(packet, Exception):
                raise packet
            return packet
        time.sleep(0.002)
        raise socket.timeout()


def simulated_console(script):
    console = HuaweiConsole("127.0.0.1", 2999, timeout=0.04)
    console._learned_prompt = "Audit"
    console._sock = ScriptedSocket(script)
    return console


SAVED = b"Info: Save the configuration successfully.\r\n<Audit>"


class SaveCompletionTests(unittest.TestCase):
    def test_confirmation_filename_and_split_packets_complete(self):
        responses = {
            "save": [b"save\r\nContinue? [y", b" / n] :"],
            "Y": [b"Please input the file name(*.cfg,*.zip)[vrpcfg", b".zip]:"],
            "": [bytes([byte]) for byte in SAVED],
        }
        console = simulated_console(lambda command: responses[command])
        output = console.save_config(timeout=0.5)
        self.assertIn("successfully", output)
        self.assertEqual(console._sock.sent, ["save", "Y", ""])
        self.assertEqual(console._sock.timeout, console.timeout)

    def test_save_completion_variations(self):
        for message in (
            "Info: The current configuration was saved to the device successfully.",
            "Configuration saved successfully.", "配置保存成功。",
        ):
            with self.subTest(message=message):
                console = simulated_console(lambda _: [(message + "\r\n<Audit>").encode()])
                self.assertIn(message, console.save_config(timeout=0.05))

    def test_silence_eof_prompt_only_success_without_prompt_and_unknown_question_fail(self):
        for packets in ([], [b"save\r\n"], [b""], [b"<Audit>"],
                        [SAVED.split(b"<Audit>")[0]], [b"Choose a destination:"],
                        [b"Error: Save failed.\r\n<Audit>"],
                        [b"<Audit>\r\nInfo: Save the configuration successfully.\r\n"]):
            with self.subTest(packets=packets):
                console = simulated_console(lambda _: packets)
                with self.assertRaises((RuntimeError, ConnectionError, TimeoutError)) as failure:
                    console.save_config(timeout=0.04)
                self.assertIn("设备回显", str(failure.exception))
                self.assertEqual(console._sock.sent, ["save"])
                self.assertEqual(console._sock.timeout, console.timeout)

    def test_repeated_confirmations_are_bounded(self):
        console = simulated_console(lambda _: [b"Continue?[Y/N]:"])
        with self.assertRaisesRegex(RuntimeError, "重复过多"):
            console.save_config(timeout=0.2)
        self.assertEqual(console._sock.sent, ["save"] + ["Y"] * 8)

    def test_filename_response_without_completion_is_not_success(self):
        responses = {"save": [b"Continue?[Y/N]:"], "Y": [b"Please input file name[vrpcfg.zip]:"], "": [b"<Audit>"]}
        console = simulated_console(lambda command: responses[command])
        with self.assertRaisesRegex(RuntimeError, "没有保存成功"):
            console.save_config(timeout=0.1)
        self.assertEqual(console._sock.sent, ["save", "Y", ""])


class ConfigStopTests(unittest.TestCase):
    @staticmethod
    def script(reject=None, timeout=None, cleanup_error=False):
        def response(command):
            if command == timeout:
                return [b"partial output\r\n"]
            if command == reject or (command == "return" and cleanup_error):
                return [b"Error: Unrecognized command\r\n[Audit]"]
            return [(command + "\r\n" + ("<Audit>" if command == "return" else "[Audit]")).encode()]
        return response

    def test_rejected_first_or_middle_command_stops_remaining_commands(self):
        for reject in ("first", "second"):
            with self.subTest(reject=reject):
                console = simulated_console(self.script(reject=reject))
                commands = ["first", "second", "third"]
                with self.assertRaises(ConfigBatchError) as failure:
                    console.send_config_set(commands)
                index = commands.index(reject)
                self.assertEqual(console._sock.sent, ["system-view"] + commands[:index + 1] + ["return"])
                self.assertEqual(failure.exception.completed, commands[:index])
                self.assertEqual(failure.exception.failed, reject)
                self.assertEqual(failure.exception.skipped, commands[index + 1:])
                self.assertIn("Error:", str(failure.exception))

    def test_system_view_rejection_sends_no_config_commands(self):
        console = simulated_console(self.script(reject="system-view"))
        with self.assertRaises(ConfigBatchError) as failure:
            console.send_config_set(["first", "second"])
        self.assertEqual(console._sock.sent, ["system-view"])
        self.assertEqual(failure.exception.skipped, ["first", "second"])

    def test_timeout_is_unconfirmed_and_stops_later_commands(self):
        console = simulated_console(self.script(timeout="second"))
        with self.assertRaises(ConfigBatchError) as failure:
            console.send_config_set(["first", "second", "third"])
        self.assertEqual(console._sock.sent, ["system-view", "first", "second", "return"])
        self.assertEqual(failure.exception.completed, ["first"])
        self.assertEqual(failure.exception.skipped, ["third"])
        self.assertIn("partial output", str(failure.exception))

    def test_cleanup_failure_does_not_hide_primary_rejection(self):
        console = simulated_console(self.script(reject="first", cleanup_error=True))
        with self.assertRaises(ConfigBatchError) as failure:
            console.send_config_set(["first", "second"])
        self.assertEqual(failure.exception.failed, "first")
        self.assertEqual(failure.exception.skipped, ["second"])
        self.assertIsNotNone(failure.exception.cleanup_error)

    def test_successful_commands_but_failed_return_are_reported(self):
        console = simulated_console(self.script(cleanup_error=True))
        with self.assertRaises(ConfigBatchError) as failure:
            console.send_config_set(["first"])
        self.assertEqual(failure.exception.completed, ["first"])
        self.assertEqual(failure.exception.failed, "return")
        self.assertEqual(failure.exception.skipped, [])

    def test_invalid_direct_config_calls_never_connect_or_send(self):
        for commands in ([], None, [" "], ["first\nsecond"], [42]):
            with self.subTest(commands=commands), patch.object(cli, "_connect") as connect:
                self.assertIsInstance(cli.push_config("Audit", commands), ToolFailure)
                connect.assert_not_called()
                console = simulated_console(self.script())
                with self.assertRaises(ValueError):
                    console.send_config_set(commands)
                self.assertEqual(console._sock.sent, [])

    def test_invalid_later_device_is_validated_before_first_device(self):
        for devices in ([], [None], [{"device_ip": " ", "commands": ["first"]}],
                        [{"device_ip": "Good", "commands": ["first"]}, {"device_ip": "Bad", "commands": []}]):
            with self.subTest(devices=devices), patch.object(cli, "push_config") as push:
                self.assertIsInstance(cli.multi_device_push_config(devices), ToolFailure)
                push.assert_not_called()

    def test_multi_device_stops_and_reports_skipped_devices(self):
        devices = [{"device_ip": name, "commands": ["first"]} for name in ("A", "B", "C")]
        with patch.object(cli, "push_config", side_effect=["A completed", ToolFailure("B rejected"), "must not run"]) as push:
            result = cli.multi_device_push_config(devices)
        self.assertIsInstance(result, ToolFailure)
        self.assertEqual([call.args[0] for call in push.call_args_list], ["A", "B"])
        self.assertIn("未执行设备: ['C']", result)
        self.assertIn("A completed", result)

    def test_last_device_failure_omits_empty_skipped_list_but_preserves_error(self):
        for names in (("A",), ("A", "B")):
            with self.subTest(names=names):
                devices = [{"device_ip": name, "commands": ["first"]} for name in names]
                outputs = ["completed"] * (len(names) - 1) + [ToolFailure("last device failed")]
                with patch.object(cli, "push_config", side_effect=outputs) as push:
                    result = cli.multi_device_push_config(devices)
                self.assertIsInstance(result, ToolFailure)
                self.assertNotIn("未执行设备", result)
                self.assertIn("last device failed", result)
                self.assertIn("已停止", result)
                self.assertEqual(push.call_count, len(names))


class FailureStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_registration_and_unregistration_use_the_same_name(self):
        with isolated_registry() as root:
            async with Client(application.server) as client:
                await client.call_tool("register_device", {"name": "R1", "port": 2001})
                registered = await client.call_tool("register_device", {"name": "r1", "port": 2002})
                self.assertFalse(registered.is_error)
                self.assertEqual(list(name for name in config._device_registry if name.casefold() == "r1"), ["R1"])
                removed = await client.call_tool("unregister_device", {"name": "r1"})
                self.assertFalse(removed.is_error)
                self.assertIn("'R1' 已注销", removed.content[0].text)
                self.assertNotIn("R1", json.loads((root / "devices.json").read_text()))

    async def test_duplicate_topology_ports_are_mcp_errors_without_file_changes(self):
        with isolated_registry() as root:
            config.sync_topology_devices([{"name": "Managed", "comPort": 2001}])
            before = {name: (root / name).read_bytes() for name in ("devices.json", "manifest.json")}
            path = root / "bad.json"
            path.write_text(json.dumps({"devices": [{"name": "A", "comPort": 2002}, {"name": "B", "comPort": 2002}]}))
            async with Client(application.server) as client:
                with self.assertLogs("ensp_mcp.server", level="ERROR"):
                    result = await client.call_tool("sync_topology_devices", {"file_path": str(path)}, raise_on_error=False)
                self.assertTrue(result.is_error)
                self.assertIn("重复控制台端口 2002", result.content[0].text)
            self.assertEqual(before, {name: (root / name).read_bytes() for name in before})

    async def test_last_device_failure_remains_mcp_error_with_no_empty_list(self):
        async with Client(application.server) as client:
            with patch.object(cli, "push_config", return_value=ToolFailure("last device failed")):
                with self.assertLogs("ensp_mcp.server", level="ERROR"):
                    result = await client.call_tool("multi_device_push_config", {"devices": [{"device_ip": "R1", "commands": ["first"]}]}, raise_on_error=False)
            self.assertTrue(result.is_error)
            self.assertNotIn("未执行设备", result.content[0].text)
            self.assertIn("last device failed", result.content[0].text)

    async def test_missing_sync_file_is_mcp_error(self):
        with isolated_registry() as root:
            async with Client(application.server) as client:
                with self.assertLogs("ensp_mcp.server", level="ERROR"):
                    result = await client.call_tool("sync_topology_devices", {"file_path": str(root / "missing.json")}, raise_on_error=False)
        self.assertTrue(result.is_error)
        self.assertIn("not found", result.content[0].text)

    async def test_auto_discovery_system_failures_are_mcp_errors(self):
        process = subprocess.CompletedProcess(["tasklist"], 0, b"eNSP_Client.exe 123 Console\n", b"")
        denied = subprocess.CompletedProcess([], 1, b"", b"Access denied")
        cases = [[denied], [OSError("tasklist unavailable")], [process, denied],
                 [process, OSError("netstat unavailable")],
                 [subprocess.CompletedProcess([], 0, b"", b"")],
                 [process, subprocess.CompletedProcess([], 0, b"", b"")]]
        with isolated_registry():
            before = deepcopy(config._device_registry)
            async with Client(application.server) as client:
                for outcomes in cases:
                    with self.subTest(outcomes=outcomes), patch.object(config.subprocess, "run", side_effect=outcomes):
                        with self.assertLogs("ensp_mcp.server", level="ERROR"):
                            result = await client.call_tool("auto_discover", raise_on_error=False)
                        self.assertTrue(result.is_error, result.content[0].text)
                        self.assertEqual(config._device_registry, before)

    async def test_empty_arrays_invalid_port_and_multiline_input_never_dispatch(self):
        cases = [
            ("register_device", {"name": "R1", "port": 0}),
            ("register_device", {"name": "R1", "port": 65536}),
            ("register_device", {"name": " ", "port": 2000}),
            ("register_device", {"name": "R1", "port": True}),
            ("push_config", {"device_ip": "R1", "commands": []}),
            ("mcp_push_config", {"device_ip": "R1", "commands": [" "]}),
            ("push_config", {"device_ip": "R1", "commands": ["first\nsecond"]}),
            ("multi_device_push_config", {"devices": []}),
            ("multi_device_push_config", {"devices": [{"device_ip": "R1", "commands": []}]}),
            ("execute_cli", {"device_ip": "R1", "command": ""}),
            ("mcp_execute", {"device_ip": "R1", "command": "first\rsecond"}),
        ]
        handler = Mock(return_value="must not run")
        with patch.dict(application.TOOL_DISPATCH, {name: handler for name, _ in cases}):
            async with Client(application.server) as client:
                for name, arguments in cases:
                    with self.subTest(name=name, arguments=arguments):
                        result = await client.call_tool(name, arguments, raise_on_error=False)
                        self.assertTrue(result.is_error)
                        self.assertIn("Invalid", result.content[0].text)
        handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
