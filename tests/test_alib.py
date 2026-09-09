import unittest

from maxctl.alib import encode_alib, format_call, tdp_params
from maxctl.const import ALIB_FAST_LIMIT, ALIB_STAPM_LIMIT
from maxctl.fan import CurvePoint, interpolate
from maxctl.hardware import mhz_from_cpuinfo
from maxctl.profiles import Settings, builtin
from maxctl.ryzenadj import command_line, parse_info


class AlibTests(unittest.TestCase):
    def test_payload_length(self):
        params = tdp_params(28, 90, True)
        payload = encode_alib(params)
        self.assertEqual(int.from_bytes(payload[:2], "little"), 2 + 5 * len(params))
        self.assertEqual(payload[2], ALIB_STAPM_LIMIT)
        stapm = int.from_bytes(payload[3:7], "little")
        self.assertEqual(stapm, 28000)
        self.assertIn(ALIB_FAST_LIMIT, payload[2::5])
        self.assertTrue(format_call(payload).startswith("\\_SB.ALIB 0x0c b"))

    def test_limits_override_packet(self):
        from maxctl.const import ALIB_SKIN_LIMIT, ALIB_SLOW_TIME
        from maxctl.profiles import Limits

        params = dict(tdp_params(28, 90, True, limits=Limits(20, 30, 24, 18, 88, 40, 8)))
        self.assertEqual(params[ALIB_STAPM_LIMIT], 20000)
        self.assertEqual(params[ALIB_FAST_LIMIT], 30000)
        self.assertEqual(params[ALIB_SKIN_LIMIT], 18000)
        self.assertEqual(params[ALIB_SLOW_TIME], 8)

    def test_no_boost_matches_tdp(self):
        params = dict(tdp_params(20, 85, False))
        self.assertEqual(params[ALIB_STAPM_LIMIT], 20000)
        self.assertEqual(params[ALIB_FAST_LIMIT], 20000)

    def test_profiles(self):
        silent = builtin("silent")
        turbo = builtin("turbo")
        self.assertLess(silent.tdp_w, turbo.tdp_w)
        self.assertEqual(silent.epp, "power")

    def test_resolved_limits_boost(self):
        settings = builtin("balanced")
        lim = settings.limits()
        self.assertEqual(lim.stapm_w, 28)
        self.assertGreater(lim.fast_w, lim.stapm_w)
        self.assertGreaterEqual(lim.fast_w, lim.slow_w)

    def test_advanced_limits(self):
        settings = Settings.from_dict({
            "advanced": True,
            "unlock": True,
            "stapm_w": 20,
            "fast_w": 30,
            "slow_w": 24,
            "skin_w": 18,
            "tctl_c": 88,
        })
        lim = settings.limits()
        self.assertEqual((lim.stapm_w, lim.fast_w, lim.slow_w, lim.skin_w, lim.tctl_c), (20, 30, 24, 18, 88))

    def test_recipes(self):
        flat = Settings(tdp_w=28).apply_recipe("flat")
        self.assertTrue(flat.advanced)
        self.assertEqual(flat.stapm_w, flat.fast_w)
        burst = Settings(tdp_w=28).apply_recipe("burst")
        self.assertGreater(burst.fast_w, burst.stapm_w)
        eff = Settings(tdp_w=28).apply_recipe("efficiency")
        self.assertLess(eff.stapm_w, eff.tdp_w)

    def test_parse_ryzenadj_info(self):
        text = """
CPU Family: Strix Point
PM Table Version: 5d000b
| Name                |   Value   | Parameter   |
| STAPM LIMIT         |    28.000 | stapm-limit |
| STAPM VALUE         |     7.670 |             |
| PPT LIMIT FAST      |    32.000 | fast-limit  |
| PPT VALUE FAST      |    12.116 |             |
| StapmTimeConst      |       nan | stapm-time  |
"""
        parsed = parse_info(text)
        self.assertEqual(parsed["meta"]["CPU Family"], "Strix Point")
        self.assertEqual(parsed["rows"]["STAPM LIMIT"]["value"], 28.0)
        self.assertIsNone(parsed["rows"]["StapmTimeConst"]["value"])
        cmd = command_line(Settings(tdp_w=28, boost=True).limits())
        self.assertIn("--stapm-limit=28000", cmd)
        self.assertIn("--fast-limit=", cmd)

    def test_mhz_from_cpuinfo(self):
        from pathlib import Path

        from maxctl.hardware import fill_missing_telemetry

        text = (
            "processor\t: 0\n"
            "cpu MHz\t\t: 1992.187\n"
            "processor\t: 1\n"
            "cpu MHz\t\t: 2100.000\n"
        )
        self.assertEqual(mhz_from_cpuinfo(text), 2046)
        self.assertIsNone(mhz_from_cpuinfo("vendor_id : AuthenticAMD\n"))
        if Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").exists():
            filled = fill_missing_telemetry({})
            self.assertIsNotNone(filled.get("cpu_mhz"))

    def test_fan_curve(self):
        curve = [CurvePoint(40, 20), CurvePoint(80, 100)]
        self.assertEqual(interpolate(40, curve), int(round(20 * 2.55)))
        self.assertEqual(interpolate(80, curve), 255)
        mid = interpolate(60, curve)
        self.assertTrue(80 < mid < 200)


if __name__ == "__main__":
    unittest.main()
