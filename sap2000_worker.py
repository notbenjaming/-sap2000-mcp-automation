"""
SAP2000 Worker - 真实 SAP2000 COM 接口封装
=========================================
用 comtypes 调用 SAP2000 OAPI，替换 MockSolver。

核心模块：
1. SAP2000Connection（COM 连接管理：启动 / 释放 / license）
2. SAP2000ModelBuilder（建模：节点 / 截面 / 构件 / 荷载）
3. SAP2000Analyzer（求解 + 结果提取）
4. SAP2000Worker（统一接口，对接 OptimizationLoop）

作者：MiniMax-M3 / Hermes CSI System
版本：v1.0.0

环境要求：
- Windows 10/11
- SAP2000 v22 / v24 / v26（任一已安装版本）
- pip install comtypes psutil
"""

from __future__ import annotations

import os
import sys
import time
import signal
import threading
import logging
import subprocess
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from contextlib import contextmanager

# Windows 专用
if sys.platform != "win32":
    raise OSError("SAP2000 Worker 只能在 Windows 上运行（COM 接口）")

try:
    import win32com.client
    import pythoncom
    import psutil
except ImportError as e:
    raise ImportError(
        "缺少依赖，请先安装:\n"
        "  pip install pywin32 psutil"
    ) from e

# 复用前 5 批模块
from ir_compiler import (
    StructuralIR, Node, Frame, Section, SectionType,
    LoadCase, LoadCaseType,
)
from optimization_loop import SolverResults, MockSolver

# ============================================================================
# 日志
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger("sap2000_worker")


# ============================================================================
# 第 1 层：SAP2000Connection COM 连接管理
# ============================================================================

class SAP2000Version(str, Enum):
    """SAP2000 版本"""
    V22 = "CSI.SAP2000.API.SapObject"
    V24 = "CSI.SAP2000.API.SapObject"
    V26 = "CSI.SAP2000.API.SapObject"


@dataclass
class SAP2000Config:
    """SAP2000 配置"""
    prog_id: str = "CSI.SAP2000.API.SapObject"
    units: str = "kN_m_C"  # 单位制：kN, m, ℃
    model_file_path: str = ""  # 模型保存路径（空=不保存）
    timeout_sec: float = 600.0  # 单次操作超时（10 分钟）
    visible: bool = False  # SAP2000 窗口是否可见（True=可见，False=隐藏）
    auto_release_on_exit: bool = True  # 退出时自动释放

    # CSiAPIService 模式专用配置
    sap2000_path: str = r"D:\SAP2000\SAP2000.exe"
    service_path: str = r"D:\SAP2000\CSiAPIService.exe"
    service_port: int = 11650


def unpack_sap_return(result, operation: str = "operation"):
    """解包 SAP2000 OAPI 调用返回值

    SAP2000 OAPI 的方法通常返回 (ret_code, [name]) 元组
    - ret_code: Long, 0=成功, 非0=错误码
    - name: String, SAP2000 实际分配的名称（ByRef 参数）

    Args:
        result: API 调用原始返回值
        operation: 操作描述（用于错误日志）

    Returns:
        (ret_code, actual_name) 元组
    """
    if isinstance(result, tuple):
        ret_code = result[0]
        actual_name = result[1] if len(result) > 1 else ""
    else:
        ret_code = result
        actual_name = ""
    return ret_code, actual_name


class SAP2000Connection:
    """SAP2000 COM 连接管理（直接 Helper 模式，无 CSiAPIService）

    工作原理：
    1. 通过 win32com.client.Dispatch("SAP2000v1.Helper") 获取 Helper
    2. 调用 Helper.CreateObjectProgID() 自动启动 SAP2000.exe
    3. SAP2000 进程与 Python 进程通过 .NET COM 直接通信

    优势：
    - 不需要 CSiAPIService 中转（避免 Console.ReadKey 问题）
    - 进程数更少，管理更简单
    - 性能更好（无 TCP 序列化）

    注意：
    - SAP2000 首次启动约需 4-5 分钟（重型 .NET 应用）
    - 必须用 STA 线程调用 COM
    """

    def __init__(self, config: Optional[SAP2000Config] = None):
        self.config = config or SAP2000Config()
        self.sap_object = None
        self.sap_model = None
        self.process_pid: Optional[int] = None
        self._is_attached = False
        self._lock = threading.Lock()

    def start(self):
        """启动 SAP2000（Helper 直连模式）

        策略：
        1. 检测 SAP2000.exe 是否已在跑（用户手动启动）
           - 如果是，直接连接，跳过 ApplicationStart
        2. 否则通过 Helper 启动（需要 desktop session）

        Raises:
            RuntimeError: 启动失败
        """
        with self._lock:
            if self._is_attached:
                logger.warning("SAP2000 已经启动，跳过")
                return

            sap_path = self.config.sap2000_path or r"D:\SAP2000\SAP2000.exe"
            if not os.path.exists(sap_path):
                raise RuntimeError(f"SAP2000 未找到: {sap_path}")

            logger.info(f"SAP2000 路径: {sap_path}")
            start = time.time()

            # 1. 检测 SAP2000 是否已经在跑（主进程，排除 launcher）
            existing_pid = self._find_existing_sap_process()

            if existing_pid:
                # 复用已有进程：直接获取 SAPObject，不调 ApplicationStart
                logger.info(
                    f"✓ 检测到 SAP2000 已在运行 (PID={existing_pid})，"
                    f"通过 Helper 连接"
                )
                pythoncom.CoInitialize()
                try:
                    helper = win32com.client.Dispatch("SAP2000v1.Helper")
                    ver = helper.GetOAPIVersionNumber()
                    logger.info(f"OAPI 版本: {ver}")
                    self.sap_object = helper.GetObjectProcess(
                        "CSI.SAP2000.API.SapObject",
                        str(existing_pid)
                    )
                    logger.info(
                        f"✓ SAPObject 已获取（连接到 PID={existing_pid}）"
                    )
                finally:
                    pythoncom.CoUninitialize()

                self.process_pid = existing_pid
                elapsed = time.time() - start
                logger.info(
                    f"✅ SAP2000 连接完成 (PID={self.process_pid}, "
                    f"耗时 {elapsed:.1f}s)"
                )
                self._is_attached = True
                return

            # 2. SAP2000 不在跑——通过 Helper 启动
            logger.info(
                "SAP2000 未运行，通过 Helper 启动...\n"
                "  ⚠️ 提示：Helper 启动可能只启动 launcher 进程。\n"
                "  ⚠️ 如果长时间卡在 <100MB，建议手动启动 SAP2000 后重试。"
            )
            self._cleanup_stale_processes()

            pythoncom.CoInitialize()
            try:
                helper = win32com.client.Dispatch("SAP2000v1.Helper")
                ver = helper.GetOAPIVersionNumber()
                logger.info(f"OAPI 版本: {ver}")
                self.sap_object = helper.CreateObjectProgID(
                    "CSI.SAP2000.API.SapObject"
                )
                logger.info("✓ SAPObject 已创建（SAP2000 启动中）")
            finally:
                pythoncom.CoUninitialize()

            # 等待 SAP2000 完全加载
            logger.info("等待 SAP2000 加载完成...")
            self.process_pid = self._wait_for_sap_ready(timeout_sec=600)

            try:
                proc = psutil.Process(self.process_pid)
                mem_mb = proc.memory_info().rss / 1024 / 1024
                if mem_mb < 200:
                    logger.warning(
                        f"⚠️ SAP2000 内存仅 {mem_mb:.0f} MB（< 200 MB），"
                        f"可能未完全加载"
                    )
            except Exception:
                pass

            elapsed = time.time() - start
            logger.info(
                f"✅ SAP2000 启动完成 (PID={self.process_pid}, "
                f"总耗时 {elapsed:.1f}s)"
            )
            self._is_attached = True

    def _find_existing_sap_process(self) -> Optional[int]:
        """查找已运行的 SAP2000 主进程（排除 launcher）"""
        try:
            candidates = []
            for proc in psutil.process_iter(['name', 'pid', 'memory_info']):
                name = proc.info.get('name') or ''
                if name != 'SAP2000.exe':
                    continue
                mem_mb = proc.info.get('memory_info', 0)
                if mem_mb:
                    mem_mb = mem_mb.rss / 1024 / 1024
                # launcher < 100MB，主进程 > 200MB
                if mem_mb and mem_mb < 100:
                    continue
                candidates.append((proc.info['pid'], mem_mb or 0))
            if not candidates:
                return None
            # 选内存最大的
            candidates.sort(key=lambda x: x[1], reverse=True)
            return candidates[0][0]
        except Exception:
            pass
        return None

    def _cleanup_stale_processes(self):
        """清理 SAP2000 残留进程（避免 Helper 误连）"""
        try:
            for proc in psutil.process_iter(['name', 'pid']):
                name = proc.info.get('name') or ''
                pid = proc.info['pid']
                if name == 'SAP2000.exe':
                    try:
                        proc.kill()
                        logger.info(f"  清理残留 SAP2000.exe (PID={pid})")
                    except Exception:
                        pass
            time.sleep(2)
        except Exception:
            pass

    def _wait_for_sap_ready(self, timeout_sec: float = 600) -> int:
        """等待 SAP2000.exe 完全加载（通过内存稳定性判断）

        SAP2000 加载完成后通常占用 1-5 GB 内存
        但在某些环境（CLI、无 desktop session）下可能只有 100-300 MB

        launcher 特征：< 100MB，稳定不动

        Returns:
            SAP2000.exe 的 PID

        Raises:
            RuntimeError: launcher 检测（30秒内稳定在 < 100MB）
        """
        start = time.time()
        last_mem = 0
        stable_count = 0
        last_pid = None
        # 最小就绪阈值（主进程至少 100MB，launcher < 100MB）
        min_ready_mem_mb = 100
        # launcher 检测：30秒内稳定在 < 100MB 视为 launcher
        launcher_mem_threshold_mb = 100
        launcher_stable_for_sec = 30

        for i in range(int(timeout_sec / 2)):
            time.sleep(2)

            try:
                for proc in psutil.process_iter(['name', 'pid', 'memory_info']):
                    name = proc.info.get('name') or ''
                    if name == 'SAP2000.exe':
                        last_pid = proc.info['pid']
                        mem_mb = proc.info['memory_info'].rss / 1024 / 1024

                        # launcher 检测：内存 < 阈值，持续稳定
                        if mem_mb < launcher_mem_threshold_mb:
                            if abs(mem_mb - last_mem) < 5:  # 基本不变
                                stable_count += 1
                            else:
                                stable_count = 0
                            elapsed = time.time() - start
                            if elapsed > launcher_stable_for_sec and stable_count >= 5:
                                raise RuntimeError(
                                    f"检测到 launcher 进程 (PID={last_pid}, "
                                    f"内存={mem_mb:.0f}MB)，30秒内未增长。\n"
                                    f"请手动双击 D:\\SAP2000\\SAP2000.exe 启动，"
                                    f"然后重试。"
                                )
                        else:
                            stable_count = 0

                        # 内存稳定判断
                        if abs(mem_mb - last_mem) < 50:
                            stable_count += 1
                            if stable_count >= 5:
                                if mem_mb >= min_ready_mem_mb:
                                    if mem_mb < 200:
                                        logger.warning(
                                            f"SAP2000 内存仅 {mem_mb:.0f} MB，"
                                            f"可能未完全加载（建议 ≥ 500 MB）"
                                        )
                                    else:
                                        logger.info(
                                            f"SAP2000 已就绪（{stable_count} 次稳定, "
                                            f"{mem_mb:.0f} MB）"
                                        )
                                    return last_pid
                                else:
                                    stable_count = 0
                        else:
                            stable_count = 0
                        last_mem = mem_mb
                        break
            except RuntimeError:
                raise  # 直接传播 launcher 错误
            except Exception as e:
                logger.debug(f"进程扫描异常: {e}")

            elapsed = time.time() - start
            if (i + 1) % 15 == 0:
                logger.info(f"  等待中... ({elapsed:.0f}s)")
                if last_pid:
                    logger.info(f"  SAP2000 PID={last_pid}, 内存={last_mem:.0f} MB")

        if last_pid:
            logger.warning(
                f"SAP2000 启动超时（{timeout_sec}s），当前内存 {last_mem:.0f} MB"
            )
            return last_pid
        raise RuntimeError(f"SAP2000 启动超时（{timeout_sec}s），未找到进程")

    def stop(self):
        """关闭 SAP2000"""
        with self._lock:
            if not self._is_attached:
                return

            try:
                logger.info("关闭 SAP2000...")

                # 通过 psutil 终止进程
                if self.process_pid:
                    try:
                        proc = psutil.Process(self.process_pid)
                        proc.terminate()
                        proc.wait(timeout=5)
                        logger.info("✓ SAP2000 已关闭")
                    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                        pass

                self.sap_object = None
                self.sap_model = None
                self._is_attached = False
                logger.info("✅ SAP2000 完整关闭")
            except Exception as e:
                logger.warning(f"关闭异常: {e}，强制清理")
                self._force_cleanup()

    def force_kill(self):
        """强制结束 SAP2000 进程"""
        logger.warning("强制结束 SAP2000 进程")
        self._force_cleanup()
        self.sap_object = None
        self.sap_model = None
        self._is_attached = False

    def _force_cleanup(self):
        """强制清理 SAP2000 进程"""
        try:
            for proc in psutil.process_iter(['name']):
                proc_name = proc.info.get('name') or ''
                if 'SAP2000.exe' == proc_name:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        except Exception:
            pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


# ============================================================================
# 第 2 层：Watchdog 超时控制
# ============================================================================

class WatchdogTimeout(Exception):
    """Watchdog 超时异常"""
    pass


@contextmanager
def watchdog(timeout_sec: float, operation: str = "operation"):
    """超时 watchdog 上下文管理器

    用法：
        with watchdog(60.0, "求解"):
            sap.sap_model.Analyze.RunAnalysis()

    超时会抛出 WatchdogTimeout 异常（但不会真的中断阻塞调用）
    """
    start = time.time()
    timed_out = [False]

    def _alarm():
        timed_out[0] = True
        logger.error(f"⏰ Watchdog: {operation} 超时 ({timeout_sec}s)")

    timer = threading.Timer(timeout_sec, _alarm)
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        elapsed = time.time() - start
        if timed_out[0]:
            raise WatchdogTimeout(f"{operation} 超时（{elapsed:.1f}s > {timeout_sec}s）")
        if elapsed > timeout_sec * 0.9:
            logger.warning(f"{operation} 接近超时阈值 ({elapsed:.1f}s)")


# ============================================================================
# 第 3 层：SAP2000ModelBuilder 建模
# ============================================================================

class SAP2000ModelBuilder:
    """从 IR 构建 SAP2000 模型"""

    def __init__(self, sap_object, config: SAP2000Config):
        # sap_object 是 SapObject COM 对象，需要从中取 SapModel
        self.sap_object = sap_object
        self.sap_model = None  # 在 build() 时通过 .SapModel 获取
        self.config = config

    def build(self, ir: StructuralIR):
        """完整建模流程"""
        logger.info(f"开始建模: {ir.model_id}")

        # 必须在 STA 线程中调用 COM
        pythoncom.CoInitialize()
        try:
            # 获取 SapModel
            self.sap_model = self.sap_object.SapModel

            # 1. 初始化新模型（空模板）
            self.sap_model.InitializeNewModel()

            # 2. 创建新文件
            result = self.sap_model.File.NewBlank()
            ret, _ = unpack_sap_return(result, "NewBlank")
            if ret != 0:
                raise RuntimeError(f"File.NewBlank 失败: ret={ret}")

            # 3. 定义材料
            self._define_materials(ir)

            # 4. 定义截面
            self._define_sections(ir)

            # 5. 创建节点（返回 IR ID → SAP2000 名称 的映射）
            self.node_name_map = self._create_nodes(ir)

            # 6. 锁定模型（必须！否则无法设置支座）
            try:
                self.sap_model.SetModelIsLocked(False)  # 确保解锁
                logger.debug("模型已解锁")
            except Exception as e:
                logger.debug(f"解锁模型异常（可忽略）: {e}")

            # 7. 创建构件（使用 SAP2000 实际节点名称）
            self._create_frames(ir, self.node_name_map)

            # 8. 设置支座（使用 SAP2000 实际节点名称）
            self._assign_restraints(ir, self.node_name_map)

            # 9. 定义荷载工况
            self._define_load_cases(ir)

            # 10. 施加载荷
            self._apply_loads(ir)

            # 11. 重新锁定模型（求解前必须锁定）
            try:
                self.sap_model.SetModelIsLocked(True)
                logger.debug("模型已锁定，准备求解")
            except Exception as e:
                logger.warning(f"锁定模型异常: {e}")

            # 12. 配置分析设置
            self._configure_analysis(ir)

            logger.info("✅ 建模完成")
        finally:
            pythoncom.CoUninitialize()

    def _configure_analysis(self, ir: StructuralIR):
        """配置分析设置

        SAP2000 默认有 MODAL + Linear Static 分析
        我们用默认设置即可
        """
        try:
            # 设置分析工况激活
            for case_name in [c.name for c in ir.load_cases]:
                try:
                    self.sap_model.Analyze.SetRunCaseFlag(case_name, True)
                    logger.debug(f"激活工况: {case_name}")
                except Exception as e:
                    logger.debug(f"激活 {case_name} 异常: {e}")
        except Exception as e:
            logger.warning(f"配置分析异常: {e}")

    def _define_materials(self, ir: StructuralIR):
        """定义材料（简化：混凝土 C30 + 钢 Q355）"""
        materials = set()
        for sec in ir.sections:
            materials.add(sec.material)

        for mat_name in materials:
            try:
                # 简化：默认按混凝土处理
                # 真实场景需要根据材料类型调用 SetMaterialGrade 等
                result = self.sap_model.PropMaterial.SetMaterial(
                    mat_name,
                    2,  # eMatTypeConcrete
                )
                ret, _ = unpack_sap_return(result, "SetMaterial")
                if ret != 0:
                    logger.warning(f"材料 {mat_name} 返回码: {ret}")
                logger.debug(f"材料已定义: {mat_name}")
            except Exception as e:
                logger.warning(f"材料 {mat_name} 定义失败: {e}")

    def _define_sections(self, ir: StructuralIR):
        """定义截面（按类型映射到 SAP2000 API）"""
        for sec in ir.sections:
            try:
                if sec.type == SectionType.CONCRETE_RECT:
                    self._define_concrete_rect(sec)
                elif sec.type == SectionType.WIDE_FLANGE:
                    self._define_wide_flange(sec)
                elif sec.type == SectionType.HSS:
                    self._define_hss(sec)
                else:
                    logger.warning(f"跳过未知截面类型: {sec.type}")
            except Exception as e:
                logger.error(f"截面 {sec.name} 定义失败: {e}")
                raise

    def _define_concrete_rect(self, sec: Section):
        """定义混凝土矩形截面"""
        if not (sec.rect_h and sec.rect_b):
            raise ValueError(f"矩形截面缺少尺寸: {sec.name}")

        result = self.sap_model.PropFrame.SetRectangle(
            sec.name,
            sec.material,
            sec.rect_h / 1000.0,  # mm → m
            sec.rect_b / 1000.0,
        )
        ret, _ = unpack_sap_return(result, "SetRectangle")
        if ret != 0:
            raise RuntimeError(f"SetRectangle {sec.name} 失败: ret={ret}")
        logger.debug(f"矩形截面: {sec.name} = {sec.rect_h}x{sec.rect_b} mm")

    def _define_wide_flange(self, sec: Section):
        """定义 W 型钢截面"""
        if not (sec.depth and sec.width):
            raise ValueError(f"W 截面缺少尺寸: {sec.name}")

        # 简化：用近似尺寸调用 SetIshape（实际需完整 W 型钢参数）
        result = self.sap_model.PropFrame.SetIshape(
            sec.name,
            sec.material,
            sec.depth / 1000.0,
            sec.width / 1000.0,
            0.01,   # 翼缘厚度 (m, 简化)
            0.008,  # 腹板厚度 (m, 简化)
            0.01,   # 翼缘宽度 (m, 简化)
        )
        ret, _ = unpack_sap_return(result, "SetIshape")
        if ret != 0:
            raise RuntimeError(f"SetIshape {sec.name} 失败: ret={ret}")
        logger.debug(f"W 型钢截面: {sec.name} = {sec.depth}x{sec.width} mm")

    def _define_hss(self, sec: Section):
        """定义空心钢管截面（占位）"""
        logger.warning(f"HSS 截面暂未实现: {sec.name}")
        # 简化：用矩形截面代替
        if sec.depth and sec.width:
            result = self.sap_model.PropFrame.SetRectangle(
                sec.name, sec.material,
                sec.depth / 1000.0, sec.width / 1000.0,
            )
            ret, _ = unpack_sap_return(result, "SetRectangle (HSS fallback)")
            if ret != 0:
                logger.warning(f"HSS 截面 {sec.name} 失败: ret={ret}")

    def _create_nodes(self, ir: StructuralIR):
        """创建节点

        SAP2000 OAPI: PointObj.AddCartesian(X, Y, Z, ByRef Name, [CSys], [MergeOff], [MergeNumber])

        CSI OAPI 文档:
        - CSys: 坐标系名称，字符串（默认 "GLOBAL"）
        - MergeOff: True=不合并重复节点
        - MergeNumber: 合并组号

        返回 (Long, String):
        - Long: 返回码，0=成功
        - String: 实际分配的名称

        已知问题：ret=1 表示参数错误（可能是 COM marshalling 问题）
        """
        node_name_map: Dict[int, str] = {}

        for node in ir.nodes:
            # 先用 4 个必需参数（最简形式）
            # AddCartesian(X, Y, Z, ByRef Name) -- 最简单形式
            try:
                result = self.sap_model.PointObj.AddCartesian(
                    node.x, node.y, node.z,
                    "",          # Name: 自动分配
                )
            except Exception as e:
                raise RuntimeError(f"AddCartesian 节点 {node.id} 调用失败: {e}")

            ret, actual_name = unpack_sap_return(result, "AddCartesian")
            if ret != 0:
                raise RuntimeError(
                    f"AddCartesian 节点 {node.id} ({node.x},{node.y},{node.z}) 失败: ret={ret}"
                )

            node_name_map[node.id] = actual_name or str(node.id)

        logger.info(f"创建 {len(ir.nodes)} 个节点")
        logger.debug(f"节点名称映射: {node_name_map}")
        return node_name_map

    def _create_frames(self, ir: StructuralIR, node_name_map: Dict[int, str]):
        """创建框架构件

        SAP2000 OAPI: FrameObj.AddByPoint(Point1, Point2, ByRef Name, ...) 返回 (Long, String)
        """
        for frame in ir.frames:
            # 使用 SAP2000 实际分配的节点名称
            p1_name = node_name_map.get(frame.i_node, str(frame.i_node))
            p2_name = node_name_map.get(frame.j_node, str(frame.j_node))

            result = self.sap_model.FrameObj.AddByPoint(
                p1_name,
                p2_name,
                "",  # Name 让 SAP2000 自动分配
                frame.section,
            )
            ret, actual_name = unpack_sap_return(result, "AddByPoint")
            if ret != 0:
                raise RuntimeError(
                    f"AddByPoint 构件 {frame.id} ({p1_name}->{p2_name}) 失败: ret={ret}"
                )

        logger.info(f"创建 {len(ir.frames)} 个构件")

    def _assign_restraints(self, ir: StructuralIR, node_name_map: Dict[int, str]):
        """设置支座约束"""
        count = 0
        for node in ir.nodes:
            if node.restrain is None:
                continue

            # 使用 SAP2000 实际节点名称
            node_name = node_name_map.get(node.id, str(node.id))

            # restrain 列表顺序: [Ux, Uy, Uz, Rx, Ry, Rz]
            result = self.sap_model.PointObj.SetRestraint(
                node_name,
                node.restrain,
            )
            ret, _ = unpack_sap_return(result, "SetRestraint")
            if ret != 0:
                logger.warning(f"节点 {node.id} ({node_name}) 支座设置失败: ret={ret}")
            count += 1

        if count:
            logger.info(f"设置 {count} 个支座")

    def _define_load_cases(self, ir: StructuralIR):
        """定义荷载工况

        SAP2000 启动后可能已有默认工况 (DEAD, LIVE, SIDESWAY 等)
        - 如果已存在，Add 会返回 ret=1（已存在），我们当作 warning
        - 如果是新工况，正常添加
        """
        for case in ir.load_cases:
            try:
                if case.type == LoadCaseType.DEAD:
                    case_type = "Dead"
                    result = self.sap_model.LoadPatterns.Add(
                        case.name, 1,  # 1 = Dead
                        case.scale_factor, True,  # self_weight
                    )
                elif case.type == LoadCaseType.LIVE:
                    case_type = "Live"
                    result = self.sap_model.LoadPatterns.Add(
                        case.name, 2,  # 2 = Live
                        case.scale_factor, False,
                    )
                elif case.type == LoadCaseType.SEISMIC:
                    case_type = "Quake"
                    result = self.sap_model.LoadPatterns.Add(
                        case.name, 5,  # 5 = Quake
                        case.scale_factor, False,
                    )
                elif case.type == LoadCaseType.WIND:
                    case_type = "Wind"
                    result = self.sap_model.LoadPatterns.Add(
                        case.name, 3,  # 3 = Wind
                        case.scale_factor, False,
                    )
                else:
                    logger.warning(f"未知工况类型 {case.type}，按 Live 处理")
                    result = self.sap_model.LoadPatterns.Add(
                        case.name, 2, case.scale_factor, False,
                    )

                # 解包返回值
                ret, _ = unpack_sap_return(result, "LoadPatterns.Add")
                if ret != 0:
                    # ret=1 通常表示工况已存在（这是非致命错误）
                    logger.debug(
                        f"工况 {case.name} Add 返回码 {ret}（可能已存在）"
                    )

                logger.debug(f"工况已定义: {case.name} ({case_type})")
            except Exception as e:
                logger.warning(f"工况 {case.name} 定义异常: {e}")

    def _apply_loads(self, ir: StructuralIR):
        """施加载荷"""
        # 点荷载
        # PointObj.SetLoadForce 签名:
        # SetLoadForce(Name, LoadPat, Value, [Replace], [CSys])
        # Value 是 6 元素数组 [Fx, Fy, Fz, Mx, My, Mz]
        for pl in ir.point_loads:
            try:
                result = self.sap_model.PointObj.SetLoadForce(
                    str(pl.node_id),
                    pl.case,
                    [pl.fx, pl.fy, pl.fz, pl.mx, pl.my, pl.mz],  # 6 元数组
                )
                ret, _ = unpack_sap_return(result, "SetLoadForce")
                if ret != 0:
                    logger.warning(f"点荷载 {pl.node_id}/{pl.case} 返回码: {ret}")
            except Exception as e:
                logger.warning(f"点荷载 {pl.node_id}/{pl.case} 失败: {e}")

        # 均布荷载
        # FrameObj.SetLoadDistributed 完整签名 (SAP2000 24):
        # SetLoadDistributed(Name, LoadPat, MyType, Dir, Dist1, Dist2,
        #                   AbsoluteStart, AbsoluteEnd, [CSys], [Replace])
        # - MyType: 1=Force per length, 2=Moment per length
        # - Dir: 1=Local 1, 2=Local 2, 3=Local 3, 4=Global X, 5=Y, 6=Z,
        #        7=Gravity (Local -Z), 8=Projected Gravity
        # - Dist1/Dist2: 起点/终点荷载值 (kN/m)
        # - AbsoluteStart/AbsoluteEnd: 起点/终点位置 (m，从构件 i 端起)
        for dl in ir.dist_loads:
            try:
                result = self.sap_model.FrameObj.SetLoadDistributed(
                    str(dl.frame_id),
                    dl.case,
                    1,           # MyType: 1=Force per length (力/长度)
                    7,           # Dir: 7=Gravity (重力方向 -Z)
                    dl.value,    # Dist1: 起点荷载值 (kN/m)
                    dl.value,    # Dist2: 终点荷载值 (kN/m)
                    0.0,         # AbsoluteStart: 起点位置 (m)
                    1.0,         # AbsoluteEnd: 终点位置 (m, 归一化到 0-1)
                )
                ret, _ = unpack_sap_return(result, "SetLoadDistributed")
                if ret != 0:
                    logger.warning(f"均布荷载 {dl.frame_id}/{dl.case} 返回码: {ret}")
            except Exception as e:
                logger.warning(f"均布荷载 {dl.frame_id}/{dl.case} 失败: {e}")

        logger.info(f"施加 {len(ir.point_loads)} 个点荷载, {len(ir.dist_loads)} 个均布荷载")


# ============================================================================
# 第 4 层：SAP2000Analyzer 求解与结果提取
# ============================================================================

class SAP2000Analyzer:
    """SAP2000 求解与结果提取"""

    def __init__(self, sap_object, config: SAP2000Config):
        self.sap_object = sap_object
        self.sap_model = None  # 在调用时获取
        self.config = config

    def run_analysis(self, timeout_sec: Optional[float] = None) -> bool:
        """运行分析

        SAP2000 要求模型必须先保存到磁盘才能求解！

        Returns:
            True=成功, False=失败
        """
        timeout = timeout_sec or self.config.timeout_sec
        try:
            pythoncom.CoInitialize()
            try:
                self.sap_model = self.sap_object.SapModel

                # 1. 求解前必须先保存模型（SAP2000 24 的硬性要求）
                import tempfile
                import os
                temp_model_path = os.path.join(
                    tempfile.gettempdir(),
                    f"sap2000_model_{int(time.time())}.sdb"
                )
                logger.info(f"保存模型到: {temp_model_path}")
                try:
                    result = self.sap_model.File.Save(temp_model_path)
                    ret_save, _ = unpack_sap_return(result, "File.Save")
                    if ret_save != 0:
                        logger.warning(f"模型保存返回码: {ret_save}（继续尝试求解）")
                except Exception as e:
                    logger.warning(f"模型保存异常: {e}（继续尝试求解）")

                # 2. 运行求解
                with watchdog(timeout, "RunAnalysis"):
                    result = self.sap_model.Analyze.RunAnalysis()
                    ret, _ = unpack_sap_return(result, "RunAnalysis")
                    if ret != 0:
                        raise RuntimeError(f"RunAnalysis 返回错误码: ret={ret}")
            finally:
                pythoncom.CoUninitialize()
            logger.info("✅ 分析完成")
            return True
        except WatchdogTimeout as e:
            logger.error(f"求解超时: {e}")
            return False
        except Exception as e:
            logger.exception(f"求解失败: {e}")
            return False

    def extract_results(self, ir: StructuralIR, node_name_map: Dict[int, str] = None) -> SolverResults:
        """提取分析结果

        Args:
            ir: 结构 IR
            node_name_map: IR 节点 ID → SAP2000 实际名称的映射（从 builder 传入）
        """
        results = SolverResults()
        if node_name_map is None:
            node_name_map = {}

        try:
            # 1. 提取节点位移
            self._extract_joint_displacements(ir, results, node_name_map)

            # 2. 提取构件内力
            self._extract_frame_forces(ir, results, node_name_map)

            # 3. 计算最大位移
            if results.joint_displacements:
                max_disp = max(
                    abs(d.get("Uz", 0.0))
                    for d in results.joint_displacements.values()
                )
                results.max_displacement_mm = max_disp * 1000

            # 4. 计算层间位移角（简化：取首层）
            results.max_drift_ratio = self._calc_drift_ratio(ir, results)

            # 5. 计算利用率（按最大应力比简化）
            results.max_utilization = self._calc_utilization(ir, results)

            logger.info(
                f"结果提取: max_disp={results.max_displacement_mm:.2f}mm, "
                f"drift={results.max_drift_ratio:.5f}, "
                f"util={results.max_utilization:.3f}"
            )
        except Exception as e:
            logger.exception(f"结果提取失败: {e}")
            # 返回部分结果（不抛异常）

        return results

    def _extract_joint_displacements(self, ir: StructuralIR, results: SolverResults, node_name_map: Dict[int, str]):
        """提取节点位移

        使用 builder 传入的 node_name_map（IR ID → SAP2000 实际名称）
        """
        try:
            # 设置输出工况
            try:
                self.sap_model.Results.Setup.DeselectAllCasesAndCombosForOutput()
                for case in ir.load_cases:
                    try:
                        self.sap_model.Results.Setup.SetCaseSelectedForOutput(case.name)
                    except Exception:
                        pass
            except Exception:
                pass

            # 用 SAP2000 实际节点名
            sap_node_names = [node_name_map.get(n.id, str(n.id)) for n in ir.nodes]

            ret = self.sap_model.Results.JointDispl(
                sap_node_names,
                1,  # ItemTypeElm=1 (Element)
            )

            # 处理返回值：可能是元组或单个值
            if isinstance(ret, int):
                logger.warning(f"JointDispl 返回 int（{ret}），无数据")
                return
            if isinstance(ret, tuple) and len(ret) >= 12:
                n = ret[0]
                # 如果 n 也是 int（不是 list），说明没有数据
                if not isinstance(n, (list, tuple)) or n == 0:
                    logger.debug(f"JointDispl 返回 n={n}")
                else:
                    # 正确解析
                    for i in range(min(len(ret[1]) if isinstance(ret[1], (list, tuple)) else 0, n)):
                        node_name = str(ret[1][i]) if isinstance(ret[1], (list, tuple)) else str(n)
                        results.joint_displacements[node_name] = {
                            "Ux": ret[6][i] if isinstance(ret[6], (list, tuple)) else ret[6],
                            "Uy": ret[7][i] if isinstance(ret[7], (list, tuple)) else ret[7],
                            "Uz": ret[8][i] if isinstance(ret[8], (list, tuple)) else ret[8],
                            "Rx": ret[9][i] if isinstance(ret[9], (list, tuple)) else ret[9],
                            "Ry": ret[10][i] if isinstance(ret[10], (list, tuple)) else ret[10],
                            "Rz": ret[11][i] if isinstance(ret[11], (list, tuple)) else ret[11],
                        }
                    logger.info(f"提取 {len(results.joint_displacements)} 个节点位移")
            else:
                logger.warning(f"JointDispl 返回格式异常: {type(ret)}")
        except Exception as e:
            logger.warning(f"位移提取失败: {e}")

    def _extract_frame_forces(self, ir: StructuralIR, results: SolverResults, node_name_map: Dict[int, str]):
        """提取构件内力

        注意：SAP2000 会为每个构件返回多个点的结果（通常 8 个点）
        我们取每个构件的第一个结果点
        """
        try:
            # 构件名让 SAP2000 自动分配了，我们需要查实际名
            # 简化用 "All" 查所有构件
            ret = self.sap_model.Results.FrameForce(
                ["All"],
                2,  # ItemTypeElm=2 (Group) - "All" 是一个组
            )

            if isinstance(ret, tuple) and len(ret) >= 13:
                n = ret[0]
                for i in range(n):
                    frame_name = str(ret[1][i])

                    # 每个构件会有多个点的结果（取第一个点）
                    if frame_name not in results.frame_forces:
                        results.frame_forces[frame_name] = {
                            "P": ret[7][i],
                            "V2": ret[8][i],
                            "V3": ret[9][i],
                            "T": ret[10][i],
                            "M2": ret[11][i],
                            "M3": ret[12][i],
                        }
                logger.info(f"提取 {len(results.frame_forces)} 个构件内力")
            else:
                logger.warning(f"FrameForce 返回格式异常: {type(ret)}")
        except Exception as e:
            logger.warning(f"内力提取失败: {e}")

    def _calc_drift_ratio(self, ir: StructuralIR, results: SolverResults) -> float:
        """计算最大层间位移角（简化：取节点最大位移/总高度）"""
        if not ir.nodes or not results.joint_displacements:
            return 0.0

        heights = [n.z for n in ir.nodes]
        max_z = max(heights)
        min_z = min(heights)
        total_height = max_z - min_z
        if total_height <= 0:
            return 0.0

        max_disp = results.max_displacement_mm / 1000.0  # 转 m
        return max_disp / total_height

    def _calc_utilization(self, ir: StructuralIR, results: SolverResults) -> float:
        """计算最大利用率（简化：基于最大弯矩与截面抗弯承载力）"""
        if not ir.sections or not results.frame_forces:
            return 0.0

        max_util = 0.0
        for frame in ir.frames:
            f_id = frame.id
            if f_id not in results.frame_forces:
                continue

            sec = next((s for s in ir.sections if s.name == frame.section), None)
            if not sec:
                continue

            forces = results.frame_forces[f_id]
            moment = abs(forces.get("M3", 0.0))

            # 简化抗弯承载力（按矩形截面估算）
            if sec.type == SectionType.CONCRETE_RECT and sec.rect_h and sec.rect_b:
                # C30 混凝土抗弯承载力 ≈ 0.2 × f_c × b × h²
                h_m = sec.rect_h / 1000.0
                b_m = sec.rect_b / 1000.0
                capacity = 0.2 * 14300 * b_m * h_m * h_m  # kN·m
                if capacity > 0:
                    util = moment / capacity
                    max_util = max(max_util, util)

        return min(max_util, 2.0)  # 上限 2.0


# ============================================================================
# 第 5 层：SAP2000Worker 统一接口（对接 OptimizationLoop）
# ============================================================================

class SAP2000Worker:
    """SAP2000 Worker - 对接 OptimizationLoop 的统一 Solver 接口

    用法：
        worker = SAP2000Worker()
        results = worker.solve(ir)  # 返回 SolverResults

        # 直接替换 MockSolver
        loop = OptimizationLoop(solver=worker)
    """

    def __init__(self, config: Optional[SAP2000Config] = None):
        self.config = config or SAP2000Config()
        self.connection = SAP2000Connection(self.config)
        self.builder: Optional[SAP2000ModelBuilder] = None
        self.analyzer: Optional[SAP2000Analyzer] = None
        self.current_ir: Optional[StructuralIR] = None  # 跟踪当前模型对应的 IR
        self._last_model_path: Optional[str] = None  # 最近保存的模型文件

    def solve(self, ir: StructuralIR) -> SolverResults:
        """求解 IR（完整流程：连接 → 建模 → 求解 → 提取 → 释放）"""
        start = time.time()

        try:
            # 1. 启动 SAP2000（CSiAPIService + Helper 模式）
            self.connection.start()
            self.builder = SAP2000ModelBuilder(
                self.connection.sap_object, self.config
            )
            self.analyzer = SAP2000Analyzer(
                self.connection.sap_object, self.config
            )

            # 2. ApplicationStart 必须在 STA 线程中调用
            pythoncom.CoInitialize()
            try:
                result = self.connection.sap_object.ApplicationStart(5)
                ret, _ = unpack_sap_return(result, "ApplicationStart")
                if ret != 0:
                    raise RuntimeError(f"ApplicationStart 返回错误码: ret={ret}")
                logger.info(f"ApplicationStart(5) 返回: {ret}")
            finally:
                pythoncom.CoUninitialize()

            # 3. 建模
            self.builder.build(ir)
            self.current_ir = ir  # 记录当前 IR

            # 4. 求解
            success = self.analyzer.run_analysis()
            if not success:
                raise RuntimeError("SAP2000 求解失败")

            # 5. 提取结果（传入 builder 的 node_name_map）
            results = self.analyzer.extract_results(ir, self.builder.node_name_map)

            elapsed = time.time() - start
            logger.info(f"✅ SAP2000 求解完成（{elapsed:.2f}s）")
            return results

        except Exception as e:
            logger.exception(f"SAP2000 求解异常: {e}")
            raise

        finally:
            # 6. 释放 SAP2000 + CSiAPIService（无论成功失败都执行）
            try:
                self.connection.stop()
            except Exception as e:
                logger.warning(f"释放失败，强制结束: {e}")
                self.connection.force_kill()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.connection._is_attached:
            self.connection.stop()

    # ============================================================
    # 批 2 新增：增量更新 + 交互式方法
    # ============================================================

    def ensure_ready(self) -> None:
        """确保 SAP2000 已连接并建模完成

        增量更新和求解都需要先有模型。
        如果 current_ir 为 None，自动调用 build() 重新建模型（会重置 SAP2000 模型）。
        """
        if not self.connection._is_attached:
            self.connection.start()
        if self.current_ir is None or self.builder is None:
            raise RuntimeError(
                "模型未初始化，请先调用 solve() 或 build_initial_model()"
            )

    def build_initial_model(self, ir: StructuralIR) -> None:
        """初始建模（不求解）

        与 solve() 的区别：建模后停止，等待用户检查 + 修改
        """
        self.connection.start()
        self.builder = SAP2000ModelBuilder(
            self.connection.sap_object, self.config
        )
        self.analyzer = SAP2000Analyzer(
            self.connection.sap_object, self.config
        )

        # ApplicationStart 必须在 STA 线程中
        pythoncom.CoInitialize()
        try:
            result = self.connection.sap_object.ApplicationStart(5)
            ret, _ = unpack_sap_return(result, "ApplicationStart")
            if ret != 0:
                raise RuntimeError(f"ApplicationStart 返回错误码: ret={ret}")
        except Exception as e:
            # launcher 或 COM 错误：传播，不吞掉
            pythoncom.CoUninitialize()
            raise
        finally:
            pythoncom.CoUninitialize()

        # 建模
        self.builder.build(ir)
        self.current_ir = ir

        # 保存到 .sdb（用户在 SAP2000 中检查）
        self.save_model()
        logger.info("✅ 初始模型已建立并保存（在 SAP2000 中可查看）")

    def save_model(self, path: Optional[str] = None) -> str:
        """保存 SAP2000 模型到 .sdb 文件

        Args:
            path: 自定义路径，None 则用默认 temp 目录

        Returns:
            保存的文件路径
        """
        import tempfile

        if path is None:
            path = os.path.join(
                tempfile.gettempdir(),
                f"sap2000_model_{int(time.time())}.sdb"
            )

        pythoncom.CoInitialize()
        try:
            if self.builder is None:
                self.builder = SAP2000ModelBuilder(
                    self.connection.sap_object, self.config
                )
            self.builder.sap_model = self.connection.sap_object.SapModel

            result = self.builder.sap_model.File.Save(path)
            ret, _ = unpack_sap_return(result, "File.Save")
            if ret != 0:
                logger.warning(f"模型保存返回码: {ret}")
            self._last_model_path = path
            logger.info(f"模型已保存: {path}")
            return path
        finally:
            pythoncom.CoUninitialize()

    def update_model(self, ir_new: StructuralIR) -> Dict[str, int]:
        """增量更新 SAP2000 模型（基于 IR diff）

        Args:
            ir_new: 新的 IR

        Returns:
            各类型更新数量统计
        """
        if self.current_ir is None:
            raise RuntimeError("请先调用 build_initial_model() 或 solve()")

        if ir_new.model_id != self.current_ir.model_id:
            logger.warning(
                f"IR ID 变化: {self.current_ir.model_id} → {ir_new.model_id}"
            )

        from ir_diff import compute_diff
        diff = compute_diff(self.current_ir, ir_new)
        summary = diff.summary()

        if not diff.has_changes():
            logger.info("无变化，跳过更新")
            return summary

        logger.info(f"检测到变化: {summary}")

        # 必须先解锁模型才能修改
        pythoncom.CoInitialize()
        try:
            try:
                self.connection.sap_object.SapModel.SetModelIsLocked(False)
            except Exception:
                pass

            try:
                # 1. 截面修改
                self._update_sections(diff.section_diffs, ir_new)

                # 2. 节点修改
                self._update_nodes(diff.node_diffs, ir_new)

                # 3. 构件修改
                self._update_frames(diff.frame_diffs, ir_new)

                # 4. 均布荷载修改
                self._update_dist_loads(diff.dist_load_diffs, ir_new)

                # 5. 点荷载修改
                self._update_point_loads(diff.point_load_diffs, ir_new)

            finally:
                # 重新锁定（避免脏读）
                try:
                    self.connection.sap_object.SapModel.SetModelIsLocked(True)
                except Exception:
                    pass
        finally:
            pythoncom.CoUninitialize()

        # 更新 current_ir（跳过 save，因为 File.Save 对已运行进程挂住）
        self.current_ir = ir_new
        # self.save_model()  # 暂时跳过以诊断

        logger.info(f"✅ 增量更新完成")
        return summary

    def _update_sections(self, diffs, ir_new):
        """更新截面（修改/新增截面）"""
        for d in diffs:
            # 从新 IR 中查找截面对象
            sec = next((s for s in ir_new.sections
                        if s.name == d.entity_id), None)
            if not sec:
                logger.warning(f"截面 {d.entity_id} 在新 IR 中不存在，跳过")
                continue

            if d.change_type.value == "modified":
                # 直接调用 OAPI 重设截面属性
                try:
                    sap_model = self.connection.sap_object.SapModel
                    mat = sec.material if sec.material else "C30"
                    h = sec.rect_h or 400
                    b = sec.rect_b or 400
                    ret = sap_model.PropFrame.SetRectangle(sec.name, mat, h, b)
                    if ret == 0:
                        logger.info(f"✓ 截面 {d.entity_id} 已更新")
                    else:
                        logger.error(f"更新截面 {d.entity_id} 失败: ret={ret}")
                except Exception as e:
                    logger.error(f"更新截面 {d.entity_id} 失败: {e}")

            elif d.change_type.value == "added":
                # 新增截面：直接调用 OAPI 创建矩形截面
                try:
                    sap_model = self.connection.sap_object.SapModel
                    mat = sec.material if sec.material else "C30"
                    h = sec.rect_h or 400
                    b = sec.rect_b or 400
                    ret = sap_model.PropFrame.SetRectangle(
                        sec.name, mat, h, b
                    )
                    if ret == 0:
                        logger.info(f"✓ 新截面 {d.entity_id} 已创建 (PropFrame.SetRectangle)")
                    else:
                        logger.error(f"创建截面 {d.entity_id} 失败: ret={ret}")
                except Exception as e:
                    logger.error(f"创建截面 {d.entity_id} 失败: {e}")

            else:
                logger.warning(f"截面 {d.entity_id} 暂不支持 {d.change_type.value}")

    def _update_nodes(self, diffs, ir_new):
        """更新节点（修改坐标或约束）"""
        for d in diffs:
            if d.change_type.value == "modified":
                # 修改坐标或约束
                new_node = next((n for n in ir_new.nodes
                                  if n.id == d.entity_id), None)
                if not new_node:
                    continue

                # 如果坐标变了：SetCoordCartesian
                coord_change = any(fc.field_name in ("x", "y", "z")
                                   for fc in d.field_changes)
                if coord_change:
                    try:
                        result = self.connection.sap_object.SapModel.PointObj.SetCoordCartesian(
                            str(new_node.id), new_node.x, new_node.y, new_node.z
                        )
                        ret, _ = unpack_sap_return(result, "SetCoordCartesian")
                        if ret == 0:
                            logger.info(f"✓ 节点 {new_node.id} 坐标已更新")
                    except Exception as e:
                        logger.error(f"更新节点 {new_node.id} 坐标失败: {e}")

                # 如果约束变了：SetRestraint
                restrain_change = any(fc.field_name == "restrain"
                                      for fc in d.field_changes)
                if restrain_change and new_node.restrain is not None:
                    try:
                        result = self.connection.sap_object.SapModel.PointObj.SetRestraint(
                            str(new_node.id), new_node.restrain
                        )
                        ret, _ = unpack_sap_return(result, "SetRestraint")
                        if ret == 0:
                            logger.info(f"✓ 节点 {new_node.id} 约束已更新")
                    except Exception as e:
                        logger.error(f"更新节点 {new_node.id} 约束失败: {e}")

            elif d.change_type.value == "added":
                # 增量添加节点（用 SAP2000 自动分配名）
                new_node = next((n for n in ir_new.nodes
                                  if n.id == d.entity_id), None)
                if not new_node:
                    continue
                try:
                    result = self.connection.sap_object.SapModel.PointObj.AddCartesian(
                        new_node.x, new_node.y, new_node.z, ""
                    )
                    ret, actual_name = unpack_sap_return(result, "AddCartesian")
                    if ret == 0:
                        # 记录新节点名
                        self.builder.node_name_map[new_node.id] = actual_name or str(new_node.id)
                        logger.info(f"✓ 节点 {new_node.id} 已添加 ({actual_name})")
                except Exception as e:
                    logger.error(f"添加节点 {new_node.id} 失败: {e}")

            elif d.change_type.value == "removed":
                logger.warning(
                    f"节点 {d.entity_id} 删除需要级联处理（暂未实现完整删除）"
                )

    def _update_frames(self, diffs, ir_new):
        """更新构件（修改截面引用）"""
        for d in diffs:
            if d.change_type.value != "modified":
                continue

            # 只处理 section 字段修改（其他修改如 i/j 节点不支持）
            section_change = any(fc.field_name == "section"
                                 for fc in d.field_changes)
            if not section_change:
                continue

            new_frame = next((f for f in ir_new.frames
                              if f.id == d.entity_id), None)
            if not new_frame:
                continue

            # SetSectionAssignment (设置构件截面)
            try:
                result = self.connection.sap_object.SapModel.FrameObj.SetSection(
                    str(new_frame.id), new_frame.section
                )
                ret, _ = unpack_sap_return(result, "SetSection")
                if ret == 0:
                    logger.info(
                        f"✓ 构件 {new_frame.id} 截面已更新: {new_frame.section}"
                    )
            except Exception as e:
                logger.error(f"更新构件 {new_frame.id} 截面失败: {e}")

    def _update_dist_loads(self, diffs, ir_new):
        """更新均布荷载（修改 value）"""
        for d in diffs:
            if d.change_type.value != "modified":
                continue

            # 找新荷载值
            frame_id, case = d.entity_id.split("/")
            frame_id = int(frame_id)

            new_dl = next((dl for dl in ir_new.dist_loads
                           if dl.frame_id == frame_id and dl.case == case), None)
            if not new_dl:
                continue

            # 检查字段（只更新 value）
            value_change = any(fc.field_name == "value"
                               for fc in d.field_changes)
            if not value_change:
                continue

            try:
                # 删除旧荷载 + 重新施加（SAP2000 没有"修改荷载"API）
                self.connection.sap_object.SapModel.FrameObj.DeleteLoadDistributed(
                    str(frame_id), case
                )
                # v24 OAPI 签名: (Name, LoadPat, MyType, Dir, Dist1, Dist2, Val1, Val2)
                # 8 个参数：之前 6 个参数会 ret=1（失败），必须给 8 个
                result = self.connection.sap_object.SapModel.FrameObj.SetLoadDistributed(
                    str(frame_id), case,
                    1,           # MyType: Force/Length
                    6,           # Dir: Gravity (Global Y)
                    0, 1,        # Dist1, Dist2 (相对范围 0~1)
                    new_dl.value, new_dl.value,  # Val1, Val2
                )
                ret, _ = unpack_sap_return(result, "SetLoadDistributed")
                if ret == 0:
                    logger.info(
                        f"✓ 均布荷载 {frame_id}/{case} 已更新: {new_dl.value}"
                    )
            except Exception as e:
                logger.error(f"更新均布荷载 {frame_id}/{case} 失败: {e}")

    def _update_point_loads(self, diffs, ir_new):
        """更新点荷载（修改 fx/fy/fz 等）"""
        for d in diffs:
            if d.change_type.value != "modified":
                continue

            node_id, case = d.entity_id.split("/")
            node_id = int(node_id)

            new_pl = next((pl for pl in ir_new.point_loads
                           if pl.node_id == node_id and pl.case == case), None)
            if not new_pl:
                continue

            try:
                # 删除旧荷载 + 重新施加
                self.connection.sap_object.SapModel.PointObj.DeleteLoadForce(
                    str(node_id), case
                )
                result = self.connection.sap_object.SapModel.PointObj.SetLoadForce(
                    str(node_id), case,
                    [new_pl.fx, new_pl.fy, new_pl.fz,
                     new_pl.mx, new_pl.my, new_pl.mz]
                )
                ret, _ = unpack_sap_return(result, "SetLoadForce")
                if ret == 0:
                    logger.info(f"✓ 点荷载 {node_id}/{case} 已更新")
            except Exception as e:
                logger.error(f"更新点荷载 {node_id}/{case} 失败: {e}")

    def run_analysis_only(self) -> bool:
        """只运行分析（不重建模型）

        用于交互式工作流：修改 → 求解 → 提取
        """
        if self.analyzer is None:
            self.analyzer = SAP2000Analyzer(
                self.connection.sap_object, self.config
            )
        return self.analyzer.run_analysis()
    def extract_results(self) -> SolverResults:
        """只提取结果（不求解）"""
        if self.analyzer is None or self.current_ir is None:
            raise RuntimeError("请先建立模型")
        # node_name_map 可能在 reinit 后丢失，从 builder 同步过来
        if hasattr(self.builder, 'node_name_map'):
            self.node_name_map = self.builder.node_name_map
        elif not hasattr(self, 'node_name_map'):
            self.node_name_map = {}
        return self.analyzer.extract_results(
            self.current_ir, self.node_name_map
        )


# ============================================================================
# 第 6 层：便捷函数 + Mock 降级
# ============================================================================

def create_sap2000_solver(
    timeout_sec: float = 600.0,
    visible: bool = False,
    fallback_to_mock: bool = True,
):
    """创建 SAP2000 Solver（自动降级到 Mock）

    Args:
        timeout_sec: 单次求解超时
        visible: SAP2000 窗口可见（调试用）
        fallback_to_mock: SAP2000 不可用时是否降级到 Mock

    Returns:
        SAP2000Worker 或 MockSolver
    """
    config = SAP2000Config(timeout_sec=timeout_sec, visible=visible)

    try:
        # 快速检测 SAP2000 是否可用
        test_conn = SAP2000Connection(config)
        test_conn.start()
        if test_conn._is_attached:
            test_conn.stop()
            logger.info("✅ SAP2000 可用")
            return SAP2000Worker(config)
    except Exception as e:
        logger.warning(f"SAP2000 不可用: {e}")

    if fallback_to_mock:
        logger.warning("⚠️ 降级到 MockSolver")
        return MockSolver()
    else:
        raise RuntimeError("SAP2000 不可用，且未启用 Mock 降级")


# ============================================================================
# 第 7 层：测试入口
# ============================================================================

def main():
    """主入口：测试 SAP2000 Worker"""
    print("=" * 70)
    print("SAP2000 Worker v1.0 - 测试入口")
    print("=" * 70)

    # 1. 检测 SAP2000 是否安装
    print("\n[1] 检测 SAP2000 安装...")
    try:
        test_obj = CreateObject("CSI.SAP2000.API.SapObject")
        print("    ✅ SAP2000 COM 对象可创建")
        del test_obj
    except Exception as e:
        print(f"    ❌ SAP2000 不可用: {e}")
        print("    请确认：")
        print("      1. SAP2000 已安装（v22/v24/v26）")
        print("      2. 以管理员身份运行此脚本")
        print("      3. SAP2000 license 有效")
        return

    # 2. 简单建模测试
    print("\n[2] 简单建模测试...")
    from ir_compiler import (
        StructuralIR, Node, Frame, Section, SectionType,
        LoadCase, LoadCaseType, AnalysisSetting, SolverType,
    )

    ir = StructuralIR(
        model_id="worker_test",
        name="Worker Test Simple Frame",
        nodes=[
            Node(id=1, x=0, y=0, z=0,
                 restrain=[True, True, True, False, False, False]),
            Node(id=2, x=5, y=0, z=0),
            Node(id=3, x=5, y=0, z=3.5,
                 restrain=[False, False, True, False, False, False]),
        ],
        frames=[
            Frame(id=1, i=1, j=2, section="BEAM", role="beam"),
            Frame(id=2, i=2, j=3, section="COL", role="column"),
        ],
        sections=[
            Section(name="BEAM", type=SectionType.CONCRETE_RECT,
                    rect_h=400, rect_b=200, material="C30"),
            Section(name="COL", type=SectionType.CONCRETE_RECT,
                    rect_h=300, rect_b=300, material="C30"),
        ],
        load_cases=[LoadCase(name="DEAD", self_weight=True)],
        analysis=AnalysisSetting(target_solver=SolverType.SAP2000),
    )

    # 3. 运行 SAP2000 Worker
    print("\n[3] 启动 SAP2000 Worker...")
    config = SAP2000Config(visible=False, timeout_sec=120.0)
    worker = SAP2000Worker(config)

    try:
        results = worker.solve(ir)
        print("\n[4] 求解结果:")
        print(f"    最大位移: {results.max_displacement_mm:.2f} mm")
        print(f"    最大层间位移角: {results.max_drift_ratio:.5f}")
        print(f"    最大利用率: {results.max_utilization:.3f}")
        print(f"    节点位移数: {len(results.joint_displacements)}")
        print(f"    构件内力数: {len(results.frame_forces)}")
        print("\n✅ SAP2000 Worker 测试通过")
    except Exception as e:
        print(f"\n❌ SAP2000 Worker 测试失败: {e}")
    finally:
        worker.connection.stop()


if __name__ == "__main__":
    main()