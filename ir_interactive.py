"""
IR Interactive Session（交互式会话管理器）
===========================================
实现用户与 CAE 模型的多轮交互：

用户 → 自然语言指令 → NLP 解析 → IR 修改 → SAP2000 增量更新
                  ↓
         show / solve / exit

会话流程：
    1. build_initial_model()  → 模型建好，停在 SAP2000
    2. while True:
         user input → NLP parse → modify / solve / show
         (用户可在 SAP2000 中检查模型)
    3. 用户说 exit → 关闭

作者：MiniMax-M3 / Hermes CSI System
版本：v1.0.0
"""

from __future__ import annotations

import sys
import logging
import shlex
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from ir_compiler import StructuralIR
from ir_nlp import IRCommandParser, CommandType, IRCommand
from ir_diff import (
    scale_all_dist_loads, scale_all_point_loads,
    apply_modification, Modification,
)
from sap2000_worker import SAP2000Worker, SAP2000Config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger("ir_interactive")


# ============================================================================
# 第 1 层：会话状态
# ============================================================================

@dataclass
class SessionState:
    """交互式会话状态"""
    ir: StructuralIR                       # 当前 IR
    worker: SAP2000Worker                  # SAP2000 Worker
    parser: IRCommandParser = field(default_factory=IRCommandParser)
    running: bool = False                 # 会话是否运行中
    model_path: Optional[str] = None       # 最近保存的模型路径

    # 统计
    modifications_count: int = 0           # 修改次数
    solve_count: int = 0                  # 求解次数

    def summary(self) -> Dict[str, Any]:
        return {
            "model_id": self.ir.model_id,
            "nodes": len(self.ir.nodes),
            "frames": len(self.ir.frames),
            "sections": len(self.ir.sections),
            "load_cases": len(self.ir.load_cases),
            "dist_loads": len(self.ir.dist_loads),
            "point_loads": len(self.ir.point_loads),
            "modifications": self.modifications_count,
            "solves": self.solve_count,
            "last_model": self.model_path,
        }


# ============================================================================
# 第 2 层：会话主循环
# ============================================================================

class InteractiveSession:
    """交互式 IR 会话管理器

    用法：
        session = InteractiveSession(ir, worker_config)
        session.run()  # 进入主循环，阻塞直到用户 exit
    """

    PROMPT = "csi> "

    def __init__(self, ir: StructuralIR, config: Optional[SAP2000Config] = None):
        self.state = SessionState(ir=ir, worker=SAP2000Worker(config))

    # ─────────────────────────────────────────────────────────────
    # 公开 API
    # ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """启动交互式会话（阻塞）"""
        self._print_banner()
        self._build_initial()
        self.state.running = True

        try:
            while self.state.running:
                try:
                    user_input = input(self.PROMPT).strip()
                except (EOFError, KeyboardInterrupt):
                    logger.info("收到中断信号")
                    user_input = "exit"

                if not user_input:
                    continue

                self._handle_input(user_input)

        except KeyboardInterrupt:
            logger.info("会话中断 (Ctrl+C)")
        finally:
            self._cleanup()

    def handle_command(self, text: str) -> Dict[str, Any]:
        """非阻塞方式处理单条指令（供外部调用）"""
        self._handle_input(text)
        return self.state.summary()

    # ─────────────────────────────────────────────────────────────
    # 内部处理
    # ─────────────────────────────────────────────────────────────

    def _build_initial(self) -> None:
        """初始建模"""
        logger.info("正在启动 SAP2000 并建模...")
        try:
            self.state.worker.build_initial_model(self.state.ir)
            self.state.model_path = self.state.worker._last_model_path
            logger.info("✅ 初始模型已建立")
            self._print_model_path()
        except Exception as e:
            logger.exception(f"初始建模失败: {e}")
            raise

    def _handle_input(self, raw: str) -> None:
        """处理用户输入"""
        # 支持多命令（用 ; 分隔）
        for cmd_text in raw.split(";"):
            cmd_text = cmd_text.strip()
            if not cmd_text:
                continue

            # 解析
            cmd = self.state.parser.parse(cmd_text)

            if cmd.command_type == CommandType.SHOW:
                self._cmd_show(cmd)
            elif cmd.command_type == CommandType.SOLVE:
                self._cmd_solve(cmd)
            elif cmd.command_type == CommandType.EXIT:
                logger.info("用户请求退出")
                self.state.running = False
            elif cmd.command_type in (CommandType.SET_SECTION,
                                       CommandType.SET_LOAD,
                                       CommandType.SCALE_LOAD,
                                       CommandType.REMOVE_NODE):
                self._cmd_modify(cmd)
            elif cmd.command_type == CommandType.UNKNOWN:
                logger.warning(f"无法理解: {cmd.error}")
                self._print_help()
            else:
                logger.warning(f"指令 {cmd.command_type} 暂未实现")

    # ─────────────────────────────────────────────────────────────
    # 指令处理函数
    # ─────────────────────────────────────────────────────────────

    def _cmd_show(self, cmd: IRCommand) -> None:
        """show 指令：显示当前 IR 状态"""
        s = self.state
        ir = s.ir

        target = cmd.args.get("target", "all").lower()

        if target in ("all", "模型", "model"):
            print(self._render_model_summary())
        elif target in ("荷载", "loads", "load"):
            print(self._render_loads())
        elif target in ("截面", "sections", "section"):
            print(self._render_sections())
        elif target in ("节点", "nodes"):
            print(self._render_nodes())
        elif target in ("构件", "frames"):
            print(self._render_frames())
        else:
            print(self._render_model_summary())

    def _cmd_solve(self, cmd: IRCommand) -> None:
        """solve 指令：求解"""
        s = self.state
        s.solve_count += 1

        logger.info("开始求解...")
        try:
            ok = s.worker.run_analysis_only()
            if ok:
                results = s.worker.extract_results()
                print(f"\n{'='*50}")
                print(f"  ✅ 求解完成（第 {s.solve_count} 次）")
                print(f"  最大位移: {results.max_displacement_mm:.2f} mm")
                print(f"  最大利用率: {results.max_utilization:.3f}")
                if results.joint_displacements:
                    print(f"  位移测点: {len(results.joint_displacements)} 个")
                if results.frame_forces:
                    print(f"  构件内力: {len(results.frame_forces)} 个")
                print(f"{'='*50}\n")
                logger.info("结果已提取，可在 SAP2000 中查看详细云图")
            else:
                logger.error("求解失败（返回 False）")
        except Exception as e:
            logger.exception(f"求解异常: {e}")
            # 即使提取失败，仍告知用户在 SAP2000 中查看
            logger.info("请在 SAP2000 中手动检查结果")

    def _cmd_modify(self, cmd: IRCommand) -> None:
        """修改类指令：set_section / set_load / scale_load / remove_node"""
        s = self.state
        ir = s.ir

        if cmd.command_type == CommandType.SCALE_LOAD:
            # 批量缩放（需要从 IR 动态生成 Modification）
            factor = cmd.args["factor"]
            case = cmd.args["case"]
            mods = scale_all_dist_loads(ir, case, factor)
            if not mods:
                logger.warning(f"未找到 {case} 工况的均布荷载")
                return
            results = self._apply_modifications(ir, mods)
            # 同步更新 s.ir（因为 apply_modification 直接改 ir）
            logger.info(f"已批量缩放 {case} × {factor}，{len(mods)} 个荷载")

        elif cmd.command_type == CommandType.SET_SECTION:
            # 改截面：先生成新截面定义，再修改 frame.section 引用
            h = int(cmd.args["height"])
            b = int(cmd.args["width"])
            entity_kind = cmd.args["entity_kind"]  # "柱" / "梁"
            entity_id = cmd.args["entity_id"]

            new_sec_name = f"{entity_kind}_{h}x{b}"

            # 1. 检查 IR 中是否已有同名截面（若有则更新尺寸，否则新增）
            existing = next((s for s in ir.sections if s.name == new_sec_name), None)
            if existing:
                existing.rect_h = h
                existing.rect_b = b
            else:
                from ir_compiler import Section, SectionType
                new_sec = Section(
                    name=new_sec_name,
                    type=SectionType.RECTANGULAR,
                    depth=float(h),
                    width=float(b),
                    rect_h=float(h),
                    rect_b=float(b),
                )
                ir.sections.append(new_sec)

            # 2. 更新 frame 的 section 引用
            from ir_diff import apply_modification
            mod = Modification(
                entity_type="frame",
                entity_id=entity_id,
                field_name="section",
                new_value=new_sec_name,
            )
            ok, msg = apply_modification(ir, mod)
            if ok:
                logger.info(f"✓ {msg}")
                self._apply_ir_update(ir)
            else:
                logger.error(f"修改失败: {msg}")

        elif cmd.command_type == CommandType.SET_LOAD:
            # 改单个荷载
            frame_id = cmd.args["frame_id"]
            case = cmd.args["case"]
            value = cmd.args["value"]

            mod = Modification(
                entity_type="dist_load",
                entity_id=(frame_id, case),
                field_name="value",
                new_value=value,
            )
            ok, msg = apply_modification(ir, mod)
            if ok:
                logger.info(f"✓ {msg}")
                self._apply_ir_update(ir)
            else:
                logger.error(f"修改失败: {msg}")

        elif cmd.command_type == CommandType.REMOVE_NODE:
            logger.warning("删除节点需要级联删除构件（暂未实现）")
            return

        else:
            logger.warning(f"指令 {cmd.command_type} 未实现")

    def _apply_modifications(self, ir, mods: List[Modification]) -> List:
        """应用修改列表到 IR + 增量更新 SAP2000"""
        from ir_diff import apply_modifications as _apply
        return _apply(ir, mods)

    def _apply_ir_update(self, ir: StructuralIR) -> None:
        """将修改后的 IR 同步到 SAP2000"""
        s = self.state
        s.modifications_count += 1

        try:
            summary = s.worker.update_model(ir)
            s.model_path = s.worker._last_model_path
            logger.info(f"✅ SAP2000 模型已更新: {summary}")
            self._print_model_path()
        except Exception as e:
            logger.exception(f"SAP2000 增量更新失败: {e}")
            raise

    # ─────────────────────────────────────────────────────────────
    # 渲染函数
    # ─────────────────────────────────────────────────────────────

    def _render_model_summary(self) -> str:
        """渲染模型总览"""
        s = self.state
        ir = s.ir

        lines = [
            "",
            f"  模型 ID: {ir.model_id}",
            f"  节点:    {len(ir.nodes)} 个",
            f"  构件:    {len(ir.frames)} 个",
            f"  截面:    {len(ir.sections)} 个",
            f"  荷载:    {len(ir.dist_loads)} 个均布 + {len(ir.point_loads)} 个点",
            f"  修改次数: {s.modifications_count}",
            f"  求解次数: {s.solve_count}",
            "",
        ]
        return "\n".join(lines)

    def _render_nodes(self) -> str:
        ir = self.state.ir
        lines = ["\n  节点列表:"]
        for n in ir.nodes:
            r = "固接" if n.restrain else "自由"
            lines.append(f"    节点 {n.id}: ({n.x}, {n.y}, {n.z})  {r}")
        lines.append("")
        return "\n".join(lines)

    def _render_frames(self) -> str:
        ir = self.state.ir
        lines = ["\n  构件列表:"]
        for f in ir.frames:
            lines.append(f"    构件 {f.id}: {f.i_node} → {f.j_node}  [{f.role}] 截面={f.section}")
        lines.append("")
        return "\n".join(lines)

    def _render_sections(self) -> str:
        ir = self.state.ir
        lines = ["\n  截面列表:"]
        for sec in ir.sections:
            if sec.type.value == "RECTANGULAR":
                lines.append(f"    {sec.name}: {sec.rect_h}x{sec.rect_b} mm  材料={sec.material}")
            elif sec.type.value == "WIDE_FLANGE":
                lines.append(f"    {sec.name}: W{depth}x{weight}  材料={sec.material}")
            else:
                lines.append(f"    {sec.name}: {sec.type.value}")
        lines.append("")
        return "\n".join(lines)

    def _render_loads(self) -> str:
        ir = self.state.ir
        lines = ["\n  均布荷载:"]
        for dl in ir.dist_loads:
            lines.append(f"    梁{dl.frame_id}  {dl.case}  {dl.value} kN/m")
        if ir.point_loads:
            lines.append("\n  点荷载:")
            for pl in ir.point_loads:
                lines.append(f"    节点{pl.node_id}  {pl.case}  "
                             f"F=({pl.fx}, {pl.fy}, {pl.fz}) kN")
        lines.append("")
        return "\n".join(lines)

    def _print_model_path(self) -> None:
        if self.state.model_path:
            print(f"\n  💾 模型已保存: {self.state.model_path}\n")

    def _print_banner(self) -> None:
        banner = """
╔═══════════════════════════════════════════════════════╗
║       CSI CAE Agent Platform - 交互式会话             ║
╠═══════════════════════════════════════════════════════╣
║  指令说明:                                            ║
║    show              查看模型总览                      ║
║    show loads        查看当前荷载                      ║
║    show sections     查看截面                         ║
║    show nodes        查看节点                          ║
║    show frames       查看构件                          ║
║                                                       ║
║    改柱1截面为600x600   改截面                        ║
║    改梁5荷载为-25       改单个荷载                    ║
║    DEAD荷载都乘1.4倍    批量缩放                      ║
║                                                       ║
║    solve             求解（修改后执行）               ║
║    exit / 退出        退出                             ║
╚═══════════════════════════════════════════════════════╝
"""
        print(banner)

    def _print_help(self) -> None:
        print("  常用指令: show / solve / exit")

    def _cleanup(self) -> None:
        """会话结束时清理"""
        logger.info("正在关闭 SAP2000...")
        try:
            self.state.worker.connection.stop()
            logger.info("✅ SAP2000 已关闭")
        except Exception as e:
            logger.warning(f"关闭失败: {e}")
            self.state.worker.connection.force_kill()
        self.state.running = False


# ============================================================================
# 第 3 层：便捷入口
# ============================================================================

def start_session(
    ir: Optional[StructuralIR] = None,
    config: Optional[SAP2000Config] = None,
    initial_model_path: Optional[str] = None,
) -> InteractiveSession:
    """启动交互式会话

    Args:
        ir: 结构 IR（None 则用 build_sample_frame_ir）
        config: SAP2000 配置（None 则用默认）
        initial_model_path: 可选，加载已有 .sdb 到 SAP2000（暂未实现）

    Returns:
        InteractiveSession 实例
    """
    if ir is None:
        from ir_compiler import build_sample_frame_ir
        ir = build_sample_frame_ir()

    session = InteractiveSession(ir, config)
    return session


def run_demo():
    """演示模式（不启动 SAP2000，仅展示交互流程）"""
    from ir_compiler import build_sample_frame_ir
    ir = build_sample_frame_ir()

    print("演示模式：仅展示交互流程，不连接 SAP2000")
    print("=" * 60)

    parser = IRCommandParser()

    demo_commands = [
        "show",
        "改柱1截面为600x600",
        "改梁5荷载为-25",
        "DEAD荷载都乘1.4倍",
        "show loads",
        "solve",
        "exit",
    ]

    for cmd_text in demo_commands:
        print(f"\ncsi> {cmd_text}")
        cmd = parser.parse(cmd_text)
        if cmd.command_type == CommandType.UNKNOWN:
            print(f"  ⚠ 无法理解: {cmd.error}")
        else:
            print(f"  → 指令类型: {cmd.command_type.value}")
            if cmd.modifications:
                for m in cmd.modifications:
                    print(f"     修改: {m.to_dict()}")


# ============================================================================
# 第 4 层：测试入口
# ============================================================================

def main():
    """主入口：根据参数启动演示或真实会话"""
    import argparse

    parser_cli = argparse.ArgumentParser(description="IR 交互式会话")
    parser_cli.add_argument("--demo", action="store_true",
                            help="演示模式（不启动 SAP2000）")
    parser_cli.add_argument("--config", type=str, default=None,
                            help="SAP2000Config JSON 文件路径")
    args = parser_cli.parse_args()

    if args.demo:
        run_demo()
        return

    # 真实会话
    from ir_compiler import build_sample_frame_ir
    ir = build_sample_frame_ir()

    config = None
    if args.config:
        import json
        with open(args.config) as f:
            cfg = json.load(f)
            config = SAP2000Config(**cfg)

    session = start_session(ir, config)
    session.run()


if __name__ == "__main__":
    main()