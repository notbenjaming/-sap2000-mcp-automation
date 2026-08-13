"""
MCP Client Demo - 客户端调用示例
=================================
演示如何通过 HTTP 调用 MCP Gateway 的 5 个工具。

支持 3 种调用方式：
1. JSON-RPC 2.0 协议（标准 MCP 格式）
2. REST 直接调用（便捷）
3. Python SDK 风格封装

运行方法：
1. 先启动 Gateway: python mcp_gateway.py
2. 再运行客户端: python mcp_client_demo.py

作者：MiniMax-M3 / Hermes CSI System
"""

from __future__ import annotations

import json
import time
import asyncio
from typing import Dict, Any, Optional
from pathlib import Path

import httpx

# ============================================================================
# 配置
# ============================================================================

GATEWAY_URL = "http://127.0.0.1:8888"
TIMEOUT = 30.0


# ============================================================================
# 示例 IR（用于演示）
# ============================================================================

SAMPLE_IR = {
    "model_id": "demo_frame_2story_3span",
    "name": "两层三跨混凝土框架（演示）",
    "units": "kN_m_C",
    "nodes": [
        {"id": 1, "x": 0, "y": 0, "z": 0, "restrain": [True, True, True, False, False, False]},
        {"id": 2, "x": 5, "y": 0, "z": 0},
        {"id": 3, "x": 10, "y": 0, "z": 0},
        {"id": 4, "x": 15, "y": 0, "z": 0},
        {"id": 5, "x": 0, "y": 0, "z": 3.5, "restrain": [True, True, True, False, False, False]},
        {"id": 6, "x": 5, "y": 0, "z": 3.5},
        {"id": 7, "x": 10, "y": 0, "z": 3.5},
        {"id": 8, "x": 15, "y": 0, "z": 3.5, "restrain": [True, True, True, False, False, False]},
    ],
    "sections": [
        {"name": "COL400x400", "type": "concrete_rect", "rect_h": 400, "rect_b": 400, "material": "C30"},
        {"name": "BEAM300x600", "type": "concrete_rect", "rect_h": 600, "rect_b": 300, "material": "C30"},
    ],
    "frames": [
        {"id": 1, "i": 1, "j": 5, "section": "COL400x400", "role": "column"},
        {"id": 2, "i": 2, "j": 6, "section": "COL400x400", "role": "column"},
        {"id": 3, "i": 3, "j": 7, "section": "COL400x400", "role": "column"},
        {"id": 4, "i": 4, "j": 8, "section": "COL400x400", "role": "column"},
        {"id": 5, "i": 5, "j": 6, "section": "BEAM300x600", "role": "beam"},
        {"id": 6, "i": 6, "j": 7, "section": "BEAM300x600", "role": "beam"},
        {"id": 7, "i": 7, "j": 8, "section": "BEAM300x600", "role": "beam"},
    ],
    "dist_loads": [
        {"frame_id": 5, "case": "DEAD", "value": -15.0},
        {"frame_id": 6, "case": "DEAD", "value": -15.0},
        {"frame_id": 7, "case": "DEAD", "value": -15.0},
        {"frame_id": 5, "case": "LIVE", "value": -8.0},
        {"frame_id": 6, "case": "LIVE", "value": -8.0},
        {"frame_id": 7, "case": "LIVE", "value": -8.0},
    ],
    "load_cases": [
        {"name": "DEAD", "type": "dead", "self_weight": True},
        {"name": "LIVE", "type": "live"},
    ],
    "analysis": {
        "type": "linear_static",
        "target_solver": "sap2000",
        "design_code": "GB50011",
    },
}


# ============================================================================
# MCP Client（3 种调用方式）
# ============================================================================

class MCPClient:
    """MCP 客户端封装"""

    def __init__(self, base_url: str = GATEWAY_URL):
        self.base_url = base_url.rstrip("/")

    async def call_jsonrpc(self, method: str, params: Optional[Dict] = None,
                           req_id: Optional[Any] = None) -> Dict[str, Any]:
        """方式 1: JSON-RPC 2.0 协议（标准 MCP 格式）"""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": req_id if req_id is not None else int(time.time() * 1000),
        }

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{self.base_url}/mcp", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def call_rest(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """方式 2: REST 直接调用（便捷）"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{self.base_url}/tools/{tool_name}/call",
                json=arguments,
            )
            resp.raise_for_status()
            return resp.json()

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """方式 3: 智能调用（自动选择 JSON-RPC）"""
        payload = {
            "name": tool_name,
            "arguments": arguments,
        }
        return await self.call_jsonrpc("tools/call", payload)

    async def list_tools(self) -> Dict[str, Any]:
        """列出工具"""
        resp = await self.call_jsonrpc("tools/list")
        return resp.get("result", resp)

    async def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(f"{self.base_url}/health")
            resp.raise_for_status()
            return resp.json()

    async def gateway_info(self) -> Dict[str, Any]:
        """Gateway 信息"""
        resp = await self.call_jsonrpc("gateway/info")
        # 兼容嵌套：返回的是 JSON-RPC 响应包
        return resp.get("result", resp)

    # ---------------------- 5 个核心工具的便捷封装 ----------------------

    async def compile_ir(self, ir: Dict[str, Any]) -> Dict[str, Any]:
        return await self.call_tool("csi.compile_ir", {"ir": ir})

    async def route_solver(self, ir: Optional[Dict] = None,
                           override: Optional[str] = None) -> Dict[str, Any]:
        args = {}
        if ir is not None:
            args["ir"] = ir
        if override is not None:
            args["override"] = override
        return await self.call_tool("csi.route_solver", args)

    async def optimize_model(self, ir: Dict[str, Any],
                             max_iterations: int = 10,
                             target_utilization: float = 0.85) -> Dict[str, Any]:
        return await self.call_tool("csi.optimize_model", {
            "ir": ir,
            "max_iterations": max_iterations,
            "target_utilization": target_utilization,
        })

    async def save_case(self, ir: Dict[str, Any],
                        optimization_result: Dict[str, Any],
                        tags: Optional[list] = None) -> Dict[str, Any]:
        args = {"ir": ir, "optimization_result": optimization_result}
        if tags:
            args["tags"] = tags
        return await self.call_tool("csi.save_case", args)

    async def retrieve_cases(self, query_ir: Optional[Dict] = None,
                             query_features: Optional[Dict] = None,
                             top_k: int = 5,
                             min_similarity: float = 0.0) -> Dict[str, Any]:
        args = {"top_k": top_k, "min_similarity": min_similarity}
        if query_ir:
            args["query_ir"] = query_ir
        if query_features:
            args["query_features"] = query_features
        return await self.call_tool("csi.retrieve_cases", args)


# ============================================================================
# 演示主流程
# ============================================================================

async def demo_full_pipeline():
    """演示完整工作流：编译 → 路由 → 优化 → 保存 → 检索"""
    print("=" * 70)
    print("MCP Gateway Client Demo - 完整工作流演示")
    print("=" * 70)

    client = MCPClient()

    # 0. 健康检查
    print("\n[0] 健康检查...")
    health = await client.health_check()
    print(f"    状态: {health['status']}")
    print(f"    工具数: {health['tools_count']}")

    # 0.5 Gateway 信息
    print("\n[0.5] Gateway 信息...")
    info = await client.gateway_info()
    print(f"    名称: {info['name']}")
    print(f"    版本: {info['version']}")
    print(f"    注册工具: {info['tools']}")

    # 1. 列出工具
    print("\n[1] 列出可用工具...")
    tools = await client.list_tools()
    print(f"    共 {len(tools['tools'])} 个工具:")
    for t in tools['tools']:
        print(f"      - {t['name']}: {t['description'][:40]}")

    # 2. 编译 IR
    print("\n[2] 调用 csi.compile_ir...")
    compile_result = await client.compile_ir(SAMPLE_IR)
    if "error" in compile_result:
        print(f"    ❌ 错误: {compile_result['error']}")
        return
    inner = compile_result["result"]["result"]
    print(f"    ✅ 编译成功: {inner['step_count']} 步")
    print(f"    目标 solver: {inner['target_solver']}")

    # 3. 路由 Solver
    print("\n[3] 调用 csi.route_solver...")
    route_result = await client.route_solver(SAMPLE_IR)
    if "error" in route_result:
        print(f"    ❌ 错误: {route_result['error']}")
        return
    inner = route_result["result"]["result"]
    print(f"    ✅ 路由结果: {inner['target_solver']}")
    print(f"    置信度: {inner['confidence']}")
    print(f"    命中规则: {inner['matched_rules']}")
    print(f"    理由: {inner['rationale'][0][:50]}...")

    # 4. 优化模型
    print("\n[4] 调用 csi.optimize_model...")
    opt_result = await client.optimize_model(SAMPLE_IR, max_iterations=5)
    if "error" in opt_result:
        print(f"    ❌ 错误: {opt_result['error']}")
        return
    inner = opt_result["result"]["result"]
    print(f"    ✅ 优化完成: {inner['iterations_run']} 次迭代")
    print(f"    停止原因: {inner['stop_reason']}")
    print(f"    改进: {inner['improvement_pct']}%")
    print(f"    最终得分: {inner['final_score']}")

    # 5. 保存案例
    print("\n[5] 调用 csi.save_case...")
    save_result = await client.save_case(
        SAMPLE_IR,
        opt_result["result"]["result"],
        tags=["demo", "client_test"],
    )
    if "error" in save_result:
        print(f"    ❌ 错误: {save_result['error']}")
        return
    inner = save_result["result"]["result"]
    print(f"    ✅ 案例已保存: {inner['case_id'][:24]}...")

    # 6. 检索案例
    print("\n[6] 调用 csi.retrieve_cases...")
    retrieve_result = await client.retrieve_cases(query_ir=SAMPLE_IR, top_k=3)
    if "error" in retrieve_result:
        print(f"    ❌ 错误: {retrieve_result['error']}")
        return
    inner = retrieve_result["result"]["result"]
    print(f"    ✅ 检索到 {inner['count']} 个相似案例:")
    for r in inner["results"]:
        print(f"      Rank {r['rank']} | 相似度={r['similarity']} | "
              f"{r['model_name']} | 得分={r['results']['final_score']}")

    print()
    print("=" * 70)
    print("✅ 演示完成")
    print("=" * 70)


async def demo_error_handling():
    """演示错误处理"""
    print("\n" + "=" * 70)
    print("错误处理演示")
    print("=" * 70)

    client = MCPClient()

    # 1. 调用不存在的工具
    print("\n[1] 调用不存在的工具...")
    result = await client.call_tool("csi.nonexistent_tool", {})
    if "error" in result:
        print(f"    ✅ 正确捕获错误: {result['error']['code']} - {result['error']['message']}")

    # 2. 参数错误（缺少必要字段）
    print("\n[2] 参数错误（缺少 ir）...")
    result = await client.call_tool("csi.compile_ir", {})
    if "error" in result:
        print(f"    ✅ 正确捕获错误: {result['error']['code']} - {result['error']['message'][:50]}")

    # 3. 无效方法
    print("\n[3] 调用不存在的方法...")
    result = await client.call_jsonrpc("nonexistent/method", {})
    if "error" in result:
        print(f"    ✅ 正确捕获错误: {result['error']['code']} - {result['error']['message']}")


async def main():
    try:
        await demo_full_pipeline()
        await demo_error_handling()
    except httpx.ConnectError:
        print("\n❌ 无法连接到 Gateway")
        print("请先在另一个终端运行: python mcp_gateway.py")
    except Exception as e:
        print(f"\n❌ 异常: {e}")


if __name__ == "__main__":
    asyncio.run(main())