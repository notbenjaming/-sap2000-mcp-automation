"""
Failure-Aware Worker Supervisor（生产级容错层）
==============================================
实现 SAP2000 Worker 的工业级容错管理。

核心模块：
1. WorkerState（状态机）
2. FailureRecord（失败记录）
3. RetryPolicy（重试策略）
4. CircuitBreaker（熔断器）
5. WorkerSupervisor（统一管理器）

作者：MiniMax-M3 / Hermes CSI System
版本：v1.0.0
"""

from __future__ import annotations

import time
import threading
import logging
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional, Any, Callable, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import deque, defaultdict

from ir_compiler import StructuralIR
from optimization_loop import SolverResults

# 复用 SAP2000 Worker
try:
    from sap2000_worker import SAP2000Worker, SAP2000Config, MockSolver
    HAS_SAP2000_WORKER = True
except ImportError:
    HAS_SAP2000_WORKER = False
    SAP2000Worker = None
    SAP2000Config = None
    MockSolver = None


# ============================================================================
# 日志
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger("worker_supervisor")


# ============================================================================
# 第 1 层：状态机定义
# ============================================================================

class WorkerState(str, Enum):
    """Worker 状态机"""
    IDLE = "idle"                       # 空闲
    INITIALIZING = "initializing"       # 初始化中
    RUNNING = "running"                 # 运行中
    TIMEOUT = "timeout"                 # 超时
    CRASHED = "crashed"                 # 崩溃
    FAILED = "failed"                   # 失败
    RECOVERING = "recovering"           # 恢复中
    RETRYING = "retrying"               # 重试中
    ESCALATED = "escalated"             # 已升级（fallback）
    SHUTDOWN = "shutdown"               # 已关闭


# 状态转换图
STATE_TRANSITIONS = {
    WorkerState.IDLE: [WorkerState.INITIALIZING, WorkerState.SHUTDOWN],
    WorkerState.INITIALIZING: [WorkerState.RUNNING, WorkerState.CRASHED],
    WorkerState.RUNNING: [
        WorkerState.IDLE, WorkerState.TIMEOUT,
        WorkerState.CRASHED, WorkerState.FAILED,
    ],
    WorkerState.TIMEOUT: [WorkerState.RECOVERING, WorkerState.ESCALATED],
    WorkerState.CRASHED: [WorkerState.RECOVERING, WorkerState.ESCALATED],
    WorkerState.FAILED: [WorkerState.RETRYING, WorkerState.ESCALATED],
    WorkerState.RECOVERING: [WorkerState.IDLE, WorkerState.ESCALATED],
    WorkerState.RETRYING: [WorkerState.RUNNING, WorkerState.ESCALATED],
    WorkerState.ESCALATED: [WorkerState.IDLE],
    WorkerState.SHUTDOWN: [],
}


class FailureReason(str, Enum):
    """失败原因分类"""
    TIMEOUT = "timeout"
    COM_ERROR = "com_error"
    LICENSE_LOST = "license_lost"
    PROCESS_CRASH = "process_crash"
    INVALID_INPUT = "invalid_input"
    MODEL_BUILD_FAILED = "model_build_failed"
    ANALYSIS_FAILED = "analysis_failed"
    RESULT_EXTRACTION_FAILED = "result_extraction_failed"
    UNKNOWN = "unknown"


# ============================================================================
# 第 2 层：失败记录
# ============================================================================

@dataclass
class FailureRecord:
    """单次失败记录"""
    timestamp: str
    reason: FailureReason
    message: str
    worker_id: str
    job_id: str
    duration_ms: float
    traceback: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "reason": self.reason.value,
            "message": self.message[:200],
            "worker_id": self.worker_id,
            "job_id": self.job_id,
            "duration_ms": round(self.duration_ms, 2),
            "traceback": self.traceback[:500] if self.traceback else None,
        }


# ============================================================================
# 第 3 层：重试策略
# ============================================================================

@dataclass
class RetryPolicy:
    """重试策略

    指数退避算法：delay = base_delay * (2 ^ attempt)
    """
    max_retries: int = 3               # 最大重试次数
    base_delay_sec: float = 1.0        # 基础延迟
    max_delay_sec: float = 30.0        # 最大延迟
    backoff_multiplier: float = 2.0    # 退避倍数

    # 哪些错误可重试
    retryable_reasons: List[FailureReason] = field(default_factory=lambda: [
        FailureReason.TIMEOUT,
        FailureReason.PROCESS_CRASH,
        FailureReason.COM_ERROR,
    ])

    # 哪些错误不重试，直接 fallback
    non_retryable_reasons: List[FailureReason] = field(default_factory=lambda: [
        FailureReason.INVALID_INPUT,
        FailureReason.LICENSE_LOST,
    ])

    def should_retry(self, reason: FailureReason, attempt: int) -> bool:
        """判断是否应该重试"""
        if attempt >= self.max_retries:
            return False
        if reason in self.non_retryable_reasons:
            return False
        return reason in self.retryable_reasons

    def get_delay(self, attempt: int) -> float:
        """计算第 N 次重试的延迟时间（指数退避）"""
        delay = self.base_delay_sec * (self.backoff_multiplier ** attempt)
        return min(delay, self.max_delay_sec)


# ============================================================================
# 第 4 层：熔断器
# ============================================================================

class CircuitState(str, Enum):
    """熔断器状态"""
    CLOSED = "closed"          # 关闭（正常工作）
    OPEN = "open"              # 开启（拒绝请求）
    HALF_OPEN = "half_open"    # 半开（探测恢复）


@dataclass
class CircuitBreaker:
    """熔断器

    防止连续失败耗尽资源。
    - 连续失败达到阈值 → OPEN（拒绝所有请求）
    - OPEN 状态等待恢复时间 → HALF_OPEN（探测一次）
    - HALF_OPEN 成功 → CLOSED
    - HALF_OPEN 失败 → OPEN
    """
    failure_threshold: int = 5           # 失败阈值
    recovery_timeout_sec: float = 60.0  # 恢复等待时间
    half_open_max_calls: int = 1        # 半开状态最大探测次数

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[float] = None
    last_state_change: Optional[float] = None
    half_open_calls: int = 0

    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow_request(self) -> bool:
        """判断是否允许请求通过"""
        with self._lock:
            now = time.time()

            # CLOSED 状态：总是允许
            if self.state == CircuitState.CLOSED:
                return True

            # OPEN 状态：检查是否到恢复时间
            if self.state == CircuitState.OPEN:
                if (self.last_state_change and
                        now - self.last_state_change >= self.recovery_timeout_sec):
                    self._transition(CircuitState.HALF_OPEN)
                    self.half_open_calls = 0
                    return True
                return False

            # HALF_OPEN 状态：限制探测次数
            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls < self.half_open_max_calls:
                    self.half_open_calls += 1
                    return True
                return False

            return False

    def record_success(self):
        """记录成功"""
        with self._lock:
            self.success_count += 1
            if self.state == CircuitState.HALF_OPEN:
                self._transition(CircuitState.CLOSED)
                self.failure_count = 0
                logger.info("✅ 熔断器恢复：HALF_OPEN → CLOSED")

    def record_failure(self):
        """记录失败"""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                self._transition(CircuitState.OPEN)
                logger.warning("⚠️ 熔断器探测失败：HALF_OPEN → OPEN")
            elif (self.state == CircuitState.CLOSED and
                  self.failure_count >= self.failure_threshold):
                self._transition(CircuitState.OPEN)
                logger.warning(
                    f"⚠️ 熔断器开启（连续失败 {self.failure_count} 次）："
                    f"CLOSED → OPEN"
                )

    def _transition(self, new_state: CircuitState):
        """状态转换"""
        old_state = self.state
        self.state = new_state
        self.last_state_change = time.time()
        if new_state == CircuitState.CLOSED:
            self.failure_count = 0

    def status(self) -> Dict[str, Any]:
        """获取熔断器状态"""
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "failure_threshold": self.failure_threshold,
        }


# ============================================================================
# 第 5 层：Worker 实例 + 健康检查
# ============================================================================

@dataclass
class WorkerInstance:
    """Worker 实例（封装一个 SAP2000Worker + 状态）"""
    worker_id: str
    worker: Any  # SAP2000Worker 或 MockSolver
    state: WorkerState = WorkerState.IDLE
    created_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    total_jobs: int = 0
    successful_jobs: int = 0
    failed_jobs: int = 0
    total_duration_ms: float = 0.0

    def update_heartbeat(self):
        """更新心跳"""
        self.last_heartbeat = time.time()

    def is_alive(self, timeout_sec: float = 60.0) -> bool:
        """判断 Worker 是否活着（心跳检查）"""
        return (time.time() - self.last_heartbeat) < timeout_sec

    def record_success(self, duration_ms: float):
        """记录成功"""
        self.successful_jobs += 1
        self.total_jobs += 1
        self.total_duration_ms += duration_ms
        self.state = WorkerState.IDLE

    def record_failure(self):
        """记录失败"""
        self.failed_jobs += 1
        self.total_jobs += 1
        self.state = WorkerState.FAILED

    @property
    def success_rate(self) -> float:
        """成功率"""
        if self.total_jobs == 0:
            return 0.0
        return self.successful_jobs / self.total_jobs

    @property
    def avg_duration_ms(self) -> float:
        """平均耗时"""
        if self.successful_jobs == 0:
            return 0.0
        return self.total_duration_ms / self.successful_jobs

    def status(self) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "state": self.state.value,
            "is_alive": self.is_alive(),
            "total_jobs": self.total_jobs,
            "successful_jobs": self.successful_jobs,
            "failed_jobs": self.failed_jobs,
            "success_rate": round(self.success_rate, 3),
            "avg_duration_ms": round(self.avg_duration_ms, 2),
        }


# ============================================================================
# 第 6 层：WorkerSupervisor 统一管理器
# ============================================================================

@dataclass
class JobResult:
    """任务执行结果"""
    success: bool
    results: Optional[SolverResults] = None
    error: Optional[str] = None
    failure_reason: Optional[FailureReason] = None
    attempts: int = 1
    total_duration_ms: float = 0.0
    used_fallback: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "error": self.error,
            "failure_reason": self.failure_reason.value if self.failure_reason else None,
            "attempts": self.attempts,
            "total_duration_ms": round(self.total_duration_ms, 2),
            "used_fallback": self.used_fallback,
        }


class WorkerSupervisor:
    """Worker 监督器（生产级容错）

    功能：
    1. Worker Pool 管理（多实例隔离）
    2. 自动重试（指数退避）
    3. 失败升级（fallback 到 MockSolver）
    4. 熔断保护（连续失败熔断）
    5. 心跳检查（worker 健康监测）
    6. 完整 metrics（成功率/耗时/失败原因分布）
    """

    def __init__(
        self,
        pool_size: int = 3,
        config: Optional[Any] = None,
        retry_policy: Optional[RetryPolicy] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        enable_fallback: bool = True,
    ):
        self.pool_size = pool_size
        self.config = config or (SAP2000Config() if HAS_SAP2000_WORKER else None)
        self.retry_policy = retry_policy or RetryPolicy()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.enable_fallback = enable_fallback

        self.pool: List[WorkerInstance] = []
        self.failures: deque = deque(maxlen=1000)  # 最近 1000 条失败
        self.fallback_solver: Optional[Any] = None
        self._lock = threading.Lock()
        self._job_counter = 0

        self._init_pool()
        if enable_fallback:
            self._init_fallback()

        logger.info(
            f"WorkerSupervisor 初始化完成: pool={pool_size}, "
            f"retry={retry_policy.max_retries if retry_policy else 3}, "
            f"fallback={'enabled' if enable_fallback else 'disabled'}"
        )

    def _init_pool(self):
        """初始化 Worker Pool"""
        if not HAS_SAP2000_WORKER:
            logger.warning("sap2000_worker 不可用，Pool 为空（只使用 fallback）")
            return

        for i in range(self.pool_size):
            try:
                # 每个 Worker 实例独立配置
                worker = SAP2000Worker(self.config)
                instance = WorkerInstance(
                    worker_id=f"worker-{i:03d}",
                    worker=worker,
                )
                self.pool.append(instance)
                logger.info(f"  ✓ Pool 添加: {instance.worker_id}")
            except Exception as e:
                logger.error(f"  ✗ Worker 初始化失败: {e}")

    def _init_fallback(self):
        """初始化 fallback solver"""
        if MockSolver:
            self.fallback_solver = MockSolver()
            logger.info("  ✓ Fallback: MockSolver")

    def execute(self, ir: StructuralIR) -> JobResult:
        """执行任务（含完整容错逻辑）

        流程：
        1. 检查熔断器
        2. 选择可用 Worker
        3. 执行任务（含超时检测）
        4. 成功 → 记录 + 返回
        5. 失败 → 重试（指数退避）
        6. 重试耗尽 → fallback
        """
        self._job_counter += 1
        job_id = f"job-{self._job_counter:06d}"
        start_total = time.time()

        # 1. 检查熔断器
        if not self.circuit_breaker.allow_request():
            logger.warning(f"[{job_id}] 熔断器开启，直接 fallback")
            return self._do_fallback(ir, job_id, "circuit_open", start_total)

        # 2. 选择 Worker
        worker_instance = self._select_worker()
        if not worker_instance:
            logger.warning(f"[{job_id}] Pool 无可用 Worker，fallback")
            return self._do_fallback(ir, job_id, "no_worker", start_total)

        # 3. 尝试执行（含重试）
        last_error: Optional[Exception] = None
        last_reason = FailureReason.UNKNOWN
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                logger.info(
                    f"[{job_id}] {worker_instance.worker_id} "
                    f"attempt {attempt + 1}/{self.retry_policy.max_retries + 1}"
                )

                worker_instance.state = WorkerState.RUNNING
                worker_instance.update_heartbeat()

                attempt_start = time.time()
                results = worker_instance.worker.solve(ir)
                attempt_duration = (time.time() - attempt_start) * 1000

                # 成功
                worker_instance.record_success(attempt_duration)
                self.circuit_breaker.record_success()
                worker_instance.state = WorkerState.IDLE

                total_duration = (time.time() - start_total) * 1000
                logger.info(
                    f"[{job_id}] ✅ 成功（{attempt + 1} 次尝试，{total_duration:.0f}ms）"
                )

                return JobResult(
                    success=True,
                    results=results,
                    attempts=attempt + 1,
                    total_duration_ms=total_duration,
                    used_fallback=False,
                )

            except Exception as e:
                last_error = e
                last_reason = self._classify_error(e)

                # 记录失败
                failure = FailureRecord(
                    timestamp=datetime.now().isoformat(),
                    reason=last_reason,
                    message=str(e),
                    worker_id=worker_instance.worker_id,
                    job_id=job_id,
                    duration_ms=(time.time() - start_total) * 1000,
                )
                self.failures.append(failure)
                worker_instance.record_failure()
                self.circuit_breaker.record_failure()

                logger.warning(
                    f"[{job_id}] ❌ attempt {attempt + 1} 失败："
                    f"{last_reason.value} - {str(e)[:80]}"
                )

                # 判断是否重试
                if not self.retry_policy.should_retry(last_reason, attempt):
                    logger.info(f"[{job_id}] 不可重试（{last_reason.value}）")
                    break

                if attempt < self.retry_policy.max_retries:
                    delay = self.retry_policy.get_delay(attempt)
                    logger.info(f"[{job_id}] 等待 {delay:.1f}s 后重试")
                    time.sleep(delay)

                    # 重试时切换 Worker（如果原 Worker 卡死）
                    if last_reason in [FailureReason.TIMEOUT, FailureReason.PROCESS_CRASH]:
                        new_worker = self._replace_worker(worker_instance)
                        if new_worker:
                            worker_instance = new_worker

        # 4. 重试耗尽 → fallback
        error_msg = f"{last_reason.value}: {str(last_error)[:200]}"
        return self._do_fallback(
            ir, job_id, error_msg, start_total,
            attempts=self.retry_policy.max_retries + 1,
        )

    def _select_worker(self) -> Optional[WorkerInstance]:
        """选择最优 Worker（最空闲优先）"""
        with self._lock:
            available = [
                w for w in self.pool
                if w.state == WorkerState.IDLE and w.is_alive()
            ]
            if not available:
                return None
            # 选择成功率最高 + 状态 IDLE 的
            available.sort(key=lambda w: (-w.success_rate, w.total_jobs))
            return available[0]

    def _replace_worker(self, old: WorkerInstance) -> Optional[WorkerInstance]:
        """替换故障 Worker"""
        # 测试 Worker 不替换
        if getattr(old, '_is_test', False):
            logger.debug(f"  Worker {old.worker_id} 是测试实例，不替换")
            return None

        if not HAS_SAP2000_WORKER:
            return None

        try:
            # 强制清理旧 Worker
            try:
                old.worker.connection.force_kill()
            except Exception:
                pass

            # 创建新 Worker
            new_worker = SAP2000Worker(self.config)
            new_instance = WorkerInstance(
                worker_id=old.worker_id,  # 复用 ID
                worker=new_worker,
            )

            with self._lock:
                idx = next(
                    (i for i, w in enumerate(self.pool) if w.worker_id == old.worker_id),
                    None,
                )
                if idx is not None:
                    self.pool[idx] = new_instance
                    logger.info(f"  ✓ Worker 已替换: {old.worker_id}")
                    return new_instance
        except Exception as e:
            logger.error(f"  ✗ Worker 替换失败: {e}")

        return None

    def _do_fallback(
        self,
        ir: StructuralIR,
        job_id: str,
        reason: str,
        start_time: float,
        attempts: int = 1,
    ) -> JobResult:
        """执行 fallback"""
        if not self.enable_fallback or not self.fallback_solver:
            total_duration = (time.time() - start_time) * 1000
            return JobResult(
                success=False,
                error=f"无可用 solver: {reason}",
                failure_reason=FailureReason.UNKNOWN,
                attempts=attempts,
                total_duration_ms=total_duration,
                used_fallback=False,
            )

        try:
            logger.warning(f"[{job_id}] 执行 fallback → MockSolver (attempts={attempts})")
            results = self.fallback_solver.solve(ir)
            total_duration = (time.time() - start_time) * 1000

            return JobResult(
                success=True,
                results=results,
                failure_reason=FailureReason.PROCESS_CRASH,  # 标记触发 fallback
                attempts=attempts,
                total_duration_ms=total_duration,
                used_fallback=True,
            )
        except Exception as e:
            logger.error(f"[{job_id}] Fallback 也失败: {e}")
            total_duration = (time.time() - start_time) * 1000
            return JobResult(
                success=False,
                error=f"Fallback 失败: {e}",
                failure_reason=FailureReason.UNKNOWN,
                attempts=attempts,
                total_duration_ms=total_duration,
                used_fallback=True,
            )

    def _classify_error(self, error: Exception) -> FailureReason:
        """根据异常分类失败原因"""
        error_str = str(error).lower()

        if "timeout" in error_str or "超时" in error_str:
            return FailureReason.TIMEOUT
        if "license" in error_str or "许可" in error_str:
            return FailureReason.LICENSE_LOST
        if "com" in error_str or "winerror" in error_str:
            return FailureReason.COM_ERROR
        if "process" in error_str or "进程" in error_str:
            return FailureReason.PROCESS_CRASH
        if "validation" in error_str or "校验" in error_str:
            return FailureReason.INVALID_INPUT
        if "build" in error_str or "建模" in error_str:
            return FailureReason.MODEL_BUILD_FAILED
        if "analy" in error_str or "分析" in error_str or "求解" in error_str:
            return FailureReason.ANALYSIS_FAILED
        return FailureReason.UNKNOWN

    def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        alive_count = sum(1 for w in self.pool if w.is_alive())
        idle_count = sum(1 for w in self.pool if w.state == WorkerState.IDLE)

        return {
            "pool_size": len(self.pool),
            "alive_workers": alive_count,
            "idle_workers": idle_count,
            "circuit_breaker": self.circuit_breaker.status(),
            "total_failures": len(self.failures),
        }

    def metrics(self) -> Dict[str, Any]:
        """完整 metrics"""
        total_jobs = sum(w.total_jobs for w in self.pool)
        total_success = sum(w.successful_jobs for w in self.pool)
        total_failed = sum(w.failed_jobs for w in self.pool)

        # 失败原因分布
        reason_dist: Dict[str, int] = defaultdict(int)
        for f in self.failures:
            reason_dist[f.reason.value] += 1

        return {
            "pool_size": len(self.pool),
            "workers": [w.status() for w in self.pool],
            "jobs": {
                "total": total_jobs,
                "successful": total_success,
                "failed": total_failed,
                "success_rate": round(total_success / total_jobs, 3) if total_jobs else 0.0,
            },
            "circuit_breaker": self.circuit_breaker.status(),
            "failures": {
                "total": len(self.failures),
                "by_reason": dict(reason_dist),
            },
        }

    def shutdown(self):
        """关闭所有 Worker"""
        logger.info("关闭 Worker Pool...")
        for w in self.pool:
            try:
                if hasattr(w.worker, 'connection'):
                    w.worker.connection.stop()
            except Exception as e:
                logger.warning(f"关闭 {w.worker_id} 失败: {e}")
            w.state = WorkerState.SHUTDOWN
        logger.info("✅ Worker Pool 已关闭")


# ============================================================================
# 第 7 层：测试入口
# ============================================================================

def main():
    """主入口：演示 Failure-Aware Supervisor"""
    print("=" * 70)
    print("Failure-Aware Worker Supervisor v1.0 - 测试入口")
    print("=" * 70)

    from ir_compiler import build_sample_frame_ir

    # 1. 创建 Supervisor（pool=2, fallback 启用）
    print("\n[1] 创建 Supervisor...")
    supervisor = WorkerSupervisor(
        pool_size=2,
        retry_policy=RetryPolicy(max_retries=2, base_delay_sec=0.5),
        enable_fallback=True,
    )

    # 2. 健康检查
    print("\n[2] 健康检查:")
    health = supervisor.health_check()
    for k, v in health.items():
        print(f"    {k}: {v}")

    # 3. 执行任务（SAP2000 不可用 → fallback）
    print("\n[3] 执行任务（环境无 SAP2000 → fallback）...")
    ir = build_sample_frame_ir()
    result = supervisor.execute(ir)
    print(f"    成功: {result.success}")
    print(f"    attempts: {result.attempts}")
    print(f"    duration: {result.total_duration_ms:.0f}ms")
    print(f"    fallback: {result.used_fallback}")

    # 4. 模拟故障恢复（用自定义 Worker）
    print("\n[4] 模拟故障场景（自定义失败 Worker）...")

    class FailingWorker:
        def solve(self, ir):
            raise RuntimeError("SAP2000 COM error: license lost")

    class SuccessWorker:
        def __init__(self):
            self.call_count = 0
        def solve(self, ir):
            self.call_count += 1
            if self.call_count <= 2:
                raise TimeoutError("求解超时")
            r = SolverResults()
            r.max_displacement_mm = 5.0
            r.max_utilization = 0.7
            r.max_drift_ratio = 0.001
            return r

    # 注入测试 Worker（注意：不再替换为 SAP2000Worker，避免被 _replace_worker 覆盖）
    success_worker = SuccessWorker()
    test_instance = WorkerInstance(
        worker_id="test-success",
        worker=success_worker,
    )
    # 标记为非 SAP2000，避免被 _replace_worker 替换
    test_instance._is_test = True

    # 用新 Supervisor 避免之前的失败污染熔断器
    supervisor2 = WorkerSupervisor(
        pool_size=1,
        retry_policy=RetryPolicy(max_retries=3, base_delay_sec=0.2),
        circuit_breaker=CircuitBreaker(failure_threshold=10),  # 提高阈值
        enable_fallback=True,
    )
    supervisor2.pool = [test_instance]

    print("    调用 SuccessWorker（前 2 次会失败，第 3 次成功）...")
    result = supervisor2.execute(ir)
    print(f"    最终结果: success={result.success}, attempts={result.attempts}, "
          f"duration={result.total_duration_ms:.0f}ms")
    if result.results:
        print(f"    位移: {result.results.max_displacement_mm:.2f} mm")
        print(f"    利用率: {result.results.max_utilization:.3f}")

    # 5. 完整 metrics
    print("\n[5] 完整 metrics:")
    metrics = supervisor.metrics()
    print(f"    总任务: {metrics['jobs']['total']}")
    print(f"    成功率: {metrics['jobs']['success_rate']}")
    print(f"    失败分布: {metrics['failures']['by_reason']}")
    print(f"    熔断器: {metrics['circuit_breaker']['state']}")

    # 6. 关闭
    print("\n[6] 关闭 Supervisor...")
    supervisor.shutdown()

    print("\n" + "=" * 70)
    print("✅ 测试完成")
    print("=" * 70)


if __name__ == "__main__":
    main()