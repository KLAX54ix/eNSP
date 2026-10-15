> **说明**：这是在一次 eNSP 结课实验实测（13 台设备 / 17 条链路 / 210 条命令）中，
> 对三个 eNSP MCP 项目挖出的全部问题清单，写成**可以直接提交 issue** 的形式。
>
> - 本仓库（dawnight0x/ENSP-MCP）自身的 **6 个已修问题**，详见 [`FIXES.md`](FIXES.md)
> - Heaxu / Simon 部分列出的是**上游仍存在的问题**，供各自仓库参考
> - 文末有一节**跨项目共性问题** —— 这是全文最有意思的部分：
>   三个项目、两套血统、隔着几个月，在完全相同的几个点上各栽一次

---

# eNSP MCP 各项目 Bug 清单（可直接提 Issue）

> 测试环境：eNSP V1.3.00.100 + VirtualBox 5.2.26r128414 + Windows + Python 3.12.6
> 测试拓扑：13 设备 / 17 链路（含链路聚合、OSPF 多区域、NAPT + NAT Server、VLAN、STP）
> 所有 bug 都是**实测复现**，附根因与修复建议。

---

# 一、Heaxu/ensp-mcp

仓库：https://github.com/Heaxu/ensp-mcp
版本：1.2.0 ｜ 发现 16 个问题，已修 13 个 ｜ 补丁：`heaxu-fixes.patch`（5 文件 +249/−50）

---

## H1 🔴 写文件用 UTF-8，但 XML 声明写 `encoding="UNICODE"` → 中文全部乱码

**复现**
```python
from ensp_mcp.topo import Topology
t = Topology.load("待改.topo")
t.save("out.topo")     # 中文设备名 / 分区标题
```
在 eNSP 里打开 `out.topo` → 中文设备名显示成 `鏍哄睍灞` 之类。

**根因** `xmlio.py:96`
```python
def atomic_write(path, text, encoding="utf-8"):     # ← 写死 UTF-8
```
而 `topo.py` 读文件时对编码很讲究：
```python
try:
    return raw.decode("utf-8-sig", errors="strict")
except UnicodeDecodeError:
    # eNSP 自带的中文示例沿用 Windows ANSI（CP936）   ← 注释里自己写了
    return raw.decode("gb18030", errors="strict")
```
**读**做了双编码兼容、注释里还点明 eNSP 用 CP936，**写**却写死 UTF-8 —— 读写不对称。

**影响**：只有拓扑含中文时才暴露（纯 ASCII 不受影响），而中文设备名在 eNSP 里非常常见。

**修复**：`atomic_write` 默认编码改 `gb18030`。

---

## H2 🔴 发命令用 `\r\n`，多出的 `\n` 被 VRP 5.130 的 `(y/n)[n]` 提示符吃成"直接回车"

**复现**
```
device_exec(target=<AR2220 的端口>, commands=["save"], ...)
```
→ 保存**必然失败**；老机型（`Router` 型号，`[Y/N]` 无默认值）却正常。

**根因** `console.py:314`
```python
self._write(command + "\r\n")
```
```
发送: s a v e \r \n
              │   └─ 这个 LF 落在紧接着弹出的确认框上
              └─ 执行 save → 弹 "Are you sure to continue? (y/n)[n]:"
设备: 收到遗留 LF → 当成"直接回车" → 取默认值 [n] → 取消保存 ✗
```
对比：
- VRP 5.110 `[Y/N]` —— 无默认值，空回车被忽略 → 不触发
- VRP 5.130 `(y/n)[n]` —— `[n]` 是默认值，空回车 = 回答"否" → 触发

**修复**：`self._write(command + "\r")`（3 处：命令、确认应答、换行）。

---

## H3 🔴 `to_user_view()` 的 `quit` 兜底会把控制台踢下线

**复现**：会话状态不明（提示符没解析出来）时调用任何 `device_*` 工具，随后控制台掉线，
回显 `Configuration console exit, please press any key to log on`。

**根因** `console.py:401`
```python
for _ in range(6):
    if self.prompt and self.prompt.view == "user":
        return
    await self.send("return")
    if self.prompt and self.prompt.view == "user":
        return
    await self.send("quit")        # ← VRP 里 quit 是按视图往上退
```
| 当前视图 | `quit` 的结果 |
|---|---|
| 子视图 `[R1-GE0/0/1]` | 回系统视图 ✓ |
| 系统视图 `[R1]` | 回用户视图 ✓ |
| **用户视图 `<R1>`** | **退出登录** ✗ |

**修复**：删掉 `quit` 兜底。

---

## H4 🔴 提示符正则用 `\Z` 锚定，遇到异步 syslog 就断线

**现象**：随机超时、会话错位、命令串味。

**根因** `console.py:35`
```python
_PROMPT_RE = re.compile(r"(?:\A|[\r\n])[ \t]*([<\[])([~*]?)([^\r\n<>\[\]]{1,80})([>\]])[ \t]*\Z")
```
要求提示符**正好在读到的字节块末尾**。但 VRP 随时会打印 syslog，常见形态：
```
<R1>display clock
2026-10-15 00:02:41
<R1>                                   ← 提示符在中间
Oct 14 2026 ... %%01IFNET/4/LINK_STATE ← syslog 在末尾
```
→ 匹配失败 → `send()` 死等 → 超时并主动断线。

**修复**：加宽松匹配兜底（`Prompt._last_plausible`）。

---

## H5 🔴 发命令前不清空遗留缓冲 → 陈旧提示符错位

**现象**：`device_exec` 成功，紧接着 `device_push_config` 报"进不了系统视图"。

**根因** `console.py:341`：发送前不 drain，缓冲区里还残留上一条命令的提示符。

**修复**：`send()` 里加 `await self._drain_idle(idle=0.15, cap=0.6)`。

---

## H6 🟠 `strip_echo` 不剥行首提示符

**根因** `console.py:103`：只剥命令本身，不剥前面粘着的 `<R1>`。
**修复**：用 `_PROMPT_LOOSE_RE.sub` 去掉行首提示符。

---

## H7 🟠 连接池复用会话时不校验状态

**根因** `console.py:496`：只查 `alive` 和超时，不看会话当前视图。
**修复**：加 `console.prompt.view in ("user", "system")` 判断。

---

## H8 🟡 默认超时仅 8 秒，大输出命令（`display current-configuration`）会超时并断线

**根因** `console.py:443`
**修复**：`_timeout_for()` 对 `display`/`show` 返回 20 秒。

---

## H9 🟡 `_error_in` 用 `marker in stripped[:40]` 误报错误

**现象**：正常输出里含 "Failed" 字样的正当回显被判定为失败。
**根因** `console.py:462`：子串匹配而非行首匹配。
**修复**：只在行首（或跳过前导空格/数字/^）匹配，并把 `"Failed"` 收紧为 `"Failed to "`。

---

## H10 🟡 多行文字标注用 LF 换行，eNSP 不换行

**根因** `annotations.py:303`：content 里用 `\n`。
**修复**：统一 CRLF。

---

## H11 🟡 缺拓扑编辑工具

**修复**：新增 `topo_move_device`（单台/批量移动设备）。

---

## H12 🟡 分区标题硬限 120 字符

**修复**：新增 `topo_add_text`（可写长文本、可调字号颜色）。

---

## H13 🟢 PNG 渲染不画分区背景框（仅记录）

**位置**：`render.py`｜只影响预览图观感。

---

## H14 🟢 不可写目录下 `mkstemp` 重试 23 万次，假死 60 秒（仅记录）

**现象**：调 `topo_create` 写一个权限不足的路径，进程卡死约 60 秒、CPU 80%，且工具持有全局锁 → 其他拓扑工具一起阻塞。

**根因链**
```
① 进程权限不足 → PermissionError
② 但 os.access(dir, W_OK) 只查 ACL、不查进程实际权限 → 返回 True（撒谎）
③ 代码用 tempfile.mkstemp(dir=parent)
④ CPython 在 Windows 上有:
     except PermissionError:
         if os.name == 'nt' and os.access(dir, os.W_OK):
             continue          # 以为"同名目录已存在"，继续重试
⑤ → 重试 TMP_MAX = 238,328 次
⑥ → 实测正好约 60 秒（CPU 80%），还持有全局锁
```
**实测**
```
[普通 open() 写入] ❌ PermissionError
[os.access(W_OK)]  ✅ True              ← 撒谎
PID 5464 CPU 383.4 → 385.8 秒（3 秒涨 2.4 秒）  ← 空转重试
```
**修复建议**：不用 `mkstemp` 落盘，或先显式 `open(path, "wb")` 探一次权限。

---

## H15 🔴 同一对设备的多条链路被拆成多个 `<line>` → 链路聚合在画布上完全重叠

**现象**：Core_1↔Core_2 之间 3 条链路（链路聚合），但 eNSP 画布上**只看得到一根**。
数据是对的（`display eth-trunk 1` 显示 `Number Of Up Port In Trunk: 3`）。

**根因**
```
【老师的原版】
  line 6: Core_1 ↔ Core_2   ★ 一个 <line> 里装了【3 条 interfacePair】
           srcIndex=21 / 22 / 23

【Heaxu 生成的】
  line 9:  Core_1 ↔ Core_2   1 条
  line10:  Core_1 ↔ Core_2   1 条   ← 拆成了 3 个独立 <line>
  line11:  Core_1 ↔ Core_2   1 条
```
eNSP 里**一个 `<line>` = "一束线"**，里面的每个 `<interfacePair>` 是一根物理线，
eNSP 会把同一束里的线**自动错开绘制**。`topo_connect` 每次调用新建一个 `<line>` ✗
→ eNSP 以为是 3 根互不相干的单线 ✗ → 端点相同 → 完全重叠 ✗

**位置**：`topo.py` 的 `Link.to_element()` + `Topology.save()`

**修复**
- `Link.to_element()` 改成只产出一个 `<interfacePair>`，并按端口序号精确匹配原始 pair
- `Topology.save()` 按设备对分组，把同一对设备的链路合并进**同一个 `<line>`**

**连带修掉一个隐藏 bug**：原 `to_element()` 会把**整个 `<line>`**（含其他 pair）复制出来，
对多 pair 的拓扑做一次"读→写"就会**把链路复制成多份**。

---

# 二、dawnight0x/ENSP-MCP

仓库：https://github.com/dawnight0x/ENSP-MCP
版本：0.2.0 ｜ 发现 7 个问题，已修 6 个 ｜ 补丁：见仓库根目录 `fixes.patch`，或 `FIXES.md`（5 文件 +143/−45）

---

## D1 🔴 `.topo` 按 UTF-8 读，但 eNSP 实际写的是 GBK

**复现**
```python
discover_topology(file_path="结课测验版本.topo")
```
→ `无法读取 .topo 文件: 'utf-8' codec can't decode byte 0xb9 in position 2967: invalid start byte`

**根因** `ensp_mcp_server/tools/topology.py`
```python
else:
    # For ASCII-compatible encodings, consult only the declaration. The
    # legacy UNICODE label is not a codec; eNSP commonly uses UTF-8 here.   ← 这行假设是错的
    encoding = declared.group("encoding") if declared else "utf-8"
    text = raw.decode("utf-8" if encoding.upper() == "UNICODE" else encoding)
```
作者很聪明地处理了"`UNICODE` 不是合法编码名"（会抛 `LookupError`）和 BOM/UTF-16 字节序，
但最后一步把 `UNICODE` 猜成了 UTF-8 —— **eNSP 实际写的是 GBK(CP936)**。

**验证矩阵（对照实验）**
| 输入 | 结果 |
|---|---|
| GBK（eNSP 真实格式） | ❌ 失败 |
| 同一内容存成 UTF-8 | ✅ 成功 |
| 纯 ASCII（无中文） | ✅ 成功 |
| UTF-16 + BOM | ✅ 成功 |

→ 只有「GBK + 含中文」必炸，而 eNSP 保存的 `.topo` 就是 GBK。

**同一问题也在浏览器侧** `topo-viewer.js:586`
```javascript
reader.readAsText(file, "utf-8");
```
更糟的是浏览器侧**不报错**：UTF-8 解码器遇非法字节只替换成 `U+FFFD`，于是
**静默产生乱码设备名**（实测原文件 720 个替换字符）。

**修复**
```python
# Python：严格 UTF-8 优先，失败退回 GB18030
candidates = [declared] if declared and declared.upper() != "UNICODE" else []
candidates += ["utf-8", "gb18030", "utf-16"]
for c in candidates:
    try: text = raw.decode(c); break
    except (LookupError, UnicodeDecodeError) as e: last = e
```
```javascript
// 浏览器：按字节读，先严格 UTF-8，失败退回 GBK
reader.readAsArrayBuffer(file);
try   { return new TextDecoder("utf-8", {fatal: true}).decode(bytes); }
catch { return new TextDecoder("gbk").decode(bytes); }
```

---

## D2 🔴 一个 `<line>` 只取第一个 `<interfacePair>` → 链路数少报、链路聚合被误判

**复现**
```
discover_topology("结课测验版本.topo")
→ 链路数量: 15        （实际 17 条 interfacePair）

  Core_1:21 ←→ Core_2:21
  Core_1:22 ←→ Core_2:22     ← 这两条不见了
  Core_1:23 ←→ Core_2:23
```
AI 拿到"Core_1↔Core_2 只有 1 条链路"的拓扑描述 → **会误判链路聚合不存在**。

**根因** `tools/topology.py`
```python
def _extract_link_info(elem) -> dict | None:      # ← 契约就是"返回一条"
    # 真实 .topo 端口信息在 interfacePair 子节点里    ← 注释是对的
    pair = elem.find("interfacePair")               # ← 但用了 find（只取第一个）
```
不是把 `find` 改成 `findall` 就行 —— **整个函数的返回契约错了**，应返回 `list[dict]`。

**同一问题也在浏览器侧** `topo-viewer.js`
```javascript
doc.querySelectorAll("lines > line").forEach((node) => {
  links.push({ srcId: ..., dstId: ... });    // ← 一个 <line> 只产出一条，完全不看 interfacePair
});
```

**修复**：新增 `_extract_link_infos()` 返回列表，逐 pair 产出；JS 侧 `querySelectorAll("interfacePair")`。

---

## D3 🟡 `save_config` 的确认框正则不认 `(y/n)[n]`

**复现**
```python
save_config(device_ip="AR1")     # AR1 是 AR2220，VRP 版本提示符是 (y/n)[n]:
```
→ `保存结果未确认：返回提示符但没有保存成功回显`

**根因** `console.py`
```python
_SAVE_CONFIRM_RE = re.compile(
    r"(?:\[\s*y\s*/\s*n\s*\]|\(\s*y\s*/\s*n\s*\))\s*[:：]?\s*$"
)
#          ↑ 期望 [y/n]        ↑ 或 (y/n)，后面直接是 : 或行尾
```
实际提示符是 `Are you sure to continue? (y/n)[n]:` —— `(y/n)` 后面还有个 `[n]` ✗

**修复**
```python
r"(?:\[\s*y\s*/\s*n\s*\]|\(\s*y\s*/\s*n\s*\))"
r"(?:\s*\[\s*[ynYN]\s*\])?"        # ← 兼容 [n] 默认值
r"\s*[:：]?\s*$"
```

---

## D4 🔴 `_send_line()` 用 `\r\n`，多出的 `\n` 抢先回答确认框（**根因**）

**现象**：修完 D3 后，确认框能认出来了，也发了 `Y`，但设备**直接回提示符、保存仍未执行**。

**实测抓到的字节流**
```
收到: b'\r\n  The current configuration will be written to the device. \r\n
         Are you sure to continue? (y/n)[n]:'
_SAVE_CONFIRM_RE 匹配: True      ← 正则已修好
>>> 发送 'Y'
--- 收到 7 字节 ---
  repr: b'\r\n<AR1>'            ← 直接回提示符，没有 "Now saving..." ✗
```

**根因** `console.py`
```python
self._sock.sendall((text + "\r\n").encode("utf-8"))
#                         ↑↑↑
```
```
① 发 save\r\n
   ├─ \r  → 执行 save，弹出确认框
   └─ \n  → 被确认框当成"直接回车" → 取默认值 n → 保存取消 ✗
② 我们才发 Y → 已经晚了，设备早回到 <AR1>，Y 变成一条空命令 ✗
```

**修复**
```python
self._sock.sendall((text + "\r").encode("utf-8"))   # 只发 CR
```
**验证**：裸 socket 直发 `save\r` + `Y\r` →
```
  It will take several minutes to save configuration file, please wait.......
  Configuration file had been saved successfully
```

> **注**：这与 Heaxu 的 H2 是**同一个根因**，两个项目各栽一次。

---

## D5 🟡 保存成功回显 `Configuration file had been saved successfully` 没在正则里

**现象**：AR 系列保存明明成功，却报"结果未确认"。

**根因** `_SAVE_SUCCESS_RE` 只匹配：
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

## D6 🔴 `ping_from_device` 的 `-t 2` 单位理解错 → 永远报 100% 丢包

**复现**
```python
ping_from_device(device_ip="AR1", target_ip="155.1.12.1")
→ 3 packet(s) transmitted, 0 packet(s) received, 100.00% packet loss
```
但同一台设备手动 ping 完全正常。

**根因** `tools/cli.py`
```python
command = f"ping -c {count} -t 2"
```
**VRP 的 `ping -t` 单位是【毫秒】**，`-t 2` = 2 毫秒超时 → 必然全超时 ✗

**对照实验**
```
ping -c 3 -t 2      155.1.12.1   → 100.00% packet loss  ✗
ping -c 3 -t 2000   155.1.12.1   →   0.00% packet loss  ✓
ping -c 3           155.1.12.1   →   0.00% packet loss  ✓
```

**影响（严重）**：这是**会误导 AI 做出完全错误判断**的 bug ——
AI 用它排查故障时会得出"网络全断"的结论，进而去查 VLAN、查电缆，全是白费。
（我们实测时就被骗了一轮。）

**修复**：`f"ping -c {count} -t 2000"`。

---

## D7 🟢 `auto_discover` 依赖 `tasklist` 找 eNSP PID → 容器/沙箱环境必然失败（仅记录）

**复现**（在 DSH 的沙箱里）
```python
auto_discover()
→ 未检测到 eNSP_Client.exe 进程，请确认 eNSP 已启动且设备已运行。
```
但 `tasklist` 在命令行里**明明能看到** `eNSP_Client.exe 9024`。

**实测对比**
| 调用方式 | 能否看到 eNSP_Client |
|---|---|
| 直接在 shell 里跑 `tasklist` | ✅ |
| 从 Python `subprocess.run(["tasklist"])` | ❌ 列表里 32 个进程，唯独没有 eNSP 系列 |
| `VBoxManage list vms`（读注册表） | ✅ |
| `VBoxManage list runningvms`（需命名管道 IPC） | ❌ |

**根因**：容器/沙箱限制子进程的进程视图与 IPC 能力（非本项目代码问题）。

**修复建议**：不要用 `tasklist` 作为硬门槛。改成**按 `.topo` 里的 `com_port` 逐个 TCP 探测** ——
既能判断设备是否起来，又顺带得到端口↔设备映射，且不受权限限制。
（我们在 `ensp-toolkit` 里就是这么实现的。）

---

# 三、cg689/Simon-Ensp-Mcp-Pro

仓库：https://github.com/cg689/Simon-Ensp-Mcp-Pro

---

## S1 🔴 `_recv_some` 把 `BlockingIOError` 当成致命接收错误 → 客户端直接崩

**复现**
```python
from grbj_ensp_mcp.telnet_client import TelnetClient
c = TelnetClient(host="127.0.0.1", port=2000)
c.connect(); c.activate()
c.send_command("display clock")
```
```
BlockingIOError: [WinError 10035] 无法立即完成一个非阻止性套接字操作。
  → TelnetConnectionError: 接收失败: [WinError 10035]
```

**根因** `telnet_client.py`
```python
def _recv_some(self, max_bytes=65535):
    try:
        chunk = self._sock.recv(max_bytes)
    except TimeoutError:
        return b""                              # ← 只处理超时
    except OSError as exc:
        raise TelnetConnectionError(...)        # ← BlockingIOError 也是 OSError！
```
socket 处于非阻塞模式时，`recv()` 抛的 `BlockingIOError`（`OSError` 子类）本意是
"现在没数据"，却被当成"连接坏了"直接中断。

**修复（一行）**
```python
except (TimeoutError, BlockingIOError):
    return b""
```

---

## S2 🟠 每条命令固定耗 10 秒（提示符识别失效后的降级路径）

**现象**：`ensp_send_command` 每调用一次固定耗时 10 秒；9 台设备下发配置要 20 分钟以上。

**根因** `telnet_client.py`
```python
def send_command(self, command, *, wait_prompt=True, timeout: float = 10.0):
    ...
    try:
        output = self.read_until(_PROMPT_RE, timeout=timeout)   # timeout = 10.0
    except TelnetTimeoutError:
        output = self.read(timeout=0.5)                          # 超时后退化成只读 0.5 秒
```
一旦 `read_until` 认不出提示符 → **每条命令都白等 10 秒**。

**修复建议**：与 H4/D4 同源的问题 —— 先修提示符识别与 `\r\n`，
再检查 `read_until` 的超时预算用法（`self._sock.settimeout(remaining)` 把整个剩余预算交给了阻塞读）。

---

# 四、跨项目共性问题（详见另一份文档）

| 共性问题 | Heaxu | Simon | dawnight0x |
|---|---|---|---|
| eNSP 的假 `encoding="UNICODE"` | ✗ 写错（H1） | — | ✗ 读错（D1） |
| VRP 5.130 `(y/n)[n]` 确认框 | ✗ `\r\n` 抢答（H2） | — | ✗ `\r\n` 抢答（D4） |
| `<line>` 的"一束线"语义 | ✗ 拆散（H15） | — | ✗ 只取首条（D2） |
| 提示符识别 + 超时 | ✗ `\Z` 太严（H4） | ✗ 固定 10 秒（S2） | ✅ |
| 能不能自动开 eNSP / 开设备 | ✗ | ✗ | ✗（D7） |
