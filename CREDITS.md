# 来源与致谢（Credits）

本仓库**不是原创项目**，而是一个修复版。这里如实记录它的全部来源。
如果你只想知道"这东西是谁做的"，看第一节就够了。

---

## 一、血统

| 层 | 仓库 | 作者 | 时间 | 做了什么 |
|---|---|---|---|---|
| **原始项目** | [Redmig110/eNSP-AI](https://github.com/Redmig110/eNSP-AI) | 黎文杰（[@Redmig110](https://github.com/Redmig110)） | 2026-03 | MCP Server、eNSP 设备 CLI 操作、拓扑查看器的基础实现 |
| **二次开发** | [dawnight0x/ENSP-MCP](https://github.com/dawnight0x/ENSP-MCP) | [@dawnight0x](https://github.com/dawnight0x) | 2026-06 | 在其基础上重构，新增 Web UI、拓扑上下文、运行时日志、Prompt 层 |
| **本仓库** | [KLAX54ix/eNSP](https://github.com/KLAX54ix/eNSP) | [@KLAX54ix](https://github.com/KLAX54ix) | 2026-10 | 修复 6 个实测问题，并补充文档 |

**每一层的贡献都应归属到对应的人。本仓库只负责最后那一行。**

---

## 二、这个血统是怎么确认的

因为上游的提交历史被**压缩**过，所以在 GitHub 上直接看不出来：

```
dawnight0x/ENSP-MCP  的 3 个提交全部是他自己的
                     fork = false
                     贡献者只有他一个人
```

我们是通过**逐字节比对代码**确认它来自 `Redmig110/eNSP-AI` 的：

| 比对项 | 结果 |
|---|---|
| `__init__.py` / `tools/__init__.py` | **逐字节相同** |
| 同名文件 | 11 个，其中 9 个是在原文件上修改 |
| **中文注释** | 上游 76 条、二次开发 47 条 → **其中 39 条逐字相同** |
| README 头两段 | **一字不差** |
| 时间线 | 上游最后推送 2026-03-15 → 二次开发 2026-06-26 才创建 |

> 换句话说：**这段关系在元数据里已经丢了，是代码本身还记着。**

---

## 三、我们把上游的历史接回来了

本仓库的 `main` 分支里有一个合并提交，它的作用只有一个：
**让原作者的署名在 GitHub 上真实可见**，而不是只在 README 里写一句。

```
*   Record upstream history: Redmig110/eNSP-AI
|\
| * b819e51  黎文杰  feat: add topo viewer + simplify MCP server
| * 028eecd  黎文杰  添加使用说明
| * b9e288b  黎文杰  feat: eNSP MCP Server v0.3.0 - AI 管理华为 eNSP 网络设备
* | 421e477  WangXu  Clarify attribution
...
```

实现方式：

```bash
git merge --allow-unrelated-histories -s ours redmig/main
```

`-s ours` 的意思是 **只要他的历史、不要他的文件** ——
所以这个合并**一个字节的代码都没有改动**（`HEAD^{tree}` 与合并前完全相同）。

做完之后，本仓库的贡献者里就有三个人了：

```
KLAX54ix      5 个提交     （6 个修复 + 文档）
dawnight0x    3 个提交     （二次开发）
Redmig110     3 个提交     （原始实现）
```

---

## 四、License 状态（**请务必看清楚**）

**这条链上的上游仓库，都没有声明开源许可证。**

| 仓库 | License |
|---|---|
| `Redmig110/eNSP-AI` | 未声明 |
| `dawnight0x/ENSP-MCP` | 未声明 |
| **本仓库** | **跟随上游，不单方面附加许可证** |

**因此本仓库的立场是：**

- ❌ **我们不对上游代码主张任何著作权** —— 那是别人的作品
- ❌ **我们也不"替你"授予任何权利** —— 原作者没有授予，我们无权转授
- ✅ **我们自己写的部分**（6 个修复、`FIXES.md`、`BUGS.md`、本文档）**欢迎自由取用，不必署名**
- ⚠️ **如果你要将这份代码用于商业用途或再次发布**，请自行确认上游的授权情况，
  必要时请直接联系原作者（[Redmig110](https://github.com/Redmig110) / [dawnight0x](https://github.com/dawnight0x)）

> **为什么这里不随手加一个 MIT License？**
> 因为那会是一句**我们没资格说的话** —— 上游没给，我们不能替他给。

---

## 五、反馈去哪里

| 你的问题 | 提到哪里 |
|---|---|
| 上游功能本身的问题 | 对应的上游仓库（[Redmig110/eNSP-AI](https://github.com/Redmig110/eNSP-AI) / [dawnight0x/ENSP-MCP](https://github.com/dawnight0x/ENSP-MCP)） |
| 这 6 个修复相关 | 本仓库，或上游 PR：https://github.com/dawnight0x/ENSP-MCP/pull/1 |

---

## 六、本仓库自己做了什么

不多，列清楚：

- 修掉 6 个在真实结课实验里实测到的问题（逐条见 [`FIXES.md`](FIXES.md)）
- 整理了一份跨项目的问题清单（[`BUGS.md`](BUGS.md)）
- 把上游的提交历史接回来，补全署名
- 本文档

**没有改动的部分，功劳属于上面的作者。**
