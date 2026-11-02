import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from maxctl import state
from maxctl.state import AppConfig, load_config, load_settings


class AppConfigFromDictTests(unittest.TestCase):
    def test_wrong_types_keep_the_defaults(self):
        cfg = AppConfig.from_dict(
            {
                "auto_switch": "false",
                "reapply_sec": None,
                "custom": [],
                "battery_profile": 3,
                "unlock": "yes",
            }
        )
        self.assertEqual(cfg, AppConfig())

    def test_json_numbers_are_accepted(self):
        cfg = AppConfig.from_dict({"reapply_sec": 10, "unlock": 1, "egpu_switch": 0})
        self.assertEqual(cfg.reapply_sec, 10.0)
        self.assertIs(cfg.unlock, True)
        self.assertIs(cfg.egpu_switch, False)

    def test_reapply_interval_is_bounded(self):
        self.assertEqual(AppConfig.from_dict({"reapply_sec": 0.01}).reapply_sec, 1.0)
        self.assertEqual(AppConfig.from_dict({"reapply_sec": 1e300}).reapply_sec, 3600.0)
        self.assertEqual(AppConfig.from_dict({"reapply_sec": float("nan")}).reapply_sec, 4.0)
        self.assertEqual(AppConfig.from_dict({"reapply_sec": 10**400}).reapply_sec, 3600.0)
        self.assertEqual(AppConfig.from_dict({"reapply_sec": -(10**400)}).reapply_sec, 1.0)

    def test_valid_config_round_trips(self):
        cfg = AppConfig(ac_profile="turbo", unlock=True, reapply_sec=8.0, custom={"tdp_w": 20}, egpu_profile="")
        self.assertEqual(AppConfig.from_dict(cfg.to_dict()), cfg)

    def test_non_object_is_the_default_config(self):
        self.assertEqual(AppConfig.from_dict(["unlock"]), AppConfig())


class LoadFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.config = root / "config.json"
        self.user_config = root / "user.json"
        self.state = root / "state.json"
        for name, path in (
            ("CONFIG_PATH", self.config),
            ("USER_CONFIG_PATH", self.user_config),
            ("STATE_PATH", self.state),
        ):
            patcher = mock.patch.object(state, name, str(path))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_json_that_is_not_an_object_is_ignored(self):
        self.config.write_text("[1, 2]\n")
        self.state.write_text('"settings"\n')
        self.assertEqual(load_config(), AppConfig())
        self.assertEqual(load_settings(AppConfig(), on_ac=True).profile, "balanced")

    def test_undecodable_file_is_ignored(self):
        self.config.write_bytes(b'\xff\xfe{"unlock": true}')
        self.assertEqual(load_config(), AppConfig())

    def test_corrupt_saved_settings_fall_back_to_the_profile(self):
        self.state.write_text(json.dumps({"settings": {"tdp_w": "lots"}}))
        settings = load_settings(AppConfig(battery_profile="silent"), on_ac=False)
        self.assertEqual(settings.profile, "silent")

    def test_saved_settings_still_win(self):
        self.state.write_text(json.dumps({"settings": {"profile": "custom", "tdp_w": 20}}))
        self.assertEqual(load_settings(AppConfig(), on_ac=True).tdp_w, 20)


if __name__ == "__main__":
    unittest.main()
