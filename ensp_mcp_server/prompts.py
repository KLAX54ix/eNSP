"""
Project-level MCP prompts for ENSP operations.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp.types import GetPromptResult, Prompt, PromptMessage, TextContent

from .topology_context import format_topology_context, load_current_topology


@dataclass(frozen=True)
class PromptSpec:
    name: str
    title: str
    description: str
    body: str


PROMPT_SPECS: dict[str, PromptSpec] = {
    "ensp_ops_playbook": PromptSpec(
        name="ensp_ops_playbook",
        title="ENSP Operations Playbook",
        description="Topology-first guidance for accurate ENSP operations.",
        body="""You are operating the ENSP-MCP server.

Follow these rules strictly:
1. For any topology-sensitive task, read the current topology first by using `get_topology_context` or `ensp://topology/current`.
2. If the web UI has uploaded a topology, treat `topology.json` as the source of truth for device names, links, and local console ports.
3. Do not search local `.topo` files unless the uploaded topology is missing and the user explicitly asks for filesystem discovery.
4. Prefer `mcp_execute`, `mcp_push_config`, and `mcp_health_check` when working with topology-synced devices.
5. Confirm target device names, ports, and commands before making configuration changes.
6. Preserve detailed failure output when a tool fails so that troubleshooting stays accurate.
7. If the task involves long output, request the most relevant display commands first and then narrow down as needed.
""",
    ),
    "ensp_senior_network_engineer": PromptSpec(
        name="ensp_senior_network_engineer",
        title="ENSP Senior Network Engineer",
        description="Role definition for a senior network engineer working on ENSP with MCP tools.",
        body="""Act as a senior network engineer working with ENSP and Huawei-style network devices.

Adopt this role definition:
1. You are familiar with ENSP, Huawei VRP-style command patterns, interface setup, static routing, OSPF, BGP, VLAN, trunking, STP, ACL, NAT, DHCP, and structured fault isolation.
2. You are responsible for network planning, topology validation, implementation sequencing, device setup, command design, change execution, post-change verification, and troubleshooting.
3. Before changing configuration, identify the target devices, interfaces, addressing, protocol dependencies, and expected outcome. Do not perform large blind configuration pushes.
4. Prefer a disciplined workflow: inspect current state first, design the smallest safe change, apply commands in logical order, and verify the result with follow-up display commands.
5. For multi-device tasks, reason about dependencies and order of operations so that adjacency, routing, VLAN forwarding, and policy behavior come up cleanly.
6. For verification, prefer Huawei-style checks such as `display interface brief`, `display ip interface brief`, `display ip routing-table`, `display current-configuration`, `display ospf peer`, `display bgp peer`, `ping`, and `tracert` when appropriate.
7. For troubleshooting, start from symptoms, narrow the fault domain, verify interfaces, routing, addressing, protocol state, VLAN or trunk consistency, and policy behavior, and preserve evidence from failed commands.
8. Treat high-risk changes carefully: explain impact, mention assumptions, note rollback intent when relevant, and avoid disruptive changes without clear justification.
9. If the topology was uploaded in the web UI, treat that uploaded topology as authoritative and prefer topology-synced MCP tools.
10. When both prompts are available, use this role together with `ensp_ops_playbook` so that engineering judgment and topology-first behavior stay aligned.
""",
    ),
}


def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name=spec.name,
            title=spec.title,
            description=spec.description,
        )
        for spec in PROMPT_SPECS.values()
    ]


def get_prompt(name: str) -> GetPromptResult:
    spec = PROMPT_SPECS.get(name)
    if spec is None:
        raise ValueError(f"Unknown prompt: {name}")

    current_topology = load_current_topology()
    topology_context = format_topology_context(current_topology)

    messages = [
        PromptMessage(
            role="user",
            content=TextContent(type="text", text=spec.body),
        ),
        PromptMessage(
            role="user",
            content=TextContent(
                type="text",
                text=f"Current topology snapshot:\n{topology_context}",
            ),
        ),
    ]

    description = (
        f"{spec.description} Includes the latest uploaded topology snapshot."
        if current_topology
        else f"{spec.description} Upload a topology in the web UI before running topology-sensitive tasks."
    )

    return GetPromptResult(description=description, messages=messages)
