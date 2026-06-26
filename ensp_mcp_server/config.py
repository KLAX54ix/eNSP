"""
eNSP MCP Server Configuration Module

设备注册表管理 + 自动发现。
通过 eNSP 本地 TCP 端口直连设备（无需配置 Cloud/Telnet）。
"""
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from pydantic import BaseModel

from .exceptions import DeviceNotFoundError

# ==================== 日志配置 ====================

_log_level = os.environ.get("ENSP_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.WARNING),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("ensp_mcp")


# ==================== 配置模型 ====================

class ServerConfig(BaseModel):
    """服务器配置"""
    server_name: str = "eNSP_NetOps_Server"
    timeout: int = 30


# 全局配置实例
config = ServerConfig()


# ==================== 设备注册表 ====================

_ROOT_DIR = Path(os.path.dirname(os.path.dirname(__file__)))
_REGISTRY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "devices.json")
_TOPOLOGY_MANIFEST_FILE = _ROOT_DIR / "topology_registry.json"
_REGISTRY_LOCK = threading.RLock()

# 支持两种格式:
#   简单: { "R1": 2000 }
#   详细: { "R1": { "port": 2000, "username": "admin", "password": "xxx" } }
_device_registry: dict[str, int | dict] = {}


def _topology_manifest_template() -> dict[str, object]:
    return {"managed_names": [], "previous_entries": {}}


def _load_topology_manifest() -> dict[str, object]:
    if not _TOPOLOGY_MANIFEST_FILE.exists():
        return _topology_manifest_template()

    try:
        with _TOPOLOGY_MANIFEST_FILE.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return _topology_manifest_template()

    if not isinstance(payload, dict):
        return _topology_manifest_template()

    managed_names = payload.get("managed_names", [])
    previous_entries = payload.get("previous_entries", {})
    return {
        "managed_names": managed_names if isinstance(managed_names, list) else [],
        "previous_entries": previous_entries if isinstance(previous_entries, dict) else {},
    }


def _save_topology_manifest(managed_names: list[str], previous_entries: dict[str, int | dict]) -> None:
    temp_file = _TOPOLOGY_MANIFEST_FILE.with_suffix(_TOPOLOGY_MANIFEST_FILE.suffix + ".tmp")
    with temp_file.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "managed_names": managed_names,
                "previous_entries": previous_entries,
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
    temp_file.replace(_TOPOLOGY_MANIFEST_FILE)


def _clear_topology_manifest() -> None:
    if _TOPOLOGY_MANIFEST_FILE.exists():
        _TOPOLOGY_MANIFEST_FILE.unlink()


def _normalize_registry_entry(entry: int | dict, default_source: str = "legacy") -> int | dict:
    if isinstance(entry, dict):
        normalized = dict(entry)
        normalized.setdefault("_source", default_source)
        return normalized
    return {"port": int(entry), "_source": default_source}


def _entry_source(entry: int | dict) -> str:
    if isinstance(entry, dict):
        return str(entry.get("_source", "legacy"))
    return "legacy"


def _build_registry_entry(
    port: int,
    *,
    source: str,
    existing: int | dict | None = None,
    username: str = "",
    password: str = "",
) -> dict[str, object]:
    entry: dict[str, object] = {"port": port, "_source": source}
    if isinstance(existing, dict):
        if existing.get("username"):
            entry["username"] = existing["username"]
        if existing.get("password"):
            entry["password"] = existing["password"]
    if username:
        entry["username"] = username
    if password:
        entry["password"] = password
    return entry


def _restore_topology_managed_entries() -> dict[str, object]:
    with _REGISTRY_LOCK:
        manifest = _load_topology_manifest()
        managed_names = [str(name).strip() for name in manifest.get("managed_names", []) if str(name).strip()]
        previous_entries = manifest.get("previous_entries", {}) if isinstance(manifest.get("previous_entries"), dict) else {}

        removed: list[str] = []
        restored: list[str] = []

        if not managed_names:
            return {"removed": removed, "restored": restored, "count": 0}

        for name in managed_names:
            if name in previous_entries:
                _device_registry[name] = previous_entries[name]
                restored.append(name)
            elif name in _device_registry:
                del _device_registry[name]
                removed.append(name)

        _save_registry()
        _clear_topology_manifest()
        return {"removed": removed, "restored": restored, "count": len(removed)}


def collect_cleanup_device_names(devices: list[dict] | None) -> list[str]:
    with _REGISTRY_LOCK:
        manifest = _load_topology_manifest()
        managed_names = [str(name).strip() for name in manifest.get("managed_names", []) if str(name).strip()]

        target_names = managed_names[:]
        if not target_names and devices:
            target_names = [
                str(item.get("name", "")).strip()
                for item in devices
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ]

        if not target_names:
            target_names = [
                name for name, entry in _device_registry.items()
                if _entry_source(entry) == "topology_sync"
            ]

        deduped: list[str] = []
        seen: set[str] = set()
        for name in target_names:
            if name and name not in seen:
                seen.add(name)
                deduped.append(name)
        return deduped


def _load_registry():
    global _device_registry
    with _REGISTRY_LOCK:
        if os.path.exists(_REGISTRY_FILE):
            try:
                with open(_REGISTRY_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    _device_registry = {
                        str(name): _normalize_registry_entry(entry)
                        for name, entry in loaded.items()
                    }
                else:
                    _device_registry = {}
                logger.info("设备注册表已加载: %d 台设备", len(_device_registry))
            except Exception as e:
                logger.error("加载注册表失败: %s", e)
                _device_registry = {}


def _save_registry():
    registry_path = Path(_REGISTRY_FILE)
    temp_file = registry_path.with_suffix(registry_path.suffix + ".tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(_device_registry, f, ensure_ascii=False, indent=2)
    temp_file.replace(registry_path)


def sync_topology_devices(
    devices: list[dict] | None,
    *,
    username: str = "",
    password: str = "",
) -> dict[str, object]:
    """Merge topology devices into the MCP registry using eNSP local ports."""
    with _REGISTRY_LOCK:
        _restore_topology_managed_entries()

        created: list[str] = []
        updated: list[str] = []
        skipped: list[str] = []
        previous_entries: dict[str, int | dict] = {}
        managed_names: list[str] = []

        if not devices:
            return {"created": created, "updated": updated, "skipped": skipped, "count": 0}

        for item in devices:
            if not isinstance(item, dict):
                continue

            name = str(item.get("name", "")).strip()
            raw_port = item.get("comPort", item.get("port"))
            if not name or raw_port in (None, ""):
                skipped.append(name or "<unknown>")
                continue

            try:
                port = int(raw_port)
            except (TypeError, ValueError):
                skipped.append(name)
                continue

            existing = _device_registry.get(name)
            existing_port = _get_device_port(existing) if existing is not None else None
            if existing is not None and name not in previous_entries:
                previous_entries[name] = deepcopy(existing)

            _device_registry[name] = _build_registry_entry(
                port,
                source="topology_sync",
                existing=existing,
                username=username,
                password=password,
            )
            managed_names.append(name)

            if existing is None:
                created.append(name)
            elif existing_port != port:
                updated.append(name)

        _save_registry()
        _save_topology_manifest(managed_names, previous_entries)
        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "count": len(created) + len(updated),
        }


def remove_topology_devices(devices: list[dict] | None) -> dict[str, object]:
    """Remove topology-linked devices from the registry while preserving unrelated entries."""
    with _REGISTRY_LOCK:
        removed: list[str] = []
        restored: list[str] = []
        skipped: list[str] = []
        manifest = _load_topology_manifest()
        previous_entries = manifest.get("previous_entries", {}) if isinstance(manifest.get("previous_entries"), dict) else {}
        target_names = collect_cleanup_device_names(devices)

        if not target_names:
            return {"removed": removed, "restored": restored, "skipped": skipped, "count": 0, "restored_count": 0}

        for name in target_names:
            if name in previous_entries:
                _device_registry[name] = previous_entries[name]
                restored.append(name)
            elif name in _device_registry:
                del _device_registry[name]
                removed.append(name)
            else:
                skipped.append(name)

        _save_registry()
        _clear_topology_manifest()
        return {
            "removed": removed,
            "restored": restored,
            "skipped": skipped,
            "count": len(removed),
            "restored_count": len(restored),
        }


def sync_topology_devices_from_file(
    file_path: str = "",
    *,
    username: str = "",
    password: str = "",
) -> str:
    """Register devices from a saved topology.json snapshot."""
    target = Path(file_path) if file_path else Path(_REGISTRY_FILE).resolve().parent / "topology.json"
    if not target.exists():
        return f"Topology file not found: {target}"

    try:
        with target.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        return f"Unable to read topology file: {exc}"

    devices = payload.get("devices", []) if isinstance(payload, dict) else []
    summary = sync_topology_devices(devices, username=username, password=password)
    return (
        f"Topology sync complete. Added {len(summary['created'])}, "
        f"updated {len(summary['updated'])}, skipped {len(summary['skipped'])}."
    )


def _get_device_port(entry: int | dict) -> int:
    if isinstance(entry, dict):
        return entry.get("port", 23)
    return entry


def registry_snapshot() -> dict[str, int | dict]:
    with _REGISTRY_LOCK:
        return deepcopy(_device_registry)


def register_device(name: str, port: int, username: str = "", password: str = "") -> str:
    with _REGISTRY_LOCK:
        _device_registry[name] = _build_registry_entry(
            port,
            source="manual",
            existing=_device_registry.get(name),
            username=username,
            password=password,
        )
        _save_registry()
    return f"设备 '{name}' 已注册，端口: {port}"


def unregister_device(name: str) -> str:
    with _REGISTRY_LOCK:
        if name in _device_registry:
            del _device_registry[name]
            _save_registry()
            return f"设备 '{name}' 已注销"
        return f"设备 '{name}' 不存在"


def clear_registered_devices(device_names: list[str] | None = None) -> dict[str, object]:
    with _REGISTRY_LOCK:
        target_names = (
            [str(name).strip() for name in device_names if str(name).strip()]
            if device_names is not None
            else list(_device_registry.keys())
        )
        removed: list[str] = []
        skipped: list[str] = []
        seen: set[str] = set()

        for name in target_names:
            if name in seen:
                continue
            seen.add(name)
            if name in _device_registry:
                del _device_registry[name]
                removed.append(name)
            else:
                skipped.append(name)

        _save_registry()
        return {"removed": removed, "skipped": skipped, "count": len(removed)}


def list_devices() -> str:
    with _REGISTRY_LOCK:
        if not _device_registry:
            return "当前没有注册任何设备。请使用 register_device 工具注册设备。"
        lines = ["已注册设备列表:"]
        lines.append(f"{'设备名':<12} {'端口':<8}")
        lines.append("-" * 20)
        for name, entry in _device_registry.items():
            port = _get_device_port(entry)
            lines.append(f"{name:<12} {port:<8}")
        return "\n".join(lines)


def _resolve_device(device_ip: str) -> tuple[str, int]:
    """
    解析设备标识，返回 (host, port)

    - 设备名: "R1" → ("127.0.0.1", 注册表中的端口)
    - IP地址: "192.168.56.10" → ("192.168.56.10", 23)
    """
    with _REGISTRY_LOCK:
        if device_ip in _device_registry:
            return ("127.0.0.1", _get_device_port(_device_registry[device_ip]))

        for name, entry in _device_registry.items():
            if name.lower() == device_ip.lower():
                return ("127.0.0.1", _get_device_port(entry))

    logger.debug("设备 '%s' 未在注册表中找到，回退到 IP 模式", device_ip)
    return (device_ip, 23)


# ==================== 自动发现 ====================

def auto_discover_devices() -> str:
    """
    自动发现 eNSP 设备：
    1. 查找 eNSP_Client.exe 进程 PID
    2. 获取监听的 TCP 端口（2000-2999）
    3. 连接获取 sysname
    4. 合并更新注册表（保留手动注册的设备）
    """
    # 1. 查找 eNSP 进程 PID
    try:
        result = subprocess.run(
            ['tasklist'], capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        return f"无法获取进程列表: {e}"

    pids = set()
    for line in result.stdout.split('\n'):
        if 'eNSP_Client' in line:
            parts = line.split()
            if len(parts) >= 2:
                pids.add(parts[1])

    if not pids:
        return "未检测到 eNSP_Client.exe 进程，请确认 eNSP 已启动且设备已运行。"

    # 2. 获取控制台端口
    try:
        result = subprocess.run(
            ['netstat', '-ano'], capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        return f"无法获取端口信息: {e}"

    ports = []
    for line in result.stdout.split('\n'):
        if 'LISTENING' not in line or '0.0.0.0:' not in line:
            continue
        for pid in pids:
            if line.strip().endswith(pid):
                match = re.search(r'0\.0\.0\.0:(\d+)', line)
                if match:
                    port = int(match.group(1))
                    if 2000 <= port <= 2999:
                        ports.append(port)

    if not ports:
        return (
            f"找到 eNSP 进程 (PID: {', '.join(pids)})，"
            "但未检测到控制台端口（2000-2999 范围）。请确认设备已启动。"
        )

    ports.sort()

    # 3. 连接获取 sysname
    discovered = {}
    default_count = 0
    for port in ports:
        sysname = _get_sysname_from_port(port)
        if sysname and sysname != "Huawei":
            discovered[sysname] = port
        else:
            default_count += 1
            discovered[f"Device{default_count}"] = port

    # 4. 合并注册表（保留手动注册的设备）
    global _device_registry
    with _REGISTRY_LOCK:
        discovered_ports = set(discovered.values())
        merged = {}
        for name, entry in _device_registry.items():
            if _get_device_port(entry) not in discovered_ports:
                merged[name] = entry
        for name, port in discovered.items():
            merged[name] = _build_registry_entry(port, source="auto_discover", existing=_device_registry.get(name))

        _device_registry = merged
        _save_registry()

    # 生成报告
    lines = [f"自动发现完成！共找到 {len(discovered)} 台设备:"]
    lines.append(f"{'设备名':<12} {'端口':<8}")
    lines.append("-" * 20)
    for name, port in discovered.items():
        lines.append(f"{name:<12} {port:<8}")

    manual_kept = {k: v for k, v in merged.items() if k not in discovered}
    if manual_kept:
        lines.append(f"\n已保留 {len(manual_kept)} 台手动注册设备:")
        for name, entry in manual_kept.items():
            lines.append(f"  {name:<12} {_get_device_port(entry):<8}")

    if default_count > 0:
        lines.append(
            f"\n提示: {default_count} 台设备使用默认主机名(Huawei)，"
            "已自动命名。建议在 eNSP 中用 'sysname R1' 设置主机名后重新发现。"
        )
    return "\n".join(lines)


def _get_sysname_from_port(port: int) -> str | None:
    """通过控制台端口连接设备，获取 sysname"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(('127.0.0.1', port))
        time.sleep(0.5)
        s.sendall(b'\r\n')
        time.sleep(1)
        s.recv(4096)
        s.sendall(b'display current-configuration | include sysname\r\n')
        time.sleep(2)
        data = s.recv(4096).decode('utf-8', errors='ignore')
        s.close()
        for line in data.split('\n'):
            stripped = line.strip()
            if stripped.startswith('sysname ') and 'display' not in stripped:
                return stripped.replace('sysname ', '')
        return None
    except Exception:
        return None


# 启动时加载注册表
_load_registry()
