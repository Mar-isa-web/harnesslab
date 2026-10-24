# -*- coding: utf-8 -*-
"""权限门：对齐真实 Harness 的权限模式设计。

- readonly: 只放行无副作用的工具，写操作直接拒绝（非交互基准用）
- ask:      写操作逐个人工确认（交互使用）
- yolo:     全部放行（受控实验环境用）
拒绝不是静默的——被拒的工具调用同样落库（status=denied），模型会收到
"permission denied" 的结果并有机会改走别的路，这正是要观测的行为。
"""


class PermissionPolicy:
    def __init__(self, mode: str = "ask"):
        assert mode in ("readonly", "ask", "yolo"), f"未知权限模式: {mode}"
        self.mode = mode

    def check(self, tool) -> bool:
        if self.mode == "yolo":
            return True
        if not tool.write:
            return True
        if self.mode == "readonly":
            return False
        ans = input(f"[权限] 工具 {tool.name} 有写副作用，允许执行? [y/N] ")
        return ans.strip().lower() == "y"
