"""
eNSP CLI 工具集

提供 CLI 命令执行、配置下发、保存、运行配置查看、
Ping/Traceroute 诊断、健康检查等核心功能。
"""
import logging
import socket
from typing import Optional

from ..config import _resolve_device, registry_snapshot
from ..console import HuaweiConsole

logger = logging.getLogger("ensp_mcp.cli")

MAX_COMMAND_LENGTH = 512
MAX_CONFIG_COMMANDS = 200
MIN_PING_COUNT = 1
MAX_PING_COUNT = 20


def _connect(device_ip: str, timeout: int = 15) -> HuaweiConsole:
    """根据设备名/IP 创建 HuaweiConsole 实例"""
    host, port = _resolve_device(device_ip)
    return HuaweiConsole(host, port, timeout=timeout)


def _normalize_text(value: object, field_name: str, max_length: int = MAX_COMMAND_LENGTH) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} cannot be empty")
    if len(text) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    return text


def _normalize_config_commands(commands: object) -> list[str]:
    if not isinstance(commands, list):
        raise ValueError("commands must be a list of strings")
    if not commands:
        raise ValueError("commands cannot be empty")
    if len(commands) > MAX_CONFIG_COMMANDS:
        raise ValueError(f"commands cannot contain more than {MAX_CONFIG_COMMANDS} items")

    normalized: list[str] = []
    for index, command in enumerate(commands, start=1):
        try:
            normalized.append(_normalize_text(command, f"commands[{index}]"))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    return normalized


def _normalize_ping_count(count: object) -> int:
    try:
        normalized = int(count)
    except (TypeError, ValueError) as exc:
        raise ValueError("count must be an integer") from exc
    if not MIN_PING_COUNT <= normalized <= MAX_PING_COUNT:
        raise ValueError(f"count must be between {MIN_PING_COUNT} and {MAX_PING_COUNT}")
    return normalized


# ==================== 核心工具 ====================

def execute_cli(device_ip: str, command: str, timeout: int = 30) -> str:
    """在设备上执行单条查询/显示命令"""
    try:
        target = _normalize_text(device_ip, "device_ip", max_length=128)
        cli_command = _normalize_text(command, "command")
        with _connect(target, timeout=timeout) as c:
            output = c.send_command(cli_command, timeout=timeout)
            return f"[{target}] 执行 '{cli_command}' 结果:\n{output}"
    except Exception as e:
        return f"无法连接设备 {device_ip} 或执行失败: {e}"


def push_config(device_ip: str, commands: list[str]) -> str:
    """向设备下发一组配置命令"""
    try:
        target = _normalize_text(device_ip, "device_ip", max_length=128)
        config_commands = _normalize_config_commands(commands)
        with _connect(target) as c:
            output = c.send_config_set(config_commands)
            return f"[{target}] 配置下发结果:\n{output}"
    except Exception as e:
        return f"配置下发失败 [{device_ip}]: {e}"


def multi_device_push_config(devices: list[dict]) -> str:
    """同时向多台设备下发配置"""
    if not isinstance(devices, list) or not devices:
        return "批量配置失败: devices must be a non-empty list"
    results = []
    for entry in devices:
        if not isinstance(entry, dict):
            results.append("批量配置跳过: device entry must be an object")
            continue
        device_ip = entry.get("device_ip", "")
        commands = entry.get("commands", [])
        results.append(push_config(device_ip, commands))
    return "\n\n".join(results)


def save_config(device_ip: str) -> str:
    """保存设备配置（处理 [Y/N] 交互确认）"""
    try:
        target = _normalize_text(device_ip, "device_ip", max_length=128)
        with _connect(target) as c:
            output = c.send_command_timing("save", delay=2.0)
            if "[Y/N]" in output or "[y/n]" in output:
                c._send_line("Y")
                import time
                time.sleep(3)
                output += c._read_available()
            return f"[{target}] 配置保存结果:\n{output}"
    except Exception as e:
        return f"保存配置失败 [{device_ip}]: {e}"


def get_running_config(device_ip: str, section: Optional[str] = None) -> str:
    """获取设备当前运行配置，可选 section 过滤"""
    try:
        target = _normalize_text(device_ip, "device_ip", max_length=128)
        with _connect(target, timeout=30) as c:
            if section:
                command = f"display current-configuration | section {_normalize_text(section, 'section', max_length=128)}"
            else:
                command = "display current-configuration"
            output = c.send_command(command, timeout=30)
            return f"[{target}] 运行配置:\n{output}"
    except Exception as e:
        return f"获取运行配置失败 [{device_ip}]: {e}"


# ==================== 诊断工具 ====================

def ping_from_device(
    device_ip: str,
    target_ip: str,
    count: int = 5,
    source_ip: Optional[str] = None,
) -> str:
    """从设备发起 Ping 测试"""
    try:
        target_device = _normalize_text(device_ip, "device_ip", max_length=128)
        target_address = _normalize_text(target_ip, "target_ip", max_length=128)
        ping_count = _normalize_ping_count(count)
        command = f"ping -c {ping_count} -t 2"
        if source_ip:
            command += f" -a {_normalize_text(source_ip, 'source_ip', max_length=128)}"
        command += f" {target_address}"

        timeout = ping_count * 2 + 10
        with _connect(target_device, timeout=timeout) as c:
            output = c.send_command(command, timeout=timeout)
            return f"[{target_device}] Ping {target_address} 结果:\n{output}"
    except Exception as e:
        return f"Ping 测试失败 [{device_ip}]: {e}"


def traceroute_from_device(
    device_ip: str,
    target_ip: str,
    source_ip: Optional[str] = None,
) -> str:
    """从设备发起 Traceroute"""
    try:
        target_device = _normalize_text(device_ip, "device_ip", max_length=128)
        target_address = _normalize_text(target_ip, "target_ip", max_length=128)
        command = "tracert"
        if source_ip:
            command += f" -a {_normalize_text(source_ip, 'source_ip', max_length=128)}"
        command += f" {target_address}"

        with _connect(target_device, timeout=60) as c:
            output = c.send_command(command, timeout=60)
            return f"[{target_device}] Traceroute 到 {target_address}:\n{output}"
    except Exception as e:
        return f"Traceroute 失败 [{device_ip}]: {e}"


# ==================== 健康检查 ====================

def health_check(device_ip: str) -> str:
    """单台设备健康检查：TCP 端口检测 + 轻量命令测试"""
    host, port = _resolve_device(device_ip)
    results = [f"[{device_ip}] 健康检查报告:"]

    # 1. TCP 端口检测
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((host, port))
        s.close()
        results.append(f"  TCP 连接 ({host}:{port}): OK")
    except Exception as e:
        results.append(f"  TCP 连接 ({host}:{port}): FAIL - {e}")
        return "\n".join(results)

    # 2. 命令测试
    try:
        with HuaweiConsole(host, port, timeout=10) as c:
            output = c.send_command("display clock", timeout=10)
            results.append(f"  CLI 命令测试: OK")
            results.append(f"  设备时间: {output.strip()}")
    except Exception as e:
        results.append(f"  CLI 命令测试: FAIL - {e}")

    return "\n".join(results)


def multi_health_check(device_names: list[str] | None = None) -> str:
    """批量健康检查"""
    if not device_names:
        registry = registry_snapshot()
        if not registry:
            return "没有注册任何设备，请先注册设备。"
        device_names = list(registry.keys())

    results = [f"批量健康检查（{len(device_names)} 台设备）:"]
    results.append("=" * 50)

    healthy = 0
    for name in device_names:
        report = health_check(name)
        if "FAIL" not in report:
            healthy += 1
        results.append(report)
        results.append("")

    results.append(f"汇总: {healthy}/{len(device_names)} 台设备健康")
    return "\n".join(results)
