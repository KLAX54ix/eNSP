"""Discovery regressions; subprocesses, sockets and persistence are mocked."""

import socket
import subprocess
import time
import unittest
from unittest.mock import Mock, patch

from ensp_mcp_server import config, runtime
from ensp_mcp_server.console import HuaweiConsole
from ensp_mcp_server.tools import cli


class PromptPacketTests(unittest.TestCase):
    @staticmethod
    def receive(chunks):
        chunks = iter(chunks)

        def recv(_size):
            try:
                return next(chunks)
            except StopIteration:
                time.sleep(0.01)
                raise socket.timeout()

        return recv

    def test_prompt_followed_by_logs_in_same_receive(self):
        for chunks in (
            [b"value\r\n<Huawei>\r\nDATASYNC_CFGCHANGE: changed\r\n"],
            [b"value\r\n<Hua", b"wei>\r\nDATASYNC_CFGCHANGE: changed\r\n"],
            [b"value\r\n<Huawei>\r\n" + b"DATASYNC_CFGCHANGE: changed\r\n" * 100],
        ):
            with self.subTest(chunks=len(chunks), size=sum(map(len, chunks))):
                console = HuaweiConsole("127.0.0.1", 2999)
                console._learned_prompt = "Huawei"
                console._sock = Mock()
                console._sock.recv.side_effect = self.receive(chunks)
                self.assertIn("value", console._read_until_prompt(timeout=0.05))

    def test_initial_prompt_and_sysname_prompt_with_coalesced_logs(self):
        for discover, pending, prompt in ((True, None, "Huawei"), (False, "AR2", "AR2")):
            with self.subTest(discover=discover):
                console = HuaweiConsole("127.0.0.1", 2999)
                console._learned_prompt = None if discover else "Huawei"
                console._pending_prompt = pending
                console._sock = Mock()
                console._sock.recv.side_effect = self.receive([
                    f"[{prompt}]\r\nDATASYNC_CFGCHANGE: changed\r\n".encode(),
                ])
                console._read_until_prompt(timeout=0.05, discover_prompt=discover)
                self.assertEqual(console._learned_prompt, prompt)


class DiscoveryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.original_registry = config._device_registry
        self.saved_registry = dict(self.original_registry)
        self.original_registry.clear()
        self.addCleanup(self.restore_registry)
        self.enterContext(patch.object(config, "_save_registry"))

    def restore_registry(self):
        config._device_registry = self.original_registry
        self.original_registry.clear()
        self.original_registry.update(self.saved_registry)

    def discover(self, rows, names=None, returncode=0):
        def run(command, **kwargs):
            self.assertNotIn("text", kwargs)
            if command[0] == "tasklist":
                return subprocess.CompletedProcess(command, 0, b"eNSP_Client.exe 123 Console 1 67,060 K\r\n", b"")
            return subprocess.CompletedProcess(command, returncode, rows.encode(), b"Access denied")

        with patch.object(config.subprocess, "run", side_effect=run), patch.object(
            config, "_get_sysname_from_port", side_effect=names, return_value="AR1"
        ) as probe:
            report = config.auto_discover_devices()
        return report, probe

    def test_pid_is_exact_and_ports_are_unique(self):
        _, probe = self.discover(
            "TCP 0.0.0.0:2000 0.0.0.0:0 LISTENING 9123\n"
            "TCP 0.0.0.0:2001 0.0.0.0:0 LISTENING 123\n"
            "TCP 0.0.0.0:2001 0.0.0.0:0 LISTENING 123\n"
        )
        self.assertEqual([c.args[0] for c in probe.call_args_list], [2001])

    def test_discovery_updates_all_registry_consumers(self):
        self.discover("TCP 0.0.0.0:2001 0.0.0.0:0 LISTENING 123\n")
        self.assertIs(config._device_registry, runtime._device_registry)
        self.assertIs(config._device_registry, cli._device_registry)
        self.assertEqual(runtime.resolve_device_label("127.0.0.1", 2001), "AR1")

    def test_failed_netstat_does_not_report_no_devices(self):
        report, probe = self.discover("", returncode=1)
        self.assertIn("无法获取端口信息", report)
        probe.assert_not_called()

    def test_same_sysname_does_not_drop_a_device(self):
        self.discover(
            "TCP 0.0.0.0:2000 0.0.0.0:0 LISTENING 123\n"
            "TCP 0.0.0.0:2001 0.0.0.0:0 LISTENING 123\n",
            names=["R1", "R1"],
        )
        self.assertEqual({config._get_device_port(e) for e in config._device_registry.values()}, {2000, 2001})

    def test_case_variant_sysnames_do_not_create_ambiguous_names(self):
        for names in (["R1", "r1"], ["Straße", "STRASSE"]):
            with self.subTest(names=names):
                config._device_registry.clear()
                self.discover(
                    "TCP 0.0.0.0:2000 0.0.0.0:0 LISTENING 123\n"
                    "TCP 0.0.0.0:2001 0.0.0.0:0 LISTENING 123\n",
                    names=names,
                )
                keys = list(config._device_registry)
                self.assertEqual(len(keys), 2)
                self.assertEqual(len({name.casefold() for name in keys}), 2)
                self.assertEqual({config._get_device_port(entry) for entry in config._device_registry.values()}, {2000, 2001})

    def test_auto_discovery_keeps_manual_case_variant_on_another_port(self):
        config._device_registry["R1"] = {"port": 2000, "_source": "manual"}
        self.discover("TCP 0.0.0.0:2001 0.0.0.0:0 LISTENING 123\n", names=["r1"])
        self.assertEqual(config._resolve_device("r1"), ("127.0.0.1", 2000))
        self.assertEqual(config._resolve_device("r1_2001"), ("127.0.0.1", 2001))

    def test_manual_and_topology_entries_survive_discovery(self):
        manual = {"port": 2000, "_source": "manual", "username": "test"}
        topology = {"port": 2001, "_source": "topology_sync"}
        config._device_registry.update({"Alias": manual, "R1": topology})
        self.discover(
            "TCP 0.0.0.0:2000 0.0.0.0:0 LISTENING 123\n"
            "TCP 0.0.0.0:2002 0.0.0.0:0 LISTENING 123\n",
            names=["Changed", "R1"],
        )
        self.assertEqual(config._device_registry["Alias"], manual)
        self.assertEqual(config._device_registry["R1"], topology)
        self.assertEqual({config._get_device_port(e) for e in config._device_registry.values()}, {2000, 2001, 2002})


class NoConsolePortTests(unittest.TestCase):
    """PC/Client/Server devices sync with comPort 0 and have no CLI console."""

    def setUp(self):
        self.original_registry = config._device_registry
        self.saved_registry = dict(self.original_registry)
        self.original_registry.clear()
        self.original_registry["PC1"] = {"port": 0, "_source": "topology_sync"}
        self.addCleanup(self.restore_registry)

    def restore_registry(self):
        config._device_registry = self.original_registry
        self.original_registry.clear()
        self.original_registry.update(self.saved_registry)

    def test_execute_cli_explains_missing_console_port(self):
        result = cli.execute_cli("PC1", "display version")
        self.assertIsInstance(result, cli.ToolFailure)
        self.assertIn("没有控制台端口", result)
        self.assertNotIn("WinError", result)

    def test_health_check_explains_missing_console_port(self):
        report = cli.health_check("PC1")
        self.assertIsInstance(report, cli.ToolFailure)
        self.assertIn("没有控制台端口", report)


if __name__ == "__main__":
    unittest.main()
