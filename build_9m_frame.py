#!/usr/bin/env python3
"""用 OAPI 直接建 2 层 3 跨 9m 混凝土框架（绕过 build_initial_model 旧路径）

要求：SAP2000 GUI 已启动（任意模型状态都行，会先清空）

模型参数：
  - 2 层 × 3 跨
  - 跨长 9m（总长 27m）
  - 层高 3m
  - 柱 400x400 mm
  - 梁 200x500 mm
  - DEAD -10 kN/m, LIVE -5 kN/m
"""

import sys
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("build_9m")

import pythoncom
import concurrent.futures
import time

from ir_compiler import build_3span_frame_9m
from modify_model import state_from_ir, save_ir_state, _find_existing_sap_pid, _STATE_FILE
from sap2000_worker import SAP2000Worker, SAP2000Config

SAP2000_PATH = r"D:\SAP2000\SAP2000.exe"


def main():
    # 检查 SAP2000
    pid, mem = _find_existing_sap_pid()
    if not (pid and mem and mem > 80):
        print("❌ SAP2000 未运行。请先启动 SAP2000 GUI")
        return
    print(f"✅ SAP2000 已运行 (PID={pid}, {mem:.0f}MB)")

    # 生成 IR
    print("\n[1] 生成 IR")
    ir = build_3span_frame_9m()
    print(f"  节点: {len(ir.nodes)}, 构件: {len(ir.frames)}, 截面: {len(ir.sections)}, 荷载: {len(ir.dist_loads)}")
    save_ir_state(state_from_ir(ir))
    print(f"  IR 状态已保存")

    # 连接 SAP2000
    config = SAP2000Config(sap2000_path=SAP2000_PATH)
    worker = SAP2000Worker(config)
    worker.connection.start()

    pythoncom.CoInitialize()
    try:
        sap = worker.connection.sap_object.SapModel
        sap.SetModelIsLocked(False)

        # 1. 清空现有模型
        print("\n[2] 清空现有模型")
        # 先删除所有构件（避免节点删除警告）
        ret = sap.FrameObj.GetNameList()
        if ret[1] > 0:
            for f in ret[2]:
                try: sap.FrameObj.Delete(f)
                except: pass
            print(f"  已删除 {ret[1]} 个旧构件")

        # 删除所有节点
        ret = sap.PointObj.GetNameList()
        if ret[1] > 0:
            for n in ret[2]:
                try:
                    sap.PointObj.Delete(n)
                except:
                    pass
            print(f"  已删除 {ret[1]} 个旧节点")

        # 删除所有截面
        ret = sap.PropFrame.GetNameList()
        if ret[1] > 0:
            for s in ret[2]:
                try: sap.PropFrame.Delete(s)
                except: pass
            print(f"  已删除 {ret[1]} 个旧截面")

        # 删除所有荷载样式
        ret = sap.LoadPatterns.GetNameList()
        if ret[1] > 0:
            for p in ret[2]:
                try: sap.LoadPatterns.Delete(p)
                except: pass

        # 删除所有荷载工况
        ret = sap.LoadCases.GetNameList()
        if ret[1] > 0:
            for c in ret[2]:
                try: sap.LoadCases.Delete(c)
                except: pass

        time.sleep(1)

        # 2. 定义截面
        print("\n[3] 定义截面")
        for sec in ir.sections:
            h_m = sec.rect_h / 1000.0
            b_m = sec.rect_b / 1000.0
            ret = sap.PropFrame.SetRectangle(sec.name, sec.material, h_m, b_m)
            if ret == 0:
                print(f"  ✅ {sec.name}: {sec.rect_h}x{sec.rect_b} mm, {sec.material}")
            else:
                print(f"  ❌ {sec.name}: ret={ret}")

        # 3. 定义荷载工况
        print("\n[4] 定义荷载工况")
        # DEAD
        ret = sap.LoadCases.AddLinearStatic("DEAD", 0, 1, 0)  # 0=LinearStatic, 1=DEAD type
        print(f"  DEAD: ret={ret}")
        # LIVE
        ret = sap.LoadCases.AddLinearStatic("LIVE", 0, 5, 0)  # 5=Live type
        print(f"  LIVE: ret={ret}")

        # 荷载样式
        ret = sap.LoadPatterns.Add("DEAD", 1, 0, True)  # 1=DEAD type, 0=No Design
        print(f"  LoadPattern DEAD: ret={ret}")
        ret = sap.LoadPatterns.Add("LIVE", 5, 0, True)  # 5=Live
        print(f"  LoadPattern LIVE: ret={ret}")

        # 4. 建节点
        print("\n[5] 创建节点")
        node_id_map = {}  # IR node id → SAP2000 node name
        for n in ir.nodes:
            ret = sap.PointObj.AddCartesian(
                n.x, n.y, n.z,
                n.id, "", ""  # 显式指定 name = n.id
            )
            # ret = (retcode, actual_name)
            actual_name = ret[1] if isinstance(ret, tuple) and len(ret) > 1 else str(n.id)
            node_id_map[n.id] = actual_name
            print(f"  节点 {n.id}: ({n.x}, {n.y}, {n.z}) → {actual_name}")

        # 5. 设约束
        print("\n[6] 设置节点约束")
        for n in ir.nodes:
            if n.restrain:
                sap_name = node_id_map[n.id]
                ret = sap.PointObj.SetRestraint(sap_name, n.restrain)
                if ret == 0:
                    print(f"  节点 {n.id} ({sap_name}): 固接")

        # 6. 建构件
        print("\n[7] 创建构件")
        for f in ir.frames:
            i_name = node_id_map[f.i_node]
            j_name = node_id_map[f.j_node]
            sec_name = f.section
            ret = sap.FrameObj.AddByPoint(i_name, j_name, "", sec_name, f.id)
            actual = ret[1] if isinstance(ret, tuple) and len(ret) > 1 else str(f.id)
            print(f"  构件 {f.id}: {i_name}→{j_name} [{f.role}] 截面={sec_name} → {actual}")

        # 7. 施荷载
        print("\n[8] 施加均布荷载")
        for dl in ir.dist_loads:
            # 找构件名
            frame = next(f for f in ir.frames if f.id == dl.frame_id)
            i_name = node_id_map[frame.i_node]
            j_name = node_id_map[frame.j_node]
            # 找构件实际名
            ret = sap.FrameObj.GetNameList()
            # FrameObj.AddByPoint 用 IR frame id 作为 name — 所以构件名就是 f.id（数字）
            frame_name = str(dl.frame_id)

            # SetLoadDistributed: Name, LoadPat, MyType, Dir, Dist1, Dist2, Val1, Val2
            ret = sap.FrameObj.SetLoadDistributed(
                frame_name, dl.case,
                1,           # MyType: Force/Length
                6,           # Dir: Gravity
                0, 1,        # Dist1, Dist2 (relative range)
                dl.value, dl.value,
            )
            print(f"  构件 {dl.frame_id} {dl.case} {dl.value} kN/m: ret={ret}")

        time.sleep(1)

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        return
    finally:
        try:
            pythoncom.CoUninitialize()
        except:
            pass

    # 8. 保存 .sdb
    print("\n[9] 保存模型")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(lambda: worker.connection.sap_object.SapModel.File.Save())
        try:
            future.result(timeout=15)
            print(f"  ✅ 模型已保存")
        except concurrent.futures.TimeoutError:
            print(f"  ⚠️ Save 超时（在 SAP2000 GUI 中手动 Ctrl+S）")

    print("\n" + "=" * 60)
    print("✅ 2 层 3 跨 9m 混凝土框架已建好！")
    print("=" * 60)
    print(f"  跨长 9m × 3 = 27m")
    print(f"  层高 3m × 2 = 6m")
    print(f"  柱 400x400 mm (4 根)")
    print(f"  梁 200x500 mm (3 根，二层)")
    print(f"  DEAD: -10 kN/m, LIVE: -5 kN/m")
    print(f"\n下一步：")
    print(f"  1. 在 SAP2000 GUI 中按 F5 刷新视图")
    print(f"  2. 运行 `python modify_model.py solve` 跑分析")
    print(f"  3. 或 `python modify_model.py show 内力` 看结果")


if __name__ == "__main__":
    main()
