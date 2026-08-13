# SAP2000 CSI Agent — 使用手册

> 通过自然语言（中文）修改 SAP2000 模型，自动分析，提取结果
> 基于 **MCP（Model Context Protocol）** 协议，支持 **Hermes / Claude Desktop / Cursor** 等所有 MCP 客户端

---

## 📑 目录

1. [快速开始](#1-快速开始-3-步接入)
2. [架构原理](#2-架构原理)
3. [工作流](#3-工作流)
4. [MCP 工具列表](#4-mcp-工具列表-12-个)
5. [自然语言指令](#5-自然语言指令)
6. [CLI 命令行用法](#6-cli-命令行用法)
7. [实际项目使用流程](#7-实际项目使用流程)
8. [故障排查](#8-故障排查)
9. [性能优化建议](#9-性能优化建议)
10. [开发与扩展](#10-开发与扩展)

---

## 1. 快速开始（3 步接入）

### 前置条件

- ✅ **Windows 10/11**
- ✅ **SAP2000 v24** 已安装（默认 `D:\SAP2000\SAP2000.exe`）
- ✅ **Python 3.11+** 已安装
- ✅ **依赖包**（一次性安装）：
  ```bash
  pip install mcp comtypes psutil
  ```
- ✅ **Hermes Agent** 已安装（或 Claude Desktop / Cursor 等其他 MCP 客户端）

### 第 1 步：启动 SAP2000

1. 双击 `D:\SAP2000\SAP2000.exe`
2. **File → Open** 加载你的模型（`.sdb` 文件）
3. 等待内存稳定到 **~500MB**（这可能需要 1-2 分钟）
4. 让 SAP2000 窗口保持打开

### 第 2 步：注册 MCP Server

```bash
hermes mcp add sap2000-csi \
  --command python \
  --args "C:\Users\21574\hermes_csi_system\mcp_server.py"
```

输出示例：
```
✓ Connected! Found 12 tool(s) from 'sap2000-csi':
  init, sync_from_sap2000, diff, show_model, show_sections, ...
✓ Saved 'sap2000-csi' to ~/AppData/Local/hermes/profiles/coder/config.yaml
  (12/12 tools enabled)
```

### 第 3 步：开始对话

```bash
hermes chat -q "用 sap2000_status 检查 SAP2000 状态" -m minimax/MiniMax-M3
```

或交互模式（在 Windows cmd / PowerShell 中运行）：
```bash
hermes chat -m minimax/MiniMax-M3
> 用 sap2000_status 检查 SAP2000 状态
> 改柱1截面为 500x600
> solve
> 退出
```

---

## 2. 架构原理

```
┌─────────────────────────────────────────────────────────┐
│  用户（自然语言）                                          │
└──────────────────────┬──────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│  LLM 客户端 (Hermes / Claude / Cursor)                    │
│  解析用户意图 → 选择 MCP 工具 → 生成调用参数                │
└──────────────────────┬──────────────────────────────────┘
                       ↓ JSON-RPC over stdio
┌─────────────────────────────────────────────────────────┐
│  MCP Server (mcp_server.py)                              │
│  12 个工具：每个工具映射到 modify_model.py 的 cmd_* 函数   │
└──────────────────────┬──────────────────────────────────┘
                       ↓ Python COM
┌─────────────────────────────────────────────────────────┐
│  SAP2000 (OAPI 1.026)                                     │
│  - IR 状态 (C:\Users\...\Temp\sap2000_ir_state.json)       │
│  - 模型 (.sdb)                                            │
└─────────────────────────────────────────────────────────┘
```

**关键设计**：
- **SAP2000 是事实来源**，IR 是它的软缓存
- **用户**手动开/关 SAP2000 GUI
- **Python** 只通过 OAPI 读写（不主动启动/关闭）
- **modify** 指令自动检测 IR 与 SAP2000 是否一致，不一致则自动 sync

---

## 3. 工作流

### 第一次使用（新项目）

```
1. 用户启动 SAP2000 + 加载 .sdb
2. Hermes 调用 sync_from_sap2000
   → IR 状态文件 = SAP2000 当前模型
3. 用户给指令："改柱1截面为 500x600"
   → Agent 解析 NLP → modify 工具
   → 系统检查 IR vs SAP2000（一致）
   → 修改 IR → OAPI 改 SAP2000
4. 用户："solve"
   → Lock/Unlock 触发 SAP2000 刷新
   → File.Save
   → RunAnalysis
   → 提取 96 字段 Joint Displacements
   → 提取 12 字段 Element Forces
   → 计算利用率
5. 用户在 SAP2000 GUI 中查看云图/变形（手动）
```

### 日常使用

```
用户："把 DEAD 荷载都乘 1.4 倍"
→ modify("DEAD荷载都乘1.4倍")
→ 自动 scale → 同步

用户："跑分析"
→ solve
→ 输出位移/内力/利用率
```

### 用户在 GUI 改完后

```
用户在 SAP2000 GUI 中手动改了某根梁的截面
Agent: 用户调用 sync_from_sap2000
→ IR 被 SAP2000 真实状态覆盖
→ GUI 的修改安全保留
```

---

## 4. MCP 工具列表（12 个）

| 工具 | 输入 | 输出 | 用途 |
|------|------|------|------|
| `init` | (无) | 状态消息 | 生成初始 IR 状态（不启动 SAP2000） |
| `sync_from_sap2000` | (无) | 同步摘要 | 从 SAP2000 读真实状态，覆盖 IR |
| `diff` | (无) | 差异列表 | 对比 IR vs SAP2000 |
| `show_model` | (无) | IR 摘要 | 节点/构件/截面/荷载数量 |
| `show_sections` | (无) | 截面列表 | 截面名称、尺寸、材料 |
| `show_loads` | (无) | 荷载列表 | 梁/工况/值 |
| `show_nodes` | (无) | 节点列表 | 坐标、约束 |
| `show_frames` | (无) | 构件列表 | i→j, role, 截面 |
| `show_forces` | (无) | 内力 + 利用率 | 实时从 SAP2000 提取（需先 solve） |
| `modify` | `command: str` | 修改结果 | NLP 中文修改 |
| `solve` | (无) | 完整结果 | 分析 + 提取位移/内力/利用率 |
| `sap2000_status` | (无) | 状态消息 | SAP2000 进程 + IR 状态 |

---

## 5. 自然语言指令

### 5.1 改截面

```
改柱1截面为500x600          ← 改柱 1 为 500mm×600mm
改柱3截面为800x800
改梁5截面为400x500
```

**支持变体**：
```
"修改柱1截面为500x600"
"把柱1改成500x600"
"柱1改500x600"
```

### 5.2 改荷载

```
改梁5荷载为-25              ← 梁 5 的 DEAD 改为 -25 kN/m
改梁6LIVE为-10              ← 梁 6 的 LIVE 改为 -10
改梁5DEAD为-30
```

### 5.3 批量缩放

```
DEAD荷载都乘1.4倍            ← 所有 DEAD 荷载乘 1.4
LIVE荷载都乘0.8倍
所有荷载都乘1.2倍
```

### 5.4 删除节点

```
删节点1                     ← 暂未实现（预留）
```

### 5.5 求解

```
solve / 求解 / 算一下 / 运行 / 执行
```

### 5.6 显示

```
show          ← 摘要
show 内力     ← SAP2000 Frame Forces + 利用率
show 截面     ← 所有截面
show 荷载     ← 所有荷载
show 节点     ← 所有节点
show 构件     ← 所有构件
```

---

## 6. CLI 命令行用法

```bash
cd C:\Users\21574\hermes_csi_system

python modify_model.py init              # 生成 IR（不启动 SAP2000）
python modify_model.py sync              # 从 SAP2000 拉取状态
python modify_model.py diff              # 对比 IR vs SAP2000

python modify_model.py show              # IR 摘要
python modify_model.py show 内力         # SAP2000 Frame Forces + 利用率
python modify_model.py show 截面
python modify_model.py show 荷载
python modify_model.py show 节点
python modify_model.py show 构件

python modify_model.py "改柱1截面为500x600"
python modify_model.py "改梁5荷载为-25"
python modify_model.py "DEAD荷载都乘1.4倍"

python modify_model.py solve             # 求解 + 提取

python modify_model.py exit              # 提示用户在 GUI 关闭
```

---

## 7. 实际项目使用流程

### 场景 1：日常修改

```bash
# 1. 启动 SAP2000 GUI + 打开项目 .sdb
# 2. 在 Hermes 聊天中：
> 同步一下 SAP2000 状态
（Agent 调用 sync_from_sap2000）

> 改柱1截面为 500x600
（Agent 调用 modify，自动 sync + 修改）

> 改梁5荷载为 -30
（Agent 调用 modify）

> 跑分析
（Agent 调用 solve，返回位移/内力/利用率）

# 3. 在 SAP2000 GUI 中查看云图/变形
```

### 场景 2：参数化研究

```bash
# 1. 同步初始状态
> 同步一下
（sync_from_sap2000）

# 2. 第一轮：DEAD × 1.0
> DEAD 荷载都乘 1.0 倍
（modify）

> solve，告诉我最大利用率
（solve + show_forces）

# 3. 第二轮：DEAD × 1.4
> DEAD 荷载都乘 1.4 倍
（modify）

> solve，告诉我最大利用率
（solve + show_forces）

# 4. 第三轮：DEAD × 1.5 + 改柱 800x800
> DEAD 荷载都乘 1.5 倍，改柱1-4截面为 800x800
（modify 多次 + solve + 汇总）
```

### 场景 3：用户在 GUI 中手动修改

```bash
# 1. 用户在 SAP2000 中手动改某根梁的截面
# 2. 在 Hermes 聊天中：
> 同步一下 SAP2000 状态
（sync_from_sap2000 覆盖 IR）

# 3. 继续做 NLP 修改
> 改梁6荷载为 -25
（modify 使用最新 IR）
```

---

## 8. 故障排查

### 8.1 "SAP2000 未运行"

**原因**：`_find_existing_sap_pid` 没找到进程（>80MB 内存）

**解决**：
1. 检查 SAP2000 是否启动：`tasklist | findstr SAP2000`
2. 检查内存：如果是 100-200MB（idle），调阈值 `existing_mem > 80`（已默认）
3. **手动启动** `D:\SAP2000\SAP2000.exe` + 加载 .sdb

### 8.2 "无法从 SAP2000 提取 Frame Forces"

**原因**：v24 OAPI 行为不稳定，需要重试

**解决**：
- `show_forces` / `solve` 已内置 15 次重试
- 等待 3-5 秒（`Lock/Unlock` 触发 SAP2000 内部刷新）
- 如果仍失败：关闭 SAP2000，重启 + 重新加载

### 8.3 截面没有真的变化

**可能原因**：
- IR 状态与 SAP2000 真实状态不一致
- 模型被锁定（`SetModelIsLocked(True)`）

**解决**：
1. 调用 `diff` 检查
2. 如不一致：调 `sync_from_sap2000`
3. 如模型锁定：调 `sync`（内部会解锁）

### 8.4 MCP Server 启动失败

```
ImportError: No module named 'mcp'
```

**解决**：
```bash
pip install mcp
```

### 8.5 error No.91

**SAP2000 v24 的非致命警告**（"error cleaning hinge properties arrays"）

**影响**：可能不显示，但不影响模型

**解决**：忽略

### 8.6 SAP2000 启动时 Helper 卡住

**症状**：Helper.CreateObjectProgID 只创建 launcher (~40MB)，不启动真 SAP2000

**原因**：这台机器的 Helper COM 限制

**解决**：
1. 杀 launcher：`psutil.Process(pid).kill()`
2. **手动启动** `D:\SAP2000\SAP2000.exe`（你已经在做）

---

## 9. 性能优化建议

### 9.1 减少重复 sync

`modify` 指令会自动 sync，但如果你做很多次连续修改，可以手动 sync 一次：
```
> 同步一下，然后改柱1截面为500x600，然后改柱2截面为500x600
```

### 9.2 减少重试次数

如果你觉得 solve 慢，可以修改 `attempts=15` → `attempts=5`（modify_model.py 顶部）

### 9.3 只在需要时 show_forces

`show_forces` 会重新 RunAnalysis（耗时 5-10 秒）。如果已经 solve 过，可以直接用 IR 缓存（暂未实现）

---

## 10. 开发与扩展

### 10.1 项目结构

```
C:\Users\21574\hermes_csi_system\
├── mcp_server.py            # MCP 服务（12 工具）
├── modify_model.py          # CLI 入口（被 MCP 复用）
├── ir_nlp.py                # 中文 NLP 解析
├── ir_compiler.py           # IR 模型定义
├── ir_diff.py               # 差异计算
├── sap2000_worker.py        # SAP2000 COM 集成
├── ir_interactive.py        # 交互 REPL
├── test_mcp_client.py       # stdio 验证脚本
├── README_MCP.md            # MCP 配置说明
├── USAGE.md                 # ← 本文档
├── ir_state.json (动态)     # IR 状态文件
```

### 10.2 添加新 NLP 指令

编辑 `ir_nlp.py`：

```python
class CommandType(str, Enum):
    SET_SECTION = "set_section"
    SET_LOAD = "set_load"
    SCALE_LOAD = "scale_load"
    REMOVE_NODE = "remove_node"
    SOLVE = "solve"
    SHOW = "show"
    # 添加新指令
    SET_PROPERTY = "set_property"
```

并在 `IRCommandParser` 中加 `_handle_set_property` 方法。

### 10.3 添加新 MCP 工具

编辑 `mcp_server.py`：

```python
@mcp.tool()
def my_new_tool(arg1: str, arg2: int = 0) -> str:
    """工具描述（LLM 会看到）"""
    return cmd_my_new_function(arg1, arg2)
```

并实现 `cmd_my_new_function` 在 `modify_model.py` 中。

### 10.4 接入其他 MCP 客户端

#### Claude Desktop

编辑 `%APPDATA%\Claude\claude_desktop_config.json`：
```json
{
  "mcpServers": {
    "sap2000-csi": {
      "command": "python",
      "args": ["C:\\Users\\21574\\hermes_csi_system\\mcp_server.py"]
    }
  }
}
```

#### Cursor

编辑 `~/.cursor/mcp.json`：
```json
{
  "mcpServers": {
    "sap2000-csi": {
      "command": "python",
      "args": ["C:\\Users\\21574\\hermes_csi_system\\mcp_server.py"]
    }
  }
}
```

---

## 附录 A：完整对话示例

```
用户：同步一下 SAP2000 状态
Hermes：[调用 sap2000_status]
       SAP2000 运行中 (PID=20720, 560MB)
       IR 状态: 8 节点, 7 构件, 2 截面
       IR 同步时间: 10:32:22

用户：把柱 1 改成 500x600
Hermes：[调用 modify("改柱1截面为500x600")]
       ✅ 柱 1 截面改为 500x600 mm，模型已更新
       更新统计: {'frames': 1, 'sections': 1}
       💡 SAP2000 保持运行

用户：DEAD 荷载都乘 1.4 倍
Hermes：[调用 modify("DEAD荷载都乘1.4倍")]
       ✅ DEAD 荷载 × 1.4 已应用
       - 梁 5 DEAD: -15 → -21
       - 梁 6 DEAD: -15 → -21
       - 梁 7 DEAD: -15 → -21
       更新统计: {'dist_loads': 3}

用户：跑分析
Hermes：[调用 solve]
       ✅ 求解完成
       最大位移: 23.18 mm
       
       构件内力 (DEAD 工况):
         框架 1: P=-100.62kN, M3=32.90kN·m, 利用率 17.9%
         框架 5: P=-9.39kN, M3=-107.32kN·m, 利用率 34.7%
         ...
       最大利用率: 框架 5 = 34.7%
       截面 BEAM300x600 (300x600 mm)  M_u=308.9 kN·m
       M_max=107.3 kN·m

用户：在 SAP2000 GUI 中看 Display → Show Deformed Shape 确认
       Display → Show Tables → Frame Forces 验证
```

---

## 附录 B：常用命令速查

| 任务 | 指令 |
|------|------|
| 改柱截面 | `改柱1截面为500x600` |
| 改梁截面 | `改梁5截面为400x500` |
| 改单梁荷载 | `改梁5荷载为-25` |
| 改某工况荷载 | `改梁5DEAD为-30` |
| 批量缩放 | `DEAD荷载都乘1.4倍` |
| 求解 | `solve` / `求解` |
| 看模型 | `show` / `show_model` |
| 看内力 | `show_forces` / `show 内力` |
| 同步 SAP2000 | `sync_from_sap2000` / `同步` |
| 对比 | `diff` / `对比` |
| 状态检查 | `sap2000_status` |

---

## 附录 C：项目交付清单

| 批次 | 内容 | 状态 |
|------|------|------|
| Batch 1-4 | ir_diff, ir_nlp, ir_compiler, sap2000_worker, ir_interactive | ✅ |
| Batch 5 | solve 工作流（DatabaseTables API 提取结果） | ✅ |
| Batch 6 | sync/diff/conflict-detection | ✅ |
| Batch 7 | Frame Forces + 利用率 | ✅ |
| Batch 8 | MCP 封装（12 工具） | ✅ |
| Batch 8.5 | 修复内存阈值 200→80MB | ✅ |

**核心文件**：
- `mcp_server.py` - MCP server（250 行）
- `modify_model.py` - CLI + 业务逻辑（1100+ 行）
- `sap2000_worker.py` - SAP2000 COM 封装（1646 行）
- `ir_nlp.py` - 中文 NLP
- `ir_diff.py` - 差异计算
- `ir_compiler.py` - IR 数据模型

**测试**：
- `test_mcp_client.py` - stdio 验证
- `README_MCP.md` - MCP 配置文档
- `USAGE.md` - 本使用手册

---

## 📞 常见问题

**Q: SAP2000 必须一直开着吗？**
A: 是的。MCP 工具通过 OAPI 连接到 SAP2000 进程。**开/关由你控制**。

**Q: IR 状态文件在哪？**
A: `C:\Users\21574\AppData\Local\Temp\sap2000_ir_state.json`

**Q: 我能在多台电脑上用同一个模型吗？**
A: 可以。复制 .sdb 文件 + 在另一台电脑运行相同配置即可。

**Q: SAP2000 用什么单位？**
A: SAP2000 用 inch/feet，但 OAPI 自动返回米。结果提取时已转换。

**Q: 修改会立刻生效吗？**
A: 是的。`modify` 完成后立刻在 SAP2000 GUI 中可见（可能需要 F5 刷新视图）。

**Q: error No.91 是什么？**
A: SAP2000 v24 的非致命警告（清理铰接属性）。忽略即可。

---

**版本**：1.0
**最后更新**：2026-07-02
**作者**：Hermes Agent + 用户
