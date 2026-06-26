"""
Helpers for exposing the latest uploaded topology as MCP-readable context.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
TOPOLOGY_JSON = ROOT_DIR / "topology.json"


def load_current_topology() -> dict[str, object] | None:
    if not TOPOLOGY_JSON.exists():
        return None

    try:
        with TOPOLOGY_JSON.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None

    return payload if isinstance(payload, dict) else None


def summarize_topology(payload: dict[str, object] | None) -> dict[str, object]:
    devices = payload.get("devices", []) if isinstance(payload, dict) else []
    links = payload.get("links", []) if isinstance(payload, dict) else []

    normalized_devices: list[dict[str, object]] = []
    for item in devices:
        if not isinstance(item, dict):
            continue
        normalized_devices.append(
            {
                "name": str(item.get("name", "")).strip(),
                "model": str(item.get("model", "")).strip(),
                "port": item.get("comPort", item.get("port", "")),
            }
        )

    return {
        "path": str(TOPOLOGY_JSON),
        "device_count": len(normalized_devices),
        "link_count": len(links) if isinstance(links, list) else 0,
        "devices": normalized_devices,
    }


def format_topology_context(payload: dict[str, object] | None) -> str:
    if not payload:
        return "No topology has been uploaded yet. Ask the user to upload a .topo file in the web UI first."

    summary = summarize_topology(payload)
    devices = summary["devices"]

    lines = [
        "Current topology context from the web UI:",
        f"- Source file: {summary['path']}",
        f"- Devices: {summary['device_count']}",
        f"- Links: {summary['link_count']}",
        "- Treat these device names and local ports as the current source of truth for MCP actions.",
        "- Device inventory:",
    ]

    if devices:
        for device in devices:
            name = device.get("name") or "<unknown>"
            model = device.get("model") or "unknown-model"
            port = device.get("port") if device.get("port") not in ("", None) else "missing-port"
            lines.append(f"  - {name} | model={model} | local_port={port}")
    else:
        lines.append("  - No devices parsed from topology.json")
    return "\n".join(lines)


def topology_json_resource(payload: dict[str, object] | None) -> str:
    if not payload:
        return json.dumps({"devices": [], "links": [], "message": "No topology uploaded yet."}, ensure_ascii=False, indent=2)
    return json.dumps(payload, ensure_ascii=False, indent=2)
