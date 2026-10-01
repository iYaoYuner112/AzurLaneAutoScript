"""Operation Siren 固定巡逻（强制移动）的目标驱动决策模块。

把「决定移动哪支舰队、去哪个目标」的纯逻辑从截图 / 点击 / 走路这类 IO 里拆出来，
只依赖目标与舰队这两个数据结构，方便单测。

核心原则：固定巡逻不是「4 队轮切」，而是
    扫描地图 → 找到目标 → 按优先级选目标 → 挑能到达且成本最低的舰队 → 驱动它过去。
特殊塞壬装置（探测装置 / 信息收集装置）优先级高于普通作战计划，处理完成后重新扫描。

本模块不 import 任何会拖入 cv2 / numpy / device 的东西，保持纯 Python，可在无依赖下测试。
"""

# 目标类型。特殊塞壬装置优先级最高，普通战斗最低——即「特殊事件优先于计划作战」。
SIREN_PROBE = 'SIREN_PROBE'                          # 塞壬探测装置（扫描装置 ScanningDevice）
SIREN_INFORMATION_DEVICE = 'SIREN_INFORMATION_DEVICE'  # 塞壬信息收集装置（灯塔 LoggingTower）
AKASHI_SHOP = 'AKASHI_SHOP'                          # 明石商店
SPECIAL_RESOURCE = 'SPECIAL_RESOURCE'                # 探索奖励 / 探索容器等特殊资源
OTHER_EVENT = 'OTHER_EVENT'                          # 其它地图事件（问号等）
NORMAL_BATTLE = 'NORMAL_BATTLE'                      # 普通战斗

TARGET_PRIORITY = {
    SIREN_PROBE: 100,
    SIREN_INFORMATION_DEVICE: 90,
    AKASHI_SHOP: 80,
    SPECIAL_RESOURCE: 70,
    OTHER_EVENT: 60,
    NORMAL_BATTLE: 10,
}


class FixedPatrolTarget:
    """固定巡逻的一个候选目标。

    Attributes:
        kind (str): 目标类型，见 TARGET_PRIORITY 的键。
        location (tuple[int, int] | None): 全局地图坐标；未知时为 None。
        handled (bool): 是否已处理。
        needs_battle (bool): 处理该目标是否需要进战斗。
    """

    def __init__(self, kind, location=None, handled=False, needs_battle=False):
        self.kind = kind
        self.location = location
        self.handled = handled
        self.needs_battle = needs_battle

    @property
    def priority(self):
        return TARGET_PRIORITY.get(self.kind, TARGET_PRIORITY[OTHER_EVENT])

    def __repr__(self):
        return 'Target(%s @ %s handled=%s)' % (self.kind, self.location, self.handled)


class FixedPatrolFleet:
    """参与固定巡逻的一支舰队。

    Attributes:
        index (int): 舰队编号 1~4。
        location (tuple[int, int] | None): 舰队当前全局坐标；未知时为 None。
        available (bool): 是否可用（例如未阵亡、未低士气）。
        busy (bool): 是否正在执行其它动作。
    """

    def __init__(self, index, location=None, available=True, busy=False):
        self.index = index
        self.location = location
        self.available = available
        self.busy = busy

    def distance_to(self, target):
        """曼哈顿距离；任一方坐标未知返回 None。"""
        if self.location is None or target.location is None:
            return None
        return abs(self.location[0] - target.location[0]) + abs(self.location[1] - target.location[1])

    def __repr__(self):
        return 'Fleet%s(%s avail=%s busy=%s)' % (self.index, self.location, self.available, self.busy)


def choose_best_target(targets):
    """从未处理的目标里挑出优先级最高的那个。

    Args:
        targets (iterable[FixedPatrolTarget]):

    Returns:
        FixedPatrolTarget | None: 没有未处理目标时返回 None。
    """
    candidates = [t for t in targets if not t.handled]
    if not candidates:
        return None
    # 优先级高的在前；同优先级优先挑「坐标已知」的，便于后续选舰队与移动。
    return max(candidates, key=lambda t: (t.priority, t.location is not None))


def choose_fleet_for_target(target, fleets):
    """为目标挑一支最合适的舰队。

    规则：只用可用且不忙的舰队；坐标都已知时挑曼哈顿距离最近的；
    有舰队坐标未知时，优先挑已知坐标中最近的一支，否则退回编号最小（主队优先）。

    Args:
        target (FixedPatrolTarget):
        fleets (iterable[FixedPatrolFleet]):

    Returns:
        FixedPatrolFleet | None: 没有可用舰队时返回 None。
    """
    candidates = [f for f in fleets if f.available and not f.busy]
    if not candidates:
        return None
    known = [f for f in candidates if f.location is not None and target.location is not None]
    if known:
        return min(known, key=lambda f: (f.distance_to(target), f.index))
    # 目标坐标未知或所有舰队坐标未知：退而求其次，主队（编号最小）优先。
    return min(candidates, key=lambda f: f.index)


def build_targets(grids):
    """把地图格子转换成固定巡逻目标列表。

    注意顺序：探测/收集装置与明石在检测里都会带 is_enemy，所以先判具体类型、
    最后才落到普通战斗，否则会把装置误判成普通敌人。

    Args:
        grids (iterable): 带 is_scanning_device / is_logging_tower / is_akashi /
            is_exploration_reward / is_exploration_container / is_enemy / location
            属性的对象（通常是 OSGridInfo）。

    Returns:
        list[FixedPatrolTarget]:
    """
    targets = []
    for grid in grids:
        if getattr(grid, 'is_scanning_device', False):
            kind = SIREN_PROBE
        elif getattr(grid, 'is_logging_tower', False):
            kind = SIREN_INFORMATION_DEVICE
        elif getattr(grid, 'is_akashi', False):
            kind = AKASHI_SHOP
        elif getattr(grid, 'is_exploration_reward', False) or getattr(grid, 'is_exploration_container', False):
            kind = SPECIAL_RESOURCE
        elif getattr(grid, 'is_enemy', False):
            kind = NORMAL_BATTLE
        else:
            continue
        targets.append(FixedPatrolTarget(kind, getattr(grid, 'location', None)))
    return targets


class AntiLoopGuard:
    """固定巡逻的防死循环守卫：同一状态连续重复超过阈值就判定无进展。"""

    def __init__(self, max_repeats=3):
        self.max_repeats = max_repeats
        self.last_state = None
        self.repeat_count = 0

    def check(self, target_kind, target_location, fleet_index, action):
        """记录一次决策并返回是否已陷入无进展循环。

        Returns:
            bool: True 表示同一状态重复次数已超过阈值，应当中断并告警。
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
