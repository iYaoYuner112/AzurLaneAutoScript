"""明石安全定位及侵蚀一未完成交互的恢复，不保存屏幕点击坐标。

对齐 AzurPilot `e8a948f01`（dev 分支）的同名设计：已确认的明石立即持久化，
成功处理前跨重启保留；入口点击夹进地图安全区；恢复预算跨换队与临时挪队。
与 AP 的差异：明石遭遇统计与资源缓存失效是 AP 侧基建，我们不接入。
"""

from module.base.timer import Timer
from module.config.config import Function
from module.config.utils import get_os_next_reset
from module.exception import GameStuckError, MapDetectionError
from module.logger import logger
from module.map.map_base import location2node
from module.os.tasks.task_context import opsi_task_context

PENDING_AKASHI_PATH = 'OpsiHazard1Leveling.OpsiHazard1Leveling.PendingAkashiRecovery'


class AkashiRecovery:
    """由 OSMap 混入，统一保留已确认明石的身份、定位和恢复预算。"""

    def _akashi_global_location(self, grid):
        """将本帧的局部格子换算为全局格子，避免挪镜头后继续使用旧坐标。"""
        if not hasattr(grid, 'location'):
            return None
        return tuple(int(g + c - v) for g, c, v in
                     zip(grid.location, self.camera, self.view.center_loca))

    def _akashi_local_grid(self, target):
        """只换算当前视野，镜头移动由带次数限制的准备步骤负责。"""
        local = tuple(int(g - c + v) for g, c, v in
                      zip(target, self.camera, self.view.center_loca))
        return self.view[local] if local in self.view else None

    def _get_pending_akashi(self):
        """按服务器、重置周期和当前海域校验隐藏的续查记录。"""
        state = self.config.cross_get(PENDING_AKASHI_PATH)
        if state is None:
            return None
        if (not isinstance(state, dict)
                or state.get('server') != self.config.SERVER
                or state.get('reset') != get_os_next_reset().isoformat()
                or state.get('zone') != self.zone.zone_id
                or self.zone.hazard_level != 1
                or self.zone.is_port
                or self.is_in_special_zone()
                or type(state.get('fleet')) is not int
                or state['fleet'] not in (1, 2, 3, 4)):
            logger.warning('[大世界-明石] 续查记录与当前服务器、重置周期或海域不符，作废旧记录')
            self.config.cross_set(PENDING_AKASHI_PATH, None)
            return None
        return dict(state)

    def _remember_pending_akashi(self, grid=None):
        """侵蚀一确认明石后立即持久化；雷达的普通问号不能创建记录。"""
        if self.config.task.command != 'OpsiHazard1Leveling':
            return
        state = self._get_pending_akashi()
        if state is not None:
            if state.get('target') is None and grid is not None:
                target = self._akashi_global_location(grid)
                if target is not None:
                    state['target'] = list(target)
                    self.config.cross_set(PENDING_AKASHI_PATH, state)
            return
        if self.zone.hazard_level != 1 or self.zone.is_port or self.is_in_special_zone():
            return
        target = self._akashi_global_location(grid)
        self.config.cross_set(PENDING_AKASHI_PATH, dict(
            server=self.config.SERVER,
            reset=get_os_next_reset().isoformat(),
            zone=self.zone.zone_id,
            fleet=self.config.OpsiFleet_Fleet,
            target=list(target) if target is not None else None,
        ))
        self._akashi_recovery_attempted = False
        logger.info('[大世界-明石] 已保存未完成交互，成功处理前不开始下一轮搜索')

    def _finish_pending_akashi(self):
        """仅在购买流程结束且正向确认返回地图后清除记录。"""
        if self.config.task.command == 'OpsiHazard1Leveling' and self._get_pending_akashi() is not None:
            self.config.cross_set(PENDING_AKASHI_PATH, None)
            logger.info('[大世界-明石] 商店处理完成，清除续查记录')

    def _prepare_akashi_click(self, target, *, camera_budget=None):
        """刷新格子，必要时至多挪两次镜头，再生成避开固定 UI 的按钮。"""
        if camera_budget is None:
            camera_budget = [2]
        try:
            self.update()
            for attempt in range(3):
                grid = self._akashi_local_grid(target)
                if grid is not None:
                    button = self._akashi_safe_click_button(grid)
                    if button is not None:
                        return button
                if attempt == 2 or camera_budget[0] <= 0:
                    break
                vector = tuple(max(-limit, min(limit, g - c))
                               for g, c, limit in zip(target, self.camera, (4, 3)))
                if vector == (0, 0):
                    break
                logger.info(f'[大世界-明石] 点击区域被按钮遮挡，调整镜头 {attempt + 1}/2')
                camera_budget[0] -= 1
                self.map_swipe(vector)
        except MapDetectionError:
            logger.warning('[大世界-明石] 无法确认当前地图格子，停止使用旧点击位置')
        return None

    def _try_akashi_grid(self, grid, drop=None):
        """尝试一次已确认明石的安全行军和购买，不在这里递归恢复。

        防御层（保留我们 10-12 的修法）：当前舰队在视野里且与明石相邻时才直接
        开店；舰队被模型挡住（视野里数不出来）时按雷达换算的路程走，窗口照路程
        缩放，不冒认错位置直接开店的风险。
        """
        target = self._akashi_global_location(grid)
        self._remember_pending_akashi(grid)
        fleets = self.view.select(is_current_fleet=True)
        fleet_visible = fleets.count == 1
        fleet = fleets[0] if fleet_visible else self.convert_radar_to_local((0, 0))
        near = fleet_visible and fleet.distance_to(grid) <= 1
        if not near:
            button = self._prepare_akashi_click(target)
            if button is None:
                return False
            self.device.click(button)
            with self.config.temporary(STORY_ALLOW_SKIP=False):
                distance = grid.distance_to(fleet)
                result = self.wait_until_walk_stable(
                    confirm_timer=Timer(1.5 + 0.6 * distance, count=4),
                    drop=drop, walk_out_of_step=False,
                )
            if 'akashi' in result:
                return True
            self.update()
            grid = self._akashi_local_grid(target)
            if grid is None or self.convert_radar_to_local((0, 0)).distance_to(grid) > 1:
                return False
        return self.handle_akashi_supply_buy(grid)

    def _visit_akashi(self, grid, drop=None):
        """将点不开商店与行军失败统一送入恢复，并只标记真实成功。"""
        node = location2node(self._akashi_global_location(grid))
        if self._try_akashi_grid(grid, drop=drop):
            self._solved_map_event.add('is_akashi')
            return True
        if (getattr(self, '_akashi_recovery_active', False)
                or getattr(self, '_in_forced_recovery', False)):
            return False
        if self.config.task.command == 'OpsiHazard1Leveling' and self._get_pending_akashi() is not None:
            return self._recover_confirmed_akashi(drop=drop)
        return self._recover_unreachable_akashi(drop, node)

    def _recover_confirmed_akashi(self, drop=None):
        """已知明石的紧急恢复：换队后临时强制移动，不修改用户的开关。"""
        if getattr(self, '_akashi_recovery_active', False):
            return False
        if not self.is_in_map():
            raise GameStuckError('明石交互未返回海域地图，保留续查记录，禁止在其他页面换队')
        if getattr(self, '_akashi_recovery_attempted', False):
            raise GameStuckError('已确认的明石仍未处理完成，保留续查记录并重启恢复')
        self._akashi_recovery_attempted = True
        self._akashi_recovery_active = True
        try:
            if self._goto_akashi_with_other_fleets(drop=drop):
                return True
            logger.warning('[大世界-明石] 换队仍无法交互，临时执行本次强制移动')
            self.execute_fixed_patrol_scan(akashi_recovery=True)
            if 'is_akashi' in self._solved_map_event:
                return True
            raise GameStuckError('明石安全点击、换队及强制移动均失败，保留续查记录')
        finally:
            self._akashi_recovery_active = False

    def _resume_pending_akashi(self):
        """在任何新自律或换图前，以侵蚀一身份续查同一海域的未完成明石。"""
        state = self._get_pending_akashi()
        if state is None:
            return False
        logger.hr('[大世界-明石] 优先续查上次中断的商店')
        with opsi_task_context(self.config, 'OpsiHazard1Leveling',
                               set_task_identity=True, disable_task_switch=True):
            # 子任务上下文会恢复运行时覆盖，不把续查专用舰队和开关写入配置。
            self.config.override(OpsiFleet_Fleet=state['fleet'], OpsiGeneral_DoRandomMapEvent=True)
            self._solved_map_event = set()
            # 旧坐标只作诊断线索；新实例必须重新识别，不能按旧屏幕位置盲点。
            self.map_rescan(rescan_mode='full')
            if 'is_akashi' not in self._solved_map_event:
                self._recover_confirmed_akashi()
        return True
