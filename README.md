# ENSP-MCP AI

基于 MCP 协议的华为 eNSP 辅助工具，项目提供 MCP Server、设备 Console 操作工具、拓扑上传与可视化 Web UI，并将当前上传的拓扑暴露给 AI 客户端作为操作上下文。

## 功能概览

- **MCP Server**：向支持 MCP 的 AI 客户端提供 eNSP 设备管理、CLI 执行、配置下发、保存配置、运行配置查看、网络诊断和健康检查工具。
- **Topo Viewer**：本地浏览器页面，支持上传 `.topo`、`.xml`、`.json` 拓扑文件，并将设备和链路信息保存为 `topology.json`。
- **拓扑上下文**：AI 可优先读取当前上传拓扑，避免在本地文件系统中盲目搜索旧拓扑文件。
- **运行时日志**：Web UI 展示工具调用摘要、异常、设备连接、命令发送和终端回显。
- **Prompt Layer**：提供 `ensp_ops_playbook` 和 `ensp_senior_network_engineer` 两个 MCP Prompts。

## 运行环境

- Windows 系统。
- 已安装并启动华为 eNSP。
- Python 3.10 或更高版本。
- 支持 MCP 的 AI 客户端，例如 Claude Desktop、Codex App、Cursor、Trae 等。

> eNSP 本身依赖 Windows 环境，本项目也按 Windows 本地使用场景设计。

## 安装

推荐使用uv独立虚拟环境运行项目，避免污染系统 Python 环境。

### 第一种方式：使用 uv 创建虚拟环境

```powershell
cd E:\path\to\ENSP-MCP-ai
uv venv .venv
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
```

### 第二种方式：使用 pip 创建系统环境

```powershell
cd E:\path\to\ENSP-MCP-ai
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## MCP 客户端配置

### Claude Desktop

编辑 `%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "ensp-MCP": {
      "command": "E:\\path\\to\\ENSP-MCP-ai\\.venv\\Scripts\\python.exe",
      "args": ["-m", "ensp_mcp_server.server"],
      "cwd": "E:\\path\\to\\ENSP-MCP-ai"
    }
  }
}
```

### Codex App

Codex App 使用 Codex 的 `config.toml` 配置 MCP Server。可在 Codex App 中通过 Settings → Configuration 打开配置文件，也可以直接编辑 `~/.codex/config.toml`。

```toml
[mcp_servers.ensp-MCP]
command = "E:\\path\\to\\ENSP-MCP-ai\\.venv\\Scripts\\python.exe"
args = ["-m", "ensp_mcp_server.server"]
cwd = "E:\\path\\to\\ENSP-MCP-ai"
```

### Cursor / Trae

在 MCP Server 配置中使用同样的启动参数：

```json
{
  "command": "E:\\path\\to\\ENSP-MCP-ai\\.venv\\Scripts\\python.exe",
  "args": ["-m", "ensp_mcp_server.server"],
  "cwd": "E:\\path\\to\\ENSP-MCP-ai"
}
```

## 启动方式

客户端会自动通过 Python 模块启动 MCP Server：

```powershell
cd E:\path\to\ENSP-MCP-ai
.\.venv\Scripts\python.exe -m ensp_mcp_server.server
```

服务启动后，Web UI 地址为：

```txt
http://127.0.0.1:18080/index.html
```

## 使用流程

1. 在 eNSP 中打开拓扑（topo）并启动设备。
2. 在AI客户端中配置并连接 MCP Server。
3. 打开 Web UI：`http://127.0.0.1:18080/index.html`。
4. 上传 `.topo`、`.xml` 或 `.json` 拓扑文件。
5. Web UI 会保存 `topology.json`，并将拓扑设备同步到 MCP 注册表。
6. 在 AI 客户端中使用自然语言执行查看、配置、故障排查和验证任务。

示例：

```text
帮我查看 R1 的路由表
基于当前拓扑检查设备是否都能连接
给 R1 和 R2 配置 OSPF，并完成配置后验证邻居状态
检查当前拓扑中各设备配置是否和链路关系匹配
```

## 提示词与 Agent 配置

项目内置 MCP Prompts：`ensp_ops_playbook` 和 `ensp_senior_network_engineer`。如果客户端支持 MCP Prompts，可直接在客户端中启用。

对于 Trae等支持自定义 Agent/角色提示词的软件，可以将 [prompts.md](prompts.md) 的内容作为内置 Agent 提示词使用。该提示词用于约束 AI 按网络工程师方式工作：先确认拓扑和目标，再分步配置，最后验证结果。

## 项目结构

```text
eNSP-MCP-ai/
├── README.md
├── requirements.txt
├── index.html
├── topo-viewer.css
├── topo-viewer.js
├── prompts.md
└── ensp_mcp_server/
    ├── __init__.py
    ├── server.py
    ├── web_ui.py
    ├── runtime.py
    ├── topology_context.py
    ├── prompts.py
    ├── config.py
    ├── console.py
    ├── exceptions.py
    └── tools/
        ├── __init__.py
        ├── cli.py
        └── topology.py
```

## License

本项目遵循 MIT License，并基于 Redmig110 原作者的开源项目 [eNSP-AI](https://github.com/Redmig110/eNSP-AI) 二次开发。原项目提供了 MCP Server 与 eNSP 设备操作基础，本项目在此基础上扩展了内置 Web UI、拓扑上下文、运行日志、Prompt 层和状态处理逻辑。
