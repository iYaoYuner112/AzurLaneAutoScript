"""塞壬装置剧情识别（对齐 AP master `_identify_siren_device_option`）。

- 5 选项 = 塞壬探测装置：按 `OpsiSirenBug.SirenResearch_Enable` / `Siren_Mode` 选择；
  未启用研究时选最后一项（离开）。
- 3 选项：显式 STORY_OPTION（0~2）按配置点；自动选择（-2）视为
  塞壬信息收集装置/柱子，点中间项（collected）。
- 其它选项数量：不是装置剧情，返回 None（回退 STORY_OPTION 逻辑）。
"""

from types import SimpleNamespace

from module.handler.info_handler import InfoHandler


class OptionStub:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


class IdentifyStub:
    """只提供 `_identify_siren_device_option` 需要的属性。"""

    def __init__(self, task='OpsiHazard1Leveling', story_option=-2,
                 research_enable=False, siren_mode='resource'):
        self.config = SimpleNamespace(
            task=SimpleNamespace(command=task),
            STORY_OPTION=story_option,
            cross_get=lambda keys, default=None: (
                siren_mode if keys.endswith('Siren_Mode') else (
                    research_enable if keys.endswith('SirenResearch_Enable') else default)),
        )
        self.siren_device_mode = 'unset'

    _identify_siren_device_option = InfoHandler._identify_siren_device_option


def options(n):
    return [OptionStub(f'STORY_OPTION_{i + 1}_OF_{n}') for i in range(n)]


def test_five_options_research_disabled_picks_leave():
    stub = IdentifyStub(research_enable=False)
    select = stub._identify_siren_device_option(options(5))
    assert select.name == 'STORY_OPTION_5_OF_5'
    assert stub.siren_device_mode is None


def test_five_options_resource_mode_picks_fourth():
    stub = IdentifyStub(research_enable=True, siren_mode='resource')
    select = stub._identify_siren_device_option(options(5))
    assert select.name == 'STORY_OPTION_4_OF_5'
    assert stub.siren_device_mode == 'resource'


def test_five_options_enemy_mode_picks_third():
    stub = IdentifyStub(research_enable=True, siren_mode='enemy')
    select = stub._identify_siren_device_option(options(5))
    assert select.name == 'STORY_OPTION_3_OF_5'
    assert stub.siren_device_mode == 'enemy'


def test_three_options_explicit_story_option_wins():
    """深渊/隐秘/要塞/跨月 用 STORY_OPTION=0 点第一项（不是装置剧情）。"""
    stub = IdentifyStub(story_option=0)
    select = stub._identify_siren_device_option(options(3))
    assert select.name == 'STORY_OPTION_1_OF_3'


def test_three_options_auto_choice_is_collected():
    """STORY_OPTION=-2（自动选择）时，3 选项按信息收集装置/柱子处理。"""
    stub = IdentifyStub(story_option=-2)
    select = stub._identify_siren_device_option(options(3))
    assert select.name == 'STORY_OPTION_2_OF_3'
    assert stub.siren_device_mode == 'collected'


def test_three_options_out_of_range_falls_back_to_collected():
    stub = IdentifyStub(story_option=9)
    select = stub._identify_siren_device_option(options(3))
    assert select.name == 'STORY_OPTION_2_OF_3'
    assert stub.siren_device_mode == 'collected'


def test_two_options_is_not_a_device_story():
    stub = IdentifyStub(story_option=0)
    assert stub._identify_siren_device_option(options(2)) is None
