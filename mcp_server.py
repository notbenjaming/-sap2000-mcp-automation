#!/usr/bin/env python3
"""SAP2000 CSI Agent — MCP Server

把 modify_model.py 的 cmd_* 函数封装成 MCP 工具，让 Claude Desktop / Cursor /
其他 MCP 客户端可以通过自然语言调用 SAP2000。

工具集（8 个）：
  - init                  : 生成 IR 状态（不启动 SAP2000）
  - sync_from_sap2000     : 从 SAP2000 读真实状态 → 覆盖 IR
  - diff                  : 对比 IR vs SAP2000
  - show_model            : 显示 IR 摘要
  - show_sections         : 显示截面详情
  - show_loads            : 显示荷载详情
  - show_forces           : 显示 SAP2000 当前 Frame Forces + 利用率
  - modify                : 修改 IR（自动冲突检测 + 同步到 SAP2000）
  - solve                 : 求解 + 提取位移/内力/利用率
  - sap2000_status        : 检查 SAP2000 进程状态

运行：
  python mcp_server.py
  # 或 stdio 模式（默认）：用 Claude Desktop 配置

作者：Hermes Agent + 用户
"""

import os
import sys
import json
import logging
from pathlib import Path

# 让 mcp_server.py 能导入同目录的 modify_model
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# 复用 modify_model 的所有 cmd_*
import modify_model
from modify_model import (
    cmd_init, cmd_sync, cmd_diff, cmd_show, cmd_modify, cmd_solve,
    load_ir_state, _find_existing_sap_pid, _STATE_FILE,
)

from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sap2000-mcp")

# ─────────────────────────────────────────────────────────────
# MCP Server 初始化
# ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="sap2000-csi-agent",
    instructions=(
        "SAP2000 结构工程助手。SAP2000 必须由用户手动启动（File → Open 模型）。"
        "工作流：\n"
        "  1) 用户启动 SAP2000 + 打开 .sdb 模型\n"
        "  2) 调用 sync_from_sap2000 把 SAP2000 当前状态拉取到 IR\n"
        "  3) 用 modify 指令（中文 NLP）修改 — 自动检测 IR 与 SAP2000 不一致\n"
        "  4) 用 solve 跑分析，提取位移/内力/利用率\n"
        "  5) 用户在 SAP2000 GUI 中查看详细云图/变形\n"
        "SAP2000 的开/关完全由用户控制。\n"
        "\n"
        "修改指令示例：\n"
        "  改柱1截面为500x600\n"
        "  改梁5荷载为-25\n"
        "  DEAD荷载都乘1.4倍\n"
    ),
)


# ─────────────────────────────────────────────────────────────
# 工具 1: init - 生成 IR 状态
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def init() -> str:
    """生成 SAP2000 模型的 IR 状态（Intermediate Representation）。

    不主动启动/关闭 SAP2000。如果 SAP2000 中已有模型，
    建议直接用 sync_from_sap2000 而不是 init。

    Returns:
        状态消息（包含 IR 文件路径和后续步骤提示）
    """
    return cmd_init()


# ─────────────────────────────────────────────────────────────
# 工具 2: sync_from_sap2000 - 从 SAP2000 读真实状态
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def sync_from_sap2000() -> str:
    """从 SAP2000 读取当前模型状态，覆盖 IR 状态文件。

    SAP2000 是事实来源，IR 是它的快照。
    调用此工具后，IR 会与 SAP2000 完全一致。
    需要 SAP2000 已启动并加载了模型。

    Returns:
        同步摘要（节点/构件/截面/荷载数量）
    """
    return cmd_sync()


# ─────────────────────────────────────────────────────────────
# 工具 3: diff - 对比 IR vs SAP2000
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def diff() -> str:
    """对比 IR 状态与 SAP2000 当前状态，显示所有差异。

    用途：在 modify 之前先检查 IR 是否陈旧（用户可能在 GUI 手动改过）。
    """
    return cmd_diff()


# ─────────────────────────────────────────────────────────────
# 工具 4-7: show_* - 各种显示
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def show_model() -> str:
    """显示 IR 模型摘要（节点/构件/截面/荷载数量）。"""
    return cmd_show("all")


@mcp.tool()
def show_sections() -> str:
    """显示 IR 中所有截面定义（名称、尺寸、材料）。"""
    return cmd_show("截面")


@mcp.tool()
def show_loads() -> str:
    """显示 IR 中所有均布荷载（梁/工况/值）。"""
    return cmd_show("荷载")


@mcp.tool()
def show_nodes() -> str:
    """显示 IR 中所有节点（坐标、约束）。"""
    return cmd_show("节点")


@mcp.tool()
def show_frames() -> str:
    """显示 IR 中所有构件（i_node → j_node、role、截面）。"""
    return cmd_show("构件")


@mcp.tool()
def show_forces() -> str:
    """从 SAP2000 提取当前 Frame Forces（轴力/剪力/弯矩）+ 计算利用率。

    需要 SAP2000 已启动并跑过分析。
    利用率 = M_max / M_u，其中 M_u = 0.2 × f_c × b × h²（C30 混凝土简化公式）。
    """
    return cmd_show("内力")


# ─────────────────────────────────────────────────────────────
# 工具 8: modify - 修改 IR（自动同步到 SAP2000）
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def modify(command: str) -> str:
    """通过中文自然语言修改 SAP2000 模型。

    Args:
        command: 修改指令，例如：
            - "改柱1截面为500x600"
            - "改梁5荷载为-25"
            - "DEAD荷载都乘1.4倍"
            - "改柱1-3截面为600x600"

    工作流：
      1) 解析 NLP 指令
      2) 自动检测 IR 与 SAP2000 是否一致（不一致 → 自动 sync）
      3) 修改 IR
      4) 通过 OAPI 同步到 SAP2000

    Returns:
        修改结果（成功/失败消息）
    """
    return cmd_modify(command)


# ─────────────────────────────────────────────────────────────
# 工具 9: solve - 求解
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def solve() -> str:
    """运行 SAP2000 分析，提取位移/内力/利用率。

    Returns:
        完整求解结果，包括：
          - 各节点位移
          - 各构件内力（轴力/剪力/弯矩）
          - 最大利用率
    """
    return cmd_solve()


# ─────────────────────────────────────────────────────────────
# 工具 10: sap2000_status - 检查 SAP2000 进程
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def sap2000_status() -> str:
    """检查 SAP2000 进程状态（PID、内存、模型文件）。

    Returns:
        状态消息（运行中/未运行/只有 launcher）
    """
    pid, mem = _find_existing_sap_pid()
    state = load_ir_state()

    if pid and mem:
        status = f"✅ SAP2000 运行中 (PID={pid}, {mem:.0f}MB)"
    elif pid:
        status = f"⚠️ 检测到 launcher (PID={pid}, {mem:.0f}MB) — 等待 SAP2000 主进程启动"
    else:
        status = "❌ SAP2000 未运行 — 请用户手动启动 D:\\SAP2000\\SAP2000.exe"

    if state:
        from datetime import datetime
        synced_at = state.get("_synced_at", 0)
        if synced_at:
            synced_str = datetime.fromtimestamp(synced_at).strftime("%H:%M:%S")
        else:
            synced_str = "(未同步)"
        status += f"\n  IR 状态: {len(state.get('nodes', []))} 节点, {len(state.get('frames', []))} 构件, {len(state.get('sections', []))} 截面"
        status += f"\n  IR 同步时间: {synced_str}"
    else:
        status += "\n  IR 状态: 无（请运行 init 或 sync_from_sap2000）"

    return status


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────

def main():
    """启动 MCP server（stdio 模式）"""
    logger.info("=" * 60)
    logger.info("SAP2000 CSI Agent — MCP Server")
    logger.info("=" * 60)
    logger.info(f"项目目录: {PROJECT_ROOT}")
    logger.info(f"IR 状态文件: {_STATE_FILE}")
    logger.info(f"SAP2000 安装: D:\\SAP2000\\SAP2000.exe")
    logger.info("")
    logger.info("已注册工具 (10):")
    logger.info("  1. init                   - 生成 IR 状态")
    logger.info("  2. sync_from_sap2000      - 从 SAP2000 覆盖 IR")
    logger.info("  3. diff                   - 对比 IR vs SAP2000")
    logger.info("  4. show_model             - 显示 IR 摘要")
    logger.info("  5. show_sections          - 显示截面")
    logger.info("  6. show_loads             - 显示荷载")
    logger.info("  7. show_nodes             - 显示节点")
    logger.info("  8. show_frames            - 显示构件")
    logger.info("  9. show_forces            - 显示 SAP2000 Frame Forces + 利用率")
    logger.info(" 10. modify                 - NLP 修改（自动同步）")
    logger.info(" 11. solve                  - 求解 + 提取结果")
    logger.info(" 12. sap2000_status         - 进程状态检查")
    logger.info("")
    logger.info("等待 MCP 客户端连接 (stdio)...")
    logger.info("=" * 60)

    # stdio 模式（默认）
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
