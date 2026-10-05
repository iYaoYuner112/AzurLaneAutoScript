"""Operation Siren 固定巡逻（强制移动）的可靠性辅助。

本模块只放与「截图 / 点击 / 走路」无关的纯逻辑，不 import 任何会拖入
cv2 / numpy / device 的东西，保持纯 Python 可在无依赖下测试。

固定巡逻的核心行为（L0/L1 换队读雷达清问号、L2 主队优先挪固定落点 + 命中即停）
与 AzurPilot 保持一致，按事件 handler 顺序处理，不做全局目标/舰队评分调度。
这里只保留一个可靠性辅助：
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
