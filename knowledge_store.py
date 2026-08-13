"""
Knowledge Store (存储层 - 知识库)
==================================
结构案例库 + 相似度检索（RAG for CAE 简化版）。

核心模块：
1. CaseRecord（单条案例记录）
2. FeatureVector（特征向量提取）
3. SimilarityCalculator（相似度计算）
4. KnowledgeStore（CRUD + 检索）

作者：MiniMax-M3 / Hermes CSI System
版本：v1.0.0
"""

from __future__ import annotations

import json
import time
import math
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime
from collections import defaultdict

# 复用前 3 批
from ir_compiler import StructuralIR, SolverType
from optimization_loop import LoopResult, EvaluationMetrics, MetricLevel


# ============================================================================
# 第 1 层：CaseRecord 案例记录
# ============================================================================

class CaseSource(str, Enum):
    """案例来源"""
    OPTIMIZATION = "optimization"     # 来自优化闭环
    MANUAL = "manual"                 # 手动添加
    IMPORTED = "imported"             # 外部导入


@dataclass
class CaseRecord:
    """单条结构案例记录"""
    case_id: str
    timestamp: str
    source: CaseSource

    # 模型特征（用于检索）
    model_id: str
    model_name: str
    node_count: int
    frame_count: int
    max_height_m: float
    max_span_m: float
    target_solver: str

    # 结果摘要
    final_utilization: float
    final_displacement_ratio: float
    final_score: float
    final_level: str
    iterations_run: int
    improvement_pct: float

    # 标签（便于分类）
    tags: List[str] = field(default_factory=list)

    # 完整数据（IR + 结果，可选）
    ir_snapshot: Optional[Dict[str, Any]] = None
    metrics_snapshot: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "timestamp": self.timestamp,
            "source": self.source.value,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "features": {
                "node_count": self.node_count,
                "frame_count": self.frame_count,
                "max_height_m": self.max_height_m,
                "max_span_m": self.max_span_m,
                "target_solver": self.target_solver,
            },
            "results": {
                "final_utilization": round(self.final_utilization, 3),
                "final_displacement_ratio": round(self.final_displacement_ratio, 3),
                "final_score": round(self.final_score, 3),
                "final_level": self.final_level,
                "iterations_run": self.iterations_run,
                "improvement_pct": round(self.improvement_pct, 1),
            },
            "tags": self.tags,
            # 完整快照默认不导出（除非显式请求）
            "_has_full_snapshot": self.ir_snapshot is not None,
        }

    def to_full_dict(self) -> Dict[str, Any]:
        """导出完整数据（含 IR 和指标快照）"""
        d = self.to_dict()
        d["ir_snapshot"] = self.ir_snapshot
        d["metrics_snapshot"] = self.metrics_snapshot
        return d


# ============================================================================
# 第 2 层：FeatureExtractor 特征提取
# ============================================================================

class FeatureExtractor:
    """从 IR + 结果中提取特征向量"""

    @staticmethod
    def from_ir(ir: StructuralIR) -> Dict[str, float]:
        """从 IR 提取几何特征"""
        if not ir.nodes:
            return {
                "node_count": 0,
                "frame_count": 0,
                "max_height": 0.0,
                "max_span": 0.0,
                "aspect_ratio": 0.0,
            }

        xs = [n.x for n in ir.nodes]
        ys = [n.y for n in ir.nodes]
        zs = [n.z for n in ir.nodes]

        max_height = max(zs) - min(zs)
        max_span = max(max(xs) - min(xs), max(ys) - min(ys))
        aspect_ratio = max_height / max_span if max_span > 0 else 0.0

        return {
            "node_count": float(len(ir.nodes)),
            "frame_count": float(len(ir.frames)),
            "max_height": max_height,
            "max_span": max_span,
            "aspect_ratio": aspect_ratio,
        }

    @staticmethod
    def from_metrics(metrics: EvaluationMetrics) -> Dict[str, float]:
        """从指标提取性能特征"""
        return {
            "utilization": metrics.max_utilization,
            "displacement_ratio": metrics.displacement_ratio,
            "score": metrics.overall_score,
            "cost": metrics.cost_estimate,
        }


# ============================================================================
# 第 3 层：SimilarityCalculator 相似度计算
# ============================================================================

class SimilarityMethod(str, Enum):
    """相似度计算方法"""
    EUCLIDEAN = "euclidean"           # 欧氏距离（标准化后）
    WEIGHTED = "weighted"             # 加权欧氏距离
    TAG_OVERLAP = "tag_overlap"       # 标签 Jaccard 相似度
    HYBRID = "hybrid"                 # 混合（几何 + 性能 + 标签）


@dataclass
class SimilarityWeights:
    """混合相似度权重"""
    geometry: float = 0.5    # 几何特征权重
    performance: float = 0.3  # 性能特征权重
    tag: float = 0.2          # 标签权重


class SimilarityCalculator:
    """相似度计算器

    几何特征：节点数、高度、跨度、高宽比
    性能特征：利用率、位移比、得分
    标签特征：Jaccard 相似度
    """

    # 特征归一化系数（用于无量纲化）
    NORM_FACTORS = {
        "node_count": 1000.0,    # 1000 节点为基准
        "frame_count": 500.0,
        "max_height": 100.0,     # 100m 为基准
        "max_span": 50.0,        # 50m 为基准
        "aspect_ratio": 5.0,
        "utilization": 1.0,
        "displacement_ratio": 1.0,
        "score": 1.5,
    }

    def __init__(self, method: SimilarityMethod = SimilarityMethod.HYBRID,
                 weights: Optional[SimilarityWeights] = None):
        self.method = method
        self.weights = weights or SimilarityWeights()

    def compute(
        self,
        query_features: Dict[str, float],
        case_features: Dict[str, float],
        query_tags: Optional[List[str]] = None,
        case_tags: Optional[List[str]] = None,
    ) -> float:
        """计算相似度（0-1，越大越相似）"""
        if self.method == SimilarityMethod.EUCLIDEAN:
            return self._euclidean(query_features, case_features)
        elif self.method == SimilarityMethod.WEIGHTED:
            return self._weighted_euclidean(query_features, case_features)
        elif self.method == SimilarityMethod.TAG_OVERLAP:
            return self._tag_jaccard(query_tags or [], case_tags or [])
        elif self.method == SimilarityMethod.HYBRID:
            geo = self._euclidean_subset(query_features, case_features,
                                          ["node_count", "frame_count",
                                           "max_height", "max_span", "aspect_ratio"])
            perf = self._euclidean_subset(query_features, case_features,
                                            ["utilization", "displacement_ratio", "score"])
            tag = self._tag_jaccard(query_tags or [], case_tags or [])

            return (
                self.weights.geometry * geo +
                self.weights.performance * perf +
                self.weights.tag * tag
            )
        return 0.0

    def _euclidean(self, a: Dict[str, float], b: Dict[str, float]) -> float:
        """标准欧氏相似度（1 / (1 + 距离)）"""
        common = set(a.keys()) & set(b.keys())
        if not common:
            return 0.0
        dist_sq = 0.0
        for k in common:
            norm = self.NORM_FACTORS.get(k, 1.0)
            da = (a[k] - b[k]) / norm
            dist_sq += da * da
        dist = math.sqrt(dist_sq)
        return 1.0 / (1.0 + dist)

    def _weighted_euclidean(self, a: Dict[str, float], b: Dict[str, float]) -> float:
        """加权欧氏距离"""
        # 性能特征权重更高
        perf_weight = {"utilization": 2.0, "displacement_ratio": 2.0, "score": 2.0}
        common = set(a.keys()) & set(b.keys())
        if not common:
            return 0.0
        dist_sq = 0.0
        for k in common:
            norm = self.NORM_FACTORS.get(k, 1.0)
            w = perf_weight.get(k, 1.0)
            da = (a[k] - b[k]) / norm
            dist_sq += w * da * da
        dist = math.sqrt(dist_sq)
        return 1.0 / (1.0 + dist)

    def _euclidean_subset(self, a: Dict[str, float], b: Dict[str, float],
                           keys: List[str]) -> float:
        """子集欧氏相似度"""
        sub_a = {k: a.get(k, 0.0) for k in keys}
        sub_b = {k: b.get(k, 0.0) for k in keys}
        return self._euclidean(sub_a, sub_b)

    def _tag_jaccard(self, tags_a: List[str], tags_b: List[str]) -> float:
        """Jaccard 标签相似度"""
        if not tags_a and not tags_b:
            return 0.0
        set_a = set(tags_a)
        set_b = set(tags_b)
        if not set_a or not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union) if union else 0.0


# ============================================================================
# 第 4 层：KnowledgeStore 知识库
# ============================================================================

@dataclass
class RetrievalResult:
    """检索结果"""
    case: CaseRecord
    similarity: float
    rank: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rank": self.rank,
            "similarity": round(self.similarity, 4),
            "case_id": self.case.case_id,
            "model_id": self.case.model_id,
            "model_name": self.case.model_name,
            "features": {
                "node_count": self.case.node_count,
                "max_height_m": self.case.max_height_m,
            },
            "results": {
                "final_utilization": self.case.final_utilization,
                "final_score": self.case.final_score,
                "level": self.case.final_level,
                "improvement_pct": self.case.improvement_pct,
            },
            "tags": self.case.tags,
        }


class KnowledgeStore:
    """结构案例知识库

    功能：
    - save: 保存案例
    - load: 从文件加载案例库
    - retrieve: 相似度检索 Top-K
    - list_all: 列出全部案例
    - delete: 删除案例
    - export/import: 案例库导入导出

    存储格式：JSON 文件
    """

    def __init__(
        self,
        db_path: str = "./knowledge_db.json",
        similarity_method: SimilarityMethod = SimilarityMethod.HYBRID,
    ):
        self.db_path = Path(db_path)
        self.cases: List[CaseRecord] = []
        self.similarity = SimilarityCalculator(method=similarity_method)
        self._load()

    # ------------------------- CRUD -------------------------

    def save_case_from_loop(
        self,
        ir: StructuralIR,
        loop_result: LoopResult,
        tags: Optional[List[str]] = None,
        include_snapshot: bool = False,
    ) -> CaseRecord:
        """从优化闭环结果保存一条案例"""
        case_id = f"case_{int(time.time() * 1000)}"

        # 提取几何特征
        geo = FeatureExtractor.from_ir(ir)

        case = CaseRecord(
            case_id=case_id,
            timestamp=datetime.now().isoformat(),
            source=CaseSource.OPTIMIZATION,
            model_id=ir.model_id,
            model_name=ir.name,
            node_count=int(geo["node_count"]),
            frame_count=int(geo["frame_count"]),
            max_height_m=geo["max_height"],
            max_span_m=geo["max_span"],
            target_solver=ir.analysis.target_solver.value,
            final_utilization=loop_result.final_metrics.max_utilization,
            final_displacement_ratio=loop_result.final_metrics.displacement_ratio,
            final_score=loop_result.final_metrics.overall_score,
            final_level=loop_result.final_metrics.level.value,
            iterations_run=loop_result.iterations_run,
            improvement_pct=loop_result.improvement_pct,
            tags=tags or self._auto_tags(ir, loop_result),
        )

        if include_snapshot:
            case.ir_snapshot = ir.model_dump()
            case.metrics_snapshot = loop_result.final_metrics.to_dict()

        self.cases.append(case)
        self._save()
        return case

    def save_case_manual(
        self,
        model_id: str,
        model_name: str,
        node_count: int,
        frame_count: int,
        max_height_m: float,
        max_span_m: float,
        target_solver: str,
        final_utilization: float,
        final_displacement_ratio: float,
        final_score: float,
        final_level: str,
        tags: Optional[List[str]] = None,
    ) -> CaseRecord:
        """手动添加一条案例"""
        case_id = f"case_{int(time.time() * 1000)}"

        case = CaseRecord(
            case_id=case_id,
            timestamp=datetime.now().isoformat(),
            source=CaseSource.MANUAL,
            model_id=model_id,
            model_name=model_name,
            node_count=node_count,
            frame_count=frame_count,
            max_height_m=max_height_m,
            max_span_m=max_span_m,
            target_solver=target_solver,
            final_utilization=final_utilization,
            final_displacement_ratio=final_displacement_ratio,
            final_score=final_score,
            final_level=final_level,
            iterations_run=0,
            improvement_pct=0.0,
            tags=tags or [],
        )
        self.cases.append(case)
        self._save()
        return case

    def delete_case(self, case_id: str) -> bool:
        """删除案例"""
        for i, c in enumerate(self.cases):
            if c.case_id == case_id:
                del self.cases[i]
                self._save()
                return True
        return False

    def get_case(self, case_id: str) -> Optional[CaseRecord]:
        """获取单条案例"""
        for c in self.cases:
            if c.case_id == case_id:
                return c
        return None

    def list_all(self, tag_filter: Optional[str] = None) -> List[CaseRecord]:
        """列出所有案例"""
        if tag_filter:
            return [c for c in self.cases if tag_filter in c.tags]
        return list(self.cases)

    # ------------------------- 检索 -------------------------

    def retrieve_similar(
        self,
        query_ir: Optional[StructuralIR] = None,
        query_features: Optional[Dict[str, float]] = None,
        query_tags: Optional[List[str]] = None,
        query_metrics: Optional[EvaluationMetrics] = None,
        top_k: int = 5,
        min_similarity: float = 0.0,
    ) -> List[RetrievalResult]:
        """检索 Top-K 相似案例

        支持 3 种查询方式：
        1. query_ir: 用 IR 自动提取几何特征
        2. query_features: 直接提供特征字典
        3. query_metrics: 提供性能指标

        Returns:
            按相似度降序排列的检索结果
        """
        if not self.cases:
            return []

        # 构建查询特征
        if query_features is None:
            query_features = {}

        if query_ir is not None:
            geo = FeatureExtractor.from_ir(query_ir)
            query_features = {**query_features, **geo}

        if query_metrics is not None:
            perf = FeatureExtractor.from_metrics(query_metrics)
            query_features = {**query_features, **perf}

        if not query_features:
            return []

        # 计算每个案例的相似度
        scored: List[Tuple[float, CaseRecord]] = []
        for case in self.cases:
            # 构建案例特征
            case_features = {
                "node_count": float(case.node_count),
                "frame_count": float(case.frame_count),
                "max_height": case.max_height_m,
                "max_span": case.max_span_m,
                "aspect_ratio": case.max_height_m / case.max_span_m if case.max_span_m > 0 else 0.0,
                "utilization": case.final_utilization,
                "displacement_ratio": case.final_displacement_ratio,
                "score": case.final_score,
            }

            sim = self.similarity.compute(
                query_features, case_features,
                query_tags or [], case.tags,
            )

            if sim >= min_similarity:
                scored.append((sim, case))

        # 排序
        scored.sort(key=lambda x: -x[0])

        # 取 Top-K
        results = []
        for rank, (sim, case) in enumerate(scored[:top_k], 1):
            results.append(RetrievalResult(
                case=case,
                similarity=sim,
                rank=rank,
            ))
        return results

    # ------------------------- 持久化 -------------------------

    def _load(self):
        """从文件加载"""
        if not self.db_path.exists():
            return
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.cases = [self._dict_to_case(d) for d in data.get("cases", [])]
        except (json.JSONDecodeError, KeyError, TypeError):
            self.cases = []

    def _save(self):
        """保存到文件"""
        data = {
            "version": "1.0.0",
            "created_at": datetime.now().isoformat(),
            "case_count": len(self.cases),
            "cases": [c.to_full_dict() for c in self.cases],
        }
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _dict_to_case(self, d: Dict[str, Any]) -> CaseRecord:
        """字典 → CaseRecord"""
        feat = d.get("features", {})
        res = d.get("results", {})
        return CaseRecord(
            case_id=d["case_id"],
            timestamp=d.get("timestamp", ""),
            source=CaseSource(d.get("source", "manual")),
            model_id=d["model_id"],
            model_name=d.get("model_name", ""),
            node_count=feat.get("node_count", 0),
            frame_count=feat.get("frame_count", 0),
            max_height_m=feat.get("max_height_m", 0.0),
            max_span_m=feat.get("max_span_m", 0.0),
            target_solver=feat.get("target_solver", "sap2000"),
            final_utilization=res.get("final_utilization", 0.0),
            final_displacement_ratio=res.get("final_displacement_ratio", 0.0),
            final_score=res.get("final_score", 0.0),
            final_level=res.get("final_level", "acceptable"),
            iterations_run=res.get("iterations_run", 0),
            improvement_pct=res.get("improvement_pct", 0.0),
            tags=d.get("tags", []),
            ir_snapshot=d.get("ir_snapshot"),
            metrics_snapshot=d.get("metrics_snapshot"),
        )

    def export(self, export_path: str):
        """导出案例库"""
        export_path = Path(export_path)
        data = {
            "version": "1.0.0",
            "exported_at": datetime.now().isoformat(),
            "case_count": len(self.cases),
            "cases": [c.to_full_dict() for c in self.cases],
        }
        export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def import_cases(self, import_path: str) -> int:
        """导入案例库，返回导入数量"""
        import_path = Path(import_path)
        with open(import_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        imported = 0
        for d in data.get("cases", []):
            case = self._dict_to_case(d)
            case.source = CaseSource.IMPORTED
            # 避免 case_id 冲突
            case.case_id = f"{case.case_id}_imp_{imported}"
            self.cases.append(case)
            imported += 1
        self._save()
        return imported

    # ------------------------- 辅助 -------------------------

    def _auto_tags(self, ir: StructuralIR, result: LoopResult) -> List[str]:
        """根据模型自动生成标签"""
        tags = []
        # 几何
        if ir.nodes:
            height = max(n.z for n in ir.nodes) - min(n.z for n in ir.nodes)
            if height < 10:
                tags.append("low_rise")
            elif height < 30:
                tags.append("mid_rise")
            else:
                tags.append("high_rise")

        # 求解器
        tags.append(f"solver:{ir.analysis.target_solver.value}")

        # 性能
        if result.final_metrics.max_utilization > 1.0:
            tags.append("over_stressed")
        elif result.final_metrics.max_utilization > 0.85:
            tags.append("well_designed")
        else:
            tags.append("under_utilized")

        # 改进幅度
        if result.improvement_pct > 30:
            tags.append("high_improvement")

        # 设计规范
        if ir.analysis.design_code:
            tags.append(f"code:{ir.analysis.design_code.lower()}")

        return tags

    def stats(self) -> Dict[str, Any]:
        """知识库统计"""
        if not self.cases:
            return {"case_count": 0}

        levels = defaultdict(int)
        solvers = defaultdict(int)
        all_tags = []

        for c in self.cases:
            levels[c.final_level] += 1
            solvers[c.target_solver] += 1
            all_tags.extend(c.tags)

        tag_freq = defaultdict(int)
        for t in all_tags:
            tag_freq[t] += 1

        return {
            "case_count": len(self.cases),
            "by_level": dict(levels),
            "by_solver": dict(solvers),
            "top_tags": sorted(tag_freq.items(), key=lambda x: -x[1])[:10],
            "avg_improvement_pct": sum(c.improvement_pct for c in self.cases) / len(self.cases),
            "avg_final_score": sum(c.final_score for c in self.cases) / len(self.cases),
        }


# ============================================================================
# 第 5 层：便捷接口
# ============================================================================

def create_store(db_path: str = "./knowledge_db.json") -> KnowledgeStore:
    """便捷函数：创建知识库"""
    return KnowledgeStore(db_path=db_path)


# ============================================================================
# 第 6 层：测试入口
# ============================================================================

def main():
    """主入口：知识库演示"""
    print("=" * 70)
    print("Knowledge Store v1.0 - 测试入口")
    print("=" * 70)

    # 清理旧库
    db_path = Path("./knowledge_db.json")
    if db_path.exists():
        db_path.unlink()

    store = KnowledgeStore(db_path=str(db_path))

    # 1. 模拟保存多个优化案例
    print("\n[1] 保存案例（模拟 6 个项目的优化结果）...")
    from optimization_loop import run_optimization, _build_initial_ir
    from ir_compiler import SolverType

    scenarios = [
        ("under_design", ["concrete", "low_rise"]),
        ("over_design", ["concrete", "low_rise"]),
        ("normal", ["concrete", "low_rise"]),
        ("under_design", ["steel", "mid_rise"]),
        ("normal", ["steel", "high_rise"]),
        ("over_design", ["composite", "mid_rise"]),
    ]

    saved_cases = []
    for i, (scenario, tags) in enumerate(scenarios, 1):
        ir = _build_initial_ir(scenario)
        # 修改标签用于演示
        if "steel" in tags:
            for s in ir.sections:
                s.type = ir.sections[0].type.CONCRETE_RECT  # 占位
        result = run_optimization(ir, max_iterations=5)
        case = store.save_case_from_loop(ir, result, tags=tags)
        saved_cases.append(case)
        print(f"  ✓ 案例 #{i}: {case.case_id} | {case.model_name} | "
              f"利用率={case.final_utilization:.2f} | 标签={case.tags}")

    # 2. 知识库统计
    print("\n[2] 知识库统计:")
    stats = store.stats()
    print(f"  案例总数: {stats['case_count']}")
    print(f"  按评级分布: {stats['by_level']}")
    print(f"  按求解器分布: {stats['by_solver']}")
    print(f"  热门标签: {stats['top_tags'][:5]}")
    print(f"  平均改进: {stats['avg_improvement_pct']:.1f}%")

    # 3. 检索相似案例
    print("\n[3] 相似度检索（场景：低层混凝土框架）:")
    query_ir = _build_initial_ir("normal")
    results = store.retrieve_similar(
        query_ir=query_ir,
        query_tags=["concrete", "low_rise"],
        top_k=3,
    )
    for r in results:
        print(f"  Rank {r.rank} | 相似度={r.similarity:.3f} | "
              f"{r.case.model_name} | 节点={r.case.node_count} | "
              f"评分={r.case.final_score:.2f}")

    # 4. 性能特征检索
    print("\n[4] 性能特征检索（目标利用率 0.85）:")
    from optimization_loop import EvaluationMetrics
    target_metrics = EvaluationMetrics()
    target_metrics.max_utilization = 0.85
    target_metrics.displacement_ratio = 0.7
    target_metrics.overall_score = 0.85

    results = store.retrieve_similar(
        query_metrics=target_metrics,
        top_k=3,
    )
    for r in results:
        print(f"  Rank {r.rank} | 相似度={r.similarity:.3f} | "
              f"{r.case.model_name} | 利用率={r.case.final_utilization:.2f}")

    # 5. 导出/导入
    print("\n[5] 案例库导出...")
    export_path = Path("./knowledge_db_export.json")
    store.export(str(export_path))
    print(f"  已导出到: {export_path.resolve()}")
    print(f"  文件大小: {export_path.stat().st_size} bytes")

    # 6. 文件读取
    print("\n[6] 案例库文件结构:")
    with open(db_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"  版本: {data['version']}")
    print(f"  案例数: {data['case_count']}")
    print(f"  字段: {list(data.keys())}")
    print(f"  首条案例摘要:")
    first = data['cases'][0]
    print(f"    case_id: {first['case_id']}")
    print(f"    model_id: {first['model_id']}")
    print(f"    tags: {first['tags']}")
    print(f"    results: {first['results']}")

    print("\n" + "=" * 70)
    print("✅ 测试完成")
    print("=" * 70)


if __name__ == "__main__":
    main()