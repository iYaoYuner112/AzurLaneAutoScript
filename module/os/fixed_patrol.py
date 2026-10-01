"""Operation Siren 固定巡逻（强制移动）的可靠性辅助。

本模块只放与「截图 / 点击 / 走路」无关的纯逻辑，不 import 任何会拖入
cv2 / numpy / device 的东西，保持纯 Python 可在无依赖下测试。

固定巡逻的核心行为（L0/L1 换队读雷达清问号、L2 主队优先挪固定落点 + 命中即停）
与 AzurPilot 保持一致，按事件 handler 顺序处理，不做全局目标/舰队评分调度。
这里只保留两个可靠性辅助：
- AntiLoopGuard：防死循环保险丝（AzurPilot 没有，是本 fork 的额外安全层，
  只在异常情况下兜底，不参与正常的舰队顺序 / 目标选择）；
- 塞壬装置交互子状态：描述「点击装置 → 弹窗 → 确认 → 完成」链，任务抢占后
  恢复时重新观察 UI 而不是回放过期点击（状态可恢复、点击序列不可恢复）。
"""

# 塞壬装置交互子状态。
DEVICE_NONE = 'DEVICE_NONE'
DEVICE_TARGETED = 'DEVICE_TARGETED'
DEVICE_DIALOG_OPEN = 'DEVICE_DIALOG_OPEN'
DEVICE_COMPLETED = 'DEVICE_COMPLETED'
DEVICE_RECOVERY_REQUIRED = 'DEVICE_RECOVERY_REQUIRED'

# 处于这些状态时被任务抢占，说明装置交互进行到一半，恢复后需重新观察 UI。
DEVICE_INTERRUPTIBLE_STATES = frozenset({DEVICE_TARGETED, DEVICE_DIALOG_OPEN})


class AntiLoopGuard:
    """固定巡逻的防死循环守卫：同一状态连续重复超过阈值就判定无进展。

    这是保险丝，不是主控制器：正常流程按 AzurPilot 的「主队优先、命中即停」走，
    本类只在同一动作反复出现时兜底报错，避免「移动 A → 重扫 → 又选 A」无限循环。
    """

    def __init__(self, max_repeats=3):
        self.max_repeats = max_repeats
        self.last_state = None
        self.repeat_count = 0

    def check(self, target_kind, target_location, fleet_index, action):
        """记录一次决策，返回是否已陷入无进展循环。

        Args:
            target_kind: 目标类型（可 None）。
            target_location: 目标坐标（可 None）。
            fleet_index: 舰队编号。
            action: 动作标识（如 'move'）。

        Returns:
            bool: True 表示同一状态重复次数已超过阈值。
        """
        state = (target_kind, target_location, fleet_index, action)
        if state == self.last_state:
            self.repeat_count += 1
        else:
            self.repeat_count = 1
            self.last_state = state
        return self.repeat_count > self.max_repeats

    def reset(self):
        self.last_state = None
        self.repeat_count = 0
