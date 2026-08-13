"""
SAP2000 交互式修改脚本（支持状态持久化）
=========================================
每次运行从 JSON 文件加载 IR 状态，确保跨进程一致性。

用法：
    python modify_model.py <指令>

前置条件：
    SAP2000 必须已启动

作者：MiniMax-M3 / Hermes CSI System
版本：v2.0.0
"""
import sys, os, json, tempfile, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

_STATE_FILE = os.path.join(tempfile.gettempdir(), "sap2000_ir_state.json")


# ─────────────────────────────────────────────────────────────
# 第 1 层：IR 序列化 / 反序列化
# ─────────────────────────────────────────────────────────────

def load_ir_state():
    """从 JSON 文件加载 IR 状态"""
    if not os.path.exists(_STATE_FILE):
        return None
    with open(_STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_ir_state(state):
    """保存 IR 状态到 JSON 文件"""
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def build_ir_from_state(state):
    """从状态字典重建 Pydantic IR 对象"""
    from ir_compiler import (
        StructuralIR, Node, Frame, Section, SectionType,
        LoadCase, LoadCaseType,
        PointLoad, DistributedLoad,
    )

    nodes = [Node(
        id=n["id"], x=n["x"], y=n["y"], z=n["z"],
        restrain=n.get("restrain")
    ) for n in state["nodes"]]

    frames = [Frame(
        id=f["id"], i_node=f["i_node"], j_node=f["j_node"],
        section=f["section"], role=f["role"]
    ) for f in state["frames"]]

    sections = [Section(
        name=s["name"],
        type=SectionType(s["type"]),
        depth=float(s.get("depth", s["rect_h"])),
        width=float(s.get("width", s["rect_b"])),
        rect_h=float(s["rect_h"]), rect_b=float(s["rect_b"]),
        material=s.get("material", "C30"),
    ) for s in state["sections"]]

    dist_loads = [DistributedLoad(
        frame_id=d["frame_id"], case=d["case"],
        load_type=d.get("load_type", "DEAD"),
        value=d["value"]
    ) for d in state.get("dist_loads", [])]

    return StructuralIR(
        model_id=state["model_id"],
        nodes=nodes,
        frames=frames,
        sections=sections,
        dist_loads=dist_loads,
        point_loads=[],
        load_cases=[
            LoadCase(name="DEAD", type=LoadCaseType.DEAD, self_weight=1.0),
            LoadCase(name="LIVE", type=LoadCaseType.LIVE, self_weight=0.0),
        ],
    )


def state_from_ir(ir):
    """从 IR 对象序列化为字典"""
    return {
        "model_id": ir.model_id,
        "nodes": [{"id": n.id, "x": n.x, "y": n.y, "z": n.z,
                   "restrain": n.restrain} for n in ir.nodes],
        "frames": [{"id": f.id, "i_node": f.i_node, "j_node": f.j_node,
                    "section": f.section, "role": f.role} for f in ir.frames],
        "sections": [{"name": s.name, "type": s.type.value,
                      "rect_h": s.rect_h, "rect_b": s.rect_b,
                      "material": getattr(s, "material", "C30")} for s in ir.sections],
        "dist_loads": [{"frame_id": d.frame_id, "case": d.case,
                        "value": d.value} for d in ir.dist_loads],
    }


# ─────────────────────────────────────────────────────────────
# 第 2 层：指令处理
# ─────────────────────────────────────────────────────────────

def cmd_init():
    """初始化 IR 状态（不动 SAP2000）

    Batch 6 改造：
    - 不再主动启动/关闭 SAP2000
    - 只生成初始 IR 并保存到 JSON
    - 提示用户：在 SAP2000 GUI 中建好模型后，用 `sync` 拉取
    """
    from ir_compiler import build_sample_frame_ir

    ir = build_sample_frame_ir()
    save_ir_state(state_from_ir(ir))

    return (
        f"✅ 初始 IR 状态已生成（8 节点 / 7 构件 / 2 截面 / 6 荷载）\n"
        f"   状态文件: {_STATE_FILE}\n"
        f"\n"
        f"💡 后续步骤（请手动操作 SAP2000）:\n"
        f"  1. 在 SAP2000 GUI 中建好相同的模型（节点 1-8、构件 1-7、截面 COL400x400 + BEAM300x600）\n"
        f"  2. 保存为 .sdb 文件\n"
        f"  3. 运行 `python modify_model.py sync` 把 SAP2000 状态拉取到 IR\n"
        f"  4. 然后用 `改梁5荷载为-25` 等指令修改（会自动 sync）\n"
        f"\n"
        f"   或者如果你已经有现成模型在 SAP2000 里：\n"
        f"     - 打开 SAP2000 + 加载 .sdb\n"
        f"     - 运行 `sync`（IR 会被覆盖为 SAP2000 真实状态）"
    )


def cmd_show(target="all"):
    """显示 IR 状态"""
    state = load_ir_state()
    if not state:
        return "⚠️ 未找到保存的 IR 状态，请先运行 init"

    lines = []
    if target in ("all", "模型"):
        lines = [
            f"  模型: {state['model_id']}",
            f"  节点: {len(state['nodes'])} 个",
            f"  构件: {len(state['frames'])} 个",
            f"  截面: {len(state['sections'])} 个",
            f"  荷载: {len(state.get('dist_loads', []))} 个均布",
        ]
    elif target in ("荷载", "loads"):
        dls = state.get("dist_loads", [])
        if not dls:
            lines = ["  无均布荷载"]
        else:
            lines = ["  均布荷载:"]
            for d in dls:
                lines.append(f"    梁{d['frame_id']}  {d['case']}  {d['value']} kN/m")
    elif target in ("截面", "sections"):
        lines = ["  截面:"]
        for s in state["sections"]:
            lines.append(f"    {s['name']}: {s['rect_h']}x{s['rect_b']} mm  材料={s.get('material','C30')}")
    elif target in ("节点",):
        lines = ["  节点:"]
        for n in state["nodes"]:
            r = "固接" if n.get("restrain") else "自由"
            lines.append(f"    节点 {n['id']}: ({n['x']}, {n['y']}, {n['z']})  {r}")
    elif target in ("构件", "frames"):
        lines = ["  构件:"]
        for f in state["frames"]:
            lines.append(f"    构件 {f['id']}: {f['i_node']} → {f['j_node']}  [{f['role']}]  截面={f['section']}")
    elif target in ("内力", "forces", "frame_forces"):
        # 从 SAP2000 提取 Frame Forces（需要先 solve）
        from sap2000_worker import SAP2000Worker, SAP2000Config
        import pythoncom
        import concurrent.futures
        import time
        config = SAP2000Config(sap2000_path=r"D:\SAP2000\SAP2000.exe")
        worker = SAP2000Worker(config)
        existing_pid, existing_mem = _find_existing_sap_pid()
        if not (existing_pid and existing_mem and existing_mem > 80):
            return "❌ SAP2000 未运行"
        worker.connection.start()
        pythoncom.CoInitialize()
        try:
            sap = worker.connection.sap_object.SapModel
            sap.SetModelIsLocked(False)

            # Lock/Unlock 触发 SAP2000 内部刷新（重要！）
            sap.SetModelIsLocked(True)
            sap.SetModelIsLocked(False)

            # 重新 RunAnalysis（确保 DB Tables 有结果）
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(lambda: sap.Analyze.RunAnalysis())
                try:
                    future.result(timeout=30)
                except:
                    pass
            time.sleep(3)

            sap.DatabaseTables.SetLoadCasesSelectedForDisplay(['DEAD'])

            frame_forces, err = _read_frame_forces_from_sap(sap, attempts=15)
            if not frame_forces:
                return f"❌ 无法提取 Frame Forces: {err}"

            sections_dict = {s["name"]: (s.get("rect_h", 0), s.get("rect_b", 0))
                            for s in state.get("sections", [])}
            utilizations = _compute_frame_utilization(frame_forces, sections_dict)

            lines = ["  构件内力 (DEAD 工况, 极值):"]
            for fid in sorted(frame_forces.keys(), key=lambda x: int(x) if x.isdigit() else 999):
                f = frame_forces[fid]
                u = utilizations.get(fid, {})
                util_str = f", 利用率 {u['utilization']*100:.1f}%" if u else ""
                lines.append(f"    框架 {fid}: P={f['P']:.2f}kN, V2={f['V2']:.2f}kN, M3={f['M3']:.2f}kN·m{util_str}")

            if utilizations:
                max_fid = max(utilizations.keys(), key=lambda k: utilizations[k]['utilization'])
                max_u = utilizations[max_fid]
                lines.append(f"")
                lines.append(f"  最大利用率: 框架 {max_fid} = {max_u['utilization']*100:.1f}%")
                lines.append(f"  截面 {max_u['section']} ({max_u['b']}x{max_u['h']} mm)  M_u={max_u['M_u_knm']:.1f} kN·m")
                lines.append(f"  M_max={max_u['M_max_knm']:.1f} kN·m  (C30 混凝土, 简化公式)")
        finally:
            try:
                pythoncom.CoUninitialize()
            except:
                pass
    else:
        lines = [f"  未知: {target}"]
    return "\n".join(lines)


def cmd_modify(cmd_text):
    """修改 IR + 同步到 SAP2000

    Batch 6 新增：修改前自动检测 IR 与 SAP2000 是否一致
    如果发现冲突（用户 GUI 改过），自动同步 SAP2000 → IR
    """
    from ir_compiler import Section, SectionType
    from ir_nlp import IRCommandParser, CommandType
    from ir_diff import scale_all_dist_loads, apply_modifications, apply_modification, Modification
    from sap2000_worker import SAP2000Worker, SAP2000Config

    state = load_ir_state()
    if not state:
        return "⚠️ 未找到保存的 IR 状态，请先运行 init"

    # Batch 6: 冲突检测 - 修改前先对比 IR 和 SAP2000
    conflict_warning = _check_sap_conflicts()
    if conflict_warning and "发现" in conflict_warning:
        # 有冲突 → 自动 sync（SAP2000 → IR）
        sync_result = cmd_sync()
        state = load_ir_state()  # 重新加载
        if not state:
            return "⚠️ IR 状态丢失"
        # 继续修改（用新同步的 IR）
        prefix = f"⚠️ 检测到 IR 与 SAP2000 不一致，已自动 sync：\n{sync_result}\n\n"
    else:
        prefix = ""

    ir = build_ir_from_state(state)
    parser = IRCommandParser()
    cmd = parser.parse(cmd_text)

    if cmd.command_type == CommandType.UNKNOWN:
        return prefix + f"⚠️ 无法理解: {cmd.error}"

    if cmd.command_type == CommandType.SCALE_LOAD:
        factor = cmd.args["factor"]
        case = cmd.args["case"]
        mods = scale_all_dist_loads(ir, case, factor)
        if not mods:
            return prefix + f"⚠️ 未找到 {case} 工况的均布荷载"
        apply_modifications(ir, mods)
        result = _sync_to_sap2000(ir)
        return result

    elif cmd.command_type == CommandType.SET_SECTION:
        h = int(cmd.args["height"])
        b = int(cmd.args["width"])
        entity_kind = cmd.args["entity_kind"]  # "柱" / "梁"
        entity_id = cmd.args["entity_id"]
        new_sec_name = f"{entity_kind}_{h}x{b}"

        # 1. 创建/更新截面定义
        existing = next((s for s in ir.sections if s.name == new_sec_name), None)
        if existing:
            existing.rect_h = h
            existing.rect_b = b
        else:
            # 找原始截面获取材质
            orig_sec_name = next(
                (f.section for f in ir.frames
                 if f.role == entity_kind and f.id == entity_id),
                "COL400x400"
            )
            orig_sec = next((s for s in ir.sections if s.name == orig_sec_name), None)
            material = orig_sec.material if orig_sec else "C30"
            new_sec = Section(
                name=new_sec_name,
                type=SectionType.CONCRETE_RECT,
                rect_h=float(h), rect_b=float(b),
                material=material,
            )
            ir.sections.append(new_sec)

        # 2. 更新 frame 的 section 引用
        mod = Modification(
            entity_type="frame", entity_id=entity_id,
            field_name="section", new_value=new_sec_name,
        )
        ok, msg = apply_modification(ir, mod)
        if not ok:
            return f"❌ {msg}"

        result = _sync_to_sap2000(ir)
        return prefix + f"✅ 柱 {entity_id} 截面改为 {h}x{b} mm，模型已更新\n{result}"

    elif cmd.command_type == CommandType.SET_LOAD:
        frame_id = cmd.args["frame_id"]
        case = cmd.args["case"]
        value = cmd.args["value"]
        mod = Modification(
            entity_type="dist_load",
            entity_id=(frame_id, case),
            field_name="value", new_value=value,
        )
        ok, msg = apply_modification(ir, mod)
        if not ok:
            return f"❌ {msg}"
        result = _sync_to_sap2000(ir)
        return prefix + f"✅ 梁 {frame_id} {case} 荷载改为 {value} kN/m，模型已更新\n{result}"

    elif cmd.command_type == CommandType.REMOVE_NODE:
        return "⚠️ 删除节点暂未实现"

    elif cmd.command_type == CommandType.SHOW:
        return cmd_show(cmd.args.get("target", "all"))

    elif cmd.command_type == CommandType.SOLVE:
        return cmd_solve()

    elif cmd.command_type == CommandType.EXIT:
        return cmd_exit()

    else:
        return prefix + f"⚠️ 指令 {cmd.command_type} 暂未实现"


def _sync_to_sap2000(ir):
    """将 IR 同步到 SAP2000（复用已有连接，不关闭）"""
    from sap2000_worker import SAP2000Worker, SAP2000Config
    import json as _json

    config = SAP2000Config(
        sap2000_path=r"D:\SAP2000\SAP2000.exe",
        service_path=r"D:\SAP2000\CSiAPIService.exe",
        service_port=11650,
    )

    # 加载旧的 IR（从状态文件）
    state = load_ir_state()
    ir_old = build_ir_from_state(state)

    worker = SAP2000Worker(config)

    try:
        # 检测 SAP2000 是否已运行（排除 launcher）
        existing_pid, existing_mem = _find_existing_sap_pid()
        if existing_pid and existing_mem and existing_mem > 80:
            # 复用已有 SAP2000（主进程，>200MB）
            worker.connection.start()  # attach 到已有进程
            worker.builder = _reinit_builder(worker.connection)
            worker.analyzer = None
            worker.current_ir = ir_old
            print(f"已连接到运行中的 SAP2000 (PID={existing_pid}, {existing_mem:.0f}MB)")
        else:
            # launcher 或无进程：杀掉后重建（慢但正确）
            if existing_pid:
                print(f"⚠️ 检测到 launcher (PID={existing_pid}, {existing_mem:.0f}MB)，杀掉后重建模型")
                import psutil
                try:
                    psutil.Process(existing_pid).kill()
                    print("launcher 已终止")
                except Exception as e:
                    print(f"警告：无法终止 launcher: {e}")
            else:
                print("SAP2000 未运行，将启动新实例")

            worker.build_initial_model(ir_old)

        # 增量更新
        summary = worker.update_model(ir)
    except RuntimeError as e:
        msg = str(e)
        if "launcher" in msg or "超时" in msg:
            # 清理残留 launcher
            import psutil
            for proc in psutil.process_iter(['name', 'pid']):
                if proc.info['name'] == 'SAP2000.exe':
                    try:
                        psutil.Process(proc.info['pid']).kill()
                    except:
                        pass
            return (
                f"⚠️ Helper 无法自动启动 SAP2000（启动卡住）。\n"
                f"\n"
                f"请手动操作：\n"
                f"  1. 关闭所有 SAP2000 窗口\n"
                f"  2. 双击 D:\\SAP2000\\SAP2000.exe\n"
                f"  3. 等待内存稳定在 ~500MB（约 1 分钟）\n"
                f"  4. 回到这里，重新发送指令\n"
            )
        raise

    # ⚠️ 不要 stop()！保留 SAP2000 连接供下次使用
    # worker.connection.stop()

    # 保存新状态
    save_ir_state(state_from_ir(ir))

    lines = [f"   更新统计: {summary}"]
    if worker._last_model_path:
        lines.append(f"   文件: {worker._last_model_path}")
    lines.append("   💡 SAP2000 保持运行，可直接查看模型变化")
    return "\n".join(lines)


def _find_existing_sap_pid():
    """检测 SAP2000 主进程（排除 launcher）

    SAP2000 launcher: 约 40 MB，name='SAP2000.exe'
    SAP2000 主进程: 约 500+ MB，name='SAP2000.exe'
    """
    import psutil
    candidates = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
        try:
            if 'SAP2000' not in proc.info['name']:
                continue
            mem_mb = proc.info['memory_info'].rss / 1024 / 1024
            candidates.append((proc.info['pid'], mem_mb))
        except Exception:
            pass
    if not candidates:
        return None, None
    # 选内存最大的（主进程）
    candidates.sort(key=lambda x: x[1], reverse=True)
    pid, mem = candidates[0]
    return pid, mem


def _reinit_builder(connection):
    """重建 Builder（复用已有连接）"""
    import pythoncom
    from sap2000_worker import SAP2000ModelBuilder
    pythoncom.CoInitialize()
    try:
        return SAP2000ModelBuilder(connection.sap_object, None)
    finally:
        pythoncom.CoUninitialize()


# ─────────────────────────────────────────────────────────────
# Batch 7：Frame Forces 提取 + 利用率计算
# ─────────────────────────────────────────────────────────────

# C30 混凝土抗压强度设计值 f_c = 14.3 MPa
# 抗弯承载力简化公式（单筋矩形截面）：
#   M_u ≈ 0.2 × f_c × b × h²  （单位：N·mm）
_FC_MPA = 14.3  # C30 混凝土


def _concrete_capacity(b_mm, h_mm, fc_mpa=_FC_MPA):
    """混凝土矩形截面抗弯承载力 (kN·m)"""
    b_m = b_mm / 1000.0
    h_m = h_mm / 1000.0
    fc_kpa = fc_mpa * 1000.0  # kPa
    mu_knm = 0.2 * fc_kpa * b_m * h_m * h_m  # kN·m
    return mu_knm


def _read_frame_forces_from_sap(sap, attempts=15):
    """从 SAP2000 读 Frame Forces，返回 dict: {frame_id: {P, V2, M2, M3, ...}} 极值

    返回: (frame_forces_dict, error_msg)
    """
    import time
    # 'Element Forces - Frames' 字段:
    # Frame(0), Station(1), OutputCase(2), CaseType(3),
    # P(4), V2(5), V3(6), T(7), M2(8), M3(9),
    # FrameElem(10), ElemStation(11)
    table_key = 'Element Forces - Frames'

    for attempt in range(attempts):
        try:
            ret = sap.DatabaseTables.GetTableForDisplayArray(
                table_key, [''], 'All'
            )
            if (len(ret) >= 6 and ret[5] is not None
                and isinstance(ret[5], (list, tuple)) and len(ret[5]) > 0):
                first = ret[5][0]
                n_records = int(ret[4]) if ret[4] else 0
                if first == '1' and n_records > 0:
                    # 提取每根构件的极值
                    frame_forces = {}
                    for i in range(n_records):
                        base = i * 12
                        if base + 12 > len(ret[5]):
                            break
                        row = ret[5][base:base+12]
                        f_id = str(row[0])
                        try:
                            P = float(row[4]) if row[4] else 0
                            V2 = float(row[5]) if row[5] else 0
                            V3 = float(row[6]) if row[6] else 0
                            T = float(row[7]) if row[7] else 0
                            M2 = float(row[8]) if row[8] else 0
                            M3 = float(row[9]) if row[9] else 0
                        except (ValueError, TypeError):
                            continue

                        # 极值（绝对值最大）
                        if f_id not in frame_forces:
                            frame_forces[f_id] = {
                                'P': P, 'V2': V2, 'V3': V3, 'T': T, 'M2': M2, 'M3': M3,
                                'P_max': abs(P), 'M2_max': abs(M2), 'M3_max': abs(M3),
                            }
                        else:
                            cur = frame_forces[f_id]
                            cur['P'] = max(cur['P'], P, key=abs) if abs(P) > abs(cur['P']) else cur['P']
                            cur['V2'] = max(cur['V2'], V2, key=abs) if abs(V2) > abs(cur['V2']) else cur['V2']
                            cur['V3'] = max(cur['V3'], V3, key=abs) if abs(V3) > abs(cur['V3']) else cur['V3']
                            cur['T'] = max(cur['T'], T, key=abs) if abs(T) > abs(cur['T']) else cur['T']
                            cur['M2'] = max(cur['M2'], M2, key=abs) if abs(M2) > abs(cur['M2']) else cur['M2']
                            cur['M3'] = max(cur['M3'], M3, key=abs) if abs(M3) > abs(cur['M3']) else cur['M3']
                            cur['P_max'] = max(cur['P_max'], abs(P))
                            cur['M2_max'] = max(cur['M2_max'], abs(M2))
                            cur['M3_max'] = max(cur['M3_max'], abs(M3))
                    return frame_forces, None
        except Exception as e:
            pass
        time.sleep(0.3)

    return None, f"无法从 SAP2000 提取 Frame Forces（{attempts} 次重试后仍为空）"


def _compute_frame_utilization(frame_forces, sections):
    """计算每根构件的利用率

    sections: dict {name: (h, b)}
    frame_forces: {fid: {P, M2, M3, M2_max, M3_max, ...}}
    """
    if not frame_forces or not sections:
        return {}

    state = load_ir_state()
    if not state:
        return {}

    # 加载构件-截面对应关系
    frame_sections = {f["id"]: f.get("section") for f in state.get("frames", [])}

    utilizations = {}
    for f_id, forces in frame_forces.items():
        sec_name = frame_sections.get(f_id)
        if not sec_name or sec_name not in sections:
            continue

        h, b = sections[sec_name]
        if h <= 0 or b <= 0:
            continue

        mu_knm = _concrete_capacity(b, h)

        # 取 M2 和 M3 中较大者（弯矩组合）
        M_max_knm = max(forces.get('M2_max', 0), forces.get('M3_max', 0))

        # 利用率 = M_max / M_u
        util = M_max_knm / mu_knm if mu_knm > 0 else 0

        utilizations[f_id] = {
            'section': sec_name,
            'h': h, 'b': b,
            'M_u_knm': mu_knm,
            'M_max_knm': M_max_knm,
            'utilization': util,
            'P': forces.get('P', 0),
            'V2': forces.get('V2', 0),
            'M2': forces.get('M2', 0),
            'M3': forces.get('M3', 0),
        }

    return utilizations


def cmd_solve():
    """求解（连接已有 SAP2000，使用 DatabaseTables API 提取结果）"""
    from sap2000_worker import SAP2000Worker, SAP2000Config

    config = SAP2000Config(sap2000_path=r"D:\SAP2000\SAP2000.exe")

    print("正在连接 SAP2000...")
    worker = SAP2000Worker(config)

    existing_pid, existing_mem = _find_existing_sap_pid()
    if existing_pid and existing_mem and existing_mem > 80:
        worker.connection.start()
        from modify_model import _reinit_builder
        worker.builder = _reinit_builder(worker.connection)
        state = load_ir_state()
        if state:
            ir = build_ir_from_state(state)
            worker.current_ir = ir
    else:
        return "❌ SAP2000 未运行，请先执行其他指令启动它"

    # 完整流程
    print("开始求解...")
    import pythoncom
    import concurrent.futures
    import time

    try:
        pythoncom.CoInitialize()
        sap = worker.connection.sap_object.SapModel

        # 1. 解锁
        sap.SetModelIsLocked(False)

        # 2. File.Save（新线程）
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(lambda: sap.File.Save())
            try:
                r = future.result(timeout=20)
                print(f"  File.Save ret={r}")
            except concurrent.futures.TimeoutError:
                print("  File.Save timeout - 继续")

        time.sleep(1)

        # 3. RunAnalysis
        r = sap.Analyze.RunAnalysis()
        print(f"  RunAnalysis ret={r}")

        time.sleep(3)

        # 4. 设置输出工况
        sap.DatabaseTables.SetLoadCasesSelectedForDisplay(['DEAD'])

        # 多次尝试提取数据
        max_disp_mm = 0.0
        disp_count = 0
        joints_data = {}

        for attempt in range(15):
            try:
                ret = sap.DatabaseTables.GetTableForDisplayArray(
                    'Joint Displacements', [''], 'All'
                )
                if len(ret) >= 6 and ret[5] is not None and isinstance(ret[5], (list, tuple)) and len(ret[5]) > 0:
                    first_data = ret[5][0]
                    n_records = int(ret[4]) if ret[4] else 0

                    if first_data == '1' and n_records > 0:
                        # 实际结构：ret[5] 是 96 元素扁平元组
                        # (8 节点 × 12 字段)
                        # 字段: Joint, OutputCase, CaseType, StepType, StepNum,
                        #        StepLabel, U1, U2, U3, R1, R2, R3
                        fields_per_row = 12
                        total_len = len(ret[5])

                        for j in range(n_records):
                            base = j * fields_per_row
                            if base + fields_per_row <= total_len:
                                jid = str(ret[5][base + 0])
                                u1 = float(ret[5][base + 6]) if ret[5][base + 6] else 0
                                u2 = float(ret[5][base + 7]) if ret[5][base + 7] else 0
                                u3 = float(ret[5][base + 8]) if ret[5][base + 8] else 0
                                joints_data[jid] = {'U1': u1, 'U2': u2, 'U3': u3}
                                disp_mag = (u1**2 + u2**2 + u3**2)**0.5
                                if disp_mag > abs(max_disp_mm):
                                    max_disp_mm = disp_mag
                                disp_count += 1
                        break
            except Exception as e:
                pass
            time.sleep(0.3)

        out = (
            f"✅ 求解完成\n"
            f"  最大位移: {max_disp_mm*1000:.4f} mm\n"
            f"  位移测点: {disp_count} 个\n"
        )
        if joints_data:
            out += "\n  位移详情 (DEAD 工况):\n"
            for jid in sorted(joints_data.keys(), key=lambda x: int(x) if x.isdigit() else 999):
                d = joints_data[jid]
                out += f"    节点 {jid}: U1={d['U1']*1000:.4f}mm, U2={d['U2']*1000:.4f}mm, U3={d['U3']*1000:.4f}mm\n"
        else:
            out += "  ⚠️ 未获取到位移数据（SAP2000 v24 OAPI 内部问题，重试）\n"

        # Batch 7: 提取 Frame Forces
        frame_forces, ff_err = _read_frame_forces_from_sap(sap, attempts=15)
        if frame_forces:
            # 加载截面信息
            state_now = load_ir_state()
            sections_dict = {}
            if state_now:
                for s in state_now.get("sections", []):
                    sections_dict[s["name"]] = (s.get("rect_h", 0), s.get("rect_b", 0))

            # 计算利用率
            utilizations = _compute_frame_utilization(frame_forces, sections_dict)

            out += f"\n  构件内力 (DEAD 工况):\n"
            for fid in sorted(frame_forces.keys(), key=lambda x: int(x) if x.isdigit() else 999):
                f = frame_forces[fid]
                u = utilizations.get(fid, {})
                util_str = f", 利用率 {u['utilization']*100:.1f}%" if u else ""
                out += f"    框架 {fid}: P={f['P']:.2f}kN, V2={f['V2']:.2f}kN, M3={f['M3']:.2f}kN·m{util_str}\n"

            if utilizations:
                max_util_fid = max(utilizations.keys(), key=lambda k: utilizations[k]['utilization'])
                max_util = utilizations[max_util_fid]['utilization']
                out += f"\n  最大利用率: 框架 {max_util_fid} = {max_util*100:.1f}% (M_max={utilizations[max_util_fid]['M_max_knm']:.2f} kN·m, M_u={utilizations[max_util_fid]['M_u_knm']:.2f} kN·m)"
        else:
            out += f"\n  ⚠️ 未获取到 Frame Forces: {ff_err or '未知错误'}\n"

        out += "  请在 SAP2000 中查看详细云图"
        return out

    except Exception as e:
        return f"❌ 求解失败: {e}"
    finally:
        try:
            pythoncom.CoUninitialize()
        except:
            pass

def cmd_exit():
    """断开 Python 与 SAP2000 的 COM 连接（不关闭 SAP2000 GUI）

    SAP2000 进程保持运行，由用户手动 File → Exit 关闭
    """
    return (
        "ℹ️  Python 端无持续连接，无需断开\n"
        "💡 如需关闭 SAP2000，请在 GUI 中 File → Exit"
    )


# ─────────────────────────────────────────────────────────────
# 第 4 层：SAP2000 → IR 同步（Batch 6 新增）
# ─────────────────────────────────────────────────────────────

def read_sap2000_state(sap):
    """从 SAP2000 读取完整模型状态，返回 IR 格式字典

    关键设计：SAP2000 是事实来源，IR 是它的快照
    通过 OAPI 读取：节点/约束/构件/截面/荷载
    """
    state = {
        "model_id": "synced_from_sap2000",
        "nodes": [],
        "frames": [],
        "sections": [],
        "dist_loads": [],
        "point_loads": [],
        "load_cases": [
            {"name": "DEAD", "type": "DEAD", "self_weight": 1.0},
            {"name": "LIVE", "type": "LIVE", "self_weight": 0.0},
        ],
        "_synced_at": time.time(),  # 时间戳
    }

    # 1. 节点
    ret = sap.PointObj.GetNameList()
    if ret[0] != 0:
        raise RuntimeError(f"GetNameList 失败: {ret[0]}")
    node_names = ret[2] if ret[1] > 0 else []

    for n in node_names:
        # 坐标
        r = sap.PointObj.GetCoordCartesian(n)
        if r[0] != 0:
            continue
        # 约束
        rest = sap.PointObj.GetRestraint(n)
        # ret = (retcode, (U1, U2, U3, R1, R2, R3))
        restrain = list(rest[1]) if rest[0] == 0 and len(rest) > 1 else [False]*6

        state["nodes"].append({
            "id": n,
            "x": float(r[1]),
            "y": float(r[2]),
            "z": float(r[3]),
            "restrain": restrain,
        })

    # 2. 截面
    ret = sap.PropFrame.GetNameList()
    sec_names = ret[2] if ret[1] > 0 else []

    for s in sec_names:
        # GetRectangle: (retcode, name, mat, t3, t2, color, ...)
        r = sap.PropFrame.GetRectangle(s)
        if r[0] != 0 or len(r) < 5:
            continue
        mat = r[2]
        t3_mm = float(r[3]) * 1000  # m → mm
        t2_mm = float(r[4]) * 1000
        state["sections"].append({
            "name": s,
            "type": "concrete_rect",  # 简化：默认混凝土矩形
            "rect_h": t3_mm,
            "rect_b": t2_mm,
            "depth": t3_mm,
            "width": t2_mm,
            "material": mat,
        })

    # 3. 框架（构件）
    ret = sap.FrameObj.GetNameList()
    frame_names = ret[2] if ret[1] > 0 else []

    for f in frame_names:
        # GetSection
        r = sap.FrameObj.GetSection(f)
        if r[0] != 0 or len(r) < 2:
            continue
        sec_name = r[1]

        # GetPoints（v24 用这个替代 GetConnectivity）
        # 返回: (retcode, PointI, PointJ)
        try:
            r_conn = sap.FrameObj.GetPoints(f)
            i_node = str(r_conn[1]) if r_conn[0] == 0 and len(r_conn) > 1 else "?"
            j_node = str(r_conn[2]) if r_conn[0] == 0 and len(r_conn) > 2 else "?"
        except Exception:
            i_node = "?"
            j_node = "?"

        # 判断 role（按截面名启发式）
        if sec_name.startswith("COL") or "柱" in sec_name:
            role = "column"
        else:
            role = "beam"

        state["frames"].append({
            "id": f,
            "i_node": i_node,
            "j_node": j_node,
            "section": sec_name,
            "role": role,
        })

    # 4. 分布荷载
    for f in frame_names:
        try:
            r = sap.FrameObj.GetLoadDistributed(f)
            if r[0] != 0 or len(r) < 12:
                continue
            # r 结构: (retcode, count, frame_names, load_cases, my_types, csys, dirs,
            #          dist1, dist2, val1, val2, abs_start, abs_end)
            count = r[1]
            if count == 0:
                continue
            cases = r[3] if isinstance(r[3], (list, tuple)) else [r[3]]
            vals = r[11] if isinstance(r[11], (list, tuple)) else [r[11]]

            for i in range(count):
                state["dist_loads"].append({
                    "frame_id": f,
                    "case": str(cases[i]),
                    "value": float(vals[i]) if vals[i] is not None else 0.0,
                })
        except Exception:
            pass

    return state


def _check_sap_conflicts():
    """轻量级冲突检测：快速对比 IR 与 SAP2000 关键字段

    返回字符串（如果有冲突返回警告，否则返回空串）
    """
    from sap2000_worker import SAP2000Worker, SAP2000Config

    config = SAP2000Config(sap2000_path=r"D:\SAP2000\SAP2000.exe")
    worker = SAP2000Worker(config)

    existing_pid, existing_mem = _find_existing_sap_pid()
    if not (existing_pid and existing_mem and existing_mem > 80):
        return ""  # SAP2000 没运行，不算冲突

    worker.connection.start()

    import pythoncom
    pythoncom.CoInitialize()
    try:
        sap = worker.connection.sap_object.SapModel
        sap.SetModelIsLocked(False)

        # 读 SAP2000 关键字段
        sap_state = read_sap2000_state(sap)

        # 读 IR
        ir_state = load_ir_state()
        if not ir_state:
            return ""

        n_diff = 0

        # 构件截面对比
        ir_frames = {f["id"]: f.get("section", "?") for f in ir_state.get("frames", [])}
        sap_frames = {f["id"]: f.get("section", "?") for f in sap_state["frames"]}
        for fid in ir_frames:
            if ir_frames[fid] != sap_frames.get(fid):
                n_diff += 1

        # 截面尺寸对比
        ir_sections = {s["name"]: (s.get("rect_h", 0), s.get("rect_b", 0))
                       for s in ir_state.get("sections", [])}
        sap_sections = {s["name"]: (s.get("rect_h", 0), s.get("rect_b", 0))
                        for s in sap_state["sections"]}
        for sname in ir_sections:
            if ir_sections[sname] != sap_sections.get(sname):
                n_diff += 1

        # 荷载对比
        ir_loads = {(d["frame_id"], d["case"]): d["value"]
                    for d in ir_state.get("dist_loads", [])}
        sap_loads = {(d["frame_id"], d["case"]): d["value"]
                     for d in sap_state["dist_loads"]}
        for key in ir_loads:
            if abs(ir_loads[key] - sap_loads.get(key, 0)) > 0.01:
                n_diff += 1

        if n_diff == 0:
            return ""
        return f"发现 {n_diff} 处差异"
    except Exception:
        return ""
    finally:
        try:
            pythoncom.CoUninitialize()
        except:
            pass


def cmd_sync(direction="both"):
    """把 SAP2000 模型同步到 IR 状态

    direction:
      "from_sap"  - 只读 SAP2000 覆盖 IR
      "to_sap"    - 把 IR 推到 SAP2000（暂未实现，用 modify）
      "both"      - 双向（默认 = from_sap）
    """
    from sap2000_worker import SAP2000Worker, SAP2000Config

    config = SAP2000Config(sap2000_path=r"D:\SAP2000\SAP2000.exe")
    worker = SAP2000Worker(config)

    existing_pid, existing_mem = _find_existing_sap_pid()
    if not (existing_pid and existing_mem and existing_mem > 80):
        return "❌ SAP2000 未运行，请先启动 SAP2000"

    worker.connection.start()

    import pythoncom
    pythoncom.CoInitialize()
    try:
        sap = worker.connection.sap_object.SapModel
        sap.SetModelIsLocked(False)

        # 读 SAP2000 状态
        sap_state = read_sap2000_state(sap)

        # 保存为新的 IR 状态
        save_ir_state(sap_state)

        # 输出摘要
        return (
            f"✅ 已从 SAP2000 同步到 IR\n"
            f"  节点: {len(sap_state['nodes'])} 个\n"
            f"  构件: {len(sap_state['frames'])} 个\n"
            f"  截面: {len(sap_state['sections'])} 个\n"
            f"  荷载: {len(sap_state['dist_loads'])} 个 (DEAD/LIVE)\n"
            f"  同步时间: {time.strftime('%H:%M:%S', time.localtime(sap_state['_synced_at']))}\n"
            f"\n  IR 状态文件: {_STATE_FILE}"
        )
    except Exception as e:
        return f"❌ 同步失败: {e}"
    finally:
        try:
            pythoncom.CoUninitialize()
        except:
            pass


def cmd_diff():
    """对比 IR 与 SAP2000 当前状态，显示差异"""
    from sap2000_worker import SAP2000Worker, SAP2000Config

    config = SAP2000Config(sap2000_path=r"D:\SAP2000\SAP2000.exe")
    worker = SAP2000Worker(config)

    existing_pid, existing_mem = _find_existing_sap_pid()
    if not (existing_pid and existing_mem and existing_mem > 80):
        return "❌ SAP2000 未运行"

    worker.connection.start()

    import pythoncom
    pythoncom.CoInitialize()
    try:
        sap = worker.connection.sap_object.SapModel
        sap.SetModelIsLocked(False)

        # 读 SAP2000
        sap_state = read_sap2000_state(sap)

        # 读 IR
        ir_state = load_ir_state()
        if not ir_state:
            return "⚠️ 无 IR 状态文件，请先运行 sync 或 init"

        diffs = []

        # 节点数对比
        ir_n_nodes = len(ir_state.get("nodes", []))
        sap_n_nodes = len(sap_state["nodes"])
        if ir_n_nodes != sap_n_nodes:
            diffs.append(f"  节点数: IR={ir_n_nodes}, SAP2000={sap_n_nodes}")

        # 构件截面对比
        ir_frames = {f["id"]: f.get("section", "?") for f in ir_state.get("frames", [])}
        sap_frames = {f["id"]: f.get("section", "?") for f in sap_state["frames"]}
        for fid in sorted(set(ir_frames.keys()) | set(sap_frames.keys())):
            ir_sec = ir_frames.get(fid, "(缺失)")
            sap_sec = sap_frames.get(fid, "(缺失)")
            if ir_sec != sap_sec:
                diffs.append(f"  构件 {fid} 截面: IR={ir_sec}, SAP2000={sap_sec}")

        # 截面尺寸对比
        ir_sections = {s["name"]: (s.get("rect_h", 0), s.get("rect_b", 0))
                       for s in ir_state.get("sections", [])}
        sap_sections = {s["name"]: (s.get("rect_h", 0), s.get("rect_b", 0))
                        for s in sap_state["sections"]}
        for sname in set(ir_sections.keys()) | set(sap_sections.keys()):
            ir_dim = ir_sections.get(sname)
            sap_dim = sap_sections.get(sname)
            if ir_dim != sap_dim:
                diffs.append(f"  截面 {sname}: IR={ir_dim}, SAP2000={sap_dim}")

        # 荷载对比
        ir_loads = {(d["frame_id"], d["case"]): d["value"]
                    for d in ir_state.get("dist_loads", [])}
        sap_loads = {(d["frame_id"], d["case"]): d["value"]
                     for d in sap_state["dist_loads"]}
        for key in set(ir_loads.keys()) | set(sap_loads.keys()):
            ir_v = ir_loads.get(key, "(缺失)")
            sap_v = sap_loads.get(key, "(缺失)")
            if abs(ir_v - sap_v) > 0.01 if isinstance(ir_v, (int, float)) and isinstance(sap_v, (int, float)) else (ir_v != sap_v):
                diffs.append(f"  荷载 {key}: IR={ir_v}, SAP2000={sap_v}")

        if not diffs:
            return "✅ IR 与 SAP2000 完全一致"

        out = f"⚠️ 发现 {len(diffs)} 处差异：\n"
        out += "\n".join(diffs[:30])
        if len(diffs) > 30:
            out += f"\n  ... 还有 {len(diffs) - 30} 处"
        out += "\n\n  💡 提示：运行 `sync` 用 SAP2000 覆盖 IR（会丢失你的 IR 改动）"
        return out
    except Exception as e:
        return f"❌ 对比失败: {e}"
    finally:
        try:
            pythoncom.CoUninitialize()
        except:
            pass


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python modify_model.py init              # 初始化（首次）")
        print("  python modify_model.py show              # 查看模型")
        print("  python modify_model.py '改梁5荷载为-25'  # 修改荷载")
        print("  python modify_model.py 'DEAD荷载都乘1.4倍'")
        print("  python modify_model.py solve             # 求解")
        print("  python modify_model.py sync              # 从 SAP2000 同步到 IR（Batch 6）")
        print("  python modify_model.py diff              # 对比 IR vs SAP2000（Batch 6）")
        print("  python modify_model.py exit              # 关闭")
        return

    cmd_text = " ".join(sys.argv[1:])

    if cmd_text in ("init", "初始化"):
        result = cmd_init()
    elif cmd_text in ("sync", "同步"):
        result = cmd_sync()
    elif cmd_text in ("diff", "对比"):
        result = cmd_diff()
    elif cmd_text in ("exit", "quit", "退出"):
        result = cmd_exit()
    elif cmd_text.startswith("show "):
        # show <target> → 提取 target
        target = cmd_text[5:].strip()
        if not target:
            target = "all"
        result = cmd_show(target)
    elif cmd_text in ("show", "显示", "查看"):
        result = cmd_show()
    else:
        result = cmd_modify(cmd_text)

    print(result)


if __name__ == "__main__":
    main()