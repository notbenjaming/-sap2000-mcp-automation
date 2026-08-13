"""
Structural IR Compiler (编译层)
================================
把高层结构模型描述（IR）编译成 SAP2000 API 可执行的步骤序列。

核心模块：
1. Schema 层（Pydantic 数据结构）
2. Validator 层（语义校验）
3. Compiler 层（IR → ExecutionPlan）
4. Step Generator 层（操作步骤生成）

作者：MiniMax-M3 / Hermes CSI System
版本：v1.0.0
"""

from __future__ import annotations

import json
import time
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional, Any, Set, Tuple
from dataclasses import dataclass, field, asdict
from collections import defaultdict, deque

from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict


# ============================================================================
# 第 1 层：Schema 定义（Pydantic 数据结构）
# ============================================================================

class SolverType(str, Enum):
    """求解器类型"""
    SAP2000 = "sap2000"
    OPENSEES = "opensees"
    ANSYS = "ansys"
    ETABS = "etabs"


class SectionType(str, Enum):
    """截面类型"""
    WIDE_FLANGE = "wide_flange"   # W 型钢
    CONCRETE_RECT = "concrete_rect"  # 混凝土矩形
    HSS = "hss"                  # 空心钢管
    CUSTOM = "custom"            # 自定义


class LoadCaseType(str, Enum):
    """荷载工况类型"""
    DEAD = "dead"
    LIVE = "live"
    SEISMIC = "seismic"
    WIND = "wind"
    TEMPERATURE = "temperature"


class AnalysisType(str, Enum):
    """分析类型"""
    LINEAR_STATIC = "linear_static"
    MODAL = "modal"
    RESPONSE_SPECTRUM = "response_spectrum"


class Node(BaseModel):
    """结构节点"""
    id: int = Field(..., ge=1, description="节点编号（从1开始）")
    x: float = Field(..., description="X 坐标 (m)")
    y: float = Field(..., description="Y 坐标 (m)")
    z: float = Field(..., description="Z 坐标 (m)")
    restrain: Optional[List[bool]] = Field(
        default=None,
        description="支座约束 [Ux, Uy, Uz, Rx, Ry, Rz]，True=约束"
    )


class Section(BaseModel):
    """构件截面"""
    name: str = Field(..., min_length=1, description="截面名称")
    type: SectionType = SectionType.WIDE_FLANGE
    # W 型钢参数
    depth: Optional[float] = Field(default=None, gt=0, description="截面高度 (mm)")
    width: Optional[float] = Field(default=None, gt=0, description="翼缘宽度 (mm)")
    # 矩形混凝土参数
    rect_h: Optional[float] = Field(default=None, gt=0, description="矩形截面高 (mm)")
    rect_b: Optional[float] = Field(default=None, gt=0, description="矩形截面宽 (mm)")
    material: str = Field(default="Q355", description="材料牌号")


class Frame(BaseModel):
    """框架构件（梁/柱/支撑）"""
    id: int = Field(..., ge=1, description="构件编号")
    i_node: int = Field(..., ge=1, alias="i", description="起始节点")
    j_node: int = Field(..., ge=1, alias="j", description="终止节点")
    section: str = Field(..., description="截面名称引用")
    role: str = Field(default="beam", description="构件角色：beam/column/bracing")

    model_config = ConfigDict(populate_by_name=True)


class PointLoad(BaseModel):
    """节点荷载"""
    node_id: int = Field(..., ge=1)
    case: str = Field(..., description="工况名称")
    fx: float = 0.0
    fy: float = 0.0
    fz: float = 0.0
    mx: float = 0.0
    my: float = 0.0
    mz: float = 0.0


class DistributedLoad(BaseModel):
    """梁上均布荷载"""
    frame_id: int = Field(..., ge=1)
    case: str = Field(..., description="工况名称")
    load_type: str = Field(default="gravity", description="gravity/axial")
    value: float = Field(..., description="荷载值 (kN/m)")


class LoadCase(BaseModel):
    """荷载工况"""
    name: str = Field(..., min_length=1)
    type: LoadCaseType = LoadCaseType.DEAD
    self_weight: bool = Field(default=False, description="是否包含自重")
    scale_factor: float = Field(default=1.0)


class AnalysisSetting(BaseModel):
    """分析设置"""
    type: AnalysisType = AnalysisType.LINEAR_STATIC
    target_solver: SolverType = SolverType.SAP2000
    design_code: Optional[str] = Field(default=None, description="设计规范，如 GB50011")


class StructuralIR(BaseModel):
    """结构 IR（顶层模型）"""
    model_id: str = Field(..., min_length=1)
    name: str = Field(default="Untitled Model")
    units: str = Field(default="kN_m_C", description="单位制")
    nodes: List[Node] = Field(default_factory=list)
    frames: List[Frame] = Field(default_factory=list)
    sections: List[Section] = Field(default_factory=list)
    point_loads: List[PointLoad] = Field(default_factory=list)
    dist_loads: List[DistributedLoad] = Field(default_factory=list)
    load_cases: List[LoadCase] = Field(default_factory=list)
    analysis: AnalysisSetting = Field(default_factory=AnalysisSetting)


# ============================================================================
# 第 2 层：Validator 语义校验器
# ============================================================================

@dataclass
class ValidationError:
    """校验错误"""
    level: str
    code: str
    message: str
    entity: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "entity": self.entity,
        }


class SemanticValidator:
    """IR 语义校验器

    检查项：
    - 节点编号唯一性
    - 构件节点存在性
    - 截面引用存在性
    - 荷载工况存在性
    - 悬空构件（孤立节点）
    - 节点坐标合理性
    - 构件连通性
    """

    def __init__(self):
        self.errors: List[ValidationError] = []
        self.warnings: List[ValidationError] = []

    def validate(self, ir: StructuralIR) -> bool:
        """执行完整校验，返回是否通过（无 ERROR）"""
        self.errors.clear()
        self.warnings.clear()

        self._check_node_ids(ir)
        self._check_node_coordinates(ir)
        self._check_frame_references(ir)
        self._check_section_references(ir)
        self._check_load_references(ir)
        self._check_connectivity(ir)
        self._check_section_parameters(ir)
        self._check_load_case_completeness(ir)

        return len(self.errors) == 0

    def report(self) -> Dict[str, Any]:
        """生成校验报告"""
        return {
            "passed": len(self.errors) == 0,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
        }

    def _check_node_ids(self, ir: StructuralIR):
        """检查节点编号唯一性"""
        ids = [n.id for n in ir.nodes]
        duplicates = [nid for nid in ids if ids.count(nid) > 1]
        if duplicates:
            self.errors.append(ValidationError(
                "ERROR", "NODE_DUPLICATE",
                f"节点编号重复: {sorted(set(duplicates))}",
                "nodes"
            ))

    def _check_node_coordinates(self, ir: StructuralIR):
        """检查节点坐标合理性"""
        for n in ir.nodes:
            if abs(n.x) > 1e6 or abs(n.y) > 1e6 or abs(n.z) > 1e6:
                self.warnings.append(ValidationError(
                    "WARNING", "NODE_COORD_LARGE",
                    f"节点 {n.id} 坐标异常大 ({n.x}, {n.y}, {n.z})",
                    f"node_{n.id}"
                ))

    def _check_frame_references(self, ir: StructuralIR):
        """检查构件节点引用是否有效"""
        node_ids = {n.id for n in ir.nodes}
        for f in ir.frames:
            if f.i_node not in node_ids:
                self.errors.append(ValidationError(
                    "ERROR", "FRAME_NODE_MISSING",
                    f"构件 {f.id} 起始节点 {f.i_node} 不存在",
                    f"frame_{f.id}"
                ))
            if f.j_node not in node_ids:
                self.errors.append(ValidationError(
                    "ERROR", "FRAME_NODE_MISSING",
                    f"构件 {f.id} 终止节点 {f.j_node} 不存在",
                    f"frame_{f.id}"
                ))
            if f.i_node == f.j_node:
                self.errors.append(ValidationError(
                    "ERROR", "FRAME_ZERO_LENGTH",
                    f"构件 {f.id} 起始与终止节点相同",
                    f"frame_{f.id}"
                ))

    def _check_section_references(self, ir: StructuralIR):
        """检查截面引用"""
        section_names = {s.name for s in ir.sections}
        for f in ir.frames:
            if f.section not in section_names:
                self.errors.append(ValidationError(
                    "ERROR", "FRAME_SECTION_MISSING",
                    f"构件 {f.id} 引用的截面 '{f.section}' 未定义",
                    f"frame_{f.id}"
                ))

    def _check_load_references(self, ir: StructuralIR):
        """检查荷载工况与构件引用"""
        load_case_names = {lc.name for lc in ir.load_cases}
        frame_ids = {f.id for f in ir.frames}
        node_ids = {n.id for n in ir.nodes}

        for pl in ir.point_loads:
            if pl.case not in load_case_names:
                self.errors.append(ValidationError(
                    "ERROR", "LOAD_CASE_MISSING",
                    f"点荷载引用了未定义工况 '{pl.case}'",
                    f"point_load_node_{pl.node_id}"
                ))
            if pl.node_id not in node_ids:
                self.errors.append(ValidationError(
                    "ERROR", "LOAD_NODE_MISSING",
                    f"点荷载作用在不存在节点 {pl.node_id}",
                    f"point_load_node_{pl.node_id}"
                ))

        for dl in ir.dist_loads:
            if dl.case not in load_case_names:
                self.errors.append(ValidationError(
                    "ERROR", "LOAD_CASE_MISSING",
                    f"均布荷载引用了未定义工况 '{dl.case}'",
                    f"dist_load_frame_{dl.frame_id}"
                ))
            if dl.frame_id not in frame_ids:
                self.errors.append(ValidationError(
                    "ERROR", "LOAD_FRAME_MISSING",
                    f"均布荷载作用在不存在构件 {dl.frame_id}",
                    f"dist_load_frame_{dl.frame_id}"
                ))

    def _check_connectivity(self, ir: StructuralIR):
        """检查节点连通性（孤立节点警告）"""
        connected_nodes: Set[int] = set()
        for f in ir.frames:
            connected_nodes.add(f.i_node)
            connected_nodes.add(f.j_node)

        isolated = [n.id for n in ir.nodes if n.id not in connected_nodes]
        if isolated:
            self.warnings.append(ValidationError(
                "WARNING", "NODE_ISOLATED",
                f"孤立节点（无构件连接）: {isolated}",
                "nodes"
            ))

    def _check_section_parameters(self, ir: StructuralIR):
        """检查截面参数完整性"""
        for s in ir.sections:
            if s.type == SectionType.WIDE_FLANGE:
                if s.depth is None or s.width is None:
                    self.errors.append(ValidationError(
                        "ERROR", "SECTION_PARAM_MISSING",
                        f"W 型钢截面 '{s.name}' 缺少 depth/width 参数",
                        f"section_{s.name}"
                    ))
            elif s.type == SectionType.CONCRETE_RECT:
                if s.rect_h is None or s.rect_b is None:
                    self.errors.append(ValidationError(
                        "ERROR", "SECTION_PARAM_MISSING",
                        f"混凝土矩形截面 '{s.name}' 缺少 rect_h/rect_b 参数",
                        f"section_{s.name}"
                    ))

    def _check_load_case_completeness(self, ir: StructuralIR):
        """检查荷载工况定义完整性"""
        defined = {lc.name for lc in ir.load_cases}
        used = set()
        for pl in ir.point_loads:
            used.add(pl.case)
        for dl in ir.dist_loads:
            used.add(dl.case)

        # 定义了但未使用
        unused = defined - used
        if unused:
            self.warnings.append(ValidationError(
                "WARNING", "LOAD_CASE_UNUSED",
                f"定义了但未使用的工况: {sorted(unused)}",
                "load_cases"
            ))

        # 使用了但未定义（双重检查）
        undefined = used - defined
        if undefined:
            self.errors.append(ValidationError(
                "ERROR", "LOAD_CASE_UNDEFINED",
                f"荷载引用了未定义的工况: {sorted(undefined)}",
                "load_cases"
            ))


# ============================================================================
# 第 3 层：Compiler 主编译器
# ============================================================================

@dataclass
class ExecutionStep:
    """单个执行步骤"""
    step_id: int
    operation: str          # 操作类型，如 "create_node"
    target: str             # 目标对象，如 "node_1"
    params: Dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "operation": self.operation,
            "target": self.target,
            "params": self.params,
            "description": self.description,
        }


@dataclass
class ExecutionPlan:
    """完整执行计划"""
    model_id: str
    target_solver: SolverType
    steps: List[ExecutionStep]
    validation_report: Dict[str, Any]
    compile_time_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "compile_result": "ok" if not self.validation_report["errors"] else "validation_failed",
            "model_id": self.model_id,
            "target_solver": self.target_solver.value,
            "step_count": len(self.steps),
            "compile_time_ms": round(self.compile_time_ms, 2),
            "validation": self.validation_report,
            "execution_graph": [s.to_dict() for s in self.steps],
        }


class IRCompiler:
    """IR 编译器：把 StructuralIR 编译为 SAP2000 可执行计划

    编译流程：
    1. 语义校验（SemanticValidator）
    2. 拓扑排序（先建节点 → 再建截面 → 再建构件 → 再建荷载 → 再建工况 → 再分析）
    3. 生成步骤序列
    4. 封装为 ExecutionPlan
    """

    def __init__(self):
        self.validator = SemanticValidator()

    def compile(self, ir: StructuralIR) -> ExecutionPlan:
        """编译 IR → 执行计划

        Returns:
            ExecutionPlan 对象（含步骤序列）
        """
        start = time.time()

        # 1. 校验
        is_valid = self.validator.validate(ir)
        report = self.validator.report()

        # 即使有 ERROR，也允许生成计划（用户可自行判断）
        # 但只有 WARNING 时正常返回
        steps: List[ExecutionStep] = []
        step_counter = 1

        if not is_valid:
            # 校验失败，但仍生成可执行的步骤（标红让用户知晓）
            steps.append(ExecutionStep(
                step_id=step_counter,
                operation="validation_warning",
                target="model",
                params={"errors": report["error_count"]},
                description=f"⚠️ IR 存在 {report['error_count']} 个错误，编译结果可能无效",
            ))
            step_counter += 1

        # 2. 拓扑排序生成步骤
        step_counter = self._generate_steps(ir, steps, step_counter)

        # 3. 构建 ExecutionPlan
        elapsed_ms = (time.time() - start) * 1000

        plan = ExecutionPlan(
            model_id=ir.model_id,
            target_solver=ir.analysis.target_solver,
            steps=steps,
            validation_report=report,
            compile_time_ms=elapsed_ms,
        )
        return plan

    def _generate_steps(self, ir: StructuralIR, steps: List[ExecutionStep], counter: int) -> int:
        """生成所有执行步骤（按 SAP2000 调用顺序）"""

        # Step A: 初始化求解器实例
        steps.append(ExecutionStep(
            step_id=counter,
            operation="init_solver",
            target="sap2000",
            params={
                "solver": ir.analysis.target_solver.value,
                "units": ir.units,
                "model_file": f"{ir.model_id}.$2k",
            },
            description=f"初始化 {ir.analysis.target_solver.value} 实例",
        ))
        counter += 1

        # Step B: 新建空模型
        steps.append(ExecutionStep(
            step_id=counter,
            operation="new_model",
            target="model",
            params={"template": "blank"},
            description="创建空白模型",
        ))
        counter += 1

        # Step C: 定义材料（简化：按截面提取材料名）
        materials_added = set()
        for sec in ir.sections:
            if sec.material not in materials_added:
                steps.append(ExecutionStep(
                    step_id=counter,
                    operation="define_material",
                    target=f"material_{sec.material}",
                    params={"name": sec.material},
                    description=f"定义材料 {sec.material}",
                ))
                counter += 1
                materials_added.add(sec.material)

        # Step D: 定义截面
        for sec in ir.sections:
            steps.append(ExecutionStep(
                step_id=counter,
                operation="define_section",
                target=f"section_{sec.name}",
                params={
                    "name": sec.name,
                    "type": sec.type.value,
                    "depth": sec.depth,
                    "width": sec.width,
                    "rect_h": sec.rect_h,
                    "rect_b": sec.rect_b,
                    "material": sec.material,
                },
                description=f"定义截面 {sec.name} ({sec.type.value})",
            ))
            counter += 1

        # Step E: 创建节点（按 id 升序）
        for node in sorted(ir.nodes, key=lambda n: n.id):
            steps.append(ExecutionStep(
                step_id=counter,
                operation="create_node",
                target=f"node_{node.id}",
                params={
                    "id": node.id,
                    "x": node.x,
                    "y": node.y,
                    "z": node.z,
                },
                description=f"创建节点 {node.id} @ ({node.x}, {node.y}, {node.z})",
            ))
            counter += 1

        # Step F: 创建构件（必须在节点之后）
        for frame in sorted(ir.frames, key=lambda f: f.id):
            steps.append(ExecutionStep(
                step_id=counter,
                operation="create_frame",
                target=f"frame_{frame.id}",
                params={
                    "id": frame.id,
                    "i_node": frame.i_node,
                    "j_node": frame.j_node,
                    "section": frame.section,
                    "role": frame.role,
                },
                description=f"创建构件 {frame.id} ({frame.role}): 节点 {frame.i_node} → {frame.j_node}",
            ))
            counter += 1

        # Step G: 设置支座约束
        for node in ir.nodes:
            if node.restrain is not None:
                steps.append(ExecutionStep(
                    step_id=counter,
                    operation="assign_restraint",
                    target=f"node_{node.id}",
                    params={
                        "node_id": node.id,
                        "restrain": node.restrain,
                    },
                    description=f"节点 {node.id} 设置支座 {node.restrain}",
                ))
                counter += 1

        # Step H: 定义荷载工况
        for case in ir.load_cases:
            steps.append(ExecutionStep(
                step_id=counter,
                operation="define_load_case",
                target=f"case_{case.name}",
                params={
                    "name": case.name,
                    "type": case.type.value,
                    "self_weight": case.self_weight,
                    "scale_factor": case.scale_factor,
                },
                description=f"定义荷载工况 {case.name} ({case.type.value})",
            ))
            counter += 1

        # Step I: 施加点荷载
        for pl in ir.point_loads:
            steps.append(ExecutionStep(
                step_id=counter,
                operation="apply_point_load",
                target=f"node_{pl.node_id}",
                params={
                    "node_id": pl.node_id,
                    "case": pl.case,
                    "fx": pl.fx,
                    "fy": pl.fy,
                    "fz": pl.fz,
                    "mx": pl.mx,
                    "my": pl.my,
                    "mz": pl.mz,
                },
                description=f"节点 {pl.node_id} 施加 {pl.case} 工况点荷载",
            ))
            counter += 1

        # Step J: 施加均布荷载
        for dl in ir.dist_loads:
            steps.append(ExecutionStep(
                step_id=counter,
                operation="apply_dist_load",
                target=f"frame_{dl.frame_id}",
                params={
                    "frame_id": dl.frame_id,
                    "case": dl.case,
                    "load_type": dl.load_type,
                    "value": dl.value,
                },
                description=f"构件 {dl.frame_id} 施加 {dl.case} 工况 {dl.value} kN/m",
            ))
            counter += 1

        # Step K: 配置分析
        steps.append(ExecutionStep(
            step_id=counter,
            operation="configure_analysis",
            target="model",
            params={
                "type": ir.analysis.type.value,
                "design_code": ir.analysis.design_code,
            },
            description=f"配置分析: {ir.analysis.type.value}",
        ))
        counter += 1

        # Step L: 保存模型
        steps.append(ExecutionStep(
            step_id=counter,
            operation="save_model",
            target="model",
            params={"path": f"{ir.model_id}.$2k"},
            description=f"保存模型到 {ir.model_id}.$2k",
        ))
        counter += 1

        # Step M: 运行分析
        steps.append(ExecutionStep(
            step_id=counter,
            operation="run_analysis",
            target="model",
            params={"type": ir.analysis.type.value},
            description=f"运行 {ir.analysis.type.value} 分析",
        ))
        counter += 1

        # Step N: 提取结果
        result_types = ["frame_forces", "joint_displacements", "joint_reactions"]
        for rt in result_types:
            steps.append(ExecutionStep(
                step_id=counter,
                operation="extract_results",
                target=rt,
                params={"result_type": rt},
                description=f"提取结果: {rt}",
            ))
            counter += 1

        # Step O: 关闭求解器
        steps.append(ExecutionStep(
            step_id=counter,
            operation="close_solver",
            target="sap2000",
            params={},
            description="关闭求解器实例（释放 license）",
        ))
        counter += 1

        return counter


# ============================================================================
# 第 4 层：便捷接口（CLI / 测试入口）
# ============================================================================

def compile_ir_to_dict(ir: StructuralIR) -> Dict[str, Any]:
    """便捷函数：编译 IR 并返回字典"""
    compiler = IRCompiler()
    plan = compiler.compile(ir)
    return plan.to_dict()


def compile_ir_to_json(ir: StructuralIR, indent: int = 2) -> str:
    """便捷函数：编译 IR 并返回 JSON 字符串"""
    plan_dict = compile_ir_to_dict(ir)
    return json.dumps(plan_dict, indent=indent, ensure_ascii=False)


def build_sample_frame_ir() -> StructuralIR:
    """构建示例三跨框架 IR（用于测试）

    结构示意（两层三跨混凝土框架）：
        5───6───7───8      ← 第2层梁（z=3.5m）
        │   │   │   │
        1───2───3───4      ← 第1层梁（z=0m）

    支座设置：底层 4 个节点全部固接（[U,V,W] 锁死）
    """
    nodes = [
        # 底层节点（全部固接）
        Node(id=1, x=0, y=0, z=0, restrain=[True, True, True, False, False, False]),
        Node(id=2, x=5, y=0, z=0, restrain=[True, True, True, False, False, False]),
        Node(id=3, x=10, y=0, z=0, restrain=[True, True, True, False, False, False]),
        Node(id=4, x=15, y=0, z=0, restrain=[True, True, True, False, False, False]),
        # 顶层节点（自由）
        Node(id=5, x=0, y=0, z=3.5),
        Node(id=6, x=5, y=0, z=3.5),
        Node(id=7, x=10, y=0, z=3.5),
        Node(id=8, x=15, y=0, z=3.5),
    ]

    sections = [
        Section(
            name="COL400x400",
            type=SectionType.CONCRETE_RECT,
            rect_h=400,
            rect_b=400,
            material="C30",
        ),
        Section(
            name="BEAM300x600",
            type=SectionType.CONCRETE_RECT,
            rect_h=600,
            rect_b=300,
            material="C30",
        ),
    ]

    frames = [
        Frame(id=1, i=1, j=5, section="COL400x400", role="column"),
        Frame(id=2, i=2, j=6, section="COL400x400", role="column"),
        Frame(id=3, i=3, j=7, section="COL400x400", role="column"),
        Frame(id=4, i=4, j=8, section="COL400x400", role="column"),
        Frame(id=5, i=5, j=6, section="BEAM300x600", role="beam"),
        Frame(id=6, i=6, j=7, section="BEAM300x600", role="beam"),
        Frame(id=7, i=7, j=8, section="BEAM300x600", role="beam"),
    ]

    load_cases = [
        LoadCase(name="DEAD", type=LoadCaseType.DEAD, self_weight=True),
        LoadCase(name="LIVE", type=LoadCaseType.LIVE, scale_factor=1.0),
    ]

    dist_loads = [
        DistributedLoad(frame_id=5, case="DEAD", value=-15.0),
        DistributedLoad(frame_id=6, case="DEAD", value=-15.0),
        DistributedLoad(frame_id=7, case="DEAD", value=-15.0),
        DistributedLoad(frame_id=5, case="LIVE", value=-8.0),
        DistributedLoad(frame_id=6, case="LIVE", value=-8.0),
        DistributedLoad(frame_id=7, case="LIVE", value=-8.0),
    ]

    return StructuralIR(
        model_id="frame_2story_3span",
        name="两层三跨混凝土框架",
        units="kN_m_C",
        nodes=nodes,
        frames=frames,
        sections=sections,
        dist_loads=dist_loads,
        load_cases=load_cases,
        analysis=AnalysisSetting(
            type=AnalysisType.LINEAR_STATIC,
            target_solver=SolverType.SAP2000,
            design_code="GB50011",
        ),
    )


def build_3span_frame_9m() -> StructuralIR:
    """构建两层三跨混凝土框架（用户指定参数）

    结构示意（2 层 × 3 跨）：
        9 ─── 10 ─── 11 ─── 12      ← 第2层梁（z=3m）
        │     │     │     │
        1 ─── 2 ─── 3 ─── 4        ← 第1层梁（z=0m）

    节点 1-4 全部固接（[U,V,W] 锁死）

    参数：
        - 柱截面: 400x400 mm
        - 梁截面: 200x500 mm
        - 层高: 3 m
        - 跨长: 9 m
        - DEAD 荷载: -10 kN/m
        - LIVE 荷载: -5 kN/m
    """
    SPAN = 9.0  # 跨长 9m
    STORY = 3.0  # 层高 3m

    # 节点：底层 1-4，顶层 5-8
    nodes = []
    # 底层（固接）
    for i, x in enumerate([0, SPAN, 2*SPAN, 3*SPAN], start=1):
        nodes.append(Node(
            id=i, x=x, y=0, z=0,
            restrain=[True, True, True, False, False, False]
        ))
    # 顶层
    for i, x in enumerate([0, SPAN, 2*SPAN, 3*SPAN], start=5):
        nodes.append(Node(id=i, x=x, y=0, z=STORY))

    sections = [
        Section(
            name="COL400x400",
            type=SectionType.CONCRETE_RECT,
            rect_h=400,
            rect_b=400,
            material="C30",
        ),
        Section(
            name="BEAM200x500",
            type=SectionType.CONCRETE_RECT,
            rect_h=500,  # h = 截面高
            rect_b=200,  # b = 截面宽
            material="C30",
        ),
    ]

    # 构件：柱 1-4（底层→顶层），梁 5-7（顶层横向）
    frames = [
        # 柱
        Frame(id=1, i=1, j=5, section="COL400x400", role="column"),
        Frame(id=2, i=2, j=6, section="COL400x400", role="column"),
        Frame(id=3, i=3, j=7, section="COL400x400", role="column"),
        Frame(id=4, i=4, j=8, section="COL400x400", role="column"),
        # 二层梁
        Frame(id=5, i=5, j=6, section="BEAM200x500", role="beam"),
        Frame(id=6, i=6, j=7, section="BEAM200x500", role="beam"),
        Frame(id=7, i=7, j=8, section="BEAM200x500", role="beam"),
    ]

    load_cases = [
        LoadCase(name="DEAD", type=LoadCaseType.DEAD, self_weight=True),
        LoadCase(name="LIVE", type=LoadCaseType.LIVE, scale_factor=1.0),
    ]

    # 荷载：DEAD -10 kN/m, LIVE -5 kN/m（仅作用在 3 根二层梁上）
    dist_loads = []
    for fid in [5, 6, 7]:
        dist_loads.append(DistributedLoad(frame_id=fid, case="DEAD", value=-10.0))
        dist_loads.append(DistributedLoad(frame_id=fid, case="LIVE", value=-5.0))

    return StructuralIR(
        model_id="frame_2story_3span_9m",
        name="两层三跨混凝土框架 (9m跨, 3m层高, 400x400柱, 200x500梁, DEAD=10kN/m)",
        units="kN_m_C",
        nodes=nodes,
        frames=frames,
        sections=sections,
        dist_loads=dist_loads,
        load_cases=load_cases,
        analysis=AnalysisSetting(
            type=AnalysisType.LINEAR_STATIC,
            target_solver=SolverType.SAP2000,
            design_code="GB50011",
        ),
    )


# ============================================================================
# 主入口（独立运行测试）
# ============================================================================

def main():
    """主入口：演示完整编译流程"""
    print("=" * 70)
    print("Structural IR Compiler v1.0 - 测试入口")
    print("=" * 70)

    # 1. 构建示例 IR
    print("\n[1] 构建示例三跨框架 IR ...")
    ir = build_sample_frame_ir()
    print(f"    模型 ID: {ir.model_id}")
    print(f"    节点数: {len(ir.nodes)}")
    print(f"    构件数: {len(ir.frames)}")
    print(f"    截面数: {len(ir.sections)}")
    print(f"    工况数: {len(ir.load_cases)}")

    # 2. 编译
    print("\n[2] 编译 IR → ExecutionPlan ...")
    compiler = IRCompiler()
    plan = compiler.compile(ir)

    # 3. 输出校验报告
    print("\n[3] 校验报告:")
    report = plan.validation_report
    print(f"    通过: {report['passed']}")
    print(f"    错误数: {report['error_count']}")
    print(f"    警告数: {report['warning_count']}")

    if report['warnings']:
        print("\n    警告详情:")
        for w in report['warnings']:
            print(f"      - [{w['code']}] {w['message']}")

    # 4. 输出步骤概览
    print(f"\n[4] 执行步骤: 共 {len(plan.steps)} 步")
    print("    前 5 步预览:")
    for step in plan.steps[:5]:
        print(f"      [{step.step_id:02d}] {step.operation:<22} → {step.target}")

    print(f"\n    中段步骤:")
    for step in plan.steps[len(plan.steps)//2:len(plan.steps)//2+3]:
        print(f"      [{step.step_id:02d}] {step.operation:<22} → {step.target}")

    print(f"\n    末尾 5 步:")
    for step in plan.steps[-5:]:
        print(f"      [{step.step_id:02d}] {step.operation:<22} → {step.target}")

    # 5. 导出 JSON
    print("\n[5] 导出 ExecutionPlan JSON ...")
    output_path = Path("./frame_execution_plan.json")
    plan_dict = plan.to_dict()
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(plan_dict, f, indent=2, ensure_ascii=False)
    print(f"    已保存到: {output_path.resolve()}")

    # 6. 性能统计
    print(f"\n[6] 性能统计:")
    print(f"    编译耗时: {plan.compile_time_ms:.2f} ms")
    print(f"    步骤密度: {len(plan.steps) / len(ir.nodes):.1f} 步/节点")

    print("\n" + "=" * 70)
    print("✅ 测试完成")
    print("=" * 70)


if __name__ == "__main__":
    main()