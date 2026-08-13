"""
SAP2000 增量更新测试
===================
模拟多轮交互式工作流：
1. 初始建模
2. 修改柱截面
3. 修改梁荷载
4. 求解
5. 再次修改 + 求解

前置条件：SAP2000 已启动（用户手动启动或通过 Helper）
"""
import sys
import time
import copy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ir_compiler import build_sample_frame_ir, StructuralIR
from ir_diff import compute_diff, scale_all_dist_loads, apply_modifications, Modification
from sap2000_worker import SAP2000Worker, SAP2000Config


def main():
    print("=" * 70)
    print("SAP2000 增量更新测试")
    print("=" * 70)

    # 配置
    config = SAP2000Config(
        sap2000_path=r"D:\SAP2000\SAP2000.exe",
        service_path=r"D:\SAP2000\CSiAPIService.exe",
        service_port=11650,
    )

    worker = SAP2000Worker(config)

    # ============ 阶段 1: 初始建模 ============
    print("\n[阶段 1] 初始建模")
    ir = build_sample_frame_ir()
    print(f"  IR: {ir.model_id}")
    print(f"  节点: {len(ir.nodes)}, 构件: {len(ir.frames)}, 截面: {len(ir.sections)}")

    worker.build_initial_model(ir)
    print(f"  ✅ 模型已保存: {worker._last_model_path}")
    print(f"  👉 请在 SAP2000 中打开该文件检查")

    # ============ 阶段 2: 用户修改（手动驱动模拟）============
    print("\n[阶段 2] 模拟用户修改 1: 改柱 1 截面为 600x600")

    ir_v2 = copy.deepcopy(ir)
    # 找柱 1 的 section（COL400x400）改为 600x600
    for sec in ir_v2.sections:
        if sec.name == "COL400x400":
            sec.rect_h = 600
            sec.rect_b = 600
            sec.name = "COL600x600"  # 改名（因为尺寸变了）
            break

    # 同时更新引用此截面的 frame
    for frame in ir_v2.frames:
        if frame.section == "COL400x400":
            frame.section = "COL600x600"

    summary = worker.update_model(ir_v2)
    print(f"  ✅ 更新统计: {summary}")

    # ============ 阶段 3: 改荷载 ============
    print("\n[阶段 3] 模拟用户修改 2: 改梁 7 DEAD 荷载为 -30")

    ir_v3 = copy.deepcopy(ir_v2)
    for dl in ir_v3.dist_loads:
        if dl.frame_id == 7 and dl.case == "DEAD":
            dl.value = -30.0

    summary = worker.update_model(ir_v3)
    print(f"  ✅ 更新统计: {summary}")

    # ============ 阶段 4: 批量缩放（NLP 解析）============
    print("\n[阶段 4] 模拟 NLP 指令: 'DEAD 荷载都乘 1.4 倍'")

    # 解析 → 生成批量修改 → 增量更新
    mods = scale_all_dist_loads(ir_v3, "DEAD", 1.4)
    print(f"  生成 {len(mods)} 条批量修改")
    apply_modifications(ir_v3, mods)

    summary = worker.update_model(ir_v3)
    print(f"  ✅ 更新统计: {summary}")
    print(f"  DEAD 荷载值:")
    for dl in ir_v3.dist_loads:
        if dl.case == "DEAD":
            print(f"    frame={dl.frame_id}: {dl.value} kN/m")

    # ============ 阶段 5: 求解 ============
    print("\n[阶段 5] 运行分析（修改完成）")
    if worker.run_analysis_only():
        results = worker.extract_results()
        print(f"  ✅ 求解完成")
        print(f"  最大位移: {results.max_displacement_mm:.2f} mm")
        print(f"  最大利用率: {results.max_utilization:.3f}")
    else:
        print(f"  ❌ 求解失败")

    # ============ 阶段 6: 关闭 ============
    print("\n[阶段 6] 关闭 SAP2000")
    worker.connection.stop()
    print("  ✅ 已关闭")

    print("\n" + "=" * 70)
    print("✅ 增量更新测试完成")
    print("=" * 70)


if __name__ == "__main__":
    main()