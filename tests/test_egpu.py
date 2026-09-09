import tempfile
import unittest
from pathlib import Path

from maxctl.const import EGPU_IGPU_MAX_MHZ, GPU_SCLK_MAX, GPU_SCLK_MIN
from maxctl.hardware import detect_egpu
from maxctl.profiles import PROFILES, builtin
from maxctl.state import AppConfig


def _pci(root: Path, addr: str, cls: str, vendor: str, device: str) -> Path:
    dev = root / addr
    dev.mkdir()
    (dev / "class").write_text(cls + "\n")
    (dev / "vendor").write_text(vendor + "\n")
    (dev / "device").write_text(device + "\n")
    return dev


class EgpuProfileTests(unittest.TestCase):
    def test_profile_caps_igpu_and_keeps_cpu_budget(self):
        egpu = builtin("egpu")
        sport = builtin("balanced")
        self.assertEqual(egpu.gpu_mode, "range")
        self.assertEqual(egpu.gpu_min, GPU_SCLK_MIN)
        self.assertEqual(egpu.gpu_max, EGPU_IGPU_MAX_MHZ)
        self.assertLess(egpu.gpu_max, GPU_SCLK_MAX)
        self.assertEqual(egpu.tdp_w, sport.tdp_w)
        self.assertEqual(egpu.epp, "balance_performance")
        self.assertTrue(egpu.cpu_boost)
        self.assertEqual(egpu.limits().stapm_w, sport.limits().stapm_w)
        self.assertIn("egpu", PROFILES)

    def test_clamp_keeps_range(self):
        egpu = builtin("egpu")
        self.assertEqual(egpu.clamp().gpu_max, EGPU_IGPU_MAX_MHZ)


class AutoProfileTests(unittest.TestCase):
    def test_egpu_overrides_power_source(self):
        cfg = AppConfig()
        self.assertEqual(cfg.auto_profile(on_ac=True, egpu=False), "balanced")
        self.assertEqual(cfg.auto_profile(on_ac=False, egpu=False), "silent")
        self.assertEqual(cfg.auto_profile(on_ac=True, egpu=True), "egpu")
        self.assertEqual(cfg.auto_profile(on_ac=False, egpu=True), "egpu")

    def test_egpu_switch_off(self):
        cfg = AppConfig(egpu_switch=False)
        self.assertEqual(cfg.auto_profile(on_ac=True, egpu=True), "balanced")
        cfg = AppConfig(egpu_profile="turbo")
        self.assertEqual(cfg.auto_profile(on_ac=True, egpu=True), "turbo")

    def test_old_config_files_still_load(self):
        cfg = AppConfig.from_dict({"ac_profile": "turbo", "battery_profile": "silent"})
        self.assertTrue(cfg.egpu_switch)
        self.assertEqual(cfg.egpu_profile, "egpu")


class DetectEgpuTests(unittest.TestCase):
    def test_ignores_igpu_and_non_display_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            igpu = _pci(root, "0000:c6:00.0", "0x038000", "0x1002", "0x150e")
            _pci(root, "0000:c3:00.0", "0x028000", "0x8086", "0x2725")
            self.assertIsNone(detect_egpu(root, igpu=igpu))
            found = _pci(root, "0000:c4:00.0", "0x030000", "0x10de", "0x2c05")
            egpu = detect_egpu(root, igpu=igpu)
            self.assertIsNotNone(egpu)
            self.assertEqual(egpu.pci, found.name)
            self.assertEqual(egpu.vendor, 0x10DE)
            self.assertEqual(egpu.name, "NVIDIA 10de:2c05")

    def test_amd_card_in_dock_is_still_external(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            igpu = _pci(root, "0000:c6:00.0", "0x038000", "0x1002", "0x150e")
            _pci(root, "0000:c4:00.0", "0x030000", "0x1002", "0x747e")
            egpu = detect_egpu(root, igpu=igpu)
            self.assertIsNotNone(egpu)
            self.assertEqual(egpu.device, 0x747E)

    def test_missing_root(self):
        self.assertIsNone(detect_egpu(Path("/nonexistent/pci"), igpu=None))


if __name__ == "__main__":
    unittest.main()
