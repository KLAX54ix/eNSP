"""
eNSP topology helpers.
"""

from __future__ import annotations

import json
import logging
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from ..topology_context import load_current_topology, summarize_topology

logger = logging.getLogger("ensp_mcp.topology")


def discover_topology(file_path: str) -> str:
    """
    解析 eNSP .topo 文件，提取拓扑信息

    Args:
        file_path: .topo 文件的完整路径

    Returns:
        拓扑描述（设备列表、接口、链路关系）
    """
    if not os.path.exists(file_path):
        return f"文件不存在: {file_path}"

    suffix = Path(file_path).suffix.lower()
    if suffix == ".json":
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            return f"无法读取 topology.json: {exc}"

        if not isinstance(payload, dict):
            return f"不是有效的 topology.json 文件: {file_path}"
        return _describe_topology_payload(payload, file_path, "网页上传的 topology 快照")

    if suffix != ".topo":
        return f"不是 .topo 或 topology.json 文件: {file_path}"

    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
    except ET.ParseError as e:
        return f"XML 解析失败: {e}"

    devices = []
    links = []

    # 灵活查找设备节点（兼容不同 eNSP 版本）
    device_nodes = _find_elements(root, ["devices/device", "device", "node", "nodes/node"])
    for dev in device_nodes:
        info = _extract_device_info(dev)
        if info:
            devices.append(info)

    # 灵活查找链路节点
    link_nodes = _find_elements(root, ["links/link", "link", "connection", "connections/connection"])
    for link in link_nodes:
        info = _extract_link_info(link)
        if info:
            links.append(info)

    return _describe_parsed_topology(file_path, devices, links)


def find_topo_files(search_dir: str = "") -> str:
    """
    搜索 .topo 文件

    Args:
        search_dir: 搜索目录，为空则搜索常见位置

    Returns:
        找到的 .topo 文件列表
    """
    uploaded_payload = load_current_topology()
    uploaded_summary = summarize_topology(uploaded_payload) if uploaded_payload else None

    search_dirs = []
    if search_dir:
        search_dirs.append(search_dir)
    else:
        # 常见 eNSP 项目目录
        home = os.path.expanduser("~")
        search_dirs.extend([
            os.path.join(home, "Desktop"),
            os.path.join(home, "Documents"),
            os.path.join(home, "Documents", "eNSP"),
            r"C:\Program Files\Huawei\eNSP",
            r"C:\eNSP",
        ])

    found = []
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for dirpath, _, filenames in os.walk(d):
            for f in filenames:
                if f.lower().endswith(".topo"):
                    found.append(os.path.join(dirpath, f))
            # 限制递归深度为 3 层
            depth = dirpath[len(d):].count(os.sep)
            if depth >= 3:
                break

    lines: list[str] = []
    if uploaded_summary:
        lines.extend(
            [
                "已检测到网页上传的当前拓扑，这就是当前应优先使用的拓扑来源：",
                f"  {uploaded_summary['path']}",
                f"  devices={uploaded_summary['device_count']}, links={uploaded_summary['link_count']}",
                "优先调用 get_topology_context 或读取资源 ensp://topology/current。",
                "如果必须传文件路径给 discover_topology，可直接使用上面的 topology.json 路径。",
            ]
        )

    if not found:
        if lines:
            lines.append("本地搜索未找到额外的 .topo 文件。")
            return "\n".join(lines)
        return "未找到 .topo 文件，且当前也没有网页上传的 topology.json。请指定 search_dir 参数或先在网页上传 TOPO。"

    if lines:
        lines.append("")
        lines.append(f"另外找到 {len(found)} 个本地 .topo 文件:")
    else:
        lines.append(f"找到 {len(found)} 个 .topo 文件:")

    for f in found[:20]:
        lines.append(f"  {f}")
    if len(found) > 20:
        lines.append(f"  ... 还有 {len(found) - 20} 个")
    return "\n".join(lines)


def _describe_topology_payload(payload: dict[str, object], file_path: str, source_label: str) -> str:
    summary = summarize_topology(payload)
    lines = [
        f"{source_label}: {Path(file_path).name}",
        f"路径: {file_path}",
        f"设备数量: {summary['device_count']}",
        f"链路数量: {summary['link_count']}",
        "这些设备名和本地端口应视为当前 MCP 操作的事实来源。",
    ]

    devices = summary.get("devices", [])
    if devices:
        lines.append("\n=== 设备列表 ===")
        for device in devices:
            name = device.get("name") or "<unknown>"
            model = device.get("model") or "unknown-model"
            port = device.get("port") if device.get("port") not in ("", None) else "missing-port"
            lines.append(f"  {name} | model={model} | local_port={port}")
    else:
        lines.append("\n=== 设备列表 ===")
        lines.append("  未解析到设备")

    return "\n".join(lines)


def _describe_parsed_topology(file_path: str, devices: list[dict], links: list[dict]) -> str:
    lines = [f"拓扑文件: {os.path.basename(file_path)}"]
    lines.append(f"设备数量: {len(devices)}")
    lines.append(f"链路数量: {len(links)}")

    if devices:
        lines.append("\n=== 设备列表 ===")
        lines.append(f"{'名称':<15} {'类型':<12} {'型号':<20}")
        lines.append("-" * 47)
        for d in devices:
            lines.append(f"{d['name']:<15} {d['type']:<12} {d['model']:<20}")
            if d.get("interfaces"):
                for iface in d["interfaces"]:
                    lines.append(f"  └─ {iface}")

    if links:
        lines.append("\n=== 链路连接 ===")
        for l in links:
            lines.append(f"  {l['src_device']}:{l['src_port']} ←→ {l['dst_device']}:{l['dst_port']}")

    return "\n".join(lines)


def _find_elements(root: ET.Element, paths: list[str]) -> list[ET.Element]:
    """尝试多种路径查找元素"""
    for path in paths:
        elements = root.findall(path)
        if elements:
            return elements
    # 也尝试递归查找
    tag = paths[-1].split("/")[-1] if paths else "device"
    return list(root.iter(tag))


def _extract_device_info(elem: ET.Element) -> dict | None:
    """从 XML 元素提取设备信息"""
    name = (
        elem.get("name")
        or elem.findtext("name")
        or elem.get("id")
        or elem.findtext("id")
    )
    if not name:
        return None

    dev_type = (
        elem.get("type")
        or elem.findtext("type")
        or elem.get("deviceType")
        or elem.findtext("deviceType")
        or "unknown"
    )

    model = (
        elem.get("model")
        or elem.findtext("model")
        or elem.get("deviceModel")
        or elem.findtext("deviceModel")
        or ""
    )

    # 提取接口列表
    interfaces = []
    for iface_tag in ["interface", "port", "interfaces/interface", "ports/port"]:
        for iface in elem.findall(iface_tag):
            iname = iface.get("name") or iface.findtext("name") or iface.text
            if iname:
                interfaces.append(iname.strip())
        if interfaces:
            break

    return {
        "name": name,
        "type": dev_type,
        "model": model,
        "interfaces": interfaces,
    }


def _extract_link_info(elem: ET.Element) -> dict | None:
    """从 XML 元素提取链路信息"""
    # 尝试多种属性/子元素格式
    src_device = (
        elem.get("srcDevice") or elem.get("src_device")
        or elem.findtext("srcDevice") or elem.findtext("source/device")
        or ""
    )
    src_port = (
        elem.get("srcPort") or elem.get("src_port")
        or elem.findtext("srcPort") or elem.findtext("source/port")
        or ""
    )
    dst_device = (
        elem.get("dstDevice") or elem.get("dst_device")
        or elem.findtext("dstDevice") or elem.findtext("destination/device")
        or ""
    )
    dst_port = (
        elem.get("dstPort") or elem.get("dst_port")
        or elem.findtext("dstPort") or elem.findtext("destination/port")
        or ""
    )

    if not (src_device and dst_device):
        return None

    return {
        "src_device": src_device,
        "src_port": src_port,
        "dst_device": dst_device,
        "dst_port": dst_port,
    }
