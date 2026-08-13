"""
Optimization Loop (决策层 - 闭环优化)
======================================
实现结构设计的闭环优化：
    Solve → Evaluate → Modify → Re-Solve
直到满足工程约束或达到最大迭代次数。

核心模块：
1. EvaluationMetrics（评估指标）
2. ResultEvaluator（结果评估器，从 mock solver 结果计算得分）
3. SectionModifier（截面修改器，遗传算法风格的修改策略）
4. OptimizationLoop（闭环控制器）

作者：MiniMax-M3 / Hermes CSI System
版本：v1.0.0
"""

from __future__ import annotations

import json
import time
import copy
import math
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple, Callable
from dataclasses import dataclass, field
from collections import defaultdict

# 复用第 1 批的 IR 定义
from ir_compiler import (
    StructuralIR, Node, Frame, Section, SectionType,
    LoadCase, LoadCaseType,
    AnalysisSetting, SolverType, AnalysisType,
)

# 复用第 2 批的路由
from solver_router import SolverRouter, SolverType as ST


# ============================================================================
# 第 1 层：评估指标与得分
# ============================================================================

class MetricLevel(str, Enum):
    """指标评级"""
    EXCELLENT = "excellent"   # < 0.7
    GOOD = "good"             # 0.7 - 0.85
    ACCEPTABLE = "acceptable" # 0.85 - 1.0
    OVER_DESIGN = "over_design"  # 1.0 - 1.2 (超配)
    FAIL = "fail"             # > 1.2 (不满足)


@dataclass
class EvaluationMetrics:
    """评估指标集合（结构工程常用）"""
    # 位移
    max_displacement_mm: float = 0.0         # 最大位移 (mm)
    displacement_ratio: float = 0.0          # 位移/限值

    # 层间位移角
    max_drift_ratio: float = 0.0             # 最大层间位移角
    drift_ratio_limit: float = 1.0 / 550     # 钢混框架限值

    # 应力利用
    max_utilization: float = 0.0             # 最大应力利用比
    avg_utilization: float = 0.0             # 平均利用比

    # 成本（基于截面总面积估算）
    total_section_area_m2: float = 0.0
    cost_estimate: float = 0.0               # 估算成本（归一化）

    # 规范校核
    code_compliance: bool = True
    violation_count: int = 0

    # 综合得分（0-1，越小越好）
    overall_score: float = 1.0

    # 评级
    level: MetricLevel = MetricLevel.ACCEPTABLE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "displacement": {
                "max_mm": round(self.max_displacement_mm, 2),
                "ratio": round(self.displacement_ratio, 3),
                "limit_mm": 30.0,  # 默认 30mm 或 L/500
            },
            "drift": {
                "max_ratio": round(self.max_drift_ratio, 5),
                "limit_ratio": round(self.drift_ratio_limit, 5),
                "ratio": round(self.max_drift_ratio / self.drift_ratio_limit, 3),
            },
            "utilization": {
                "max": round(self.max_utilization, 3),
                "avg": round(self.avg_utilization, 3),
            },
            "cost": {
                "section_area_m2": round(self.total_section_area_m2, 4),
                "estimate": round(self.cost_estimate, 2),
            },
            "code": {
                "compliant": self.code_compliance,
                "violations": self.violation_count,
            },
            "overall": {
                "score": round(self.overall_score, 3),
                "level": self.level.value,
            },
        }


# ============================================================================
# 第 2 层：ResultEvaluator 结果评估器
# ============================================================================

class SolverResults:
    """求解结果（mock solver 输出的结构化结果）"""
    def __init__(self):
        self.frame_forces: Dict[int, Dict[str, float]] = {}    # frame_id -> {P, V2, V3, M2, M3, T}
        self.joint_displacements: Dict[int, Dict[str, float]] = {}  # node_id -> {Ux, Uy, Uz, Rx, Ry, Rz}
        self.max_displacement_mm: float = 0.0
        self.max_drift_ratio: float = 0.0
        self.max_utilization: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frame_forces": self.frame_forces,
            "joint_displacements": self.joint_displacements,
            "max_displacement_mm": self.max_displacement_mm,
            "max_drift_ratio": self.max_drift_ratio,
            "max_utilization": self.max_utilization,
        }


class ResultEvaluator:
    """结果评估器：从 solver 结果计算工程指标

    设计哲学：
    - 利用比（utilization）是最核心指标
    - 位移 / 挠度是次要约束
    - 成本是辅助参考
    """

    # 默认限值
    DEFAULT_DISP_LIMIT_MM = 30.0
    DEFAULT_DRIFT_LIMIT = 1.0 / 550   # 钢混框架
    UTILIZATION_TARGET = 0.85         # 目标利用率
    UTILIZATION_MAX = 1.0             # 上限利用率

    def __init__(
        self,
        disp_limit_mm: float = DEFAULT_DISP_LIMIT_MM,
        drift_limit: float = DEFAULT_DRIFT_LIMIT,
    ):
        self.disp_limit_mm = disp_limit_mm
        self.drift_limit = drift_limit

    def evaluate(
        self,
        ir: StructuralIR,
        results: SolverResults,
    ) -> EvaluationMetrics:
        """评估求解结果，返回指标集合"""
        metrics = EvaluationMetrics()

        # 1. 位移指标
        if results.joint_displacements:
            displacements = [
                abs(d.get("Uz", 0.0))
                for d in results.joint_displacements.values()
            ]
            if displacements:
                # 位移从 m 转 mm
                max_disp_m = max(displacements)
                metrics.max_displacement_mm = max_disp_m * 1000
                metrics.displacement_ratio = metrics.max_displacement_mm / self.disp_limit_mm

        # 2. 层间位移角
        metrics.max_drift_ratio = results.max_drift_ratio
        metrics.drift_ratio_limit = self.drift_limit

        # 3. 利用率
        metrics.max_utilization = results.max_utilization
        if ir.frames:
            # 简化：所有构件利用率用 max 表示
            metrics.avg_utilization = results.max_utilization * 0.7

        # 4. 成本（按截面总面积估算）
        total_area = 0.0
        for frame in ir.frames:
            sec = next((s for s in ir.sections if s.name == frame.section), None)
            if sec:
                if sec.type == SectionType.CONCRETE_RECT and sec.rect_h and sec.rect_b:
                    # mm² → m²
                    area_m2 = (sec.rect_h * sec.rect_b) / 1e6
                    total_area += area_m2
                elif sec.type == SectionType.WIDE_FLANGE and sec.depth and sec.width:
                    area_m2 = (sec.depth * sec.width) / 1e6
                    total_area += area_m2
        metrics.total_section_area_m2 = total_area
        # 成本 = 截面面积 × 单位造价系数（归一化）
        metrics.cost_estimate = total_area * 1000

        # 5. 规范校核
        violations = 0
        if metrics.displacement_ratio > 1.0:
            violations += 1
        if metrics.max_drift_ratio > self.drift_limit:
            violations += 1
        if metrics.max_utilization > 1.0:
            violations += 1
        metrics.violation_count = violations
        metrics.code_compliance = (violations == 0)

        # 6. 综合得分（加权归一化）
        # 利用率主导（占比 50%），位移 + 挠度各 20%，成本 10%
        util_score = min(metrics.max_utilization / self.UTILIZATION_TARGET, 1.5)
        disp_score = metrics.displacement_ratio
        drift_score = metrics.max_drift_ratio / self.drift_limit
        cost_score = min(total_area / 0.5, 1.5)  # 0.5 m² 作为参考

        metrics.overall_score = (
            0.5 * util_score +
            0.2 * disp_score +
            0.2 * drift_score +
            0.1 * cost_score
        )

        # 7. 评级
        if metrics.max_utilization > 1.2 or metrics.displacement_ratio > 1.2:
            metrics.level = MetricLevel.FAIL
        elif metrics.max_utilization > 1.0 or metrics.displacement_ratio > 1.0:
            metrics.level = MetricLevel.OVER_DESIGN
        elif metrics.max_utilization > 0.85:
            metrics.level = MetricLevel.ACCEPTABLE
        elif metrics.max_utilization > 0.7:
            metrics.level = MetricLevel.GOOD
        else:
            metrics.level = MetricLevel.EXCELLENT

        return metrics


# ============================================================================
# 第 3 层：SectionModifier 截面修改器
# ============================================================================

class ModificationStrategy(str, Enum):
    """修改策略"""
    AGGRESSIVE = "aggressive"   # 大幅调整（±30%）
    NORMAL = "normal"           # 适中调整（±15%）
    CONSERVATIVE = "conservative"  # 谨慎调整（±5%）
    TARGETED = "targeted"       # 定向调整（按利用率定向）


@dataclass
class ModificationRecord:
    """单次修改记录"""
    iter_idx: int
    strategy: ModificationStrategy
    modified_sections: Dict[str, Dict[str, float]]  # section_name -> {param: new_value}
    rationale: str


class SectionModifier:
    """截面修改器

    修改策略：
    - 利用率过高 → 放大截面（强度不足）
    - 利用率过低 → 缩小截面（节省材料）
    - 位移超限 → 增加截面刚度
    """

    # 混凝土矩形截面标准尺寸序列（mm）
    CONCRETE_RECT_SIZES = [
        (300, 300), (300, 400), (300, 500), (300, 600),
        (400, 400), (400, 500), (400, 600), (400, 700), (400, 800),
        (500, 500), (500, 600), (500, 700), (500, 800), (500, 1000),
        (600, 600), (600, 800), (600, 1000),
        (800, 800), (800, 1000),
    ]

    # 修改系数
    STEP_UP_RATIO = 1.15      # +15%
    STEP_DOWN_RATIO = 0.90    # -10%
    STEP_AGGRESSIVE_UP = 1.30 # +30%
    STEP_AGGRESSIVE_DOWN = 0.75  # -25%

    def __init__(self, strategy: ModificationStrategy = ModificationStrategy.NORMAL):
        self.strategy = strategy

    def modify(
        self,
        ir: StructuralIR,
        metrics: EvaluationMetrics,
        iter_idx: int,
    ) -> Tuple[StructuralIR, ModificationRecord]:
        """根据评估结果修改截面

        Returns:
            (新 IR, 修改记录)
        """
        new_ir = ir.model_copy(deep=True)

        # 决策修改方向
        modified = {}
        rationale_parts = []

        # 规则 1: 利用率超 1.0 → 全面放大
        if metrics.max_utilization > 1.0:
            for sec in new_ir.sections:
                new_sec = self._increase_section(sec)
                modified[sec.name] = {"new_size": new_sec}
                self._apply_section_update(new_ir, sec.name, new_sec)
            rationale_parts.append(
                f"利用率 {metrics.max_utilization:.2f} > 1.0 → 放大截面"
            )

        # 规则 2: 位移超限 → 增加刚度
        elif metrics.displacement_ratio > 1.0:
            # 仅放大柱截面（提高整体侧向刚度）
            for sec in new_ir.sections:
                new_sec = self._increase_section(sec)
                modified[sec.name] = {"new_size": new_sec}
                self._apply_section_update(new_ir, sec.name, new_sec)
            rationale_parts.append(
                f"位移/限值 {metrics.displacement_ratio:.2f} > 1.0 → 增加截面"
            )

        # 规则 3: 利用率 < 0.7 且位移富余 → 缩小截面
        elif metrics.max_utilization < 0.7 and metrics.displacement_ratio < 0.7:
            for sec in new_ir.sections:
                new_sec = self._decrease_section(sec)
                modified[sec.name] = {"new_size": new_sec}
                self._apply_section_update(new_ir, sec.name, new_sec)
            rationale_parts.append(
                f"利用率 {metrics.max_utilization:.2f} < 0.7 且位移富余 → 缩小截面"
            )

        # 规则 4: 利用率适中但位移有富余 → 仅缩小柱
        elif metrics.max_utilization < 0.85 and metrics.displacement_ratio < 0.85:
            # 找柱截面（被 column 角色引用）
            column_sections = self._get_column_sections(new_ir)
            for sec_name in column_sections:
                sec = next(s for s in new_ir.sections if s.name == sec_name)
                new_sec = self._decrease_section(sec)
                modified[sec.name] = {"new_size": new_sec}
                self._apply_section_update(new_ir, sec.name, new_sec)
            rationale_parts.append(
                f"利用率/位移适中 → 缩小柱截面节省材料"
            )

        # 规则 5: 利用率刚好达 0.85-0.95 → 收敛停止
        else:
            rationale_parts.append(
                f"利用率 {metrics.max_utilization:.2f} 在目标区间 [0.85, 0.95] → 收敛"
            )

        record = ModificationRecord(
            iter_idx=iter_idx,
            strategy=self.strategy,
            modified_sections=modified,
            rationale="; ".join(rationale_parts) if rationale_parts else "无修改",
        )

        return new_ir, record

    def _increase_section(self, sec: Section) -> str:
        """放大截面"""
        if sec.type == SectionType.CONCRETE_RECT:
            cur_h, cur_b = sec.rect_h or 400, sec.rect_b or 400
            # 找下一档更大的标准尺寸
            candidates = [s for s in self.CONCRETE_RECT_SIZES if s[0] * s[1] > cur_h * cur_b]
            if candidates:
                new_h, new_b = candidates[0]
                return f"{new_h}x{new_b}"
        return f"{sec.rect_h}x{sec.rect_b}"

    def _decrease_section(self, sec: Section) -> str:
        """缩小截面"""
        if sec.type == SectionType.CONCRETE_RECT:
            cur_h, cur_b = sec.rect_h or 400, sec.rect_b or 400
            # 找下一档更小的标准尺寸
            candidates = [s for s in self.CONCRETE_RECT_SIZES if s[0] * s[1] < cur_h * cur_b]
            if candidates:
                new_h, new_b = candidates[-1]  # 取最接近的（最小可缩档）
                return f"{new_h}x{new_b}"
        return f"{sec.rect_h}x{sec.rect_b}"

    def _apply_section_update(self, ir: StructuralIR, sec_name: str, new_size_str: str):
        """应用截面更新到 IR"""
        try:
            new_h, new_b = map(int, new_size_str.split("x"))
            for sec in ir.sections:
                if sec.name == sec_name:
                    sec.rect_h = new_h
                    sec.rect_b = new_b
        except (ValueError, AttributeError):
            pass

    def _get_column_sections(self, ir: StructuralIR) -> List[str]:
        """获取所有柱截面名称"""
        col_secs = set()
        for f in ir.frames:
            if f.role == "column":
                col_secs.add(f.section)
        return list(col_secs)


# ============================================================================
# 第 4 层：MockSolver 求解器（用于演示）
# ============================================================================

class MockSolver:
    """Mock 求解器：模拟 SAP2000 的求解行为

    用于在没有真实 SAP2000 环境下演示优化闭环。
    真实部署时应替换为 SAP2000Worker（comtypes）。
    """

    def solve(self, ir: StructuralIR) -> SolverResults:
        """模拟求解，返回结果

        简化力学模型：
        - 总刚度 ∝ Σ(柱截面面积 × 数量)
        - 位移反比于刚度
        - 利用率 ∝ 荷载 / 截面承载力
        """
        results = SolverResults()

        # 1. 计算总刚度（按柱截面估算）
        col_area_total = 0.0
        beam_area_total = 0.0
        for f in ir.frames:
            sec = next((s for s in ir.sections if s.name == f.section), None)
            if not sec:
                continue
            area_m2 = 0.0
            if sec.type == SectionType.CONCRETE_RECT and sec.rect_h and sec.rect_b:
                area_m2 = (sec.rect_h * sec.rect_b) / 1e6
            elif sec.type == SectionType.WIDE_FLANGE and sec.depth and sec.width:
                area_m2 = (sec.depth * sec.width) / 1e6

            if f.role == "column":
                col_area_total += area_m2
            elif f.role == "beam":
                beam_area_total += area_m2

        total_area = col_area_total + beam_area_total

        # 2. 模拟位移（与刚度反相关）
        # 假设总荷载为常数（基于高度和跨度估算）
        height = max([n.z for n in ir.nodes]) - min([n.z for n in ir.nodes]) if ir.nodes else 3.5
        span_x = max([n.x for n in ir.nodes]) - min([n.x for n in ir.nodes]) if ir.nodes else 5.0

        # 等效侧向荷载 = 高度 × 单位宽度荷载
        equiv_lateral_load = height * 20.0  # 20 kN/m 假设

        # 位移 = 荷载 / 刚度（简化）
        # 基准：col_area=0.5m² → 位移 20mm
        base_disp = 20.0 * (equiv_lateral_load / 100.0) / max(col_area_total, 0.1)
        results.max_displacement_mm = base_disp

        # 3. 模拟层间位移角
        # 假设各层均匀分布
        n_stories = max(1, int(height / 3.5))
        results.max_drift_ratio = base_disp / 1000.0 / height if height > 0 else 0.0

        # 4. 模拟利用率（与荷载和截面相关）
        # 假设柱承担总竖向荷载（高度相关）
        n_floors = max(1, int(height / 3.5))
        # 每层 50 kN/m²（含恒活载）+ 柱自重
        floor_load = 50.0  # kN/m²
        # 总荷载 = 跨度 × 每层荷载 × 层数 × 受荷面积系数
        tributary_area = span_x * 5.0  # 每柱受荷面积 5m 宽
        total_load = tributary_area * floor_load * n_floors

        # 利用率 = 荷载 / (柱总截面承载力)
        # 基准：1 根 400x400 柱 (0.16 m²) 承载 720 kN → 利用率 ≈ 0.85
        base_capacity_per_m2 = 9000.0  # kN/m²（C30 混凝土）
        capacity = col_area_total * base_capacity_per_m2
        utilization = total_load / max(capacity, 1.0)

        # 轻微扰动（用固定种子保证测试可重复）
        import random
        rng = random.Random(42)
        utilization *= rng.uniform(0.95, 1.05)
        utilization = max(0.3, min(utilization, 1.8))
        results.max_utilization = utilization

        # 5. 模拟节点位移
        for node in ir.nodes:
            # 上层节点位移更大
            z_factor = node.z / max(height, 1.0)
            disp_z = -base_disp / 1000.0 * z_factor
            results.joint_displacements[node.id] = {
                "Ux": 0.0, "Uy": 0.0,
                "Uz": disp_z,
                "Rx": 0.0, "Ry": 0.0, "Rz": 0.0,
            }

        # 6. 模拟构件内力
        for frame in ir.frames:
            sec = next((s for s in ir.sections if s.name == frame.section), None)
            if not sec:
                continue
            results.frame_forces[frame.id] = {
                "P": utilization * 100,
                "V2": utilization * 30,
                "V3": utilization * 20,
                "M2": utilization * 50,
                "M3": utilization * 80,
                "T": 0.0,
            }

        return results


# ============================================================================
# 第 5 层：OptimizationLoop 闭环控制器
# ============================================================================

class LoopStopReason(str, Enum):
    """循环停止原因"""
    CONVERGED = "converged"               # 收敛
    MAX_ITERATIONS = "max_iterations"     # 达到最大迭代次数
    NO_IMPROVEMENT = "no_improvement"     # 连续无改进
    ERROR = "error"                       # 错误停止


@dataclass
class LoopIteration:
    """单次迭代记录"""
    iter_idx: int
    metrics: EvaluationMetrics
    modification: Optional[ModificationRecord]
    solver_results: SolverResults
    timestamp_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iter_idx": self.iter_idx,
            "metrics": self.metrics.to_dict(),
            "modification": {
                "strategy": self.modification.strategy.value if self.modification else None,
                "modified_sections": self.modification.modified_sections if self.modification else {},
                "rationale": self.modification.rationale if self.modification else "",
            } if self.modification else None,
            "iteration_time_ms": round(self.timestamp_ms, 2),
        }


@dataclass
class LoopResult:
    """闭环优化结果"""
    initial_ir: StructuralIR
    final_ir: StructuralIR
    initial_metrics: EvaluationMetrics
    final_metrics: EvaluationMetrics
    history: List[LoopIteration]
    iterations_run: int
    stop_reason: LoopStopReason
    total_time_ms: float
    improvement_pct: float  # 综合得分改进百分比

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iterations_run": self.iterations_run,
            "stop_reason": self.stop_reason.value,
            "total_time_ms": round(self.total_time_ms, 2),
            "improvement_pct": round(self.improvement_pct, 1),
            "initial_score": round(self.initial_metrics.overall_score, 3),
            "final_score": round(self.final_metrics.overall_score, 3),
            "initial_level": self.initial_metrics.level.value,
            "final_level": self.final_metrics.level.value,
            "history": [h.to_dict() for h in self.history],
        }


class OptimizationLoop:
    """结构优化闭环控制器

    流程：
        for i in range(max_iter):
            1. solver.solve(ir)        # 求解
            2. evaluator.evaluate()    # 评估
            3. check convergence        # 检查收敛
            4. modifier.modify()       # 修改
            5. ir = new_ir             # 准备下一轮
    """

    def __init__(
        self,
        max_iterations: int = 10,
        target_utilization: float = 0.85,
        convergence_tolerance: float = 0.02,
        patience: int = 3,
        strategy: ModificationStrategy = ModificationStrategy.NORMAL,
        solver: Optional[Any] = None,
        on_iteration: Optional[Callable[[LoopIteration], None]] = None,
    ):
        self.max_iterations = max_iterations
        self.target_utilization = target_utilization
        self.convergence_tolerance = convergence_tolerance
        self.patience = patience
        self.strategy = strategy
        self.solver = solver or MockSolver()
        self.evaluator = ResultEvaluator()
        self.modifier = SectionModifier(strategy=strategy)
        self.on_iteration = on_iteration

    def run(self, ir: StructuralIR) -> LoopResult:
        """执行优化闭环

        Args:
            ir: 初始 IR

        Returns:
            LoopResult: 优化结果（含历史）
        """
        start_total = time.time()
        current_ir = ir.model_copy(deep=True)
        history: List[LoopIteration] = []
        no_improve_count = 0
        stop_reason = LoopStopReason.MAX_ITERATIONS

        # 第一次求解（初始模型）
        iter_idx = 0
        iter_start = time.time()

        solver_results = self.solver.solve(current_ir)
        metrics = self.evaluator.evaluate(current_ir, solver_results)
        iter_time = (time.time() - iter_start) * 1000

        initial_iter = LoopIteration(
            iter_idx=0,
            metrics=metrics,
            modification=None,
            solver_results=solver_results,
            timestamp_ms=iter_time,
        )
        history.append(initial_iter)
        if self.on_iteration:
            self.on_iteration(initial_iter)

        initial_metrics = metrics
        prev_score = metrics.overall_score

        # 主循环
        for i in range(1, self.max_iterations + 1):
            iter_idx = i
            iter_start = time.time()

            # 检查收敛
            if self._is_converged(metrics):
                stop_reason = LoopStopReason.CONVERGED
                break

            # 检查是否需要停止（连续无改进）
            if metrics.overall_score >= prev_score - self.convergence_tolerance:
                no_improve_count += 1
                if no_improve_count >= self.patience:
                    stop_reason = LoopStopReason.NO_IMPROVEMENT
                    break
            else:
                no_improve_count = 0
            prev_score = metrics.overall_score

            # 修改
            new_ir, mod_record = self.modifier.modify(current_ir, metrics, iter_idx)

            # 求解
            solver_results = self.solver.solve(new_ir)

            # 评估
            metrics = self.evaluator.evaluate(new_ir, solver_results)
            iter_time = (time.time() - iter_start) * 1000

            iter_record = LoopIteration(
                iter_idx=iter_idx,
                metrics=metrics,
                modification=mod_record,
                solver_results=solver_results,
                timestamp_ms=iter_time,
            )
            history.append(iter_record)
            if self.on_iteration:
                self.on_iteration(iter_record)

            current_ir = new_ir

        # 计算改进百分比
        initial_score = initial_metrics.overall_score
        final_score = metrics.overall_score
        if initial_score > 0:
            improvement_pct = ((initial_score - final_score) / initial_score) * 100
        else:
            improvement_pct = 0.0

        total_time = (time.time() - start_total) * 1000

        return LoopResult(
            initial_ir=ir,
            final_ir=current_ir,
            initial_metrics=initial_metrics,
            final_metrics=metrics,
            history=history,
            iterations_run=len(history) - 1,
            stop_reason=stop_reason,
            total_time_ms=total_time,
            improvement_pct=improvement_pct,
        )

    def _is_converged(self, metrics: EvaluationMetrics) -> bool:
        """检查是否收敛

        收敛条件：
        - 利用率在 [target - tol, target + tol] 区间
        - 位移/限值 < 1.0
        - 规范校核通过
        """
        util_ok = abs(metrics.max_utilization - self.target_utilization) <= self.convergence_tolerance
        disp_ok = metrics.displacement_ratio <= 1.0
        drift_ok = metrics.max_drift_ratio <= metrics.drift_ratio_limit
        return util_ok and disp_ok and drift_ok


# ============================================================================
# 第 6 层：便捷接口
# ============================================================================

def run_optimization(
    ir: StructuralIR,
    max_iterations: int = 10,
    target_utilization: float = 0.85,
    solver: Optional[Any] = None,
) -> LoopResult:
    """便捷函数：运行结构优化闭环

    Args:
        ir: 结构 IR
        max_iterations: 最大迭代次数
        target_utilization: 目标利用率
        solver: 自定义 Solver（如 SAP2000Worker），默认使用 MockSolver
    """
    loop = OptimizationLoop(
        max_iterations=max_iterations,
        target_utilization=target_utilization,
        solver=solver,
    )
    return loop.run(ir)


# ============================================================================
# 第 7 层：测试入口
# ============================================================================

def _build_initial_ir(scenario: str = "under_design") -> StructuralIR:
    """构造测试初始 IR"""
    if scenario == "under_design":
        # 欠设计：截面偏小
        nodes = [
            Node(id=1, x=0, y=0, z=0, restrain=[True, True, True, False, False, False]),
            Node(id=2, x=5, y=0, z=0),
            Node(id=3, x=0, y=0, z=3.5, restrain=[True, True, True, False, False, False]),
            Node(id=4, x=5, y=0, z=3.5),
        ]
        sections = [
            Section(name="COL", type=SectionType.CONCRETE_RECT,
                    rect_h=300, rect_b=300, material="C30"),
            Section(name="BEAM", type=SectionType.CONCRETE_RECT,
                    rect_h=400, rect_b=200, material="C30"),
        ]
    elif scenario == "over_design":
        # 超设计：截面偏大
        sections = [
            Section(name="COL", type=SectionType.CONCRETE_RECT,
                    rect_h=800, rect_b=800, material="C30"),
            Section(name="BEAM", type=SectionType.CONCRETE_RECT,
                    rect_h=800, rect_b=400, material="C30"),
        ]
        nodes = [
            Node(id=1, x=0, y=0, z=0, restrain=[True, True, True, False, False, False]),
            Node(id=2, x=5, y=0, z=0),
            Node(id=3, x=0, y=0, z=3.5, restrain=[True, True, True, False, False, False]),
            Node(id=4, x=5, y=0, z=3.5),
        ]
    else:
        # 适中
        sections = [
            Section(name="COL", type=SectionType.CONCRETE_RECT,
                    rect_h=400, rect_b=400, material="C30"),
            Section(name="BEAM", type=SectionType.CONCRETE_RECT,
                    rect_h=600, rect_b=300, material="C30"),
        ]
        nodes = [
            Node(id=1, x=0, y=0, z=0, restrain=[True, True, True, False, False, False]),
            Node(id=2, x=5, y=0, z=0),
            Node(id=3, x=0, y=0, z=3.5, restrain=[True, True, True, False, False, False]),
            Node(id=4, x=5, y=0, z=3.5),
        ]

    frames = [
        Frame(id=1, i=1, j=3, section="COL", role="column"),
        Frame(id=2, i=2, j=4, section="COL", role="column"),
        Frame(id=3, i=3, j=4, section="BEAM", role="beam"),
    ]
    load_cases = [
        LoadCase(name="DEAD", self_weight=True),
        LoadCase(name="LIVE"),
    ]

    return StructuralIR(
        model_id=f"opt_{scenario}",
        name=f"优化测试_{scenario}",
        nodes=nodes,
        frames=frames,
        sections=sections,
        load_cases=load_cases,
        analysis=AnalysisSetting(target_solver=SolverType.SAP2000, design_code="GB50011"),
    )


def main():
    """主入口：优化闭环演示"""
    print("=" * 70)
    print("Optimization Loop v1.0 - 测试入口")
    print("=" * 70)

    scenarios = [
        ("场景 1: 欠设计（截面偏小）", "under_design"),
        ("场景 2: 超设计（截面偏大）", "over_design"),
        ("场景 3: 适中设计", "normal"),
    ]

    for desc, scenario in scenarios:
        print(f"\n{'=' * 70}")
        print(f"{desc}")
        print("=" * 70)

        ir = _build_initial_ir(scenario)

        def print_iter(iter_record: LoopIteration):
            m = iter_record.metrics
            print(f"  迭代 [{iter_record.iter_idx}] "
                  f"利用率: {m.max_utilization:.3f} "
                  f"位移比: {m.displacement_ratio:.3f} "
                  f"得分: {m.overall_score:.3f} "
                  f"评级: {m.level.value}")

        loop = OptimizationLoop(
            max_iterations=10,
            target_utilization=0.85,
            on_iteration=print_iter,
        )
        result = loop.run(ir)

        print(f"\n  → 停止原因: {result.stop_reason.value}")
        print(f"  → 总迭代: {result.iterations_run}")
        print(f"  → 初始得分: {result.initial_metrics.overall_score:.3f} "
              f"({result.initial_metrics.level.value})")
        print(f"  → 最终得分: {result.final_metrics.overall_score:.3f} "
              f"({result.final_metrics.level.value})")
        print(f"  → 改进: {result.improvement_pct:+.1f}%")
        print(f"  → 总耗时: {result.total_time_ms:.2f} ms")

    # 详细报告导出（场景 1）
    print(f"\n{'=' * 70}")
    print("场景 1 完整报告（JSON）:")
    print("=" * 70)
    ir = _build_initial_ir("under_design")
    result = run_optimization(ir, max_iterations=5)
    report = result.to_dict()
    output_path = Path("./optimization_report.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"已保存到: {output_path.resolve()}")
    print(f"\n关键摘要:")
    print(f"  停止原因: {report['stop_reason']}")
    print(f"  改进: {report['improvement_pct']}%")
    print(f"  迭代次数: {report['iterations_run']}")

    print(f"\n{'=' * 70}")
    print("✅ 测试完成")
    print("=" * 70)


if __name__ == "__main__":
    main()