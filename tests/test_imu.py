import csv
import io
import json
import math
import random
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from maxctl import cli, imu

TRUE_BIAS = (-0.0015, -0.0111, 0.0048)  # rad/s
LSB = 0.001065                            # BMI260 gyro step, rad/s


def make_still(path: Path, seconds=120.0, fs=100.0, seed=0, knock_at=None) -> Path:
    """Synthetic still recording: known gyro bias, white noise, sysfs quantisation, -z up."""
    rng = random.Random(seed)
    n = int(seconds * fs)
    t = 8_588_353_946_672
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(imu.COLUMNS)
        for i in range(n):
            t += int(1e9 / fs) + rng.randint(-300_000, 300_000)
            g = [b + rng.gauss(0, 0.0017) for b in TRUE_BIAS]
            a = [-0.04 + rng.gauss(0, 0.02), -0.10 + rng.gauss(0, 0.02), -9.72 + rng.gauss(0, 0.02)]
            if knock_at is not None and int(knock_at * fs) <= i < int(knock_at * fs) + 30:
                g = [x + 0.2 for x in g]
                a = [x + 2.0 for x in a]
            g = [round(x / LSB) * LSB for x in g]
            w.writerow([t, 1_792_399_872_833_684_377 + i * 10_000_000]
                       + [f"{x:.7f}" for x in g] + [f"{x:.6f}" for x in a]
                       + ["40.400" if i % 10 == 0 else ""])
    return path


class ImuMathTests(unittest.TestCase):
    def test_allan_white_noise(self):
        rng = random.Random(2)
        x = [rng.gauss(0, 1.0) for _ in range(100_000)]
        ad = imu.allan_dev(x, 100.0, (1.0, 10.0))
        # white rate noise: sigma(tau) = sigma / sqrt(tau * fs)
        self.assertAlmostEqual(ad[1.0], 1 / math.sqrt(100), delta=0.01)
        self.assertAlmostEqual(ad[10.0], 1 / math.sqrt(1000), delta=0.006)

    def test_allan_skips_taus_too_long(self):
        self.assertEqual(imu.allan_dev([0.0] * 100, 100.0, (0.4, 0.6)), {0.4: 0.0})

    def test_integrate_constant_rate(self):
        t = [i * 10_000_000 for i in range(101)]  # 1 s
        rates = [(0.1, -0.2, 0.0)] * 101
        out = imu.integrate(rates, t)
        self.assertAlmostEqual(out[0], 0.1)
        self.assertAlmostEqual(out[1], -0.2)
        self.assertAlmostEqual(imu.integrate(rates, t, (0.1, 0.0, 0.0))[0], 0.0)


class ImuAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.still = make_still(cls.dir / "still.csv")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_bias_recovered_and_still(self):
        rep = imu.analyze(imu.load(self.still), skip=5.0)
        for est, true in zip(rep["gyro_bias_rad_s"], TRUE_BIAS):
            self.assertLess(abs(est - true), 1e-4)  # stderr ~ 1.7e-3 / sqrt(11500) = 1.6e-5
        self.assertEqual(rep["motion_check"]["status"], "STILL")
        self.assertEqual(rep["motion_check"]["flagged_samples"], 0)
        self.assertEqual(rep["up_axis"], "-z")
        self.assertLess(rep["tilt_from_up_axis_deg"], 1.0)

    def test_heading_drift(self):
        rep = imu.analyze(imu.load(self.still), skip=5.0)
        drift = rep["heading_drift"]
        used = drift["used_s"]
        # with no bias removed the angle grows by bias * time (the measured
        # bias, which quantisation moves slightly off TRUE_BIAS)
        bias = rep["gyro_bias_rad_s"]
        self.assertAlmostEqual(drift["no_bias_removed_deg"][2], math.degrees(bias[2]) * used, delta=0.05)
        self.assertAlmostEqual(drift["no_bias_removed_deg_per_min"][1], math.degrees(bias[1]) * 60, delta=0.01)
        self.assertGreater(abs(drift["no_bias_removed_deg"][1]), 60.0)
        # with the bias from the other half it stays small: one sigma is about
        # sqrt(2) * 1.7e-3 / sqrt(5750) rad/s over 57 s, roughly 0.1 deg
        for x in drift["holdout_first_half_bias_on_second_half_deg"]:
            self.assertLess(abs(x), 0.5)

    def test_allan_in_report(self):
        rep = imu.analyze(imu.load(self.still), skip=5.0)
        ad = rep["allan_dev_deg_s"]["gz"]
        self.assertIn("1.0", ad)
        self.assertAlmostEqual(ad["1.0"], math.degrees(0.0017 / math.sqrt(100)), delta=0.003)

    def test_knock_is_flagged_and_excluded(self):
        path = make_still(self.dir / "knock.csv", seed=1, knock_at=60.0)
        rep = imu.analyze(imu.load(path))
        self.assertGreater(rep["motion_check"]["flagged_samples"], 0)
        self.assertLess(rep["samples_used"], rep["samples_in_window"] - 30)
        for est, true in zip(rep["gyro_bias_rad_s"], TRUE_BIAS):
            self.assertLess(abs(est - true), 1e-4)

    def test_summary_with_bias(self):
        rec = imu.load(self.still)
        raw = imu.summary(rec)
        fixed = imu.summary(rec, TRUE_BIAS)
        self.assertAlmostEqual(raw["rate_hz"], 100.0, delta=0.5)
        self.assertGreater(abs(raw["integrated_angle_deg"][1]), 50.0)
        self.assertLess(max(abs(x) for x in fixed["integrated_angle_deg"]), 1.0)
        self.assertEqual(raw["temp_c_first_last"], [40.4, 40.4])

    def test_load_rejects_bad_files(self):
        bad = self.dir / "bad.csv"
        bad.write_text("t_mono_ns,gx\n1,0\n")
        with self.assertRaises(imu.ImuError):
            imu.load(bad)
        short = self.dir / "short.csv"
        with short.open("w") as fh:
            fh.write(",".join(imu.COLUMNS) + "\n" + "1,1,0,0,0,0,0,-9.8,\n" * 10)
        with self.assertRaises(imu.ImuError):
            imu.load(short)


class ImuCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.still = make_still(self.dir / "still.csv", seconds=40.0)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_bias_then_apply_match(self):
        out = self.dir / "out"
        code, text = self.run_cli("imu", "bias", str(self.still), "--skip", "2", "--accel-bias", "level",
                                  "--g", "9.798", "--out", str(out))
        self.assertEqual(code, 0)
        rep = json.loads(text)
        self.assertAlmostEqual(rep["accel_bias_subtracted_m_s2"][2], -9.72 + 9.798, delta=0.01)
        report = out / "still.bias.json"
        corrected = out / "still.bias.csv"
        self.assertTrue(report.exists() and corrected.exists())
        with self.still.open() as f1, corrected.open() as f2:
            rows_in, rows_out = list(csv.DictReader(f1)), list(csv.DictReader(f2))
        self.assertEqual(len(rows_in), len(rows_out))
        for k in ("t_mono_ns", "t_wall_ns", "temp_c"):
            self.assertEqual([r[k] for r in rows_in], [r[k] for r in rows_out])
        code, _ = self.run_cli("imu", "apply", str(self.still), "--bias", str(report), "--out", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(corrected.read_text(), (out / "still.biasapplied.csv").read_text())

    def test_refuses_to_overwrite(self):
        out = self.dir / "out"
        self.assertEqual(self.run_cli("imu", "bias", str(self.still), "--skip", "2", "--out", str(out))[0], 0)
        self.assertEqual(self.run_cli("imu", "bias", str(self.still), "--skip", "2", "--out", str(out))[0], 1)
        self.assertEqual(
            self.run_cli("imu", "bias", str(self.still), "--skip", "2", "--out", str(out), "--overwrite")[0], 0
        )

    def test_summary_uses_bias_report(self):
        out = self.dir / "out"
        self.run_cli("imu", "bias", str(self.still), "--skip", "2", "--out", str(out))
        code, text = self.run_cli("imu", "summary", str(self.still), "--bias", str(out / "still.bias.json"))
        self.assertEqual(code, 0)
        self.assertLess(max(abs(x) for x in json.loads(text)["integrated_angle_deg"]), 0.5)


class ImuDeviceTests(unittest.TestCase):
    def fake_iio(self, root: Path) -> Path:
        (root / "iio:device0").mkdir(parents=True)
        (root / "iio:device0" / "name").write_text("some_als\n")
        dev = root / "iio:device1"
        dev.mkdir()
        files = {
            "name": "bmi260", "in_anglvel_scale": "0.001065264", "in_accel_scale": "0.002394",
            "in_anglvel_x_raw": "-1", "in_anglvel_y_raw": "-10", "in_anglvel_z_raw": "5",
            "in_accel_x_raw": "-17", "in_accel_y_raw": "-41", "in_accel_z_raw": "-4060",
            "in_temp_raw": "180", "in_temp_offset": "11776", "in_temp_scale": "1.953125",
            "in_anglvel_sampling_frequency": "200",
        }
        for name, value in files.items():
            (dev / name).write_text(value + "\n")
        return dev

    def test_find_and_record_from_sysfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "iio"
            dev = self.fake_iio(root)
            self.assertEqual(imu.find_device(root), dev)
            self.assertIsNone(imu.find_device(Path(tmp) / "missing"))
            out = Path(tmp) / "rec" / "imu_test.csv"
            meta = imu.record(out, seconds=0.2, rate=50.0, dev=dev)
            self.assertEqual(meta["name"], "bmi260")
            self.assertEqual(meta["in_anglvel_sampling_frequency"], "200")
            with out.open() as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 10)
            self.assertAlmostEqual(float(rows[0]["gy"]), -10 * 0.001065264, places=6)
            self.assertAlmostEqual(float(rows[0]["az"]), -4060 * 0.002394, places=5)
            self.assertAlmostEqual(float(rows[0]["temp_c"]), (180 + 11776) * 1.953125 / 1000, places=3)
            self.assertEqual(rows[1]["temp_c"], "")


if __name__ == "__main__":
    unittest.main()
