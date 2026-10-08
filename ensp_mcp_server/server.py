"""
eNSP MCP Server entrypoint.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.prompts import Message, Prompt as FastMCPPrompt, PromptResult
from fastmcp.resources import Resource as FastMCPResource
from fastmcp.tools import Tool as FastMCPTool, ToolResult
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp.types import Resource, TextContent, Tool
from pydantic import PrivateAttr

from .config import auto_discover_devices, config, list_devices, register_device, sync_topology_devices_from_file, unregister_device
from .exceptions import ENSPError, ToolFailure
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

SINGLE_LINE_STRING = {
    "type": "string", "minLength": 1, "pattern": r"\S",
    "not": {"pattern": r"[\r\n\u0000]"},
}
COMMAND_LIST = {"type": "array", "minItems": 1, "items": SINGLE_LINE_STRING}

TOOLS = [
    Tool(
        name="register_device",
        description="Register an eNSP device by name and TCP port.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {**SINGLE_LINE_STRING, "description": "Device name, e.g. R1"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535, "description": "Local TCP port, e.g. 2000"},
                "username": {"type": "string", "description": "Optional username"},
                "password": {"type": "string", "description": "Optional password"},
            },
            "required": ["name", "port"],
        },
    ),
    Tool(
        name="unregister_device",
        description="Remove a registered eNSP device.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Device name"}},
            "required": ["name"],
        },
    ),
    Tool(
        name="list_devices",
        description="List registered eNSP devices.",
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="auto_discover",
        description="Auto-discover eNSP devices by scanning local ports.",
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="execute_cli",
        description="Execute a single display/query command on a device.",
        input_schema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Device name or IP"},
                "command": {**SINGLE_LINE_STRING, "description": "CLI command"},
            },
            "required": ["device_ip", "command"],
        },
    ),
    Tool(
        name="mcp_execute",
        description="Execute a single command on a topology-synced eNSP device by its name via local MCP console port.",
        input_schema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Topology device name or IP"},
                "command": {**SINGLE_LINE_STRING, "description": "CLI command"},
            },
            "required": ["device_ip", "command"],
        },
    ),
    Tool(
        name="push_config",
        description="Push configuration commands sequentially; stop on the first failure and report unexecuted commands. Applied commands are not rolled back.",
        input_schema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Device name or IP"},
                "commands": {
                    **COMMAND_LIST,
                    "description": "Configuration commands",
                },
            },
            "required": ["device_ip", "commands"],
        },
    ),
    Tool(
        name="mcp_push_config",
        description="Push commands to a topology-synced device; stop on the first failure. Applied commands are not rolled back.",
        input_schema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Topology device name or IP"},
                "commands": {
                    **COMMAND_LIST,
                    "description": "Configuration commands",
                },
            },
            "required": ["device_ip", "commands"],
        },
    ),
    Tool(
        name="multi_device_push_config",
        description="Validate all batches, then push configuration sequentially. Stop remaining commands and devices on the first failure; no automatic rollback.",
        input_schema={
            "type": "object",
            "properties": {
                "devices": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "device_ip": SINGLE_LINE_STRING,
                            "commands": COMMAND_LIST,
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
        description="Save configuration, answering Y/N and accepting a default filename. Success requires a save completion message and final console prompt.",
        input_schema={
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
        input_schema={
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
        input_schema={
            "type": "object",
            "properties": {
                "device_ip": {"type": "string", "description": "Device name or IP"},
                "target_ip": {"type": "string", "description": "Ping target IP"},
                "count": {"type": "integer", "default": 5, "description": "Ping count"},
                "source_ip": {"type": "string", "description": "Optional source IP"},
            },
            "required": ["device_ip", "target_ip"],
        },
    ),
    Tool(
        name="traceroute_from_device",
        description="Run traceroute from a device.",
        input_schema={
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
        input_schema={
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
        input_schema={
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
        input_schema={
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
        input_schema={
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
        input_schema={
            "type": "object",
            "properties": {
                "search_dir": {"type": "string", "description": "Optional search directory"},
            },
        },
    ),
    Tool(
        name="sync_topology_devices",
        description="Register devices from topology.json into the MCP device registry using their eNSP local console ports.",
        input_schema={
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
        input_schema={"type": "object", "properties": {}},
    ),
]

for tool_spec in TOOLS:
    # Apply the same device-name boundary to every console tool and alias.
    for field in ("device_ip", "name"):
        field_schema = tool_spec.input_schema.get("properties", {}).get(field)
        if field_schema is not None:
            field_schema.update(SINGLE_LINE_STRING)

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


def _topology_description() -> str:
    current_topology = load_current_topology()
    summary = summarize_topology(current_topology)
    return (
        f"Latest uploaded topology from the web UI with {summary['device_count']} devices and {summary['link_count']} links."
        if current_topology
        else "Latest uploaded topology from the web UI. No topology uploaded yet."
    )


async def handle_call_tool(name: str, arguments: dict[str, Any]) -> ToolResult:
    handler = TOOL_DISPATCH.get(name)
    if handler is None:
        raise ToolError(f"Unknown tool: {name}")

    is_error = False
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
            if isinstance(result, ToolFailure):
                raise ENSPError(str(result))
        except Exception as exc:  # pragma: no cover - surfaced as text to client
            is_error = True
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

    return ToolResult(
        content=[TextContent(type="text", text=result if isinstance(result, str) else str(result))],
        is_error=is_error,
    )


class EnspTool(FastMCPTool):
    """Keep the published JSON schemas while using FastMCP's tool lifecycle."""

    _input_validator: Draft202012Validator = PrivateAttr()

    def model_post_init(self, context: Any) -> None:
        super().model_post_init(context)
        Draft202012Validator.check_schema(self.parameters)
        self._input_validator = Draft202012Validator(self.parameters)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        # Custom Tool subclasses do not get FunctionTool's Pydantic validation.
        # Validate before dispatch so malformed requests cannot reach devices.
        try:
            self._input_validator.validate(arguments)
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path) or "arguments"
            raise ToolError(f"Invalid {path}: {exc.message}") from exc
        return await handle_call_tool(self.name, arguments)


class CurrentTopologyResource(FastMCPResource):
    """Read the uploaded snapshot on every request, including its description."""

    def to_mcp_resource(self, **overrides: Any) -> Resource:
        overrides.setdefault("description", _topology_description())
        return super().to_mcp_resource(**overrides)

    async def read(self) -> str:
        return await asyncio.to_thread(lambda: topology_json_resource(load_current_topology()))


class EnspPrompt(FastMCPPrompt):
    """Preserve both prompt messages and attach the current topology at render time."""

    async def render(self, arguments: dict[str, Any] | None = None) -> PromptResult:
        result = await asyncio.to_thread(get_prompt, self.name)
        return PromptResult(
            messages=[Message(message.content, role=message.role) for message in result.messages],
            description=result.description,
        )


server = FastMCP(
    config.server_name,
    instructions=SERVER_INSTRUCTIONS,
    version="0.2.0",
    on_duplicate="error",
)

for tool_spec in TOOLS:
    server.add_tool(
        EnspTool(
            name=tool_spec.name,
            description=tool_spec.description,
            parameters=tool_spec.input_schema,
        )
    )

server.add_resource(
    CurrentTopologyResource(
        name="current_topology",
        uri="ensp://topology/current",
        description="Latest uploaded topology from the web UI.",
        mime_type="application/json",
    )
)


@server.resource(
    "ensp://devices/registered",
    name="registered_devices",
    description="Current MCP device registry derived from manual registration, auto-discovery, or topology sync.",
    mime_type="text/plain",
)
def registered_devices_resource() -> str:
    return list_devices()


for prompt_spec in PROMPTS:
    server.add_prompt(
        EnspPrompt(name=prompt_spec.name, title=prompt_spec.title, description=prompt_spec.description)
    )


async def run() -> None:
    start_ui_server()
    await server.run_async(transport="stdio", show_banner=False)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
