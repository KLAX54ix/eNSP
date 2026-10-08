"""Input validation shared by MCP handlers and direct/local callers."""

import re


def validate_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip() or any(c in name for c in "\r\n\x00"):
        raise ValueError("设备名不能为空，也不能包含换行或 NUL")
    return name.strip()


def validate_port(port: object, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(port) is not int or not minimum <= port <= 65535:
        raise ValueError(f"控制台端口必须是 {minimum}–65535 范围的整数")
    return port


def validate_commands(commands: object) -> list[str]:
    if not isinstance(commands, list) or not commands:
        raise ValueError("commands 必须是非空命令列表")
    for command in commands:
        if not isinstance(command, str) or not command.strip() or any(c in command for c in "\r\n\x00"):
            raise ValueError("每条命令必须是非空的单行字符串")
    return list(commands)


def validate_topology_devices(devices: object) -> list[dict]:
    if not isinstance(devices, list):
        raise ValueError("topology.devices 必须是数组；空拓扑请显式使用 devices: []")
    normalized = []
    seen = set()
    console_ports: dict[int, str] = {}
    for index, item in enumerate(devices):
        if not isinstance(item, dict):
            raise ValueError(f"devices[{index}] 必须是设备对象")
        name = validate_name(item.get("name"))
        if name.casefold() in seen:
            raise ValueError(f"拓扑中存在重复设备名: {name}")
        seen.add(name.casefold())
        port = item.get("comPort", item.get("port"))
        # Older web snapshots serialize ports as decimal strings.
        if isinstance(port, str) and re.fullmatch(r"[0-9]+", port.strip()):
            port = int(port)
        port = validate_port(port, allow_zero=True)
        if port:
            if port in console_ports:
                raise ValueError(f"拓扑中存在重复控制台端口 {port}: {console_ports[port]} 与 {name}")
            console_ports[port] = name
        normalized.append({**item, "name": name, "comPort": port})
    return normalized


def validate_topology_payload(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or "devices" not in payload:
        raise ValueError("拓扑必须是包含 devices 数组的 JSON 对象")
    if "links" in payload and (not isinstance(payload["links"], list)
                               or any(not isinstance(link, dict) for link in payload["links"])):
        raise ValueError("topology.links 必须是链路对象数组")
    return validate_topology_devices(payload["devices"])
