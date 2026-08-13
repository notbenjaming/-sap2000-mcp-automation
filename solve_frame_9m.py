"""
solve_frame_9m.py
=================
求解 + 结果提取（针对 SAP2000 v24 + win32com）

已知 v24 SAFEARRAY bug (2026-07-01 验证):
  - Results.JointDispl/FrameForce/BaseReact/JointReact 全返回 (1, 0, None, ...)
  - 绕过方案: DatabaseTables.SetLoadCasesSelectedForDisplay(['DEAD'])
            + DatabaseTables.GetTableForDisplayCSVString(tableName, [], '')
            + csvString 在 ret[3] (不是 ret[1])
            + 表名需带 "Output - " 前缀 (例: "Output - Joint Displacements")

用法 (PowerShell):
  cd C:\\Users\\21574\\hermes_csi_system
  python solve_frame_9m.py

前置:
  - SAP2000 已启动, 模型已用 build_frame_9m.py 建模
  - .sdb 文件已存在
"""
import sys
import time
import csv
import io
import win32com.client
import pythoncom
import psutil


# ────────────────── 参数 ──────────────────
CASE_DEAD = "DEAD"
SEC_COL = "COL400x400"
SEC_BEAM = "BEAM200x500"
# C30 混凝土抗压强度设计值 f_c = 14.3 MPa
# 抗弯承载力简化公式 (单筋矩形): M_u ≈ 0.2 × f_c × b × h²
FC_MPA = 14.3


def find_sap_pid():
    """找 SAP2000 主进程 (排除 launcher < 100MB)"""
    cands = []
    for p in psutil.process_iter(['name', 'pid', 'memory_info']):
        if p.info.get('name') != 'SAP2000.exe':
            continue
        mb = p.info['memory_info'].rss / 1024 / 1024
        if mb < 100:
            continue
        cands.append((p.info['pid'], mb))
    if not cands:
        return None
    cands.sort(key=lambda x: x[1], reverse=True)
    return cands[0][0]


def attach_sap():
    """附加到用户已启动的 SAP2000"""
    pid = find_sap_pid()
    if not pid:
        raise RuntimeError(
            "未找到 SAP2000 主进程。请先启动 SAP2000, 等内存稳定后跑 build。"
        )
    print(f"[attach] SAP2000 PID={pid}")
    helper = win32com.client.Dispatch("SAP2000v1.Helper")
    sap = helper.GetObjectProcess("CSI.SAP2000.API.SapObject", str(pid))
    print(f"[attach] 已附加")
    return sap


def run_analysis(sm):
    """运行分析 - 必须先 Save 再 RunAnalysis (v24 quirk)"""
    print("\n=== 求解 ===")

    # 0. 解锁模型 (Results API 需要)
    sm.SetModelIsLocked(False)
    print("  模型已解锁")

    # 1. 保存 (RunAnalysis 要求模型有文件名)
    print("  File.Save...")
    ret = sm.File.Save()
    print(f"    ret={ret}")
    if ret != 0:
        # 模型未保存过, 需要指定路径
        import os
        save_path = r"C:\\Users\\21574\\hermes_csi_system\\frame_3span_9m.sdb"
        if not os.path.exists(save_path):
            sm.File.Save(save_path)
        else:
            sm.File.Save(save_path)
        print(f"    已保存到 {save_path}")

    # 2. 求解
    print("  Analyze.RunAnalysis...")
    t0 = time.time()
    ret = sm.Analyze.RunAnalysis()
    elapsed = time.time() - t0
    print(f"    ret={ret}, 耗时 {elapsed:.1f}s")
    if ret != 0:
        raise RuntimeError(f"RunAnalysis 失败 ret={ret}")

    # 3. 等分析结果写盘 (数据库刷新)
    time.sleep(3)
    print("  ✓ 分析完成")


def get_csv(sm, table_name, case=CASE_DEAD):
    """从 SAP2000 DatabaseTables 读 CSV 字符串

    v24 准则:
      - 必须先 SetLoadCasesSelectedForDisplay([case])
      - 表名带 "Output - " 前缀
      - 返回值 ret = (ret_code, fill_done, n_records, csvString, ...)
      - csvString 在 ret[3]
    """
    # 1. 选工况
    sm.DatabaseTables.SetLoadCasesSelectedForDisplay([case])

    # 2. 取 CSV
    # 签名: GetTableForDisplayCSVString(TableName, GroupName, TableVersion)
    # GroupName=[] (空=所有), TableVersion='' (默认)
    ret = sm.DatabaseTables.GetTableForDisplayCSVString(
        table_name, [], '')

    if not isinstance(ret, tuple) or len(ret) < 4:
        raise RuntimeError(
            f"GetTableForDisplayCSVString 返回格式异常: {type(ret)}")

    csv_string = ret[3]
    n_records = ret[2] if len(ret) > 2 else 0
    print(f"  [{table_name}] ret={ret[0]}, n_records={n_records}, "
          f"csv_len={len(csv_string) if csv_string else 0}")
    if not csv_string:
        return [], []
    return parse_csv(csv_string)


def parse_csv(csv_string):
    """解析 SAP2000 输出的 CSV 字符串 → (header, rows)"""
    reader = csv.reader(io.StringIO(csv_string))
    rows = [row for row in reader if row]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def extract_displacements(sm):
    """提取节点位移 - 用 'Joint Displacements' (带 Output - 前缀?)"""
    print("\n=== 节点位移 (DEAD) ===")

    # 尝试多种表名格式
    candidates = [
        "Output - Joint Displacements",
        "Joint Displacements",
    ]
    for tbl in candidates:
        try:
            header, rows = get_csv(sm, tbl)
            if rows:
                print(f"  表: {tbl}, 字段: {header}")
                print(f"  记录数: {len(rows)}")

                # Joint, OutputCase, CaseType, StepType, StepNum, StepLabel,
                # U1, U2, U3, R1, R2, R3
                # 找 U1/U2/U3 列索引
                try:
                    i_joint = header.index("Joint")
                    i_u1 = header.index("U1")
                    i_u2 = header.index("U2")
                    i_u3 = header.index("U3")
                except ValueError as e:
                    print(f"  ⚠️ 字段缺失: {e}")
                    continue

                joints_data = {}
                max_disp_mm = 0.0
                max_disp_joint = None

                for row in rows:
                    if len(row) <= max(i_joint, i_u3):
                        continue
                    jname = row[i_joint]
                    try:
                        u1 = float(row[i_u1]) if row[i_u1] else 0.0
                        u2 = float(row[i_u2]) if row[i_u2] else 0.0
                        u3 = float(row[i_u3]) if row[i_u3] else 0.0
                    except ValueError:
                        continue

                    joints_data[jname] = {"U1": u1, "U2": u2, "U3": u3}
                    disp_mm = abs(u3) * 1000.0  # m → mm
                    if disp_mm > abs(max_disp_mm):
                        max_disp_mm = disp_mm
                        max_disp_joint = jname

                print(f"\n  节点位移汇总 (前 12):")
                for jn, d in list(joints_data.items())[:12]:
                    print(f"    {jn}: U1={d['U1']*1000:+.4f}mm, "
                          f"U2={d['U2']*1000:+.4f}mm, "
                          f"U3={d['U3']*1000:+.4f}mm")

                print(f"\n  最大竖向位移: |U3| = {max_disp_mm:.4f} mm "
                      f"@ {max_disp_joint}")
                return joints_data, max_disp_mm, max_disp_joint
        except Exception as e:
            print(f"  [{tbl}] 失败: {e}")
            continue

    return {}, 0.0, None


def extract_frame_forces(sm):
    """提取构件内力 - 'Element Forces - Frames'"""
    print("\n=== 构件内力 (DEAD) ===")

    candidates = [
        "Output - Element Forces - Frames",
        "Element Forces - Frames",
    ]
    for tbl in candidates:
        try:
            header, rows = get_csv(sm, tbl)
            if rows:
                print(f"  表: {tbl}, 字段: {header[:6]}...")
                print(f"  记录数: {len(rows)}")

                # Frame, Station, OutputCase, CaseType, P, V2, V3, T, M2, M3,
                # FrameElem, ElemStation
                try:
                    i_frame = header.index("Frame")
                    i_p = header.index("P")
                    i_v2 = header.index("V2")
                    i_m2 = header.index("M2")
                    i_m3 = header.index("M3")
                except ValueError as e:
                    print(f"  ⚠️ 字段缺失: {e}")
                    continue

                # 每根构件取极值
                frame_max = {}
                for row in rows:
                    if len(row) <= i_m3:
                        continue
                    fn = row[i_frame]
                    try:
                        p = float(row[i_p]) if row[i_p] else 0.0
                        v2 = float(row[i_v2]) if row[i_v2] else 0.0
                        m2 = float(row[i_m2]) if row[i_m2] else 0.0
                        m3 = float(row[i_m3]) if row[i_m3] else 0.0
                    except ValueError:
                        continue

                    if fn not in frame_max:
                        frame_max[fn] = {"P": p, "V2": v2, "M2": m2, "M3": m3,
                                         "P_max": abs(p),
                                         "M2_max": abs(m2),
                                         "M3_max": abs(m3)}
                    else:
                        cur = frame_max[fn]
                        for k, v in [("P", p), ("V2", v2), ("M2", m2), ("M3", m3)]:
                            if abs(v) > abs(cur[k]):
                                cur[k] = v
                        cur["P_max"] = max(cur["P_max"], abs(p))
                        cur["M2_max"] = max(cur["M2_max"], abs(m2))
                        cur["M3_max"] = max(cur["M3_max"], abs(m3))

                # 计算利用率
                print(f"\n  构件内力 (极值):")
                utils = {}
                for fn in sorted(frame_max.keys()):
                    f = frame_max[fn]
                    # 拿构件截面 (区分柱/梁)
                    try:
                        sec_name = sm.FrameObj.GetSection(fn)[0]
                    except Exception:
                        sec_name = "?"
                    if SEC_COL in sec_name:
                        b_mm, h_mm = 400, 400
                    elif SEC_BEAM in sec_name:
                        b_mm, h_mm = 200, 500
                    else:
                        b_mm, h_mm = 400, 400

                    # M_u = 0.2 × f_c × b × h² (kN·m, b/h 单位 m)
                    b_m, h_m = b_mm / 1000.0, h_mm / 1000.0
                    mu_knm = 0.2 * (FC_MPA * 1000.0) * b_m * h_m * h_m
                    m_max = max(f["M2_max"], f["M3_max"])
                    util = m_max / mu_knm if mu_knm > 0 else 0

                    utils[fn] = {"section": sec_name, "b": b_mm, "h": h_mm,
                                 "M_u": mu_knm, "M_max": m_max, "util": util,
                                 **f}
                    print(f"    {fn}: P={f['P']:+.2f}kN, V2={f['V2']:+.2f}kN, "
                          f"M2={f['M2']:+.2f}kN·m, M3={f['M3']:+.2f}kN·m  "
                          f"[{sec_name}, 利用率={util*100:.1f}%]")

                if utils:
                    max_fn = max(utils.keys(),
                                 key=lambda k: utils[k]["util"])
                    mu = utils[max_fn]
                    print(f"\n  最大利用率: {max_fn} = {mu['util']*100:.1f}%")
                    print(f"    截面: {mu['section']} ({mu['b']}x{mu['h']} mm)")
                    print(f"    M_max={mu['M_max']:.2f} kN·m, "
                          f"M_u={mu['M_u']:.2f} kN·m")

                return frame_max, utils
        except Exception as e:
            print(f"  [{tbl}] 失败: {e}")
            continue

    return {}, {}


def main():
    pythoncom.CoInitialize()
    try:
        print("=== solve_frame_9m.py ===\n")

        sap = attach_sap()
        sm = sap.SapModel

        # 检查模型存在
        n_frames = sm.FrameObj.Count()
        n_points = sm.PointObj.Count()
        print(f"\n当前 SAP2000 模型: {n_points} 节点, {n_frames} 构件")
        if n_frames == 0:
            raise RuntimeError("模型为空, 请先跑 build_frame_9m.py")

        run_analysis(sm)

        joints, max_disp, max_joint = extract_displacements(sm)
        forces, utils = extract_frame_forces(sm)

        # 总结
        print("\n" + "=" * 50)
        print("=== 总结 (DEAD 工况) ===")
        print(f"  最大竖向位移: {max_disp:.4f} mm @ {max_joint}")
        if utils:
            max_fn = max(utils.keys(), key=lambda k: utils[k]["util"])
            mu = utils[max_fn]
            print(f"  最大利用率: {max_fn} ({mu['section']}) = "
                  f"{mu['util']*100:.1f}%")
            print(f"    M_max={mu['M_max']:.2f} kN·m, M_u={mu['M_u']:.2f} kN·m")
        print("=" * 50)
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


if __name__ == "__main__":
    main()