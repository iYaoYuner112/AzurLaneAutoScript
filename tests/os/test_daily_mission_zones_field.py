"""`OpsiDaily.MissionZones`（保留海域记录）必须是「程序自维护、界面不出现」。

这份记录由 `os_daily_set_keep_mission_zone()` 追加海域 ID、
由 `os_daily_clear_all_mission_zones()` 消费后清空，用户在界面上删掉一个 ID
就等于那块保留海域整月不再被清理。所以它按 AzurPilot 的定义标成
`type: stored` + `display: hide`，两者缺一不可：
`config_updater.config_update()` 每次加载配置都会跑，`display: hide` 但类型不是
`stored` 的参数会被直接抹回默认值。
"""

from module.config.config_updater import ConfigUpdater
from module.config.utils import filepath_args, read_file


def mission_zones_meta():
    args = read_file(filepath_args())
    return args['OpsiDaily']['OpsiDaily']['MissionZones']


def test_mission_zones_is_an_hidden_stored_field():
    meta = mission_zones_meta()
    assert meta['type'] == 'stored'
    assert meta['display'] == 'hide'


def test_keep_mission_zone_switch_stays_visible():
    """开关是给用户用的，必须仍然出现在界面上。"""
    args = read_file(filepath_args())
    visible = [
        name for name, meta in args['OpsiDaily']['OpsiDaily'].items()
        if meta.get('display') != 'hide'
    ]
    assert 'KeepMissionZone' in visible
    assert 'MissionZones' not in visible


def test_stored_record_survives_config_update():
    """stored 的记录在配置加载后原样保留（这就是必须给它 stored 的原因）。"""
    new = ConfigUpdater().config_update({
        'OpsiDaily': {'OpsiDaily': {'MissionZones': '10233 10421'}},
    })
    assert new['OpsiDaily']['OpsiDaily']['MissionZones'] == '10233 10421'


def test_hidden_non_stored_field_is_reset_by_config_update():
    """反证：只隐藏不给 stored 的字段每次加载都会被抹回默认值。

    `GemsFarming.Emotion.Fleet2Value` 是仓库里现成的 hide + input 字段，默认 119。
    """
    args = read_file(filepath_args())
    meta = args['GemsFarming']['Emotion']['Fleet2Value']
    assert meta['display'] == 'hide' and meta['type'] != 'stored'
    new = ConfigUpdater().config_update({
        'GemsFarming': {'Emotion': {'Fleet2Value': 88}},
    })
    assert new['GemsFarming']['Emotion']['Fleet2Value'] == meta['value']
