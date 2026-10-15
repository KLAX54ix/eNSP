# 修复清单（Fixes）

本仓库基于 [`dawnight0x/ENSP-MCP`](https://github.com/dawnight0x/ENSP-MCP)，
它的上游是 [`Redmig110/eNSP-AI`](https://github.com/Redmig110/eNSP-AI)。

我们在一次完整的结课实验实测中（13 台设备 / 17 条链路 / 210 条命令）挖出 **7 个问题**，
这里修掉了 **6 个**（第 7 个属运行环境限制，见文末）。

一共改动 **5 个文件，+143 / −45 行**。

---

## 1. `.topo` 按 UTF-8 读，但 eNSP 实际写的是 GBK

**现象**

```
discover_topology(file_path="结课测验版本.topo")
→ 无法读取 .topo 文件: 'utf-8' codec can't decode byte 0xb9 in position 2967: invalid start byte
```

浏览器侧更隐蔽 —— **不报错，静默变乱码**（实测原文件出现 720 个 `U+FFFD` 替换字符）。

**根因**

eNSP 保存的 `.topo` 里，XML 声明写着

```xml
<?xml version="1.0" encoding="UNICODE" ?>
```

**但文件内容其实是 GBK(CP936)。** 而 `UNICODE` 根本不是合法编码名，
Python 会直接抛 `LookupError`。

原代码防住了 `LookupError`，但最后一步把 `UNICODE` 猜成了 UTF-8：

```python
# ensp_mcp_server/tools/topology.py
# The legacy UNICODE label is not a codec; eNSP commonly uses UTF-8 here.   ← 这行假设是错的
text = raw.decode("utf-8" if encoding.upper() == "UNICODE" else encoding)
```

**验证矩阵（对照实验）**

| 输入 | 结果 |
|---|---|
| GBK（eNSP 真实格式） | ❌ 失败 |
| 同一内容存成 UTF-8 | ✅ 成功 |
| 纯 ASCII（无中文） | ✅ 成功 |
| UTF-16 + BOM | ✅ 成功 |

→ 只有「GBK + 含中文」必炸。

**修复**：严格 UTF-8 优先，失败依次退回 GB18030 / UTF-16。

```python
candidates = [declared] if declared and declared.upper() != "UNICODE" else []
candidates += ["utf-8", "gb18030", "utf-16"]
```

浏览器侧（`topo-viewer.js`）改成 `readAsArrayBuffer` + `TextDecoder`，
先严格 UTF-8，失败退回 GBK。

---

## 2. 一个 `<line>` 只取第一个 `<interfacePair>`

**现象**

```
discover_topology(...)
→ 链路数量: 15        （实际 17 条 interfacePair）
```

AI 拿到"Core_1 ↔ Core_2 只有 1 条链路"的描述 → **会误判链路聚合不存在**。

**根因**

eNSP 里**一个 `<line>` 是一"束"线**，每条 `<interfacePair>` 是一根物理线：

```xml
<line srcDeviceID="..." destDeviceID="...">
    <interfacePair srcIndex="21" tarIndex="21" .../>   ┐
    <interfacePair srcIndex="22" tarIndex="22" .../>   ├ 一束 = 3 根
    <interfacePair srcIndex="23" tarIndex="23" .../>   ┘
</line>
```

原代码用 `elem.find("interfacePair")` —— **只取第一条**。
不是把 `find` 改成 `findall` 就行：整个函数的返回契约错了，应该返回 `list`。

浏览器侧 `topo-viewer.js` 同样只产出一条（`querySelectorAll("lines > line")`，
完全不看 `interfacePair`）。

**修复**：新增 `_extract_link_infos()` 返回列表，逐 pair 产出；JS 侧遍历 `interfacePair`。

---

## 3. `save` 确认框正则不认 `(y/n)[n]`

**现象**

```
save_config(device_ip="AR1")     # AR2220，VRP 版本提示符是 (y/n)[n]:
→ 保存结果未确认：返回提示符但没有保存成功回显
```

**根因**

```python
_SAVE_CONFIRM_RE = re.compile(
    r"(?:\[\s*y\s*/\s*n\s*\]|\(\s*y\s*/\s*n\s*\))\s*[:：]?\s*$"
)
#          ↑ 期望 [y/n]        ↑ 或 (y/n)，后面直接是 : 或行尾
```

实际提示符是 `Are you sure to continue? (y/n)[n]:` —— `(y/n)` 后面还有个 `[n]`。

**修复**

```python
r"(?:\[\s*y\s*/\s*n\s*\]|\(\s*y\s*/\s*n\s*\))"
r"(?:\s*\[\s*[ynYN]\s*\])?"        # ← 兼容 [n] 默认值
r"\s*[:：]?\s*$"
```

---

## 4. `_send_line()` 用 `\r\n` → `\n` 抢答确认框（**根因**）

**现象**：修完 #3 之后，确认框能认出来了、也发了 `Y`，
**但设备直接回提示符、保存仍未执行。**

**实测抓到的字节流**

```
收到: b'\r\n  The current configuration will be written to the device. \r\n
         Are you sure to continue? (y/n)[n]:'
_SAVE_CONFIRM_RE 匹配: True      ← 正则已经修好了
>>> 发送 'Y'
--- 收到 7 字节 ---
  repr: b'\r\n<AR1>'            ← 直接回提示符，没有 "Now saving..." ✗
```

**根因**

```python
self._sock.sendall((text + "\r\n").encode("utf-8"))
#                         ↑↑↑
```

```
① 发 save\r\n
   ├─ \r  → 执行 save，弹出确认框
   └─ \n  → 被确认框当成"直接回车" → 取默认值 n → 保存取消 ✗
② 我们才发 Y → 已经晚了，设备早回到 <AR1> ✗
```

VRP 只认 `\r` 当回车。多出来的 `\n` 会被"下一个还没出现的提示符"吃掉。

**修复**

```python
self._sock.sendall((text + "\r").encode("utf-8"))   # 只发 CR
```

**验证**：裸 socket 直发 `save\r` + `Y\r` →

```
  It will take several minutes to save configuration file, please wait.......
  Configuration file had been saved successfully
```

> **注**：这与 [`Heaxu/ensp-mcp`](https://github.com/Heaxu/ensp-mcp) 的同类 bug 是**同一个根因** ——
> 两个项目各自栽了一次。

---

## 5. 保存成功回显没在正则里

**现象**：AR 系列保存明明成功，却报"保存结果未确认"。

**根因**：`_SAVE_SUCCESS_RE` 只覆盖了这几种措辞：

```
Save (the )?(current )?configuration successfully
(The )?(current )?configuration (was |has been )?saved (to the device )?successfully
Successfully saved (the )?(current )?configuration
配置(文件)?保存成功
```

而 AR2220 的实际回显是 `Configuration file had been saved successfully` ✗

**修复**：补一条

```python
r"|Configuration\s+file\s+had\s+been\s+saved\s+successfully[.!]?"
```

---

## 6. `ping_from_device` 的 `-t 2` 单位理解错 → 永远报 100% 丢包

**现象**

```python
ping_from_device(device_ip="AR1", target_ip="155.1.12.1")
→ 3 packet(s) transmitted, 0 packet(s) received, 100.00% packet loss
```

但同一台设备手动 ping 完全正常。

**根因**

```python
command = f"ping -c {count} -t 2"
```

**VRP 的 `ping -t` 单位是毫秒**，`-t 2` = 2 毫秒超时 → 必然全超时。

**对照实验**

```
ping -c 3 -t 2      155.1.12.1   → 100.00% packet loss   ✗
ping -c 3 -t 2000   155.1.12.1   →   0.00% packet loss   ✓
ping -c 3           155.1.12.1   →   0.00% packet loss   ✓
```

**影响（严重）**：这是**会让 AI 做出完全错误判断**的 bug ——
用 `ping_from_device` 排查故障时会得出"网络全断"的结论，
进而去查 VLAN、查电缆，全是白费。

**修复**：`f"ping -c {count} -t 2000"`。

---

## 7. `auto_discover` 依赖 `tasklist`（未修，属环境限制）

**现象**（在容器 / Agent 沙箱环境里）

```
auto_discover()
→ 未检测到 eNSP_Client.exe 进程，请确认 eNSP 已启动且设备已运行。
```

但 `tasklist` 在命令行里明明能看到 `eNSP_Client.exe`。

**实测对比**

| 调用方式 | 能否看到 eNSP_Client |
|---|---|
| 直接在 shell 里跑 `tasklist` | ✅ |
| 从 Python `subprocess.run(["tasklist"])` | ❌ |
| `VBoxManage list vms`（读注册表） | ✅ |
| `VBoxManage list runningvms`（需命名管道 IPC） | ❌ |

**原因**：容器 / 沙箱会限制子进程的进程视图与 IPC 能力，**不是本项目代码的问题**。

**建议的替代做法**：不要用 `tasklist` 作为硬门槛，
改成**按 `.topo` 里的 `com_port` 逐个 TCP 探测** ——
既能判断设备是否起来，又顺带得到"设备 ↔ 端口"的映射，且不受权限限制。

---

## 附：其它可优化项（未改）

- `README.md` 说 `mcp_execute` 按 "name" 定位设备，但实际 schema 参数是 `device_ip`
- 工具集有两套语义相近的命名（`execute_cli` / `mcp_execute`、`push_config` / `mcp_push_config`、
  `health_check` / `mcp_health_check`），文档里没说清区别
- 仓库没有 LICENSE

---

## 复现与验证

本次实测环境：

```
eNSP V1.3.00.100 + VirtualBox 5.2.26r128414 + Windows + Python 3.12.6
拓扑：13 台设备 / 17 条链路（OSPF 多区域 / 链路聚合 / STP / VLAN / NAPT / NAT Server）
```

修复后：

- 9 台网络设备、210 条命令**一次跑通**（约 35 秒）
- 9 台设备**全部保存成功**（覆盖 `(y/n)[n]` 与 `[Y/N]` 两种提示符）
- 端到端验收通过：站点 B 的 Client 通过 `www.webtest.com`
  访问到站点 A 的内网 Web 服务器
- NAT 会话表同时体现源 NAT（Easy IP）与目的 NAT（NAT Server）
