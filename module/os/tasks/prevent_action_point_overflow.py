"""大世界行动力溢出保护。

行动力每 10 分钟自然回复 1 点、上限 200。挂机时如果行动力已经接近上限，
回复的部分就白白浪费掉。本任务按「离回满还有多久」给自己排下一次运行：
行力每 10 分钟自然回复 1 点；到点后读一次真实行动力，没到上限就重新排期
退出，到了就代跑一轮目标任务，每跑完一轮重新读，低于下限就停手。

与 AzurPilot `prevent_action_point_overflow.py` 对齐：
- 自排期：`(上限 - 当前) * 10 分钟`，到上限则 1 分钟后
- 代跑期间 `OS_ACTION_POINT_BOX_USE=False`、`OpsiGeneral_BuyActionPointLimit=0`
- 用 `prevent_overflow_context` 共享本轮延迟请求，子任务申请的延迟优先采用
"""

from dataclasses import replace

from module.config.config import TaskEnd
from module.logger import logger
from module.os.tasks.scheduling import OpsiScheduling
from module.os.tasks.task_context import OpsiStatus, prevent_overflow_context
from module.os_handler.action_point import ActionPointLimit

# 大世界行动力每 10 分钟自然回复 1 点。
ACTION_POINT_RECOVER_SECONDS = 600
# 大世界当前行动力自然上限。
NATURAL_ACTION_POINT_LIMIT = 200

TASK_NAME = 'OpsiPreventActionPointOverflow'
CONFIG_PREFIX = 'OpsiPreventActionPointOverflow.OpsiPreventActionPointOverflow'
TASK_NAME_SCHEDULING = 'OpsiScheduling'
TASK_NAME_HAZARD1_LEVELING = 'OpsiHazard1Leveling'
TASK_NAME_MEOWFFICER_FARMING = 'OpsiMeowfficerFarming'
TARGET_TASKS = (TASK_NAME_SCHEDULING, TASK_NAME_HAZARD1_LEVELING, TASK_NAME_MEOWFFICER_FARMING)


class OpsiPreventActionPointOverflow(OpsiScheduling):
    """防止当前行动力自然回复溢出的任务。"""

    # ------------------------------------------------------------ 配置读写

    def is_prevent_overflow_enabled(self):
        """本任务是否启用。

        Returns:
            bool: 启用返回 True。
        """
        return bool(self.config.cross_get(keys=f'{TASK_NAME}.Scheduler.Enable', default=False))

    def _set_prevent_overflow_enabled(self, enabled):
        """启用或关闭本任务。

        Args:
            enabled (bool): 是否启用。
        """
        self.config.cross_set(keys=f'{TASK_NAME}.Scheduler.Enable', value=bool(enabled))

    def _get_thresholds(self):
        """读取触发上限与停手下限（上限钳制在 1~200，下限钳制在 0~上限）。

        Returns:
            tuple[int, int]: (上限, 下限)。
        """
        upper = self.config.cross_get(
            keys=f'{CONFIG_PREFIX}.ActionPointUpperbound',
            default=NATURAL_ACTION_POINT_LIMIT)
        lower = self.config.cross_get(
            keys=f'{CONFIG_PREFIX}.ActionPointLowerbound',
            default=10)
        try:
            upper = int(upper)
        except (TypeError, ValueError):
            upper = NATURAL_ACTION_POINT_LIMIT
        try:
            lower = int(lower)
        except (TypeError, ValueError):
            lower = 10
        upper = max(1, min(NATURAL_ACTION_POINT_LIMIT, upper))
        lower = max(0, min(upper, lower))
        return upper, lower

    def _get_target_task(self):
        """读取到点后要代跑一轮的目标任务，非法值回落为 OpsiScheduling。

        Returns:
            str: 任务标识名。
        """
        target = self.config.cross_get(
            keys=f'{CONFIG_PREFIX}.Task',
            default=TASK_NAME_SCHEDULING)
        if target not in TARGET_TASKS:
            logger.warning(f'[大世界-防止行动力溢出] 目标任务无效: {target}，改为 {TASK_NAME_SCHEDULING}')
            target = TASK_NAME_SCHEDULING
        return target

    # ------------------------------------------------------------ 行动力与排期

    def _get_current_action_point_for_overflow(self):
        """读一次真实行动力（复用调度器的面板管理，读完关闭面板）。

        Returns:
            int: 当前可用行动力。
        """
        _, current = self._get_scheduling_action_point(keep_open=False)
        return max(0, int(current))

    def update_prevent_action_point_overflow_schedule(self, current_ap=None, enable=True):
        """按当前真实行动力更新本任务的下次运行时间。

        Args:
            current_ap (int | None): 当前行动力；None 时现场读一次。
            enable (bool): 是否顺带启用本任务。
        """
        if current_ap is None:
            current_ap = self._get_current_action_point_for_overflow()
        try:
            current_ap = int(current_ap)
        except (TypeError, ValueError):
            current_ap = 0
        current_ap = max(0, min(NATURAL_ACTION_POINT_LIMIT, current_ap))

        upper, _ = self._get_thresholds()
        if current_ap >= upper:
            delay_minutes = 1
        else:
            delay_minutes = max(1, (upper - current_ap) * ACTION_POINT_RECOVER_SECONDS / 60)

        logger.info(
            f'[大世界-防止行动力溢出] 当前={current_ap} 上限={upper}，'
            f'{delay_minutes:.0f} 分钟后运行')

        with self.config.multi_set():
            self._set_prevent_overflow_enabled(enable)
            self.config.task_delay(minute=delay_minutes, task=TASK_NAME)

    # ------------------------------------------------------------ 执行

    def _run_with_prevent_action_point_overflow_context(self, task_name, func, *args, **kwargs):
        """在防溢出上下文里代跑一轮目标任务。

        Args:
            task_name (str): 目标任务标识名。
            func (Callable): 目标任务"跑一轮"的入口。
            *args: 透传的位置参数。
            **kwargs: 透传的关键字参数。

        Returns:
            Any: func 的返回值。
        """
        with prevent_overflow_context(self.config):
            return self._run_with_opsi_task_context(task_name, func, *args, **kwargs)

    def _run_prevent_action_point_overflow_target_once(self, task_name, lowerbound):
        """按配置执行一轮目标任务。

        期间不买行动力、不使用行动力药箱：这一轮的目的是把行动力花掉，
        反过来再买回来或者吃药箱就失去意义了。

        Args:
            task_name (str): 目标任务标识名。
            lowerbound (int): 本轮的行动力下限。

        Returns:
            OpsiTaskResult | None: 目标任务本轮的返回值（智能调度/短猫会返回
            结果对象，侵蚀 1 返回 None），用于判断「本轮有没有真的花掉行动力」。
        """
        with self.config.temporary(
                OS_ACTION_POINT_BOX_USE=False,
                OpsiGeneral_BuyActionPointLimit=0):
            if task_name == TASK_NAME_SCHEDULING:
                return self._run_with_prevent_action_point_overflow_context(
                    TASK_NAME_SCHEDULING, self.run_smart_scheduling_once)
            elif task_name == TASK_NAME_HAZARD1_LEVELING:
                return self._run_with_prevent_action_point_overflow_context(
                    TASK_NAME_HAZARD1_LEVELING, self.run_hazard1_leveling_once,
                    ap_preserve=lowerbound)
            elif task_name == TASK_NAME_MEOWFFICER_FARMING:
                return self._run_with_prevent_action_point_overflow_context(
                    TASK_NAME_MEOWFFICER_FARMING, self.run_meowfficer_farming_once,
                    ap_preserve=lowerbound)
            else:
                raise ValueError(f'未知防止行动力溢出目标任务: {task_name}')

    def os_prevent_action_point_overflow(self):
        """防止当前行动力自然回复溢出的任务入口。"""
        logger.hr('大世界-防止行动力溢出', level=1)
        upperbound, lowerbound = self._get_thresholds()
        task_name = self._get_target_task()
        logger.attr('行动力上限', upperbound)
        logger.attr('行动力下限', lowerbound)
        logger.attr('溢出任务', task_name)

        started = False
        while True:
            current_ap = self._get_current_action_point_for_overflow()
            if not started:
                if current_ap < upperbound:
                    logger.info(f'[大世界-防止行动力溢出] 当前行动力未达上限 '
                                f'({current_ap} < {upperbound})，不处理')
                    self.update_prevent_action_point_overflow_schedule(
                        current_ap=current_ap, enable=True)
                    self.config.task_stop()
            elif current_ap < lowerbound:
                logger.info(f'[大世界-防止行动力溢出] 当前行动力已低于下限 '
                            f'({current_ap} < {lowerbound})，停止')
                self.update_prevent_action_point_overflow_schedule(
                    current_ap=current_ap, enable=True)
                self.config.task_stop()

            started = True
            with prevent_overflow_context(self.config) as overflow:
                try:
                    result = self._run_prevent_action_point_overflow_target_once(
                        task_name, lowerbound)
                    # 目标任务这一轮没有任何可执行内容（补币任务仓库空了、
                    # 海域清完了等）：行动力花不出去，继续读一次也只会再转
                    # 一圈，直接推迟到服务器刷新（对齐 AzurPilot 在
                    # `_handle_coin_task_no_content` 里对防溢出的处理）。
                    if getattr(result, 'status', None) is OpsiStatus.NO_CONTENT:
                        logger.info('[大世界-防止行动力溢出] 目标任务本轮没有可执行内容，'
                                    '推迟到服务器刷新')
                        self.config.task_delay(server_update=True)
                        self.config.task_stop()
                except ActionPointLimit as e:
                    current = getattr(e, 'current', None)
                    if current is None:
                        current = current_ap
                    logger.info('[大世界-防止行动力溢出] 行动力不足以进入目标海域，重排')
                    self.update_prevent_action_point_overflow_schedule(
                        current_ap=current, enable=True)
                    self.config.task_stop()
                except TaskEnd:
                    delay_request = overflow.request
                    if delay_request is not None:
                        logger.info('[大世界-防止行动力溢出] 采用子任务申请的延迟')
                        replace(delay_request, task=TASK_NAME).apply(self.config)
                        raise
                    try:
                        current_ap = self._get_current_action_point_for_overflow()
                    except Exception:
                        logger.debug('[大世界-防止行动力溢出] 任务结束后读数失败，沿用上一次')
                    self.update_prevent_action_point_overflow_schedule(
                        current_ap=current_ap, enable=True)
                    raise
