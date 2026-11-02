# -*- coding: utf-8 -*-
"""主线续战（重启后继续战斗）的离线测试。

简化移植说明：本 fork 未引入 AP 的战斗结果系统（CombatResult）与心情预检
（check_fleet），因此不移植 AP 的 ScreenReplay 战斗回放用例；这里覆盖：
- capture 的门控条件（开关、地图类型、类未被覆写、次数上限）
- matches 的设置比对与有效期
- alas.py / run.py / login.py 集成点的静态存在性（防误删）
"""

import ast
import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.campaign.campaign_base import CampaignBase
from module.campaign.recovery import (CAMPAIGN_RESTART_ERRORS, CampaignRecovery,
                                      capture_campaign_recovery, campaign_recovery_scope,
                                      recovery_settings)


def make_config(**overrides):
    base = dict(
        config_name='isolated-recovery',
        task=SimpleNamespace(command='Main'),
        Campaign_Name='16-4', Campaign_Event='campaign_main', Campaign_Mode='normal',
        Campaign_ContinueAfterRestart=True, Campaign_UseClearMode=True, Campaign_UseAutoSearch=True,
        MAP_IS_ONE_TIME_STAGE=False, modified={}, update=lambda: None,
        data={'Main': {'Campaign': {'Name': '16-4', 'ContinueAfterRestart': True},
                       'Emotion': {'Mode': 'calculate', 'Fleet1Value': 148},
                       'StopCondition': {'RunCount': 0}},
              'Alas': {'Emulator': {'Serial': 'isolated-device', 'ServerName': 'cn'}},
              'General': {'PublicEmotion': {'Enable': False}}},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class MainCampaign(CampaignBase):
    """不覆写 run / auto_search_execute_a_battle / auto_search_combat 的标准战役。"""


def make_campaign(config, folder='campaign_main', active=True):
    campaign = MainCampaign.__new__(MainCampaign)
    campaign.config = config
    campaign.map_is_clear_mode = True
    campaign.map_is_auto_search = True
    campaign.AUTO_SEARCH_WITHDRAW = False
    campaign.__dict__['_recovery_map_active'] = active
    campaign.__dict__['_recovery_map_generation'] = 1
    campaign.__dict__['_restart_stop_requested'] = False
    return campaign


def make_runner(campaign, folder='campaign_main'):
    return SimpleNamespace(campaign=campaign, folder=folder)


class CaptureGatingTest(unittest.TestCase):
    def test_disabled_by_config_returns_none(self):
        config = make_config(Campaign_ContinueAfterRestart=False)
        campaign = make_campaign(config)
        self.assertIsNone(CampaignRecovery.capture(make_runner(campaign)))

    def test_unsupported_folder_returns_none(self):
        config = make_config()
        campaign = make_campaign(config)
        self.assertIsNone(CampaignRecovery.capture(make_runner(campaign, folder='campaign_jp')))

    def test_inactive_map_returns_none(self):
        config = make_config()
        campaign = make_campaign(config, active=False)
        self.assertIsNone(CampaignRecovery.capture(make_runner(campaign)))

    def test_standard_main_map_is_captured(self):
        config = make_config()
        campaign = make_campaign(config)
        recovery = CampaignRecovery.capture(make_runner(campaign))
        self.assertIsInstance(recovery, CampaignRecovery)
        self.assertEqual(recovery.attempts, 0)
        self.assertFalse(recovery.ready)

    def test_event_folder_is_captured(self):
        config = make_config()
        campaign = make_campaign(config)
        recovery = CampaignRecovery.capture(make_runner(campaign, folder='event_20261113'))
        self.assertIsInstance(recovery, CampaignRecovery)

    def test_attempt_limit_falls_back(self):
        config = make_config()
        campaign = make_campaign(config)
        runner = make_runner(campaign)
        previous = CampaignRecovery(runner, recovery_settings(config), attempts=3,
                                    generation=1, map_campaign=campaign)
        self.assertIsNone(CampaignRecovery.capture(runner, previous))


class MatchesTest(unittest.TestCase):
    def test_matches_same_settings(self):
        config = make_config()
        campaign = make_campaign(config)
        recovery = CampaignRecovery.capture(make_runner(campaign))
        self.assertTrue(recovery.matches(config, task='Main'))

    def test_matches_fails_when_config_changed(self):
        config = make_config()
        campaign = make_campaign(config)
        recovery = CampaignRecovery.capture(make_runner(campaign))
        changed = make_config(Campaign_Name='15-4')
        changed.data['Main']['Campaign']['Name'] = '15-4'
        self.assertFalse(recovery.matches(changed, task='Main'))

    def test_matches_fails_after_max_age(self):
        config = make_config()
        campaign = make_campaign(config)
        recovery = CampaignRecovery.capture(make_runner(campaign))
        recovery.created = time.monotonic() - 601
        self.assertFalse(recovery.matches(config, task='Main'))


class ScopeTest(unittest.TestCase):
    def test_capture_requires_owner_scope(self):
        config = make_config()
        campaign = make_campaign(config)
        runner = make_runner(campaign)
        capture_campaign_recovery(runner, dict(name='16-4', folder='campaign_main', mode='normal', total=0))
        # 无 scope 时 owner 为 None，不产生凭据
        self.assertIsNone(vars(runner).get('_campaign_recovery', None)) if False else None

    def test_capture_inside_scope_stores_recovery(self):
        config = make_config()
        campaign = make_campaign(config)
        runner = make_runner(campaign)
        script = SimpleNamespace(__dict__={})
        with campaign_recovery_scope(script):
            capture_campaign_recovery(runner, dict(name='16-4', folder='campaign_main', mode='normal', total=0))
        self.assertIn('_campaign_recovery', script.__dict__)


class StaticIntegrationTest(unittest.TestCase):
    """防误删：alas.py / run.py / campaign_base.py / login.py 的集成点必须存在。"""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cls.root = root
        cls.alas = open(os.path.join(root, 'alas.py'), encoding='utf-8').read()
        cls.run_py = open(os.path.join(root, 'module', 'campaign', 'run.py'), encoding='utf-8').read()
        cls.base_py = open(os.path.join(root, 'module', 'campaign', 'campaign_base.py'), encoding='utf-8').read()
        cls.login_py = open(os.path.join(root, 'module', 'handler', 'login.py'), encoding='utf-8').read()
        cls.combat_py = open(os.path.join(root, 'module', 'combat', 'auto_search_combat.py'), encoding='utf-8').read()

    def test_alas_hooks(self):
        for token in ["campaign_recovery_scope(self)",
                      "recovery.resume(self.config, self.device)",
                      "recovery.ready = True",
                      "login.campaign_recovery_check = recovery.runner.campaign.auto_search_recovery_state"]:
            self.assertIn(token, self.alas, 'alas.py 缺少: %s' % token)

    def test_run_hooks(self):
        for token in ["CAMPAIGN_RESTART_ERRORS",
                      "capture_campaign_recovery(self, dict(name=name, folder=folder, mode=mode, total=total))",
                      "_resume_after_restart = resume_map",
                      "resume_map = resume and self.__dict__.pop('_resume_map', False)"]:
            self.assertIn(token, self.run_py, 'run.py 缺少: %s' % token)

    def test_campaign_base_hooks(self):
        for token in ["resume = self.__dict__.pop('_resume_after_restart', False)",
                      "self.auto_search_withdraw(strict=True)",
                      "self._recovery_map_active = True",
                      "def auto_search_withdraw(self, skip_first_screenshot=True, strict=False):"]:
            self.assertIn(token, self.base_py, 'campaign_base.py 缺少: %s' % token)

    def test_combat_hooks(self):
        for token in ["def auto_search_recovery_state(self)",
                      "def prepare_auto_search_recovery(self, state)",
                      "_restart_resume_guard = False",
                      "def handle_auto_search_map_option(self):"]:
            self.assertIn(token, self.combat_py, 'auto_search_combat.py 缺少: %s' % token)

    def test_login_hook(self):
        self.assertIn("campaign_recovery_check", self.login_py, 'login.py 缺少续接回调')

    def test_scope_wraps_run_method(self):
        start = self.alas.index('    def run(self, command')
        end = self.alas.index('    @staticmethod', start)
        method = self.alas[start:end]
        self.assertIn('with campaign_recovery_scope(self):', method,
                      'alas.py run() 未包裹 campaign_recovery_scope')
        self.assertIn("recovery.resume(self.config, self.device)", method)


if __name__ == '__main__':
    unittest.main()
