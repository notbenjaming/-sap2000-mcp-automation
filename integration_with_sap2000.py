"""
SAP2000 Worker 集成示例
=======================
展示如何把 SAP2000Worker 接入：
1. OptimizationLoop（替换 MockSolver）
2. MCP Gateway（通过 csi.optimize_model 工具）
3. 自动降级（SAP2000 不可用 → MockSolver）

作者：MiniMax-M3 / Hermes CSI System
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# 复用前 6 批模块
from ir_compiler import build_sample_frame_ir, StructuralIR
from optimization_loop import OptimizationLoop
from sap2000_worker import SAP2000Worker, create_sap2000_solver


def demo_direct_loop():
    """演示 1: 直接用 SAP2000Worker 替换 MockSolver"""
    print("=" * 70)
    print("演示 1: SAP2000Worker 直接接入 OptimizationLoop")
    print("=" * 70)

    ir = build_sample_frame_ir()

    # 自动检测 SAP2000（不可用时降级）
    solver = create_sap2000_solver(timeout_sec=120.0, fallback_to_mock=True)

    print(f"\n使用 Solver: {type(solver).__name__}")

    # 创建 Loop（替换 MockSolver）
    loop = OptimizationLoop(
        max_iterations=3,
        target_utilization=0.85,
        solver=solver,
    )

    # 运行优化
    print("\n开始优化...")
    result = loop.run(ir)

    print(f"\n优化结果:")
    print(f"  迭代次数: {result.iterations_run}")
    print(f"  停止原因: {result.stop_reason.value}")
    print(f"  改进: {result.improvement_pct:+.1f}%")
    print(f"  最终得分: {result.final_metrics.overall_score:.3f}")
    print(f"  最终利用率: {result.final_metrics.max_utilization:.3f}")
    print(f"  最终位移: {result.final_metrics.max_displacement_mm:.2f} mm")


def demo_manual_lifecycle():
    """演示 2: 手动管理 SAP2000 生命周期"""
    print("\n" + "=" * 70)
    print("演示 2: 手动管理 SAP2000 生命周期")
    print("=" * 70)

    ir = build_sample_frame_ir()

    print("\n[1] 尝试启动 SAP2000...")
    from sap2000_worker import SAP2000Worker, SAP2000Config, SAP2000Connection

    config = SAP2000Config(visible=False, timeout_sec=60.0)
    conn = SAP2000Connection(config)

    try:
        conn.start()
    except RuntimeError as e:
        print(f"    ⚠️ SAP2000 不可用: {e}")
        print("    本演示将展示 SAP2000 不可用时的优雅降级")
        return

    # 如果 SAP2000 可用，继续演示
    try:
        print("[2] 第一次求解...")
        start = time.time()
        results = SAP2000Worker(config).solve(ir)
        elapsed1 = time.time() - start
        print(f"    ✓ 求解完成 ({elapsed1:.2f}s)")
        print(f"    位移: {results.max_displacement_mm:.2f} mm")

        print("\n✅ 手动生命周期演示完成")
    except Exception as e:
        print(f"\n❌ 异常: {e}")
    finally:
        try:
            conn.stop()
        except Exception:
            pass


def demo_mcp_integration():
    """演示 3: SAP2000Worker 接入 MCP Gateway"""
    print("\n" + "=" * 70)
    print("演示 3: SAP2000Worker 接入 MCP Gateway")
    print("=" * 70)
    print("""
本演示需要：
1. 修改 mcp_gateway.py 的 MCPTools.optimize_model，使用 SAP2000Worker
2. 启动 Gateway: python mcp_gateway.py 8888
3. 调用 csi.optimize_model 工具

修改示例：

# mcp_gateway.py 第 240 行附近
from sap2000_worker import create_sap2000_solver

# 在 __init__ 中
self.solver = create_sap2000_solver(timeout_sec=300.0, fallback_to_mock=True)

# 在 optimize_model 中
result = run_optimization(
    ir_obj, max_iterations=max_iterations,
    target_utilization=target_utilization,
    solver=self.solver,  # 传入 SAP2000 Worker
)
""")


def demo_error_recovery():
    """演示 4: 错误恢复与 license 释放"""
    print("\n" + "=" * 70)
    print("演示 4: 错误恢复测试")
    print("=" * 70)

    from sap2000_worker import SAP2000Connection, SAP2000Config, SAP2000ModelBuilder
    from ir_compiler import StructuralIR

    # 故意构造错误 IR（缺材料定义）
    bad_ir = StructuralIR(
        model_id="bad_test",
        nodes=[],
        frames=[],
        sections=[],
        load_cases=[],
    )

    config = SAP2000Config(timeout_sec=30.0)
    conn = SAP2000Connection(config)

    try:
        conn.start()
        builder = SAP2000ModelBuilder(conn.sap_model, config)

        # 这个调用不会失败（空 IR 是合法的）
        builder.build(bad_ir)
        print("    ✓ 空 IR 正常处理")
    except Exception as e:
        print(f"    捕获异常: {e}")
    finally:
        # 确保 SAP2000 被释放
        conn.stop()
        print("    ✓ SAP2000 已释放 license")


def main():
    print("\n" + "=" * 70)
    print("SAP2000 Worker 集成演示套件")
    print("=" * 70)

    try:
        demo_direct_loop()
        demo_manual_lifecycle()
        demo_mcp_integration()
        demo_error_recovery()

        print("\n" + "=" * 70)
        print("✅ 所有演示完成")
        print("=" * 70)
    except Exception as e:
        print(f"\n❌ 演示异常: {e}")


if __name__ == "__main__":
    main()