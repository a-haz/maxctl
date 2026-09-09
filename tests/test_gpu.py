import unittest

from maxctl.gpu import parse_od_sclk


class OdParseTests(unittest.TestCase):
    def test_parses_strix_table(self):
        text = "OD_SCLK:\n0:        600Mhz\n1:       1000Mhz\nOD_RANGE:\nSCLK:     600Mhz       2900Mhz\n"
        self.assertEqual(parse_od_sclk(text), (600, 1000))

    def test_ignores_range_block_and_garbage(self):
        self.assertIsNone(parse_od_sclk("OD_RANGE:\nSCLK: 600Mhz 2900Mhz\n"))
        self.assertIsNone(parse_od_sclk(""))
        self.assertIsNone(parse_od_sclk("OD_SCLK:\n0: abc\n1: 2900Mhz\n"))


if __name__ == "__main__":
    unittest.main()
