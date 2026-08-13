"""
MCP Gateway - 工业级 MCP 协议网关
=================================
把 4 个核心模块（IR Compiler / Solver Router / Optimization Loop / Knowledge Store）
封装为 MCP 工具，对外暴露 JSON-RPC 2.0 兼容 HTTP 接口。

核心模块：
1. MCPToolRegistry（工具注册中心）
2. MCPRequest/Response（协议模型）
3. GatewayServer（FastAPI 应用）
4. ToolRouter（请求分发）

作者：MiniMax-M3 / Hermes CSI System
版本：v1.0.0
"""

from __future__ import annotations

import json
import time
import uuid
import logging
import asyncio
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional, Any, Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from contextlib import asynccontextmanager

from pydantic import BaseModel, Field, ConfigDict

# FastAPI
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
import uvicorn

# 复用前 4 批模块
from ir_compiler import (
    StructuralIR, IRCompiler,
    SolverType as IRSolverType,
)
from solver_router import SolverRouter
from optimization_loop import run_optimization, LoopResult
from knowledge_store import KnowledgeStore

# 真实 SAP2000 接入（带 Mock 降级）
try:
    from sap2000_worker import create_sap2000_solver
    HAS_SAP2000_WORKER = True
except ImportError:
    HAS_SAP2000_WORKER = False
    logger.warning("sap2000_worker 不可用，optimize_model 将使用纯 Mock 模式")

# ============================================================================
# 日志配置
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger("mcp_gateway")


# ============================================================================
# 第 1 层：MCP 协议数据模型
# ============================================================================

class MCPErrorCode(int, Enum):
    """MCP 错误码（参考 JSON-RPC 2.0 + 自定义）"""
    PARSE_ERROR = -32700           # JSON 解析失败
    INVALID_REQUEST = -32600       # 无效请求
    METHOD_NOT_FOUND = -32601      # 方法不存在
    INVALID_PARAMS = -32602        # 参数无效
    INTERNAL_ERROR = -32603        # 内部错误
    TOOL_NOT_FOUND = -32001        # 工具未找到
    TOOL_EXECUTION_ERROR = -32002  # 工具执行失败
    VALIDATION_ERROR = -32003      # 校验失败


@dataclass
class MCPError:
    """MCP 错误"""
    code: MCPErrorCode
    message: str
    data: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "data": self.data,
        }


class MCPRequest(BaseModel):
    """MCP 请求（兼容 JSON-RPC 2.0）"""
    model_config = ConfigDict(extra="allow")

    jsonrpc: str = Field(default="2.0", description="JSON-RPC 版本")
    method: str = Field(..., description="方法名，如 'tools/call'")
    params: Dict[str, Any] = Field(default_factory=dict, description="参数")
    id: Optional[Any] = Field(default=None, description="请求 ID")


class MCPResponse(BaseModel):
    """MCP 响应（兼容 JSON-RPC 2.0）"""
    model_config = ConfigDict(extra="allow")

    jsonrpc: str = "2.0"
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None
    id: Optional[Any] = None


# ============================================================================
# 第 2 层：MCPToolDefinition 工具定义
# ============================================================================

@dataclass
class MCPToolDefinition:
    """MCP 工具定义

    每个工具对应一个 Python 函数，参数通过 JSON Schema 描述。
    """
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[..., Awaitable[Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


# ============================================================================
# 第 3 层：MCPToolRegistry 工具注册中心
# ============================================================================

class MCPToolRegistry:
    """MCP 工具注册中心

    提供：
    - register: 注册工具
    - get: 获取工具
    - list_tools: 列出所有工具
    """

    def __init__(self):
        self.tools: Dict[str, MCPToolDefinition] = {}

    def register(self, tool: MCPToolDefinition):
        """注册一个工具"""
        if tool.name in self.tools:
            raise ValueError(f"工具已存在: {tool.name}")
        self.tools[tool.name] = tool
        logger.info(f"已注册工具: {tool.name}")

    def get(self, name: str) -> Optional[MCPToolDefinition]:
        """获取工具"""
        return self.tools.get(name)

    def list_tools(self) -> List[Dict[str, Any]]:
        """列出所有工具（JSON Schema 格式）"""
        return [t.to_dict() for t in self.tools.values()]


# ============================================================================
# 第 4 层：5 个核心 MCP 工具实现
# ============================================================================

class MCPTools:
    """5 个 MCP 工具的实现

    工具清单：
    1. csi.compile_ir        - 编译 IR 为执行计划
    2. csi.route_solver      - 路由到最佳 solver
    3. csi.optimize_model    - 闭环优化结构
    4. csi.save_case         - 保存案例到知识库
    5. csi.retrieve_cases    - 检索相似案例
    """

    def __init__(self, knowledge_db_path: str = "./mcp_knowledge_db.json"):
        self.compiler = IRCompiler()
        self.router = SolverRouter()
        self.knowledge = KnowledgeStore(db_path=knowledge_db_path)

        # 初始化 Solver（自动检测 SAP2000，不可用则降级 Mock）
        if HAS_SAP2000_WORKER:
            self.solver = create_sap2000_solver(
                timeout_sec=300.0,
                fallback_to_mock=True,
            )
            logger.info(f"使用 Solver: {type(self.solver).__name__}")
        else:
            from optimization_loop import MockSolver
            self.solver = MockSolver()
            logger.info("使用 Solver: MockSolver（无 SAP2000 Worker）")

    # ---------------------- 工具 1: compile_ir ----------------------

    async def compile_ir(self, ir: Dict[str, Any]) -> Dict[str, Any]:
        """工具 1: 编译 IR → 执行计划"""
        try:
            ir_obj = StructuralIR(**ir)
            plan = self.compiler.compile(ir_obj)
            return plan.to_dict()
        except Exception as e:
            logger.exception("compile_ir 失败")
            raise RuntimeError(f"IR 编译失败: {e}")

    # ---------------------- 工具 2: route_solver ----------------------

    async def route_solver(
        self,
        ir: Optional[Dict[str, Any]] = None,
        override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """工具 2: 路由 IR → solver"""
        try:
            if ir:
                ir_obj = StructuralIR(**ir)
                # override 必须通过构造函数传入，每次新建 Router 实例
                target_solver = IRSolverType(override) if override else None
                router = SolverRouter(manual_override=target_solver)
                decision = router.route(ir_obj)
                return decision.to_dict()
            else:
                # 无 IR 时返回所有可用 solver
                return {
                    "available_solvers": [s.value for s in IRSolverType],
                    "default": IRSolverType.SAP2000.value,
                }
        except Exception as e:
            logger.exception("route_solver 失败")
            raise RuntimeError(f"路由失败: {e}")

    # ---------------------- 工具 3: optimize_model ----------------------

    async def optimize_model(
        self,
        ir: Dict[str, Any],
        max_iterations: int = 10,
        target_utilization: float = 0.85,
    ) -> Dict[str, Any]:
        """工具 3: 闭环优化（支持真实 SAP2000 / 自动降级 Mock）"""
        try:
            ir_obj = StructuralIR(**ir)
            result = run_optimization(
                ir_obj,
                max_iterations=max_iterations,
                target_utilization=target_utilization,
                solver=self.solver,  # ← 使用 SAP2000 Worker 或 Mock
            )
            return result.to_dict()
        except Exception as e:
            logger.exception("optimize_model 失败")
            raise RuntimeError(f"优化失败: {e}")

    # ---------------------- 工具 4: save_case ----------------------

    async def save_case(
        self,
        ir: Dict[str, Any],
        optimization_result: Dict[str, Any],
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """工具 4: 保存案例"""
        try:
            # 反序列化 IR
            ir_obj = StructuralIR(**ir)

            # 从 optimization_result 提取指标
            # 注意：LoopResult.to_dict() 输出的是摘要，需要从 history 最后一条拿完整指标
            history = optimization_result.get("history", [])
            if not history:
                raise ValueError("optimization_result 缺少 history 字段")

            last_iter = history[-1]
            metrics_dict = last_iter["metrics"]

            # 重建 EvaluationMetrics
            from optimization_loop import EvaluationMetrics, MetricLevel
            final_metrics = EvaluationMetrics(
                max_displacement_mm=metrics_dict["displacement"]["max_mm"],
                displacement_ratio=metrics_dict["displacement"]["ratio"],
                max_drift_ratio=metrics_dict["drift"]["max_ratio"],
                drift_ratio_limit=metrics_dict["drift"]["limit_ratio"],
                max_utilization=metrics_dict["utilization"]["max"],
                avg_utilization=metrics_dict["utilization"]["avg"],
                total_section_area_m2=metrics_dict["cost"]["section_area_m2"],
                cost_estimate=metrics_dict["cost"]["estimate"],
                code_compliance=metrics_dict["code"]["compliant"],
                violation_count=metrics_dict["code"]["violations"],
                overall_score=metrics_dict["overall"]["score"],
                level=MetricLevel(metrics_dict["overall"]["level"]),
            )

            # 构造 LoopResult 用于接口兼容
            from optimization_loop import LoopResult, LoopStopReason
            loop_result = LoopResult(
                initial_ir=ir_obj,
                final_ir=ir_obj,
                initial_metrics=final_metrics,
                final_metrics=final_metrics,
                history=[],
                iterations_run=optimization_result["iterations_run"],
                stop_reason=LoopStopReason(optimization_result["stop_reason"]),
                total_time_ms=optimization_result["total_time_ms"],
                improvement_pct=optimization_result["improvement_pct"],
            )

            case = self.knowledge.save_case_from_loop(
                ir_obj, loop_result, tags=tags,
            )
            return case.to_dict()
        except Exception as e:
            logger.exception("save_case 失败")
            raise RuntimeError(f"保存案例失败: {e}")

    # ---------------------- 工具 5: retrieve_cases ----------------------

    async def retrieve_cases(
        self,
        query_ir: Optional[Dict[str, Any]] = None,
        query_features: Optional[Dict[str, Any]] = None,
        top_k: int = 5,
        min_similarity: float = 0.0,
    ) -> Dict[str, Any]:
        """工具 5: 检索相似案例"""
        try:
            query_ir_obj = None
            if query_ir:
                query_ir_obj = StructuralIR(**query_ir)

            results = self.knowledge.retrieve_similar(
                query_ir=query_ir_obj,
                query_features=query_features,
                top_k=top_k,
                min_similarity=min_similarity,
            )

            return {
                "count": len(results),
                "results": [r.to_dict() for r in results],
            }
        except Exception as e:
            logger.exception("retrieve_cases 失败")
            raise RuntimeError(f"检索失败: {e}")


# ============================================================================
# 第 5 层：ToolRouter 请求路由器
# ============================================================================

class ToolRouter:
    """工具路由器

    解析 MCP 请求，分发到对应工具。
    支持的方法：
    - tools/list          : 列出所有工具
    - tools/call          : 调用指定工具
    - gateway/health      : 健康检查
    - gateway/info        : Gateway 信息
    """

    def __init__(self, registry: MCPToolRegistry):
        self.registry = registry

    async def route(self, request: MCPRequest) -> MCPResponse:
        """路由 MCP 请求到对应处理函数"""
        try:
            method = request.method
            params = request.params or {}

            # 系统方法
            if method == "tools/list":
                return self._success(request.id, {
                    "tools": self.registry.list_tools(),
                })

            elif method == "gateway/health":
                return self._success(request.id, {
                    "status": "ok",
                    "timestamp": datetime.now().isoformat(),
                    "tools_registered": len(self.registry.tools),
                })

            elif method == "gateway/info":
                return self._success(request.id, {
                    "name": "Hermes CSI MCP Gateway",
                    "version": "1.0.0",
                    "tools": [t.name for t in self.registry.tools.values()],
                    "uptime_s": time.time() - _START_TIME,
                })

            # 工具调用
            elif method == "tools/call":
                tool_name = params.get("name")
                tool_args = params.get("arguments", {})

                tool = self.registry.get(tool_name)
                if not tool:
                    return self._error(
                        request.id,
                        MCPErrorCode.TOOL_NOT_FOUND,
                        f"工具不存在: {tool_name}",
                    )

                # 执行工具
                start = time.time()
                try:
                    result = await tool.handler(**tool_args)
                    elapsed = (time.time() - start) * 1000
                    return self._success(request.id, {
                        "tool": tool_name,
                        "result": result,
                        "execution_time_ms": round(elapsed, 2),
                    })
                except TypeError as e:
                    return self._error(
                        request.id,
                        MCPErrorCode.INVALID_PARAMS,
                        f"参数错误: {e}",
                    )
                except Exception as e:
                    return self._error(
                        request.id,
                        MCPErrorCode.TOOL_EXECUTION_ERROR,
                        f"工具执行失败: {e}",
                    )

            else:
                return self._error(
                    request.id,
                    MCPErrorCode.METHOD_NOT_FOUND,
                    f"方法不存在: {method}",
                )

        except Exception as e:
            logger.exception("route() 异常")
            return self._error(
                request.id,
                MCPErrorCode.INTERNAL_ERROR,
                f"网关内部错误: {e}",
            )

    def _success(self, req_id: Any, result: Any) -> MCPResponse:
        return MCPResponse(id=req_id, result=result)

    def _error(self, req_id: Any, code: MCPErrorCode, message: str,
               data: Any = None) -> MCPResponse:
        err = MCPError(code=code, message=message, data=data)
        return MCPResponse(id=req_id, error=err.to_dict())


_START_TIME = time.time()


# ============================================================================
# 第 6 层：GatewayServer FastAPI 服务
# ============================================================================

# 全局状态（用于 lifespan）
_state: Dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务生命周期"""
    logger.info("=" * 60)
    logger.info("MCP Gateway 启动中...")
    logger.info("=" * 60)

    # 初始化
    registry = MCPToolRegistry()
    tools = MCPTools(knowledge_db_path="./mcp_knowledge_db.json")

    # 注册 5 个核心工具
    registry.register(MCPToolDefinition(
        name="csi.compile_ir",
        description="编译结构 IR 为 SAP2000 可执行的步骤序列",
        input_schema={
            "type": "object",
            "properties": {
                "ir": {"type": "object", "description": "结构 IR 对象"},
            },
            "required": ["ir"],
        },
        handler=tools.compile_ir,
    ))

    registry.register(MCPToolDefinition(
        name="csi.route_solver",
        description="根据 IR 特征路由到最佳 solver（SAP2000/OpenSees/ANSYS/ETABS）",
        input_schema={
            "type": "object",
            "properties": {
                "ir": {"type": "object", "description": "结构 IR 对象（可选）"},
                "override": {"type": "string", "description": "强制指定 solver（可选）"},
            },
        },
        handler=tools.route_solver,
    ))

    registry.register(MCPToolDefinition(
        name="csi.optimize_model",
        description="对结构 IR 进行闭环优化（求解-评估-修改-重解）",
        input_schema={
            "type": "object",
            "properties": {
                "ir": {"type": "object", "description": "结构 IR 对象"},
                "max_iterations": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                "target_utilization": {"type": "number", "default": 0.85, "minimum": 0.1, "maximum": 1.0},
            },
            "required": ["ir"],
        },
        handler=tools.optimize_model,
    ))

    registry.register(MCPToolDefinition(
        name="csi.save_case",
        description="保存优化案例到知识库",
        input_schema={
            "type": "object",
            "properties": {
                "ir": {"type": "object", "description": "结构 IR 对象"},
                "optimization_result": {"type": "object", "description": "优化结果"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "标签列表"},
            },
            "required": ["ir", "optimization_result"],
        },
        handler=tools.save_case,
    ))

    registry.register(MCPToolDefinition(
        name="csi.retrieve_cases",
        description="从知识库检索相似结构案例",
        input_schema={
            "type": "object",
            "properties": {
                "query_ir": {"type": "object", "description": "查询 IR（可选）"},
                "query_features": {"type": "object", "description": "查询特征字典（可选）"},
                "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
                "min_similarity": {"type": "number", "default": 0.0, "minimum": 0.0, "maximum": 1.0},
            },
        },
        handler=tools.retrieve_cases,
    ))

    # 创建路由
    router = ToolRouter(registry)

    _state["registry"] = registry
    _state["tools"] = tools
    _state["router"] = router
    _state["start_time"] = time.time()

    logger.info(f"已注册 {len(registry.tools)} 个 MCP 工具:")
    for name in registry.tools:
        logger.info(f"  - {name}")
    logger.info("✅ Gateway 启动完成")

    yield

    logger.info("Gateway 关闭")


app = FastAPI(
    title="Hermes CSI MCP Gateway",
    version="1.0.0",
    description="MCP 协议网关，封装 SAP2000 自动化能力",
    lifespan=lifespan,
)


# ------------------------- RESTful API -------------------------

@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "Hermes CSI MCP Gateway",
        "version": "1.0.0",
        "endpoints": {
            "mcp": "/mcp",
            "tools_list": "/tools",
            "health": "/health",
            "docs": "/docs",
        },
    }


@app.get("/health")
async def health():
    """健康检查（REST）"""
    return {
        "status": "ok",
        "tools_count": len(_state.get("registry", {}).tools) if _state else 0,
        "uptime_s": time.time() - _state.get("start_time", time.time()),
    }


@app.get("/tools")
async def list_tools():
    """列出所有工具（REST）"""
    registry = _state.get("registry")
    if not registry:
        raise HTTPException(status_code=503, detail="Gateway 未就绪")
    return {"tools": registry.list_tools()}


@app.post("/mcp")
async def mcp_endpoint(request: MCPRequest):
    """MCP 主端点（JSON-RPC 2.0 兼容）"""
    router = _state.get("router")
    if not router:
        raise HTTPException(status_code=503, detail="Gateway 未就绪")

    response = await router.route(request)
    return response.model_dump(exclude_none=True)


# ------------------------- 直接工具调用（便捷） -------------------------

@app.post("/tools/{tool_name}/call")
async def call_tool_direct(tool_name: str, arguments: Dict[str, Any]):
    """直接调用工具（REST 便捷接口）"""
    request = MCPRequest(
        method="tools/call",
        params={"name": tool_name, "arguments": arguments},
        id=str(uuid.uuid4()),
    )
    router = _state.get("router")
    response = await router.route(request)
    return response.model_dump(exclude_none=True)


# ============================================================================
# 第 7 层：便捷启动函数
# ============================================================================

def run_gateway(host: str = "127.0.0.1", port: int = 8000, log_level: str = "info"):
    """启动 Gateway"""
    logger.info(f"启动 Gateway @ http://{host}:{port}")
    uvicorn.run(
        "mcp_gateway:app",
        host=host,
        port=port,
        log_level=log_level,
        reload=False,
    )


def check_port_available(host: str, port: int) -> bool:
    """检查端口是否可用"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


if __name__ == "__main__":
    import sys

    host = "127.0.0.1"
    port = 8000

    # 解析命令行参数
    if len(sys.argv) > 1:
        port = int(sys.argv[1])

    # 端口冲突检测
    if not check_port_available(host, port):
        logger.error(f"端口 {port} 已被占用，请使用其他端口")
        logger.info(f"用法: python mcp_gateway.py [port]")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"Hermes CSI MCP Gateway")
    print(f"{'=' * 60}")
    print(f"端点:")
    print(f"  MCP:    http://{host}:{port}/mcp")
    print(f"  Tools:  http://{host}:{port}/tools")
    print(f"  Health: http://{host}:{port}/health")
    print(f"  Docs:   http://{host}:{port}/docs")
    print(f"{'=' * 60}\n")

    run_gateway(host=host, port=port)