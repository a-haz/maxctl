import unittest

from maxctl.alib import encode_alib, format_call, tdp_params
from maxctl.const import ALIB_FAST_LIMIT, ALIB_STAPM_LIMIT
from maxctl.fan import CurvePoint, interpolate
from maxctl.profiles import builtin


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

    def test_no_boost_matches_tdp(self):
        params = dict(tdp_params(20, 85, False))
        self.assertEqual(params[ALIB_STAPM_LIMIT], 20000)
        self.assertEqual(params[ALIB_FAST_LIMIT], 20000)

    def test_profiles(self):
        silent = builtin("silent")
        turbo = builtin("turbo")
        self.assertLess(silent.tdp_w, turbo.tdp_w)
        self.assertEqual(silent.epp, "power")

    def test_fan_curve(self):
        curve = [CurvePoint(40, 20), CurvePoint(80, 100)]
        self.assertEqual(interpolate(40, curve), int(round(20 * 2.55)))
        self.assertEqual(interpolate(80, curve), 255)
        mid = interpolate(60, curve)
        self.assertTrue(80 < mid < 200)


if __name__ == "__main__":
    unittest.main()
