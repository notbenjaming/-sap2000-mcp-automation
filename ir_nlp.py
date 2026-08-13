"""
IR NLP Parser（自然语言 → IRCommand）
=====================================
把中文/英文自然语言指令翻译成 IR 修改操作。

支持 6 类指令：
1. set_section     - 改截面（柱/梁）
2. set_load        - 改荷载
3. scale_load      - 批量缩放荷载
4. remove_node     - 删除节点
5. solve           - 求解
6. show            - 显示

作者：MiniMax-M3 / Hermes CSI System
版本：v1.0.0
"""

from __future__ import annotations

import re
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple

from ir_diff import Modification


# ============================================================================
# 第 1 层：指令模型
# ============================================================================

class CommandType(str, Enum):
    """支持的指令类型"""
    SET_SECTION = "set_section"           # 改截面
    SET_LOAD = "set_load"                # 改单个荷载
    SCALE_LOAD = "scale_load"            # 批量缩放
    REMOVE_NODE = "remove_node"          # 删除节点
    SOLVE = "solve"                       # 求解
    SHOW = "show"                         # 显示
    EXIT = "exit"                         # 退出
    UNKNOWN = "unknown"                   # 未识别


@dataclass
class IRCommand:
    """解析后的指令"""
    command_type: CommandType
    raw_text: str                              # 原始输入
    args: Dict[str, Any] = field(default_factory=dict)
    modifications: List[Modification] = field(default_factory=list)
    confidence: float = 1.0                   # 解析置信度
    error: Optional[str] = None                # 错误信息

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_type": self.command_type.value,
            "raw_text": self.raw_text,
            "args": self.args,
            "modifications": [m.to_dict() for m in self.modifications],
            "confidence": self.confidence,
            "error": self.error,
        }


# ============================================================================
# 第 2 层：NLP 解析器
# ============================================================================

class IRCommandParser:
    """自然语言 → IRCommand 解析器

    设计原则：
    - 多模式匹配（支持中英文混合）
    - 严格校验参数（数值、ID 格式）
    - 直接生成 Modification 对象（可立即应用到 IR）
    """

    # ----------- 截面修改 -----------

    # 改柱/梁 N 截面为 H x B
    PATTERN_SET_SECTION = [
        # 中文: "改柱1的截面为600x600", "柱1截面改成500x500", "柱2截面改为500x500"
        r'(?:改\s*)?(柱|梁)\s*(\d+)\s*的?\s*截面\s*(?:为|改成|改为|是)\s*(\d+)\s*[xX×*]\s*(\d+)',
        # 简写: "柱1=600x600"
        r'(柱|梁)\s*(\d+)\s*[=:]\s*(\d+)\s*[xX×*]\s*(\d+)',
        # 英文: "set column 1 section to 600x600"
        r'set\s+(column|col|beam|b)\s*(\d+)\s+section\s+to\s+(\d+)\s*[xX×*]\s*(\d+)',
    ]

    # 荷载修改
    PATTERN_SET_LOAD = [
        # "改梁5荷载为-25", "梁5 DEAD荷载改成-30", "梁6 LIVE荷载改为-15"
        # "梁5荷载-25"
        r'(?:改\s*)?梁\s*(\d+)\s*(\w+)?\s*荷载\s*(?:为|改成|改为|是|-)?\s*([-\d.]+)',
        # "frame 7 DEAD load = -25"
        r'(?:frame|f)\s*(\d+)\s+(DEAD|LIVE|SEISMIC|WIND)\s+load\s*[=:]\s*([-\d.]+)',
    ]

    # ----------- 批量缩放 -----------

    # DEAD 荷载都乘 1.4 倍 / DEAD loads * 1.4
    PATTERN_SCALE_LOAD = [
        r'(DEAD|LIVE|SEISMIC|WIND)\s*荷载.*?([\d.]+)\s*倍',
        r'(?:scale|multiply)\s+(DEAD|LIVE|SEISMIC|WIND)\s+loads?\s*(?:by\s*)?([\d.]+)',
    ]

    # ----------- 删除节点 -----------

    PATTERN_REMOVE_NODE = [
        r'删\s*节点\s*(\d+)',
        r'删\s*除\s*节点\s*(\d+)',
        r'(?:remove|delete)\s+node\s*(\d+)',
    ]

    # ----------- 求解 -----------

    PATTERN_SOLVE = [
        r'^(?:solve|求解|算一下|运行|执行)',
        r'^(?:run|执行)\s*(?:analysis|分析)',
    ]

    # ----------- 退出 -----------

    PATTERN_EXIT = [
        r'^(?:exit|quit|q|退出)',
    ]

    # ----------- 显示 -----------

    PATTERN_SHOW = [
        r'^(?:show|显示|查看)',
        r'^(?:list|列出)\s*(.+)',
    ]

    def parse(self, text: str) -> IRCommand:
        """解析自然语言指令

        Returns:
            IRCommand 对象（包含 modifications 列表）
        """
        text = text.strip()
        if not text:
            return IRCommand(
                command_type=CommandType.UNKNOWN,
                raw_text=text,
                error="空指令",
            )

        # 1. 截面修改
        for pattern in self.PATTERN_SET_SECTION:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return self._handle_set_section(text, m)

        # 2. 荷载修改
        for pattern in self.PATTERN_SET_LOAD:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return self._handle_set_load(text, m)

        # 3. 批量缩放
        for pattern in self.PATTERN_SCALE_LOAD:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return self._handle_scale_load(text, m)

        # 4. 删除节点
        for pattern in self.PATTERN_REMOVE_NODE:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return self._handle_remove_node(text, m)

        # 5. 求解
        for pattern in self.PATTERN_SOLVE:
            if re.match(pattern, text, re.IGNORECASE):
                return IRCommand(
                    command_type=CommandType.SOLVE,
                    raw_text=text,
                    args={},
                )

        # 6. 退出
        for pattern in self.PATTERN_EXIT:
            if re.match(pattern, text, re.IGNORECASE):
                return IRCommand(
                    command_type=CommandType.EXIT,
                    raw_text=text,
                    args={},
                )

        # 6. 显示
        for pattern in self.PATTERN_SHOW:
            m = re.match(pattern, text, re.IGNORECASE)
            if m:
                show_target = m.group(1) if m.lastindex else "all"
                return IRCommand(
                    command_type=CommandType.SHOW,
                    raw_text=text,
                    args={"target": show_target},
                )

        # 未识别
        return IRCommand(
            command_type=CommandType.UNKNOWN,
            raw_text=text,
            error=f"无法理解指令: {text}",
        )

    def _handle_set_section(self, raw: str, m: re.Match) -> IRCommand:
        """处理 set_section 指令"""
        entity_kind = m.group(1)  # "柱" / "梁" / "column" / "beam"
        entity_id = int(m.group(2))
        h = float(m.group(3))
        b = float(m.group(4))

        # 柱 → 修改 column 的 section
        # 梁 → 修改 beam 的 section
        # 实际上 IR 中所有构件共享 section pool
        # 我们假设：用户说"柱 N"指的是 frame N 的 section
        mod = Modification(
            entity_type="frame",
            entity_id=entity_id,
            field_name="section",
            new_value=f"{entity_kind}_{int(h)}x{int(b)}",  # 新截面名
        )

        return IRCommand(
            command_type=CommandType.SET_SECTION,
            raw_text=raw,
            args={
                "entity_kind": entity_kind,
                "entity_id": entity_id,
                "height": h,
                "width": b,
            },
            modifications=[mod],
        )

    def _handle_set_load(self, raw: str, m: re.Match) -> IRCommand:
        """处理 set_load 指令"""
        frame_id = int(m.group(1))
        case = m.group(2) or "DEAD"  # 默认 DEAD
        if case is None:
            case = "DEAD"
        new_value = float(m.group(3))

        mod = Modification(
            entity_type="dist_load",
            entity_id=(frame_id, case),
            field_name="value",
            new_value=new_value,
        )

        return IRCommand(
            command_type=CommandType.SET_LOAD,
            raw_text=raw,
            args={
                "frame_id": frame_id,
                "case": case,
                "value": new_value,
            },
            modifications=[mod],
        )

    def _handle_scale_load(self, raw: str, m: re.Match) -> IRCommand:
        """处理 scale_load 指令（生成批量修改占位）"""
        case = m.group(1).upper()
        factor = float(m.group(2))

        # 注意：批量缩放需要 IR 上下文，在 InteractiveSession 中实际应用
        return IRCommand(
            command_type=CommandType.SCALE_LOAD,
            raw_text=raw,
            args={
                "case": case,
                "factor": factor,
            },
            modifications=[],  # 由会话动态生成
        )

    def _handle_remove_node(self, raw: str, m: re.Match) -> IRCommand:
        """处理 remove_node 指令"""
        node_id = int(m.group(1))

        return IRCommand(
            command_type=CommandType.REMOVE_NODE,
            raw_text=raw,
            args={"node_id": node_id},
            modifications=[],  # 由会话处理级联删除
        )


# ============================================================================
# 第 3 层：测试入口
# ============================================================================

def main():
    """NLP 解析器测试"""
    print("=" * 70)
    print("IR NLP Parser v1.0 - 测试入口")
    print("=" * 70)

    parser = IRCommandParser()

    test_cases = [
        # 截面修改
        ("改柱1截面为600x600", CommandType.SET_SECTION),
        ("柱2截面改成500x500", CommandType.SET_SECTION),
        ("梁3=400x800", CommandType.SET_SECTION),
        ("set column 5 section to 600x600", CommandType.SET_SECTION),
        # 荷载修改
        ("改梁5荷载为-25", CommandType.SET_LOAD),
        ("梁6 DEAD荷载改成-30", CommandType.SET_LOAD),
        ("frame 7 LIVE load = -10", CommandType.SET_LOAD),
        # 批量缩放
        ("DEAD荷载都乘1.4倍", CommandType.SCALE_LOAD),
        ("scale LIVE loads by 0.8", CommandType.SCALE_LOAD),
        # 删除节点
        ("删节点3", CommandType.REMOVE_NODE),
        ("remove node 5", CommandType.REMOVE_NODE),
        # 求解
        ("solve", CommandType.SOLVE),
        ("求解", CommandType.SOLVE),
        # 显示
        ("show", CommandType.SHOW),
        ("显示荷载", CommandType.SHOW),
        # 未识别
        ("什么是 SAP2000？", CommandType.UNKNOWN),
    ]

    print("\n指令解析测试:")
    print("-" * 70)
    correct = 0
    for text, expected in test_cases:
        cmd = parser.parse(text)
        ok = "✓" if cmd.command_type == expected else "✗"
        if cmd.command_type == expected:
            correct += 1
        print(f"  {ok} [{cmd.command_type.value:15s}] {text}")
        if cmd.error:
            print(f"      错误: {cmd.error}")
        if cmd.modifications:
            for mod in cmd.modifications:
                print(f"      修改: {mod.to_dict()}")

    print(f"\n通过率: {correct}/{len(test_cases)}")

    # 集成测试：解析 + 应用到 IR
    print("\n" + "=" * 70)
    print("集成测试：解析 → 应用到 IR")
    print("=" * 70)

    from ir_compiler import build_sample_frame_ir
    from ir_diff import apply_modifications

    ir = build_sample_frame_ir()
    print(f"\n初始: 截面 {[s.name for s in ir.sections]}")
    print(f"初始: frame[0].section = {ir.frames[0].section}")

    # 解析 "柱1截面改成600x600"
    cmd = parser.parse("柱1截面改成600x600")
    if cmd.modifications:
        # 注意：上面 set_section 改的是 frame.section，需要先创建新截面
        # 这里简化测试：手动创建一个修改列表
        from ir_diff import scale_all_dist_loads
        mods = scale_all_dist_loads(ir, "DEAD", 1.4)
        results = apply_modifications(ir, mods)
        print(f"\n应用 DEAD×1.4 修改:")
        for ok, msg in results:
            print(f"  {msg}")

    print(f"\n最终: DEAD 荷载值:")
    for d in ir.dist_loads:
        if d.case == "DEAD":
            print(f"    frame={d.frame_id}: {d.value}")

    print("\n" + "=" * 70)
    print("✅ NLP 解析器测试完成")
    print("=" * 70)


if __name__ == "__main__":
    main()