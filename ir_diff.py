"""
IR Diff & Modifier（差异计算与字段修改器）
===========================================
实现 IR 的增量修改支持：
1. compute_diff(old, new) → IRDiff（含 6 类字段变化）
2. apply_modifications(ir, modifications) → 修改后的 IR
3. 严格字段白名单（防误改未指定字段）

作者：MiniMax-M3 / Hermes CSI System
版本：v1.0.0
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Optional, Any, Set, Tuple
import logging

from ir_compiler import (
    StructuralIR, Node, Frame, Section, SectionType,
    LoadCase, LoadCaseType,
    PointLoad, DistributedLoad,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ir_diff")


# ============================================================================
# 第 1 层：变更类型 + 差异结构
# ============================================================================

class ChangeType(str, Enum):
    """变更类型"""
    ADDED = "added"           # 新增
    REMOVED = "removed"       # 删除
    MODIFIED = "modified"     # 修改


@dataclass
class FieldChange:
    """单个字段的变更"""
    field_name: str           # 字段路径（如 "rect_h", "restrain"）
    old_value: Any
    new_value: Any

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field_name,
            "old": str(self.old_value),
            "new": str(self.new_value),
        }


@dataclass
class EntityDiff:
    """单个实体的差异"""
    entity_type: str            # "node", "frame", "section", "load_case", "point_load", "dist_load"
    entity_id: Any              # 实体 ID（数字或字符串）
    change_type: ChangeType
    field_changes: List[FieldChange] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": str(self.entity_id),
            "change_type": self.change_type.value,
            "field_changes": [fc.to_dict() for fc in self.field_changes],
        }


@dataclass
class IRDiff:
    """完整 IR 差异"""
    node_diffs: List[EntityDiff] = field(default_factory=list)
    frame_diffs: List[EntityDiff] = field(default_factory=list)
    section_diffs: List[EntityDiff] = field(default_factory=list)
    load_case_diffs: List[EntityDiff] = field(default_factory=list)
    point_load_diffs: List[EntityDiff] = field(default_factory=list)
    dist_load_diffs: List[EntityDiff] = field(default_factory=list)

    def has_changes(self) -> bool:
        return bool(
            self.node_diffs or self.frame_diffs or self.section_diffs or
            self.load_case_diffs or self.point_load_diffs or self.dist_load_diffs
        )

    def summary(self) -> Dict[str, int]:
        return {
            "nodes": len(self.node_diffs),
            "frames": len(self.frame_diffs),
            "sections": len(self.section_diffs),
            "load_cases": len(self.load_case_diffs),
            "point_loads": len(self.point_load_diffs),
            "dist_loads": len(self.dist_load_diffs),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary(),
            "nodes": [d.to_dict() for d in self.node_diffs],
            "frames": [d.to_dict() for d in self.frame_diffs],
            "sections": [d.to_dict() for d in self.section_diffs],
            "load_cases": [d.to_dict() for d in self.load_case_diffs],
            "point_loads": [d.to_dict() for d in self.point_load_diffs],
            "dist_loads": [d.to_dict() for d in self.dist_load_diffs],
        }


# ============================================================================
# 第 2 层：差异计算
# ============================================================================

def _diff_entity(old, new, entity_type: str, id_attr: str = "id",
                comparable_fields: Optional[List[str]] = None,
                entity_id_override: Any = None) -> Optional[EntityDiff]:
    """比较两个实体，返回差异或 None

    Args:
        entity_id_override: 直接指定 entity_id（用于复合 key 的情况）
    """
    if old is None and new is None:
        return None

    if entity_id_override is not None:
        eid = entity_id_override
    elif id_attr == "_key":
        eid = None  # 不支持
    else:
        eid = (new or old).__getattribute__(id_attr)

    if old is None:
        return EntityDiff(
            entity_type=entity_type,
            entity_id=eid,
            change_type=ChangeType.ADDED,
            field_changes=[],
        )
    if new is None:
        return EntityDiff(
            entity_type=entity_type,
            entity_id=eid,
            change_type=ChangeType.REMOVED,
            field_changes=[],
        )

    # 两者都在，比较字段
    field_changes = []
    fields = comparable_fields or _get_comparable_fields(old)
    for fname in fields:
        old_val = getattr(old, fname, None)
        new_val = getattr(new, fname, None)
        if _values_differ(old_val, new_val):
            field_changes.append(FieldChange(
                field_name=fname,
                old_value=old_val,
                new_value=new_val,
            ))

    if field_changes:
        return EntityDiff(
            entity_type=entity_type,
            entity_id=eid,
            change_type=ChangeType.MODIFIED,
            field_changes=field_changes,
        )
    return None


def _get_comparable_fields(entity) -> List[str]:
    """获取实体的可比较字段（排除派生字段）"""
    if isinstance(entity, Node):
        return ["x", "y", "z", "restrain"]
    elif isinstance(entity, Frame):
        return ["i_node", "j_node", "section", "role"]
    elif isinstance(entity, Section):
        return ["name", "type", "depth", "width", "rect_h", "rect_b", "material"]
    elif isinstance(entity, LoadCase):
        return ["name", "type", "self_weight", "scale_factor"]
    elif isinstance(entity, PointLoad):
        return ["node_id", "case", "fx", "fy", "fz", "mx", "my", "mz"]
    elif isinstance(entity, DistributedLoad):
        return ["frame_id", "case", "load_type", "value"]
    return []


def _values_differ(a, b) -> bool:
    """判断两个值是否不同"""
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    if isinstance(a, list) and isinstance(b, list):
        return list(a) != list(b)
    return a != b


def compute_diff(old: StructuralIR, new: StructuralIR) -> IRDiff:
    """计算两个 IR 之间的差异"""
    diff = IRDiff()

    # 节点
    old_nodes = {n.id: n for n in old.nodes}
    new_nodes = {n.id: n for n in new.nodes}
    all_node_ids = set(old_nodes.keys()) | set(new_nodes.keys())
    for nid in sorted(all_node_ids):
        d = _diff_entity(old_nodes.get(nid), new_nodes.get(nid), "node")
        if d:
            diff.node_diffs.append(d)

    # 构件
    old_frames = {f.id: f for f in old.frames}
    new_frames = {f.id: f for f in new.frames}
    all_frame_ids = set(old_frames.keys()) | set(new_frames.keys())
    for fid in sorted(all_frame_ids):
        d = _diff_entity(old_frames.get(fid), new_frames.get(fid), "frame")
        if d:
            diff.frame_diffs.append(d)

    # 截面（按 name 索引）
    old_sections = {s.name: s for s in old.sections}
    new_sections = {s.name: s for s in new.sections}
    all_section_names = set(old_sections.keys()) | set(new_sections.keys())
    for sname in all_section_names:
        d = _diff_entity(old_sections.get(sname), new_sections.get(sname), "section",
                        entity_id_override=sname)
        if d:
            diff.section_diffs.append(d)

    # 荷载工况（按 name 索引）
    old_cases = {c.name: c for c in old.load_cases}
    new_cases = {c.name: c for c in new.load_cases}
    all_case_names = set(old_cases.keys()) | set(new_cases.keys())
    for cname in all_case_names:
        d = _diff_entity(old_cases.get(cname), new_cases.get(cname), "load_case",
                        entity_id_override=cname)
        if d:
            diff.load_case_diffs.append(d)

    # 点荷载
    old_pls = {(p.node_id, p.case): p for p in old.point_loads}
    new_pls = {(p.node_id, p.case): p for p in new.point_loads}
    for key in set(old_pls.keys()) | set(new_pls.keys()):
        d = _diff_entity(old_pls.get(key), new_pls.get(key), "point_load",
                        entity_id_override=f"{key[0]}/{key[1]}")
        if d:
            diff.point_load_diffs.append(d)

    # 均布荷载
    old_dls = {(d.frame_id, d.case): d for d in old.dist_loads}
    new_dls = {(d.frame_id, d.case): d for d in new.dist_loads}
    for key in set(old_dls.keys()) | set(new_dls.keys()):
        d = _diff_entity(old_dls.get(key), new_dls.get(key), "dist_load",
                        entity_id_override=f"{key[0]}/{key[1]}")
        if d:
            diff.dist_load_diffs.append(d)

    return diff


# ============================================================================
# 第 3 层：指令应用（白名单修改）
# ============================================================================

# 允许修改的字段白名单（防止误改）
ALLOWED_MODIFICATIONS = {
    "node": {"x", "y", "z", "restrain"},
    "frame": {"i_node", "j_node", "section", "role"},
    "section": {"depth", "width", "rect_h", "rect_b", "material"},
    "load_case": {"scale_factor"},
    "point_load": {"fx", "fy", "fz", "mx", "my", "mz"},
    "dist_load": {"value", "load_type"},
}


class Modification:
    """单条修改指令"""
    def __init__(self, entity_type: str, entity_id: Any,
                 field_name: str, new_value: Any):
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.field_name = field_name
        self.new_value = new_value

    def validate(self) -> Tuple[bool, str]:
        """校验修改是否合法"""
        if self.entity_type not in ALLOWED_MODIFICATIONS:
            return False, f"不支持修改实体类型: {self.entity_type}"

        allowed_fields = ALLOWED_MODIFICATIONS[self.entity_type]
        if self.field_name not in allowed_fields:
            return False, (
                f"字段 '{self.field_name}' 不在白名单。"
                f"允许: {sorted(allowed_fields)}"
            )
        return True, "OK"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": str(self.entity_id),
            "field": self.field_name,
            "new_value": str(self.new_value),
        }


def apply_modification(ir: StructuralIR, mod: Modification) -> Tuple[bool, str]:
    """应用单条修改到 IR

    Returns:
        (success, message)
    """
    # 1. 校验
    ok, msg = mod.validate()
    if not ok:
        return False, msg

    # 2. 找到实体并修改
    if mod.entity_type == "node":
        target = next((n for n in ir.nodes if n.id == mod.entity_id), None)
        if not target:
            return False, f"节点 {mod.entity_id} 不存在"
        setattr(target, mod.field_name, mod.new_value)
        return True, f"节点 {mod.entity_id}.{mod.field_name} = {mod.new_value}"

    elif mod.entity_type == "frame":
        target = next((f for f in ir.frames if f.id == mod.entity_id), None)
        if not target:
            return False, f"构件 {mod.entity_id} 不存在"
        setattr(target, mod.field_name, mod.new_value)
        return True, f"构件 {mod.entity_id}.{mod.field_name} = {mod.new_value}"

    elif mod.entity_type == "section":
        target = next((s for s in ir.sections if s.name == mod.entity_id), None)
        if not target:
            return False, f"截面 {mod.entity_id} 不存在"
        setattr(target, mod.field_name, mod.new_value)
        return True, f"截面 {mod.entity_id}.{mod.field_name} = {mod.new_value}"

    elif mod.entity_type == "dist_load":
        # 通过 (frame_id, case) 定位
        frame_id, case = mod.entity_id
        target = next((d for d in ir.dist_loads
                       if d.frame_id == frame_id and d.case == case), None)
        if not target:
            return False, f"未找到均布荷载 (frame={frame_id}, case={case})"
        setattr(target, mod.field_name, mod.new_value)
        return True, f"均布荷载 {frame_id}/{case}.{mod.field_name} = {mod.new_value}"

    elif mod.entity_type == "point_load":
        node_id, case = mod.entity_id
        target = next((p for p in ir.point_loads
                       if p.node_id == node_id and p.case == case), None)
        if not target:
            return False, f"未找到点荷载 (node={node_id}, case={case})"
        setattr(target, mod.field_name, mod.new_value)
        return True, f"点荷载 {node_id}/{case}.{mod.field_name} = {mod.new_value}"

    return False, f"未实现的实体类型: {mod.entity_type}"


def apply_modifications(ir: StructuralIR,
                        modifications: List[Modification]) -> List[Tuple[bool, str]]:
    """批量应用修改"""
    results = []
    for mod in modifications:
        ok, msg = apply_modification(ir, mod)
        results.append((ok, msg))
        if not ok:
            logger.warning(f"修改失败: {msg}")
        else:
            logger.info(f"✓ {msg}")
    return results


# ============================================================================
# 第 4 层：便捷操作（批量修改）
# ============================================================================

def scale_all_dist_loads(ir: StructuralIR, case: str, factor: float) -> List[Modification]:
    """批量缩放指定工况的所有均布荷载

    Returns:
        修改列表（不直接应用 IR，由调用方 apply）
    """
    mods = []
    for d in ir.dist_loads:
        if d.case == case:
            new_value = d.value * factor
            mods.append(Modification(
                entity_type="dist_load",
                entity_id=(d.frame_id, d.case),
                field_name="value",
                new_value=new_value,
            ))
    return mods


def scale_all_point_loads(ir: StructuralIR, case: str, factor: float) -> List[Modification]:
    """批量缩放指定工况的所有点荷载"""
    mods = []
    for p in ir.point_loads:
        if p.case == case:
            for field_name in ("fx", "fy", "fz", "mx", "my", "mz"):
                old_val = getattr(p, field_name)
                if old_val != 0:
                    mods.append(Modification(
                        entity_type="point_load",
                        entity_id=(p.node_id, p.case),
                        field_name=field_name,
                        new_value=old_val * factor,
                    ))
    return mods


def remove_node_safe(ir: StructuralIR, node_id: int) -> List[Modification]:
    """安全删除节点（同时删除引用该节点的构件和荷载）

    返回需要应用的修改列表（含节点、构件、荷载删除）
    """
    # 注：当前实现只返回节点删除，由调用方决定是否级联
    # 实际删除节点需要同时从 frames/loads 中移除引用
    logger.warning(
        f"删除节点 {node_id} 需要级联删除相关构件和荷载（未实现）"
    )
    return []


# ============================================================================
# 第 5 层：测试入口
# ============================================================================

def main():
    """演示差异计算 + 修改应用"""
    from ir_compiler import build_sample_frame_ir

    print("=" * 70)
    print("IR Diff & Modifier v1.0 - 测试入口")
    print("=" * 70)

    # 加载示例 IR
    ir = build_sample_frame_ir()
    ir_old = ir.model_copy(deep=True)

    print("\n[1] 初始 IR 状态:")
    print(f"  节点数: {len(ir.nodes)}, 构件数: {len(ir.frames)}, 截面数: {len(ir.sections)}")

    # 修改：柱 1 截面改为 600x600
    print("\n[2] 应用修改: 柱 1 截面 400x400 → 600x600")
    ir.sections[0].rect_h = 600
    ir.sections[0].rect_b = 600

    # 修改：梁 7 荷载改为 -25 kN/m
    print("[3] 应用修改: 梁 7 DEAD 荷载 -15 → -25")
    for d in ir.dist_loads:
        if d.frame_id == 7 and d.case == "DEAD":
            d.value = -25

    # 计算 diff
    print("\n[4] 计算 diff:")
    diff = compute_diff(ir_old, ir)
    summary = diff.summary()
    for k, v in summary.items():
        if v > 0:
            print(f"  {k}: {v} 个变更")
            for d in getattr(diff, f"{k}_diffs"):
                for fc in d.field_changes:
                    print(f"    {d.entity_type} #{d.entity_id}: "
                          f"{fc.field_name}: {fc.old_value} → {fc.new_value}")

    # 测试白名单校验
    print("\n[5] 测试白名单（尝试修改不允许的字段）")
    bad_mod = Modification(
        entity_type="section", entity_id="BEAM300x600",
        field_name="name", new_value="HACKED"  # name 不在白名单
    )
    ok, msg = bad_mod.validate()
    print(f"  修改 section.name: ok={ok}, msg={msg}")

    # 测试白名单外的修改尝试
    print("\n[6] 尝试非法修改 apply_modification")
    ok, msg = apply_modification(ir, bad_mod)
    print(f"  结果: ok={ok}, msg={msg}")

    # 批量缩放
    print("\n[7] 批量缩放 DEAD 荷载 × 1.4")
    ir2 = build_sample_frame_ir()
    mods = scale_all_dist_loads(ir2, "DEAD", 1.4)
    print(f"  生成 {len(mods)} 条修改")
    apply_modifications(ir2, mods)
    for d in ir2.dist_loads:
        if d.case == "DEAD":
            print(f"    frame={d.frame_id}: {d.value} kN/m")

    print("\n" + "=" * 70)
    print("✅ 测试完成")
    print("=" * 70)


if __name__ == "__main__":
    main()