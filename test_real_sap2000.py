"""
SAP2000 真实 Worker 测试（在 PowerShell/cmd 中手动运行）

用法：
    python test_real_sap2000.py

注意：
- SAP2000 首次启动需要 4-5 分钟
- 需要 SAP2000 24 已安装在 D:\\SAP2000
- 需要 .NET 8.0 已安装
"""

import os
import sys
import time

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir_compiler import build_sample_frame_ir
from sap2000_worker import SAP2000Worker, SAP2000Config, create_sap2000_solver


def main():
    print("=" * 70)
    print("SAP2000 真实 Worker 端到端测试")
    print("=" * 70)

    # 1. 创建 IR
    print("\n[1] 加载测试 IR...")
    ir = build_sample_frame_ir()
    print(f"    模型: {ir.model_id}")
    print(f"    节点: {len(ir.nodes)}, 构件: {len(ir.frames)}, 截面: {len(ir.sections)}")

    # 2. 创建 Worker（真实 SAP2000）
    print("\n[2] 创建 SAP2000Worker...")
    print("    注意：首次启动 SAP2000 需要 4-5 分钟")
    config = SAP2000Config(
        timeout_sec=600.0,
        sap2000_path=r"D:\SAP2000\SAP2000.exe",
        service_path=r"D:\SAP2000\CSiAPIService.exe",
        service_port=11650,
    )

    start_time = time.time()
    worker = SAP2000Worker(config)

    try:
        print("\n[3] 启动 SAP2000 + 建模 + 求解...")
        print("    流程: CSiAPIService → Helper → SAP2000.exe → 建模 → RunAnalysis")
        print("    " + "-" * 60)

        results = worker.solve(ir)

        elapsed = time.time() - start_time
        print("\n" + "=" * 70)
        print(f"✅ SAP2000 求解成功！（总耗时 {elapsed:.1f}s）")
        print("=" * 70)
        print(f"\n结果:")
        print(f"  最大位移: {results.max_displacement_mm:.2f} mm")
        print(f"  最大层间位移角: {results.max_drift_ratio:.5f}")
        print(f"  最大利用率: {results.max_utilization:.3f}")
        print(f"  节点位移数: {len(results.joint_displacements)}")
        print(f"  构件内力数: {len(results.frame_forces)}")

    except Exception as e:
        elapsed = time.time() - start_time
        print(f"\n❌ 失败（耗时 {elapsed:.1f}s）: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # Worker.solve() 已经在 finally 里 stop 了
        pass


if __name__ == "__main__":
    main()