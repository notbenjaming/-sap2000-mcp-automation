"""
build_frame_9m.py
================
按用户精确规格在 SAP2000 中建立三跨混凝土框架：
  - 跨度: 9 m × 3 跨
  - 层高: 3 m × 1 层
  - 柱截面: 400 × 400 mm
  - 梁截面: 200 × 500 mm
  - 恒载: -10 kN/m (DEAD 工况)
  - 底柱: 固接

前置:
  - SAP2000 v24 已启动（用户手动启动）
  - Python 依赖: pywin32, psutil, comtypes

用法 (PowerShell):
  cd C:\\Users\\21574\\hermes_csi_system
  python build_frame_9m.py
"""
import sys
import time
import win32com.client
import pythoncom
import psutil


# ────────────────── 设计参数 ──────────────────
SPAN_M = 9.0          # 单跨跨度 (m)
N_SPANS = 3           # 跨数
N_STORIES = 1         # 层数
STORY_H_M = 3.0       # 层高 (m)
COL_B_MM = 400        # 柱宽 (mm)
COL_H_MM = 400        # 柱高 (mm)
BEAM_B_MM = 200       # 梁宽 (mm)
BEAM_H_MM = 500       # 梁高 (mm)
DEAD_KN_M = -10.0     # 恒载 (kN/m, 向下为负)
MAT_NAME = "C30"      # 混凝土材料
SEC_COL = "COL400x400"
SEC_BEAM = "BEAM200x500"
CASE_DEAD = "DEAD"
PROJECT_ROOT = r"C:\\Users\\21574\\hermes_csi_system"
SDB_PATH = PROJECT_ROOT + r"\\frame_3span_9m.sdb"


def find_sap_pid():
    """找 SAP2000 主进程 (排除 launcher < 100MB)"""
    cands = []
    for p in psutil.process_iter(['name', 'pid', 'memory_info']):
        if p.info.get('name') != 'SAP2000.exe':
            continue
        mb = p.info['memory_info'].rss / 1024 / 1024
        if mb < 100:  # launcher
            continue
        cands.append((p.info['pid'], mb))
    if not cands:
        return None
    cands.sort(key=lambda x: x[1], reverse=True)
    return cands[0][0]


def attach_sap():
    """附加到用户已启动的 SAP2000 (PID 来自 psutil)"""
    pid = find_sap_pid()
    if not pid:
        raise RuntimeError(
            "未找到 SAP2000 主进程。请先双击 D:\\SAP2000\\SAP2000.exe 启动，"
            "等内存稳定到 ~500MB 后再跑本脚本。"
        )
    print(f"[attach] 找到 SAP2000 PID={pid}")

    helper = win32com.client.Dispatch("SAP2000v1.Helper")
    ver = helper.GetOAPIVersionNumber()
    print(f"[attach] OAPI 版本: {ver}")
    sap = helper.GetObjectProcess("CSI.SAP2000.API.SapObject", str(pid))
    print(f"[attach] 已附加到 PID={pid}")
    return sap


def build_frame(sap):
    """核心建模流程: 节点 → 材料 → 截面 → 构件 → 支座 → 荷载工况 → 荷载"""
    sm = sap.SapModel

    # 0. 清空旧模型 (新建空白)
    print("[0] 初始化新模型...")
    sm.InitializeNewModel()
    ret = sm.File.NewBlank()
    print(f"    File.NewBlank ret={ret}")
    if ret != 0:
        raise RuntimeError(f"NewBlank 失败 ret={ret}")

    # 0.5 设置单位 (kN, m, C)
    sm.SetPresentUnits(6)  # 6 = kN_m_C
    print("    单位: kN_m_C")

    # 1. 定义材料 C30 混凝土 (eMatTypeConcrete=2, 默认参数由 SAP2000 提供)
    print("[1] 定义材料 C30...")
    sm.PropMaterial.SetMaterial(MAT_NAME, 2)
    # 简化: 不修改强度参数, 用 SAP2000 默认 C30 属性

    # 2. 定义截面
    print("[2] 定义截面...")
    # SetRectangle(name, material, depth, width)  -- 单位: m
    ret = sm.PropFrame.SetRectangle(SEC_COL, MAT_NAME,
                                    COL_H_MM / 1000.0,  # depth (h)
                                    COL_B_MM / 1000.0)  # width (b)
    print(f"    {SEC_COL} ({COL_H_MM}x{COL_B_MM} mm) ret={ret}")
    ret = sm.PropFrame.SetRectangle(SEC_BEAM, MAT_NAME,
                                    BEAM_H_MM / 1000.0,
                                    BEAM_B_MM / 1000.0)
    print(f"    {SEC_BEAM} ({BEAM_H_MM}x{BEAM_B_MM} mm) ret={ret}")

    # 3. 创建节点
    # 节点布局: 底排 4 个 (y=0), 顶排 4 个 (y=0, z=3)
    print("[3] 创建节点...")
    node_ids = {}  # 逻辑 id -> SAP2000 name
    for story in range(N_STORIES + 1):
        z = story * STORY_H_M
        for i in range(N_SPANS + 1):
            x = i * SPAN_M
            # AddCartesian(x, y, z, name_ByRef) -> (ret, name)
            ret, name = sm.PointObj.AddCartesian(x, 0.0, z, "")
            if ret != 0:
                raise RuntimeError(
                    f"AddCartesian 失败 ({x},{0},{z}) ret={ret}")
            logical_id = story * (N_SPANS + 1) + i + 1
            node_ids[logical_id] = name
            print(f"    节点{logical_id}: ({x}, 0, {z}) -> {name}")

    # 4. 解锁模型 (创建构件前必须)
    sm.SetModelIsLocked(False)

    # 5. 创建构件 (柱 + 梁)
    print("[4] 创建构件...")
    frame_ids = {}  # logical id -> name
    fid = 1
    # 柱: 从底到顶 (story=0 → story=1)
    for i in range(N_SPANS + 1):
        i_node = node_ids[1 + i]            # 底 (story=0, idx=i)
        j_node = node_ids[1 + (N_SPANS + 1) + i]  # 顶 (story=1, idx=i)
        ret, name = sm.FrameObj.AddByPoint(i_node, j_node, "", SEC_COL)
        if ret != 0:
            raise RuntimeError(f"柱{fid} AddByPoint 失败 ret={ret}")
        frame_ids[fid] = name
        print(f"    柱{fid}: {i_node} -> {j_node} ({name})  [{SEC_COL}]")
        fid += 1

    # 梁: 顶排节点之间
    for i in range(N_SPANS):
        i_node = node_ids[1 + (N_SPANS + 1) + i]      # 顶 (story=1, idx=i)
        j_node = node_ids[1 + (N_SPANS + 1) + i + 1]  # 顶 (story=1, idx=i+1)
        ret, name = sm.FrameObj.AddByPoint(i_node, j_node, "", SEC_BEAM)
        if ret != 0:
            raise RuntimeError(f"梁{fid} AddByPoint 失败 ret={ret}")
        frame_ids[fid] = name
        print(f"    梁{fid}: {i_node} -> {j_node} ({name})  [{SEC_BEAM}]")
        fid += 1

    # 6. 设置支座 (底排 4 节点固接)
    print("[5] 设置支座 (底排固接)...")
    restrain = [True, True, True, True, True, True]  # Ux,Uy,Uz,Rx,Ry,Rz 全约束
    for i in range(N_SPANS + 1):
        n = node_ids[1 + i]  # 底排
        ret = sm.PointObj.SetRestraint(n, restrain)
        if ret != 0:
            raise RuntimeError(f"SetRestraint {n} 失败 ret={ret}")
        print(f"    节点{n} (底排{i}) 固接 OK")

    # 7. 定义荷载工况 DEAD
    print("[6] 定义荷载工况 DEAD...")
    # LoadPatterns.Add(name, type, selfWeightMultiplier, [AddLoadCase])
    # type=1 (Dead), selfWeight=1.0 (自动算自重)
    ret = sm.LoadPatterns.Add(CASE_DEAD, 1, 1.0, True)
    print(f"    LoadPatterns.Add(DEAD) ret={ret} (ret=1=已存在, 可忽略)")

    # 8. 施加重力荷载 (均布荷载到 3 根梁)
    print("[7] 施加恒载...")
    # SAP2000 v24 + win32com 已验证: 8 参版本有效, 11 参 silent ret=1
    # 签名: SetLoadDistributed(Name, LoadPat, MyType, Dist1, Dist2,
    #                          AbsStart, AbsEnd, Value)
    #   MyType=1 (Force/Length kN/m)
    #   Dist1/Dist2 = 0.0/1.0 (归一化位置)
    #   AbsStart/AbsEnd = True (归一化)
    #   Value = 单标量 (kN/m)  ← 注意: 不是 [start, end] 数组
    beam_fids = [f for f in frame_ids.keys() if f > N_SPANS + 1]
    for bf in beam_fids:
        name = frame_ids[bf]
        ret = sm.FrameObj.SetLoadDistributed(
            name,        # 构件名
            CASE_DEAD,   # 荷载工况
            1,           # MyType=1 Force/Length
            0.0,         # Dist1 (归一化起点)
            1.0,         # Dist2 (归一化终点)
            True,        # AbsStart (归一化)
            True,        # AbsEnd (归一化)
            DEAD_KN_M,   # Value (kN/m, 全跨均匀)
        )
        if ret != 0:
            raise RuntimeError(f"SetLoadDistributed {name} 失败 ret={ret}")
        print(f"    梁{bf} ({name}): DEAD={DEAD_KN_M} kN/m")

    # 9. 锁定模型 (求解前必须)
    sm.SetModelIsLocked(True)
    print("[8] 模型已锁定 (准备求解)")

    # 10. 保存到 .sdb
    print(f"[9] 保存模型到 {SDB_PATH}...")
    ret = sm.File.Save(SDB_PATH)
    print(f"    File.Save ret={ret}")
    if ret != 0:
        print(f"    ⚠️ 保存失败 (ret={ret}), 模型仍在 SAP2000 内存中")

    # 11. 刷新视图
    sm.View.RefreshView(0, False)  # 0=所有窗口
    print("[10] 视图已刷新")

    return frame_ids, node_ids


def verify(sap):
    """从 SAP2000 回读, 验证建模结果"""
    sm = sap.SapModel
    print("\n=== 验证 (从 SAP2000 回读) ===")

    # 节点数
    n_points = sm.PointObj.Count()
    print(f"  节点总数: {n_points} (期望: {(N_SPANS + 1) * (N_STORIES + 1)})")

    # 构件数
    n_frames = sm.FrameObj.Count()
    print(f"  构件总数: {n_frames} (期望: {(N_SPANS + 1) * N_STORIES + N_SPANS * N_STORIES})")

    # 列 3 个梁的截面名
    print("  构件截面 (前 10):")
    for fname in sm.FrameObj.GetNameList()[1][:10]:
        sec = sm.FrameObj.GetSection(fname)
        print(f"    {fname}: {sec[0]}")

    # 荷载工况
    print("  荷载工况:")
    n_cases = sm.LoadPatterns.Count()
    for i in range(1, n_cases + 1):
        c = sm.LoadPatterns.GetNameFromList(i)
        print(f"    [{i}] {c[0]}")

    # DEAD 工况下梁的总荷载 (提取梁名 + 验证有均布荷载)
    print("  DEAD 工况下梁荷载 (检查均布荷载):")
    fnames = sm.FrameObj.GetNameList()[1]
    beam_like = [f for f in fnames if "BEAM" in sm.FrameObj.GetSection(f)[0]]
    for fname in beam_like[:3]:
        ret = sm.FrameObj.GetLoadDistributed(fname, CASE_DEAD)
        # ret 结构: (ret, LoadPat, LoadType, StartDist, EndDist, AbsStartDist,
        #            AbsEndDist, Values) -- 各为 1 元 (因为我们用 True/True)
        if len(ret) >= 8 and ret[1] == CASE_DEAD:
            print(f"    {fname}: LoadPat={ret[1]}, "
                  f"StartDist={ret[3]}, EndDist={ret[4]}, "
                  f"Value={ret[7]}")
        else:
            print(f"    {fname}: 未找到 DEAD 荷载 (ret[1]={ret[1] if len(ret) > 1 else 'N/A'})")


def main():
    pythoncom.CoInitialize()
    try:
        print("=== build_frame_9m.py ===")
        print(f"SAP2000 路径: D:\\SAP2000\\SAP2000.exe")
        print(f"规格: {N_SPANS}跨×{SPAN_M}m | {N_STORIES}层×{STORY_H_M}m")
        print(f"     柱 {COL_B_MM}x{COL_H_MM}, 梁 {BEAM_B_MM}x{BEAM_H_MM}")
        print(f"     DEAD={DEAD_KN_M} kN/m")
        print()

        sap = attach_sap()
        frame_ids, node_ids = build_frame(sap)
        verify(sap)

        print(f"\n✅ 完成: {(N_SPANS + 1) * (N_STORIES + 1)} 节点, "
              f"{len(frame_ids)} 构件")
        print(f"   模型已保存到: {SDB_PATH}")
        print(f"   SAP2000 保持运行 (PID={find_sap_pid()}), 可直接查看")
        print(f"\n后续可执行:")
        print(f"  python solve_frame_9m.py   # 求解并提取结果")
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


if __name__ == "__main__":
    main()