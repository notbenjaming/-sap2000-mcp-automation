# SAP2000 MCP Automation

SAP2000 v24 自动化建模与求解工具集，通过 COM/OAPI 与 SAP2000 GUI 交互，并通过 MCP (Model Context Protocol) 暴露给 AI agent。

## 项目结构

```
.
├── 核心引擎
│   ├── sap2000_worker.py        # SAP2000 COM 封装（连接 / 建模 / 求解 / 提取结果）
│   ├── solver_router.py         # 多求解器路由（SAP2000 / 未来 ETABS）
│   ├── ir_compiler.py           # IR (Intermediate Representation) → SAP2000 API
│   ├── ir_interactive.py        # IR 交互式会话管理
│   ├── ir_nlp.py                # IR 自然语言修改接口
│   ├── ir_diff.py               # IR 状态差异对比
│   └── modify_model.py          # 模型修改主入口
├── MCP
│   ├── mcp_server.py            # MCP 协议服务端（stdio）
│   ├── mcp_gateway.py           # MCP HTTP 网关
│   ├── mcp_client_demo.py       # MCP 客户端示例
│   └── test_mcp_client.py       # MCP 客户端测试
├── 智能调度
│   ├── failure_aware_supervisor.py  # 失败感知监督器
│   ├── optimization_loop.py         # 自动优化循环
│   └── knowledge_store.py           # 历史案例知识库
├── 集成示例
│   ├── build_9m_frame.py        # 9m 跨度框架构造示例
│   ├── build_frame_9m.py        # 备用框架构造
│   ├── solve_frame_9m.py        # 9m 框架求解脚本
│   ├── debug_addcartesian.py    # AddCartesian 调试
│   ├── list_sap_processes.py    # 列出 SAP2000 进程
│   └── integration_with_sap2000.py
├── 单元测试
│   ├── test_sap_basic.py
│   ├── test_sap_oapi.py
│   ├── test_sap_v3.py
│   ├── test_real_sap2000.py
│   └── test_incremental_update.py
└── 文档
    ├── USAGE.md                 # 使用文档
    └── README_MCP.md            # MCP 协议说明
```

## 前置条件

- **操作系统**: Windows 10 / 11
- **SAP2000**: v24（已安装并能正常启动 GUI）
- **Python**: 3.11+
- **依赖**: `pywin32` (COM 接口)
- **.NET**: 8.0 Desktop Runtime（SAP2000 OAPI 要求）

## 安装

```powershell
git clone https://github.com/notbenjaming/-sap2000-mcp-automation.git
cd -sap2000-mcp-automation
pip install pywin32
```

## 快速开始

### 1. 直接调用 SAP2000

```powershell
python solve_frame_9m.py
```

### 2. 启动 MCP 服务（stdio）

```powershell
python mcp_server.py
```

### 3. 通过 MCP 客户端调用

见 `mcp_client_demo.py` 示例。

## 设计要点

- **IR (Intermediate Representation)**: 解耦自然语言意图与 SAP2000 API，AI agent 修改 IR，再由 IR compiler 推送到 SAP2000
- **Worker 模式**: `sap2000_worker.py` 是 SAP2000 COM 的唯一封装层，所有 OAPI 调用集中于此
- **失败感知**: `failure_aware_supervisor.py` 监控求解过程，自动决策下一步（修改截面 / 加密网格 / 警告）
- **MCP 协议**: 通过 `mcp_server.py` 把 SAP2000 能力暴露给任意 MCP 兼容 AI 客户端

## 已验证的 SAP2000 v24 OAPI 陷阱

1. `Results.*` 接口返回 (1, 0, None) — 用 `DatabaseTables.GetTableForDisplayCSVString` 取，table 名要加 "Output - " 前缀
2. `RunAnalysis()` 在模型为 "(Untitled)" 时静默 no-op — 必须先 `File.Save`
3. `File.OpenFile` 错误路径返回 1 但会静默清空模型 — 调用前必须校验
4. 模型在 `RunAnalysis` 后自动锁 — 编辑前必须解锁
5. `SetLoadDistributed` 只支持 8 参数签名

## License

MIT
