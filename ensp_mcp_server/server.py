"""
eNSP MCP Server entrypoint.
"""

from __future__ import annotations

import asyncio
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import GetPromptResult, Prompt, Resource, TextContent, Tool
from pydantic import AnyUrl

from .config import auto_discover_devices, config, list_devices, register_device, sync_topology_devices_from_file, unregister_device
from .runtime import finish_request, publish, request_scope
from .prompts import get_prompt, list_prompts
from .topology_context import format_topology_context, load_current_topology, summarize_topology, topology_json_resource
from .tools.cli import (
    execute_cli,
    get_running_config,
    health_check,
    multi_device_push_config,
    multi_health_check,
    ping_from_device,
    push_config,
    save_config,
    traceroute_from_device,
)
from .tools.topology import discover_topology, find_topo_files
from .web_ui import start_ui_server

logger = logging.getLogger("ensp_mcp.server")

SERVER_INSTRUCTIONS = (
    "Before any topology-sensitive decision, first use get_topology_context or read ensp://topology/current. "
    "If the web UI has uploaded a topology, that uploaded topology.json is the current source of truth for device names and local console ports. "
    "Do not search local .topo files unless the current uploaded topology is missing and the user explicitly wants filesystem discovery. "
    "When available, use the built-in prompts ensp_ops_playbook for topology-first operating discipline and "
    "ensp_senior_network_engineer for senior network engineering role guidance."
)

server = Server(config.server_name, instructions=SERVER_INSTRUCTIONS)


TOOLS = [
    Tool(
        name="register_device",
        description="Register an eNSP device by name and TCP port.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Device name, e.g. R1"},
                "port": {"type": "integer", "description": "Local TCP port, e.g. 2000"},
                "username": {"type": "string", "description": "Optional username"},
                "password": {"type": "string", "description": "Optional password"},
            },
            "required": ["name", "port"],
        },
    ),
    Tool(
        name="unregister_device",
        description="Remove a registered eNSP device.",
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Device name"}},
            "required": ["name"],
        },
    ),
    Tool(
        name="list_devices",
        description="List registered eNSP devices.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="auto_discover",
        description="Auto-discover eNSP devices by scanning local ports.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="execute_cli",
        description="Execute a single display/query command on a device.",
        inputSchema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Device name or IP"},
                "command": {"type": "string", "description": "CLI command"},
            },
            "required": ["device_ip", "command"],
        },
    ),
    Tool(
        name="mcp_execute",
        description="Execute a single command on a topology-synced eNSP device by its name via local MCP console port.",
        inputSchema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Topology device name or IP"},
                "command": {"type": "string", "description": "CLI command"},
            },
            "required": ["device_ip", "command"],
        },
    ),
    Tool(
        name="push_config",
        description="Push a list of configuration commands to a device.",
        inputSchema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Device name or IP"},
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 200,
                    "description": "Configuration commands",
                },
            },
            "required": ["device_ip", "commands"],
        },
    ),
    Tool(
        name="mcp_push_config",
        description="Push configuration commands to a topology-synced eNSP device via local MCP console port.",
        inputSchema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Topology device name or IP"},
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 200,
                    "description": "Configuration commands",
                },
            },
            "required": ["device_ip", "commands"],
        },
    ),
    Tool(
        name="multi_device_push_config",
        description="Push configuration to multiple devices.",
        inputSchema={
            "type": "object",
            "properties": {
                "devices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "device_ip": {"type": "string"},
                            "commands": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 200},
                        },
                        "required": ["device_ip", "commands"],
                    },
                    "description": "Device / command batches",
                }
            },
            "required": ["devices"],
        },
    ),
    Tool(
        name="save_config",
        description="Save the current device configuration.",
        inputSchema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Device name or IP"},
            },
            "required": ["device_ip"],
        },
    ),
    Tool(
        name="get_running_config",
        description="Read the running configuration of a device.",
        inputSchema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Device name or IP"},
                "section": {"type": "string", "description": "Optional section filter"},
            },
            "required": ["device_ip"],
        },
    ),
    Tool(
        name="ping_from_device",
        description="Run ping from a device.",
        inputSchema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Device name or IP"},
                "target_ip": {"type": "string", "description": "Ping target IP"},
                "count": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20, "description": "Ping count"},
                "source_ip": {"type": "string", "description": "Optional source IP"},
            },
            "required": ["device_ip", "target_ip"],
        },
    ),
    Tool(
        name="traceroute_from_device",
        description="Run traceroute from a device.",
        inputSchema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Device name or IP"},
                "target_ip": {"type": "string", "description": "Traceroute target IP"},
                "source_ip": {"type": "string", "description": "Optional source IP"},
            },
            "required": ["device_ip", "target_ip"],
        },
    ),
    Tool(
        name="health_check",
        description="Run a quick health check on one device.",
        inputSchema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Device name or IP"},
            },
            "required": ["device_ip"],
        },
    ),
    Tool(
        name="mcp_health_check",
        description="Check connectivity for a topology-synced eNSP device via local MCP console port.",
        inputSchema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Topology device name or IP"},
            },
            "required": ["device_ip"],
        },
    ),
    Tool(
        name="multi_health_check",
        description="Run health checks for multiple devices.",
        inputSchema={
            "type": "object",
            "properties": {
                "device_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional device name list",
                }
            },
        },
    ),
    Tool(
        name="discover_topology",
        description="Parse an eNSP .topo file, or summarize the current uploaded topology.json from the web UI when given that JSON path.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Full path to a .topo file, or to the uploaded topology.json snapshot"},
            },
            "required": ["file_path"],
        },
    ),
    Tool(
        name="find_topo_files",
        description="Check whether the web UI already uploaded topology.json, and only then search for extra local .topo files if needed.",
        inputSchema={
            "type": "object",
            "properties": {
                "search_dir": {"type": "string", "description": "Optional search directory"},
            },
        },
    ),
    Tool(
        name="sync_topology_devices",
        description="Register devices from topology.json into the MCP device registry using their eNSP local console ports.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Optional topology.json path"},
                "username": {"type": "string", "description": "Optional username to store with each device"},
                "password": {"type": "string", "description": "Optional password to store with each device"},
            },
        },
    ),
    Tool(
        name="get_topology_context",
        description="Preferred first step for topology-aware tasks. Read the latest topology uploaded from the web UI so the AI can understand current devices, links, and local ports.",
        inputSchema={"type": "object", "properties": {}},
    ),
]

PROMPTS = list_prompts()


TOOL_DISPATCH = {
    "register_device": lambda a: register_device(
        a["name"],
        a["port"],
        username=a.get("username", ""),
        password=a.get("password", ""),
    ),
    "unregister_device": lambda a: unregister_device(a["name"]),
    "list_devices": lambda a: list_devices(),
    "auto_discover": lambda a: auto_discover_devices(),
    "execute_cli": lambda a: execute_cli(a["device_ip"], a["command"]),
    "mcp_execute": lambda a: execute_cli(a["device_ip"], a["command"]),
    "push_config": lambda a: push_config(a["device_ip"], a["commands"]),
    "mcp_push_config": lambda a: push_config(a["device_ip"], a["commands"]),
    "multi_device_push_config": lambda a: multi_device_push_config(a["devices"]),
    "save_config": lambda a: save_config(a["device_ip"]),
    "get_running_config": lambda a: get_running_config(a["device_ip"], a.get("section")),
    "ping_from_device": lambda a: ping_from_device(
        a["device_ip"],
        a["target_ip"],
        a.get("count", 5),
        source_ip=a.get("source_ip"),
    ),
    "traceroute_from_device": lambda a: traceroute_from_device(
        a["device_ip"],
        a["target_ip"],
        source_ip=a.get("source_ip"),
    ),
    "health_check": lambda a: health_check(a["device_ip"]),
    "mcp_health_check": lambda a: health_check(a["device_ip"]),
    "multi_health_check": lambda a: multi_health_check(a.get("device_names")),
    "discover_topology": lambda a: discover_topology(a["file_path"]),
    "find_topo_files": lambda a: find_topo_files(a.get("search_dir", "")),
    "sync_topology_devices": lambda a: sync_topology_devices_from_file(
        a.get("file_path", ""),
        username=a.get("username", ""),
        password=a.get("password", ""),
    ),
    "get_topology_context": lambda a: format_topology_context(load_current_topology()),
}


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return TOOLS


@server.list_resources()
async def handle_list_resources() -> list[Resource]:
    current_topology = load_current_topology()
    summary = summarize_topology(current_topology)
    description = (
        f"Latest uploaded topology from the web UI with {summary['device_count']} devices and {summary['link_count']} links."
        if current_topology
        else "Latest uploaded topology from the web UI. No topology uploaded yet."
    )

    return [
        Resource(
            name="current_topology",
            uri="ensp://topology/current",
            description=description,
            mimeType="application/json",
        ),
        Resource(
            name="registered_devices",
            uri="ensp://devices/registered",
            description="Current MCP device registry derived from manual registration, auto-discovery, or topology sync.",
            mimeType="text/plain",
        ),
    ]


@server.list_prompts()
async def handle_list_prompts() -> list[Prompt]:
    return PROMPTS


@server.get_prompt()
async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
    return get_prompt(name)


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> str:
    uri_text = str(uri)
    if uri_text == "ensp://topology/current":
        return topology_json_resource(load_current_topology())
    if uri_text == "ensp://devices/registered":
        return list_devices()
    return f"Unknown resource: {uri_text}"


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    handler = TOOL_DISPATCH.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    with request_scope(name, arguments) as request_id:
        publish(
            "tool_start",
            tool=name,
            text=f"{name} started",
            payload={"arguments": arguments},
            extra={"status": "running"},
        )
        try:
            result = await asyncio.to_thread(handler, arguments)
        except Exception as exc:  # pragma: no cover - surfaced as text to client
            logger.error("Tool %s failed: %s", name, exc, exc_info=True)
            result = f"Tool {name} failed: {exc}"
            publish(
                "tool_error",
                tool=name,
                text=result,
                payload={"arguments": arguments},
                extra={"status": "error", "error": str(exc)},
            )
            finish_request(request_id, "error", error=str(exc))
        else:
            result_text = result if isinstance(result, str) else str(result)
            publish(
                "tool_end",
                tool=name,
                text=result_text,
                payload={"arguments": arguments},
                extra={"status": "done"},
            )
            finish_request(request_id, "done", result=result_text)

    return [TextContent(type="text", text=result)]


async def run() -> None:
    start_ui_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
