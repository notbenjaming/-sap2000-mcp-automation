# MCP 客户端使用示例

## 1. 启动 MCP Server

在 Hermes CSI 项目目录启动 stdio server：

```bash
cd C:\Users\21574\hermes_csi_system
python mcp_server.py
```

Server 会等待 JSON-RPC over stdio 连接。

---

## 2. Claude Desktop 配置

编辑 Claude Desktop 配置文件（`%APPDATA%\Claude\claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "sap2000-csi-agent": {
      "command": "python",
      "args": ["C:\\Users\\21574\\hermes_csi_system\\mcp_server.py"],
      "env": {
        "PYTHONPATH": "C:\\Users\\21574\\hermes_csi_system"
      }
    }
  }
}
```

重启 Claude Desktop。会在工具列表中看到 12 个工具。

---

## 3. Cursor 配置

编辑 `~/.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "sap2000-csi-agent": {
      "command": "python",
      "args": ["C:\\Users\\21574\\hermes_csi_system\\mcp_server.py"]
    }
  }
}
```

---

## 4. 客户端测试脚本（手写 stdio JSON-RPC）

`test_mcp_client.py` — 用 Python 模拟客户端，发送 JSON-RPC 消息：

```python
import subprocess
import json
import sys

# 启动 mcp_server.py
proc = subprocess.Popen(
    [sys.executable, "mcp_server.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)

def send(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()

def recv():
    line = proc.stdout.readline()
    return json.loads(line) if line else None

# 1) initialize
send({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"}
    }
})
print("init:", recv())

# 2) initialized notification
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

# 3) list tools
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
print("tools:", recv())

# 4) call sap2000_status
send({
    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
    "params": {"name": "sap2000_status", "arguments": {}}
})
print("status:", recv())

proc.terminate()
```

---

## 5. 典型 Agent 工作流

```
用户（中文）               →  Agent（LLM）                →  MCP 工具调用
─────────────────────────────────────────────────────────────────
"看下模型现在什么样"       →  sap2000_status()           →  show_model()
"把柱1改成500x600"         →  modify("改柱1截面为500x600") →  1. diff() 检查
                                                          2. 自动 sync
                                                          3. 修改 SAP2000
"跑一下分析"              →  solve()                    →  Lock/Unlock
                                                          →  File.Save
                                                          →  RunAnalysis
                                                          →  提取 96 字段 Joint Displacements
                                                          →  提取 12 字段 Element Forces
                                                          →  计算利用率
                                                          →  返回完整结果
```

---

## 6. 工具完整列表

| 工具 | 用途 | 何时调用 |
|------|------|----------|
| `sap2000_status` | 检查 SAP2000 进程 | 任何时候（轻量） |
| `init` | 生成 IR 状态 | 首次使用或新建项目 |
| `sync_from_sap2000` | 从 SAP2000 覆盖 IR | 用户在 SAP2000 GUI 改完后 |
| `diff` | 对比 IR vs SAP2000 | modify 之前（modify 也会自动做） |
| `show_model` | 显示 IR 摘要 | 用户问"现在什么状态" |
| `show_sections` | 显示截面 | 用户问"截面有哪些" |
| `show_loads` | 显示荷载 | 用户问"荷载多少" |
| `show_nodes` | 显示节点 | 用户问"节点坐标" |
| `show_frames` | 显示构件 | 用户问"哪些梁哪些柱" |
| `show_forces` | 显示 SAP2000 当前内力 + 利用率 | 用户问"内力多大" |
| `modify` | NLP 修改 | 用户给指令"改..." |
| `solve` | 求解 + 提取 | 用户说"分析"/"求解" |

---

## 7. 重要约束

- **SAP2000 进程由用户手动管理**：开（双击 SAP2000.exe + 加载 .sdb）和关（File → Exit）
- **Python 不主动启动/关闭 SAP2000**（已改）
- **IR 是软缓存**：以 SAP2000 真实状态为准
- **大模型加载需 1-2 分钟**：Helper 启动后内存从 40MB 涨到 500MB+
