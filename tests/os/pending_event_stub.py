"""测试替身：只提供续查记录的存取。

真实的读写/校验逻辑由 module/os/map_event_resume.py 覆盖
（tests/os/test_map_event_resume.py），这里只让引用 OSMap 方法的桩对象
不会因为没有续查记录而报 AttributeError。
"""


class PendingEventStub:
    """把续查记录存在实例字典里，避免测试去碰真实配置。"""

    def _pending_event_records(self):
        store = self.__dict__.get('_pending_records')
        if store is None:
            store = {}
            self._pending_records = store
        return store

    def _save_pending_map_event(self, event_type, grid):
        self._pending_event_records()[event_type] = getattr(grid, 'location', None)

    def _read_pending_map_event(self):
        records = self.__dict__.get('_pending_records')
        if not records:
            return None
        event_type = next(iter(records))
        return {'type': event_type, 'node': records[event_type]}

    def _update_pending_map_event(self, **kwargs):
        # 只记下调用参数：续查记录本身的读写由 test_map_event_resume.py 覆盖
        self.__dict__.setdefault('pending_updates', []).append(kwargs)

    def _finish_pending_map_event(self, event_type, node=None):
        self._pending_event_records().pop(event_type, None)

    def _record_siren_followup_progress(self):
        self.__dict__['progress_calls'] = self.__dict__.get('progress_calls', 0) + 1

    def _resume_pending_map_event(self, drop=None):
        return None


class SirenRoundStub:
    """装置轮数的极简替身：只数自律轮数。

    自律开关确认与结算兜底的细节由 tests/os/test_siren_device_rounds.py 覆盖，
    这里让只关心"跑了几轮"的用例不必再实现整套战斗界面。
    """

    def _run_siren_device_rounds(self, mode, rounds, fleet=0, drop=None):
        for _ in range(rounds):
            self.os_auto_search_run(drop=drop)
            self._record_siren_followup_progress()
        return rounds
