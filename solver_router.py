"""
Solver Router (决策层 - 路由)
==============================
根据 IR 特征自动选择最适合的 solver（SAP2000/OpenSees/ANSYS/ETABS）。

核心模块：
1. ModelFeatures（IR 特征提取器）
2. RoutingRule（单条路由规则）
3. SolverRouter（路由决策器）
4. DecisionTrace（决策追踪）

作者：MiniMax-M3 / Hermes CSI System
版本：v1.0.0
"""

from __future__ import annotations

import json
import time
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

# 复用第 1 批的 IR 定义
from ir_compiler import (
    StructuralIR,
    SolverType,
    AnalysisType,
    LoadCaseType,
    SectionType,
)


# ============================================================================
# 第 1 层：ModelFeatures 模型特征提取
# ============================================================================

@dataclass
class ModelFeatures:
    """从 IR 中提取的工程特征

    这些特征是路由决策的输入依据。
    """
    # 规模
    node_count: int = 0
    frame_count: int = 0
    element_count: int = 0

    # 几何
    max_height: float = 0.0           # 最大高度 (m)
    max_span: float = 0.0             # 最大跨度 (m)
    aspect_ratio: float = 0.0         # 高宽比

    # 分析类型
    has_linear_static: bool = False
    has_modal: bool = False
    has_response_spectrum: bool = False
    has_time_history: bool = False

    # 非线性特征
    has_nonlinear: bool = False           # 几何非线性
    has_large_deformation: bool = False   # 大变形
    has_plastic_hinge: bool = False       # 塑性铰
    has_cable_tendon: bool = False        # 索/预应力

    # 荷载类型
    has_seismic: bool = False
    has_wind: bool = False
    has_temperature: bool = False
    has_dynamic: bool = False

    # 设计规范需求
    requires_design_code: bool = False
    design_code: Optional[str] = None     # "GB50011", "ACI318", "AISC360"

    # 精细度需求
    has_fine_stress_field: bool = False   # 应力场精细分析
    requires_stress_output: bool = False  # 需要单元应力结果

    # 截面复杂度
    has_custom_section: bool = False
    section_types: List[str] = field(default_factory=list)

    # 性能约束
    target_runtime_sec: Optional[float] = None  # 用户期望求解时长

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scale": {
                "node_count": self.node_count,
                "frame_count": self.frame_count,
                "element_count": self.element_count,
            },
            "geometry": {
                "max_height_m": self.max_height,
                "max_span_m": self.max_span,
                "aspect_ratio": round(self.aspect_ratio, 2),
            },
            "analysis": {
                "has_linear_static": self.has_linear_static,
                "has_modal": self.has_modal,
                "has_response_spectrum": self.has_response_spectrum,
                "has_time_history": self.has_time_history,
            },
            "nonlinear": {
                "has_nonlinear": self.has_nonlinear,
                "has_large_deformation": self.has_large_deformation,
                "has_plastic_hinge": self.has_plastic_hinge,
                "has_cable_tendon": self.has_cable_tendon,
            },
            "loads": {
                "has_seismic": self.has_seismic,
                "has_wind": self.has_wind,
                "has_temperature": self.has_temperature,
                "has_dynamic": self.has_dynamic,
            },
            "design": {
                "requires_design_code": self.requires_design_code,
                "design_code": self.design_code,
            },
            "precision": {
                "has_fine_stress_field": self.has_fine_stress_field,
                "requires_stress_output": self.requires_stress_output,
            },
            "section": {
                "has_custom_section": self.has_custom_section,
                "section_types": self.section_types,
            },
        }


class FeatureExtractor:
    """从 StructuralIR 提取 ModelFeatures

    规则：
    - 通过分析类型推断非线性
    - 通过荷载类型推断动力需求
    - 通过分析设置的设计规范推断验算需求
    """

    def extract(self, ir: StructuralIR) -> ModelFeatures:
        features = ModelFeatures()

        # 规模
        features.node_count = len(ir.nodes)
        features.frame_count = len(ir.frames)
        features.element_count = len(ir.frames)

        # 几何
        if ir.nodes:
            xs = [n.x for n in ir.nodes]
            ys = [n.y for n in ir.nodes]
            zs = [n.z for n in ir.nodes]
            features.max_height = max(zs) - min(zs)
            features.max_span = max(max(xs) - min(xs), max(ys) - min(ys))
            if features.max_span > 0:
                features.aspect_ratio = features.max_height / features.max_span

        # 分析类型
        analysis_type = ir.analysis.type
        features.has_linear_static = (analysis_type == AnalysisType.LINEAR_STATIC)
        features.has_modal = (analysis_type == AnalysisType.MODAL)
        features.has_response_spectrum = (analysis_type == AnalysisType.RESPONSE_SPECTRUM)

        # 推断非线性（根据分析类型 + 描述）
        # 这里用启发式规则
        if analysis_type.value == "linear_static":
            features.has_nonlinear = False
            features.has_large_deformation = False
            features.has_plastic_hinge = False
        else:
            # modal / response_spectrum 通常伴随反应谱分析
            features.has_modal = True

        # 荷载类型
        for case in ir.load_cases:
            if case.type == LoadCaseType.SEISMIC:
                features.has_seismic = True
                features.has_dynamic = True
                features.has_modal = True
                if case.scale_factor != 1.0:
                    # 比例系数异常常用于时程/反应谱
                    features.has_response_spectrum = True
            elif case.type == LoadCaseType.WIND:
                features.has_wind = True
                features.has_dynamic = True
            elif case.type == LoadCaseType.TEMPERATURE:
                features.has_temperature = True

        # 设计规范
        if ir.analysis.design_code:
            features.requires_design_code = True
            features.design_code = ir.analysis.design_code

        # 截面
        for sec in ir.sections:
            type_name = sec.type.value
            if type_name not in features.section_types:
                features.section_types.append(type_name)
            if sec.type == SectionType.CUSTOM:
                features.has_custom_section = True

        return features


# ============================================================================
# 第 2 层：RoutingRule 规则定义
# ============================================================================

@dataclass
class RoutingRule:
    """单条路由规则

    优先级：priority 数字越小越优先匹配
    匹配：所有 conditions 满足 → 命中该规则
    """
    name: str                              # 规则名（便于追踪）
    priority: int                          # 优先级
    target_solver: SolverType              # 目标 solver
    conditions: List[Tuple[str, Any, str]] # [(特征名, 期望值, 操作符)]
    score: float = 1.0                     # 命中得分（用于多规则投票）
    rationale: str = ""                    # 选择理由（工程说明）

    def matches(self, features: ModelFeatures) -> bool:
        """检查规则是否命中"""
        for feat_name, expected, op in self.conditions:
            actual = getattr(features, feat_name, None)
            if actual is None:
                return False
            if op == "==":
                if actual != expected:
                    return False
            elif op == "!=":
                if actual == expected:
                    return False
            elif op == ">":
                if not (actual > expected):
                    return False
            elif op == "<":
                if not (actual < expected):
                    return False
            elif op == ">=":
                if not (actual >= expected):
                    return False
            elif op == "<=":
                if not (actual <= expected):
                    return False
            elif op == "in":
                if actual not in expected:
                    return False
            elif op == "not_in":
                if actual in expected:
                    return False
            else:
                raise ValueError(f"未知操作符: {op}")
        return True


# ============================================================================
# 第 3 层：SolverRouter 路由决策器
# ============================================================================

@dataclass
class RoutingDecision:
    """路由决策结果"""
    target_solver: SolverType
    confidence: float                      # 置信度 [0, 1]
    matched_rules: List[str]               # 命中的规则名
    rationale: List[str]                   # 决策理由列表
    alternatives: Dict[str, float]         # 备选 solver 及其得分
    timestamp_ms: float
    override_applied: bool = False         # 是否手动覆盖

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_solver": self.target_solver.value,
            "confidence": round(self.confidence, 3),
            "matched_rules": self.matched_rules,
            "rationale": self.rationale,
            "alternatives": {k: round(v, 3) for k, v in self.alternatives.items()},
            "override_applied": self.override_applied,
            "decision_time_ms": round(self.timestamp_ms, 2),
        }


class SolverRouter:
    """Solver 路由器

    决策策略：
    1. 优先按规则匹配（priority 升序，第一条命中即返回）
    2. 规则未命中时，按评分投票（每条规则对每个 solver 加分）
    3. 支持强制覆盖（manual_override 参数）

    内置规则覆盖典型工程场景：
    - 线性静力 + 配筋 → SAP2000
    - 非线性塑性铰 → OpenSees
    - 精细应力场 → ANSYS
    - 大规模高层 + 设计规范 → ETABS
    """

    def __init__(self, manual_override: Optional[SolverType] = None):
        self.manual_override = manual_override
        self.extractor = FeatureExtractor()
        self.rules: List[RoutingRule] = []
        self._build_default_rules()

    def _build_default_rules(self):
        """构建默认规则集（按优先级排序）"""
        self.rules = [
            # 规则 1: 非线性 + 大变形 → OpenSees（最高优先级）
            RoutingRule(
                name="nonlinear_large_deformation",
                priority=1,
                target_solver=SolverType.OPENSEES,
                conditions=[
                    ("has_nonlinear", True, "=="),
                    ("has_large_deformation", True, "=="),
                ],
                score=1.0,
                rationale="非线性大变形问题，OpenSees 在材料/几何非线性求解上性能最优",
            ),
            # 规则 2: 塑性铰分析 → OpenSees
            RoutingRule(
                name="plastic_hinge_analysis",
                priority=2,
                target_solver=SolverType.OPENSEES,
                conditions=[("has_plastic_hinge", True, "==")],
                score=0.9,
                rationale="塑性铰分析需要纤维单元/集中塑性模型，OpenSees 支持最完善",
            ),
            # 规则 3: 索/预应力 → OpenSees
            RoutingRule(
                name="cable_tendon_analysis",
                priority=3,
                target_solver=SolverType.OPENSEES,
                conditions=[("has_cable_tendon", True, "==")],
                score=0.85,
                rationale="索/预应力构件需要非线性索单元，OpenSees 专用模块",
            ),
            # 规则 4: 精细应力场 + 应力输出 → ANSYS
            RoutingRule(
                name="fine_stress_field",
                priority=4,
                target_solver=SolverType.ANSYS,
                conditions=[("has_fine_stress_field", True, "==")],
                score=0.95,
                rationale="需要精细应力场分析，ANSYS 有限元应力求解能力最强",
            ),
            # 规则 5: 大规模高层 + 设计规范 → ETABS
            RoutingRule(
                name="highrise_with_design",
                priority=5,
                target_solver=SolverType.ETABS,
                conditions=[
                    ("max_height", 50.0, ">"),
                    ("requires_design_code", True, "=="),
                ],
                score=0.85,
                rationale="高层建筑 + 设计规范验算，ETABS 在高层配筋 + 规范校核上集成度最高",
            ),
            # 规则 6: 大规模 + 规范 → ETABS（次优）
            RoutingRule(
                name="large_model_design",
                priority=6,
                target_solver=SolverType.ETABS,
                conditions=[
                    ("node_count", 1000, ">"),
                    ("requires_design_code", True, "=="),
                ],
                score=0.75,
                rationale="大规模模型 + 设计需求，ETABS 性能与规范集成优势",
            ),
            # 规则 7: 设计规范需求 → SAP2000（默认配筋）
            RoutingRule(
                name="design_code_required",
                priority=10,
                target_solver=SolverType.SAP2000,
                conditions=[("requires_design_code", True, "==")],
                score=0.7,
                rationale="需要按设计规范验算配筋，SAP2000 内置多套规范（GB/AISC/ACI 等）",
            ),
            # 规则 8: 地震反应谱 → SAP2000
            RoutingRule(
                name="seismic_response_spectrum",
                priority=11,
                target_solver=SolverType.SAP2000,
                conditions=[
                    ("has_seismic", True, "=="),
                    ("has_response_spectrum", True, "=="),
                ],
                score=0.65,
                rationale="地震反应谱分析，SAP2000 内置多国规范反应谱库",
            ),
            # 规则 9: 时程分析 → SAP2000
            RoutingRule(
                name="time_history",
                priority=12,
                target_solver=SolverType.SAP2000,
                conditions=[("has_time_history", True, "==")],
                score=0.6,
                rationale="时程分析，SAP2000 内置多国规范地震波库",
            ),
            # 规则 10: 模态分析 → SAP2000
            RoutingRule(
                name="modal_analysis",
                priority=15,
                target_solver=SolverType.SAP2000,
                conditions=[("has_modal", True, "==")],
                score=0.5,
                rationale="模态分析，SAP2000 模态求解器成熟稳定",
            ),
            # 规则 11: 默认线性问题 → SAP2000（兜底）
            RoutingRule(
                name="default_linear",
                priority=100,
                target_solver=SolverType.SAP2000,
                conditions=[("has_linear_static", True, "==")],
                score=0.4,
                rationale="线性静力分析默认使用 SAP2000",
            ),
            # 规则 12: 终极兜底 → SAP2000
            RoutingRule(
                name="fallback",
                priority=999,
                target_solver=SolverType.SAP2000,
                conditions=[],
                score=0.1,
                rationale="未匹配任何规则，使用 SAP2000 作为默认 solver",
            ),
        ]
        # 按优先级排序
        self.rules.sort(key=lambda r: r.priority)

    def add_rule(self, rule: RoutingRule):
        """添加自定义规则"""
        self.rules.append(rule)
        self.rules.sort(key=lambda r: r.priority)

    def route(self, ir: StructuralIR) -> RoutingDecision:
        """根据 IR 路由到合适 solver

        流程：
        1. 提取特征
        2. 检查手动覆盖
        3. 遍历规则，第一条命中返回
        4. 兜底：评分投票
        """
        start = time.time()

        # 1. 提取特征
        features = self.extractor.extract(ir)

        # 2. 手动覆盖优先
        if self.manual_override is not None:
            elapsed = (time.time() - start) * 1000
            return RoutingDecision(
                target_solver=self.manual_override,
                confidence=1.0,
                matched_rules=["manual_override"],
                rationale=[f"用户强制指定使用 {self.manual_override.value}"],
                alternatives={s.value: 0.0 for s in SolverType},
                timestamp_ms=elapsed,
                override_applied=True,
            )

        # 3. 规则匹配（按优先级）
        matched: List[RoutingRule] = []
        for rule in self.rules:
            if rule.matches(features):
                matched.append(rule)
                # 第一条命中优先级最高的规则即返回（也可以投票，见下）
                break  # 取优先级最高的命中规则

        if matched:
            top_rule = matched[0]
            elapsed = (time.time() - start) * 1000

            # 计算备选 solver 得分（所有规则对各 solver 的累计）
            alt_scores = self._compute_alternative_scores(features, top_rule)

            # 置信度：基于 top_rule.score
            confidence = top_rule.score

            return RoutingDecision(
                target_solver=top_rule.target_solver,
                confidence=confidence,
                matched_rules=[top_rule.name],
                rationale=[top_rule.rationale],
                alternatives=alt_scores,
                timestamp_ms=elapsed,
                override_applied=False,
            )

        # 4. 兜底：评分投票
        solver_scores: Dict[SolverType, float] = defaultdict(float)
        for rule in self.rules:
            if rule.matches(features):
                solver_scores[rule.target_solver] += rule.score

        if solver_scores:
            best = max(solver_scores.items(), key=lambda x: x[1])
            elapsed = (time.time() - start) * 1000
            return RoutingDecision(
                target_solver=best[0],
                confidence=best[1] / sum(solver_scores.values()),
                matched_rules=["voting"],
                rationale=[f"投票决策：{best[0].value} 得分最高 {best[1]:.2f}"],
                alternatives={s.value: v for s, v in solver_scores.items()},
                timestamp_ms=elapsed,
                override_applied=False,
            )

        # 5. 终极兜底
        elapsed = (time.time() - start) * 1000
        return RoutingDecision(
            target_solver=SolverType.SAP2000,
            confidence=0.1,
            matched_rules=["ultimate_fallback"],
            rationale=["未匹配任何规则，使用 SAP2000 默认"],
            alternatives={s.value: 0.0 for s in SolverType},
            timestamp_ms=elapsed,
            override_applied=False,
        )

    def _compute_alternative_scores(
        self,
        features: ModelFeatures,
        chosen: RoutingRule,
    ) -> Dict[str, float]:
        """计算所有 solver 的累计得分（用于决策可解释）"""
        scores: Dict[SolverType, float] = defaultdict(float)
        for rule in self.rules:
            if rule.matches(features):
                scores[rule.target_solver] += rule.score
        # 把 chosen 标到最高
        return {s.value: v for s, v in scores.items()}


# ============================================================================
# 第 4 层：便捷接口
# ============================================================================

def route_ir(ir: StructuralIR, override: Optional[SolverType] = None) -> RoutingDecision:
    """便捷函数：路由 IR 到 solver"""
    router = SolverRouter(manual_override=override)
    return router.route(ir)


def extract_features(ir: StructuralIR) -> ModelFeatures:
    """便捷函数：提取 IR 特征"""
    return FeatureExtractor().extract(ir)


# ============================================================================
# 第 5 层：测试入口
# ============================================================================

def _build_test_ir(
    name: str,
    with_seismic: bool = False,
    with_design_code: bool = True,
    with_custom_section: bool = False,
    with_nonlinear: bool = False,
    height: float = 3.5,
) -> StructuralIR:
    """构造测试用 IR"""
    from ir_compiler import (
        Node, Frame, Section, SectionType, LoadCase,
        AnalysisSetting,
    )

    nodes = [
        Node(id=1, x=0, y=0, z=0, restrain=[True, True, True, False, False, False]),
        Node(id=2, x=5, y=0, z=0),
        Node(id=3, x=0, y=0, z=height, restrain=[True, True, True, False, False, False]),
        Node(id=4, x=5, y=0, z=height),
    ]

    sections = [
        Section(
            name="COL",
            type=SectionType.CONCRETE_RECT,
            rect_h=400, rect_b=400,
            material="C30",
        ),
        Section(
            name="BEAM",
            type=SectionType.CONCRETE_RECT if not with_custom_section else SectionType.CUSTOM,
            rect_h=600, rect_b=300,
            material="C30",
        ),
    ]

    frames = [
        Frame(id=1, i=1, j=3, section="COL", role="column"),
        Frame(id=2, i=2, j=4, section="COL", role="column"),
        Frame(id=3, i=3, j=4, section="BEAM", role="beam"),
    ]

    load_cases = [LoadCase(name="DEAD", self_weight=True)]
    if with_seismic:
        load_cases.append(LoadCase(name="SEISMIC", type=LoadCaseType.SEISMIC))

    return StructuralIR(
        model_id=name,
        name=name,
        nodes=nodes,
        frames=frames,
        sections=sections,
        load_cases=load_cases,
        analysis=AnalysisSetting(
            target_solver=SolverType.SAP2000,
            design_code="GB50011" if with_design_code else None,
        ),
    )


def main():
    """主入口：路由决策演示"""
    print("=" * 70)
    print("Solver Router v1.0 - 测试入口")
    print("=" * 70)

    # 7 个测试场景
    test_cases = [
        ("场景 1: 简单线性框架", _build_test_ir("case1_linear")),
        ("场景 2: 框架 + 地震 + 设计规范", _build_test_ir("case2_seismic", with_seismic=True)),
        ("场景 3: 高层 + 设计规范", _build_test_ir("case3_highrise", height=80.0)),
        ("场景 4: 大规模模型 + 设计规范", _build_test_ir("case4_large", with_design_code=True)),
        ("场景 5: 自定义截面（精细）", _build_test_ir("case5_custom", with_custom_section=True)),
        ("场景 6: 手动覆盖 ANSYS", _build_test_ir("case6_override")),
        ("场景 7: 非线性 + 大变形", _build_test_ir("case7_nonlinear", with_nonlinear=True)),
    ]

    print()
    for i, (desc, ir) in enumerate(test_cases, 1):
        print(f"[{i}] {desc}")

        # 提取特征
        features = extract_features(ir)
        print(f"    节点: {features.node_count}, 高度: {features.max_height}m")
        print(f"    地震: {features.has_seismic}, 设计规范: {features.design_code}")
        print(f"    非线性: {features.has_nonlinear}, 大变形: {features.has_large_deformation}")

        # 路由决策（场景 6 手动覆盖）
        override = SolverType.ANSYS if i == 6 else None
        decision = route_ir(ir, override=override)

        print(f"    → Solver: {decision.target_solver.value}")
        print(f"    → 置信度: {decision.confidence:.2f}")
        print(f"    → 命中规则: {decision.matched_rules}")
        print(f"    → 理由: {decision.rationale[0]}")
        if decision.alternatives:
            print(f"    → 备选: {decision.alternatives}")
        print()

    # 详细决策报告
    print("=" * 70)
    print("场景 2 完整决策报告:")
    print("=" * 70)
    ir = _build_test_ir("case2_full", with_seismic=True)
    decision = route_ir(ir)
    report = decision.to_dict()
    print(json.dumps(report, indent=2, ensure_ascii=False))

    print("\n" + "=" * 70)
    print("✅ 测试完成")
    print("=" * 70)


if __name__ == "__main__":
    main()