import csv
import hashlib
import io
import json
import math
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from maxctl import cli, imu

T0 = 1_000_000_000_000
STEP = 10_000_000  # 100 Hz


def write_rec(path, n=200, gyro=None, accel=None, temp=None, t=None, columns=imu.COLUMNS):
    """Noise-free synthetic recording. Each callable gets the row index."""
    gyro = gyro or (lambda i: (0.0, 0.0, 0.0))
    accel = accel or (lambda i: (0.0, 0.0, -9.8))
    temp = temp or (lambda i: "")
    t = t or (lambda i: T0 + i * STEP)
    with Path(path).open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        for i in range(n):
            g, a = gyro(i), accel(i)
            row = {
                "t_mono_ns": t(i), "t_wall_ns": 1_700_000_000_000_000_000 + i * STEP,
                "gx": f"{g[0]:.7f}", "gy": f"{g[1]:.7f}", "gz": f"{g[2]:.7f}",
                "ax": f"{a[0]:.6f}", "ay": f"{a[1]:.6f}", "az": f"{a[2]:.6f}",
                "temp_c": temp(i),
            }
            w.writerow([row.get(c, "") for c in columns])
    return Path(path)


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class ImuLoadTests(TmpDirCase):
    def test_parses_values_temps_and_duration(self):
        path = write_rec(self.dir / "r.csv", n=120,
                         gyro=lambda i: (0.001 * i, -0.002, 0.5),
                         accel=lambda i: (0.25, -0.5, -9.75),
                         temp=lambda i: f"{30 + i / 10:.3f}" if i % 10 == 0 else "")
        rec = imu.load(path)
        self.assertEqual(len(rec.t), 120)
        self.assertEqual(rec.t[0], T0)
        self.assertEqual(rec.gyro[7], (0.007, -0.002, 0.5))
        self.assertEqual(rec.accel[0], (0.25, -0.5, -9.75))
        self.assertEqual(rec.temps, [30.0 + k for k in range(12)])
        self.assertAlmostEqual(rec.duration_s, 1.19)
        self.assertEqual(rec.rows[3]["t_mono_ns"], str(T0 + 3 * STEP))

    def test_row_count_threshold(self):
        imu.load(write_rec(self.dir / "ok.csv", n=100))
        with self.assertRaisesRegex(imu.ImuError, "only 99 rows"):
            imu.load(write_rec(self.dir / "short.csv", n=99))

    def test_header_only_and_empty_file(self):
        with self.assertRaisesRegex(imu.ImuError, "only 0 rows"):
            imu.load(write_rec(self.dir / "hdr.csv", n=0))
        empty = self.dir / "empty.csv"
        empty.write_text("")
        with self.assertRaisesRegex(imu.ImuError, "missing columns"):
            imu.load(empty)

    def test_missing_column_is_named(self):
        cols = tuple(c for c in imu.COLUMNS if c != "temp_c")
        with self.assertRaisesRegex(imu.ImuError, "temp_c"):
            imu.load(write_rec(self.dir / "notemp.csv", columns=cols))

    def test_extra_and_reordered_columns_are_accepted(self):
        cols = ("extra",) + tuple(reversed(imu.COLUMNS))
        rec = imu.load(write_rec(self.dir / "odd.csv", columns=cols, gyro=lambda i: (0.1, 0.2, 0.3)))
        self.assertEqual(rec.gyro[0], (0.1, 0.2, 0.3))
        self.assertEqual(rec.accel[0], (0.0, 0.0, -9.8))

    def test_time_must_strictly_increase(self):
        dup = write_rec(self.dir / "dup.csv", t=lambda i: T0 + (i - (i == 50)) * STEP)
        with self.assertRaisesRegex(imu.ImuError, "strictly increasing"):
            imu.load(dup)
        back = write_rec(self.dir / "back.csv", t=lambda i: T0 + i * STEP - (2 * STEP if i == 150 else 0))
        with self.assertRaises(imu.ImuError):
            imu.load(back)

    def test_non_numeric_value_raises_value_error(self):
        path = write_rec(self.dir / "nan.csv")
        text = path.read_text().splitlines()
        parts = text[20].split(",")
        parts[2] = "abc"
        text[20] = ",".join(parts)
        path.write_text("\n".join(text) + "\n")
        with self.assertRaises(ValueError):
            imu.load(path)

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            imu.load(self.dir / "nope.csv")


class ImuWriteTests(TmpDirCase):
    def test_write_corrected_subtracts_and_round_trips(self):
        src = write_rec(self.dir / "in.csv", n=100,
                        gyro=lambda i: (0.01, -0.02, 0.03 + i * 1e-4),
                        accel=lambda i: (0.5, -0.25, -9.75),
                        temp=lambda i: "41.000" if i % 10 == 0 else "")
        rec = imu.load(src)
        out = self.dir / "out.csv"
        imu.write_corrected(rec, out, (0.01, 0.01, 0.03), (0.5, 0.0, -0.05))
        fixed = imu.load(out)
        self.assertEqual(fixed.t, rec.t)
        self.assertEqual(fixed.temps, rec.temps)
        for i in (0, 42, 99):
            self.assertAlmostEqual(fixed.gyro[i][0], 0.0, places=7)
            self.assertAlmostEqual(fixed.gyro[i][1], -0.03, places=7)
            self.assertAlmostEqual(fixed.gyro[i][2], i * 1e-4, places=7)
            self.assertEqual(fixed.accel[i], (0.0, -0.25, -9.7))
        self.assertEqual([r["t_wall_ns"] for r in fixed.rows], [r["t_wall_ns"] for r in rec.rows])

    def test_zero_accel_bias_by_default(self):
        rec = imu.load(write_rec(self.dir / "in.csv", accel=lambda i: (0.123456, 1.0, -9.5)))
        out = self.dir / "out.csv"
        imu.write_corrected(rec, out, (0.0, 0.0, 0.0))
        self.assertEqual(imu.load(out).accel, rec.accel)

    def test_sha256_matches_hashlib(self):
        path = self.dir / "blob.bin"
        data = bytes(range(256)) * 9000  # larger than one read chunk
        path.write_bytes(data)
        self.assertEqual(imu.sha256(path), hashlib.sha256(data).hexdigest())


class ImuMathEdgeTests(unittest.TestCase):
    def test_integrate_uneven_steps_is_trapezoid(self):
        t = [0, 10_000_000, 30_000_000, 60_000_000, 100_000_000]
        rates = [(s / 1e9, 1.0, 0.0) for s in t]  # rate = time, exact under trapezoid
        out = imu.integrate(rates, t, (0.0, 0.5, 0.0))
        self.assertAlmostEqual(out[0], 0.1 ** 2 / 2)
        self.assertAlmostEqual(out[1], 0.5 * 0.1)
        self.assertEqual(out[2], 0.0)

    def test_integrate_single_sample_is_zero(self):
        self.assertEqual(imu.integrate([(1.0, 2.0, 3.0)], [5]), [0.0, 0.0, 0.0])

    def test_allan_alternating_series(self):
        x = [1.0 if i % 2 == 0 else -1.0 for i in range(1000)]
        ad = imu.allan_dev(x, 100.0, (0.001, 0.01, 0.02))
        self.assertNotIn(0.001, ad)  # m rounds to 0
        self.assertAlmostEqual(ad[0.01], math.sqrt(2))  # successive single samples differ by 2
        self.assertAlmostEqual(ad[0.02], 0.0)  # pairs average to 0

    def test_up_axis_sign_and_tilt(self):
        g = 9.8
        idx, sign, unit, norm, tilt = imu._up([0.0, g * math.sin(math.radians(30)), -g * math.cos(math.radians(30))])
        self.assertEqual((idx, sign), (2, -1.0))
        self.assertAlmostEqual(norm, g)
        self.assertAlmostEqual(tilt, 30.0)
        self.assertAlmostEqual(math.sqrt(sum(u * u for u in unit)), 1.0)
        idx, sign, _, _, tilt = imu._up([9.8, 0.0, 0.0])
        self.assertEqual((idx, sign, tilt), (0, 1.0, 0.0))
        with self.assertRaises(imu.ImuError):
            imu._up([0.0, 0.0, 0.0])


class ImuSummaryEdgeTests(TmpDirCase):
    def test_gap_rate_up_axis_and_no_temps(self):
        # one 50 ms dropout between rows 99 and 100
        path = write_rec(self.dir / "gap.csv", n=200, accel=lambda i: (0.1, 9.7, 0.2),
                         t=lambda i: T0 + i * STEP + (40_000_000 if i >= 100 else 0))
        rep = imu.summary(imu.load(path))
        self.assertEqual(rep["samples"], 200)
        self.assertEqual(rep["max_gap_ms"], 50.0)
        self.assertEqual(rep["duration_s"], 2.03)
        self.assertAlmostEqual(rep["rate_hz"], 199 / 2.03, places=2)
        self.assertEqual(rep["up_axis"], "+y")
        self.assertIsNone(rep["temp_c_first_last"])
        self.assertEqual(rep["gyro_std_deg_s"], [0.0, 0.0, 0.0])

    def test_constant_rate_integrates_to_known_angle(self):
        path = write_rec(self.dir / "spin.csv", n=301, gyro=lambda i: (0.0, 0.0, math.radians(10)))
        rep = imu.summary(imu.load(path))
        self.assertAlmostEqual(rep["integrated_angle_deg"][2], 30.0, places=3)
        self.assertAlmostEqual(rep["gyro_mean_deg_s"][2], 10.0, places=4)
        fixed = imu.summary(imu.load(path), (0.0, 0.0, math.radians(10)))
        self.assertAlmostEqual(fixed["integrated_angle_deg"][2], 0.0, places=3)
        self.assertAlmostEqual(fixed["bias_subtracted_deg_s"][2], 10.0, places=4)


class ImuAnalyzeEdgeTests(TmpDirCase):
    def two_level(self, name="two.csv"):
        # 40 s; gz bias 0.010 rad/s for t < 20 s and 0.012 after (0.11 deg/s step, below the motion limit)
        return write_rec(self.dir / name, n=4001,
                         gyro=lambda i: (0.0, 0.0, 0.010 if i < 2000 else 0.012))

    def test_window_overrides_skip_and_tail(self):
        rec = imu.load(self.two_level())
        rep = imu.analyze(rec, skip=0.0, window=(25.0, 39.0))
        self.assertEqual(rep["window_s"], [25.0, 39.0])
        self.assertAlmostEqual(rep["gyro_bias_rad_s"][2], 0.012, places=9)
        self.assertEqual(rep["motion_check"]["status"], "STILL")
        rep = imu.analyze(rec, skip=1.0, tail=22.0)
        self.assertEqual(rep["window_s"], [1.0, 18.0])
        self.assertAlmostEqual(rep["gyro_bias_rad_s"][2], 0.010, places=9)
        self.assertEqual(rep["samples_in_window"], 1701)

    def test_split_half_diff_sees_bias_step(self):
        rep = imu.analyze(imu.load(self.two_level()), skip=0.0)
        self.assertAlmostEqual(rep["gyro_bias_split_half_diff_deg_s"][2], math.degrees(0.002), places=4)
        self.assertEqual(rep["gyro_bias_split_half_diff_deg_s"][:2], [0.0, 0.0])

    def test_window_too_short_raises(self):
        rec = imu.load(write_rec(self.dir / "s.csv", n=500))
        with self.assertRaisesRegex(imu.ImuError, "holds only"):
            imu.analyze(rec, window=(1.0, 1.5))
        with self.assertRaisesRegex(imu.ImuError, "holds only"):
            imu.analyze(rec)  # default 15 s skip is past the end of a 5 s file

    def test_all_motion_leaves_too_few_samples(self):
        rec = imu.load(write_rec(self.dir / "shake.csv", n=1000,
                                 gyro=lambda i: (0.5 if i % 2 else -0.5, 0.0, 0.0)))
        with self.assertRaisesRegex(imu.ImuError, "after the motion check"):
            imu.analyze(rec, skip=0.0)

    def test_knock_marks_not_still_and_guard_drops_neighbours(self):
        knock = range(1500, 1550)  # 50 of 3001 samples, 1.7 %
        rec = imu.load(write_rec(self.dir / "k.csv", n=3001,
                                 gyro=lambda i: (0.0, 0.3 if i in knock else 0.002, 0.0)))
        rep = imu.analyze(rec, skip=0.0, guard=0.5)
        mc = rep["motion_check"]
        self.assertEqual(mc["flagged_samples"], 50)
        self.assertEqual(mc["status"], "NOT_STILL")
        # 50 flagged plus 50 guard samples on each side
        self.assertEqual(rep["samples_used"], 3001 - 150)
        self.assertAlmostEqual(rep["gyro_bias_rad_s"][1], 0.002, places=9)
        rep = imu.analyze(rec, skip=0.0, motion_gyro_dps=30.0)
        self.assertEqual(rep["motion_check"]["flagged_samples"], 0)

    def test_accel_norm_jump_is_flagged(self):
        rec = imu.load(write_rec(self.dir / "a.csv", n=2001,
                                 accel=lambda i: (0.0, 0.0, -11.0 if 1000 <= i < 1010 else -9.8)))
        rep = imu.analyze(rec, skip=0.0, guard=0.0)
        self.assertEqual(rep["motion_check"]["flagged_samples"], 10)
        self.assertEqual(rep["samples_used"], 1991)
        self.assertEqual(rep["motion_check"]["status"], "STILL")  # 0.5 % is under the 1 % limit
        rep = imu.analyze(rec, skip=0.0, guard=0.0, motion_accel=2.0)
        self.assertEqual(rep["motion_check"]["flagged_samples"], 0)

    def test_linear_trend_is_reported_per_minute(self):
        r = 0.01 / 30.0  # rad/s per s
        rec = imu.load(write_rec(self.dir / "ramp.csv", n=3001, gyro=lambda i: (0.0, 0.0, 0.003 + r * i / 100)))
        rep = imu.analyze(rec, skip=0.0)
        self.assertAlmostEqual(rep["gyro_trend_deg_s_per_min"][2], math.degrees(r * 60), places=3)
        self.assertAlmostEqual(rep["gyro_bias_rad_s"][2], 0.003 + r * 15, places=6)

    def test_accel_bias_if_level_and_tilt(self):
        rec = imu.load(write_rec(self.dir / "lvl.csv", n=2001, accel=lambda i: (0.1, -0.2, -9.7)))
        rep = imu.analyze(rec, skip=0.0, g=9.8)
        bx, by, bz = rep["accel_bias_if_level_m_s2"]
        self.assertAlmostEqual(bx, 0.1)
        self.assertAlmostEqual(by, -0.2)
        self.assertAlmostEqual(bz, 0.1)
        self.assertEqual(rep["up_axis"], "-z")
        self.assertEqual(rep["g_m_s2"], 9.8)
        norm = math.sqrt(0.01 + 0.04 + 9.7 ** 2)
        self.assertAlmostEqual(rep["tilt_from_up_axis_deg"], math.degrees(math.acos(9.7 / norm)), places=3)
        self.assertAlmostEqual(rep["gyro_bias_about_up_deg_s"], 0.0)


class ImuCliEdgeTests(TmpDirCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_apply_math_and_default_accel_bias(self):
        src = write_rec(self.dir / "rec.csv", n=201, gyro=lambda i: (0.02, -0.01, 0.0),
                        accel=lambda i: (0.3, 0.0, -9.6))
        report = self.dir / "b.json"
        report.write_text(json.dumps({"gyro_bias_rad_s": [0.02, -0.01, 0.0]}))
        out = self.dir / "out"
        code, text, _ = self.run_cli("imu", "apply", str(src), "--bias", str(report), "--out", str(out))
        self.assertEqual(code, 0)
        res = json.loads(text)
        self.assertEqual(res["accel_bias_subtracted_m_s2"], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(res["integrated_deg_raw"][0], math.degrees(0.02 * 2.0), places=5)
        self.assertEqual(res["integrated_deg_corrected"], [0.0, 0.0, 0.0])
        self.assertEqual(res["input_sha256"], hashlib.sha256(src.read_bytes()).hexdigest())
        self.assertEqual(json.loads((out / "rec.biasapplied.json").read_text())["output_csv"], res["output_csv"])
        fixed = imu.load(out / "rec.biasapplied.csv")
        self.assertEqual(fixed.accel, imu.load(src).accel)
        self.assertTrue(all(g == (0.0, 0.0, 0.0) for g in fixed.gyro))

    def test_apply_with_report_lacking_bias_fails_cleanly(self):
        src = write_rec(self.dir / "rec.csv")
        report = self.dir / "bad.json"
        report.write_text(json.dumps({"something": 1}))
        out = self.dir / "out"
        code, text, err = self.run_cli("imu", "apply", str(src), "--bias", str(report), "--out", str(out))
        self.assertEqual(code, 1)
        self.assertEqual(text, "")
        self.assertIn("gyro_bias_rad_s", err)
        self.assertFalse((out / "rec.biasapplied.csv").exists())

    def test_apply_with_unreadable_report(self):
        src = write_rec(self.dir / "rec.csv")
        bad = self.dir / "bad.json"
        bad.write_text("{not json")
        for report in (bad, self.dir / "missing.json"):
            code, _, err = self.run_cli("imu", "apply", str(src), "--bias", str(report), "--out", str(self.dir))
            self.assertEqual(code, 1)
            self.assertTrue(err.startswith("maxctl: "))

    def test_apply_requires_bias_and_out(self):
        src = write_rec(self.dir / "rec.csv")
        for argv in (("imu", "apply", str(src), "--out", str(self.dir)),
                     ("imu", "apply", str(src), "--bias", "x.json"),
                     ("imu",)):
            with self.assertRaises(SystemExit) as cm:
                self.run_cli(*argv)
            self.assertEqual(cm.exception.code, 2)

    def test_summary_and_bias_report_bad_input(self):
        code, text, err = self.run_cli("imu", "summary", str(self.dir / "missing.csv"))
        self.assertEqual((code, text), (1, ""))
        self.assertIn("missing.csv", err)
        short = write_rec(self.dir / "short.csv", n=10)
        code, _, err = self.run_cli("imu", "bias", str(short))
        self.assertEqual(code, 1)
        self.assertIn("only 10 rows", err)

    def test_bias_window_and_tail_flags(self):
        src = write_rec(self.dir / "w.csv", n=3001, gyro=lambda i: (0.0, 0.0, 0.004))
        code, text, _ = self.run_cli("imu", "bias", str(src), "--window", "5", "20")
        self.assertEqual(code, 0)
        rep = json.loads(text)
        self.assertEqual(rep["window_s"], [5.0, 20.0])
        self.assertEqual(rep["accel_bias_mode"], "none")
        self.assertEqual(rep["accel_bias_subtracted_m_s2"], [0.0, 0.0, 0.0])
        self.assertNotIn("output_csv", rep)
        code, text, _ = self.run_cli("imu", "bias", str(src), "--skip", "2", "--tail", "8")
        self.assertEqual(json.loads(text)["window_s"], [2.0, 22.0])
        with self.assertRaises(SystemExit):
            self.run_cli("imu", "bias", str(src), "--accel-bias", "tilted")

    def test_log_writes_recording_and_summary(self):
        calls = []

        def fake_record(out, seconds, rate, progress=None):
            calls.append((seconds, rate, progress))
            out.parent.mkdir(parents=True, exist_ok=True)
            write_rec(out, n=150)
            return {"name": "bmi260"}

        out = self.dir / "logs"
        with mock.patch.object(imu, "record", fake_record):
            code, text, _ = self.run_cli("imu", "log", "--label", "desk", "--seconds", "3",
                                         "--rate", "50", "--out", str(out), "--progress")
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][:2], (3.0, 50.0))
        self.assertTrue(callable(calls[0][2]))
        files = sorted(p.name for p in out.iterdir())
        self.assertEqual(len(files), 2)
        self.assertRegex(files[0], r"^imu_desk_\d{8}_\d{6}\.csv$")
        self.assertEqual(files[1], files[0] + ".summary.json")
        summary = json.loads((out / files[1]).read_text())
        self.assertEqual(summary["samples"], 150)
        self.assertEqual(summary["meta"], {"name": "bmi260"})
        self.assertIn("Recording 3 s at 50 Hz", text)

    def test_log_without_device_fails_cleanly(self):
        with mock.patch.object(imu, "find_device", return_value=None):
            code, _, err = self.run_cli("imu", "log", "--seconds", "1", "--out", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("no BMI260 IIO device", err)
        self.assertEqual(list(self.dir.iterdir()), [])


class ImuDeviceEdgeTests(TmpDirCase):
    def make_dev(self, root, name, temp=True):
        dev = root / name
        dev.mkdir(parents=True)
        files = {"name": "bmi270", "in_anglvel_scale": "0.001", "in_accel_scale": "0.01",
                 "in_anglvel_x_raw": "2", "in_anglvel_y_raw": "0", "in_anglvel_z_raw": "-3",
                 "in_accel_x_raw": "0", "in_accel_y_raw": "0", "in_accel_z_raw": "-980"}
        if temp:
            files.update({"in_temp_raw": "0", "in_temp_offset": "12000", "in_temp_scale": "2"})
        for k, v in files.items():
            (dev / k).write_text(v + "\n")
        return dev

    def test_find_device_skips_nameless_and_matches_bmi270(self):
        root = self.dir / "iio"
        (root / "iio:device0").mkdir(parents=True)  # no name file
        dev = self.make_dev(root, "iio:device1")
        self.make_dev(root, "iio:device2")
        self.assertEqual(imu.find_device(root), dev)
        notdir = self.dir / "file"
        notdir.write_text("")
        self.assertIsNone(imu.find_device(notdir))

    def test_record_without_temp_and_with_progress(self):
        dev = self.make_dev(self.dir / "iio", "iio:device0", temp=False)
        meta = imu.device_meta(dev)
        self.assertEqual(meta["accel_scale"], 0.01)
        self.assertNotIn("in_anglvel_sampling_frequency", meta)
        seen = []
        out = self.dir / "r.csv"
        with mock.patch.object(imu.time, "sleep"):
            imu.record(out, seconds=10.5, rate=100.0, dev=dev, progress=seen.append)
        self.assertEqual(seen, [10.0])
        rec = imu.load(out)
        self.assertEqual(len(rec.t), 1050)
        self.assertEqual(rec.temps, [])
        self.assertAlmostEqual(rec.gyro[0][2], -0.003)
        self.assertAlmostEqual(rec.accel[0][2], -9.8)

    def test_record_temperature_conversion(self):
        dev = self.make_dev(self.dir / "iio", "iio:device0")
        out = self.dir / "t.csv"
        with mock.patch.object(imu.time, "sleep"):
            imu.record(out, seconds=1.0, rate=100.0, dev=dev)
        rec = imu.load(out)
        self.assertEqual(rec.temps, [24.0] * 10)  # (0 + 12000) * 2 / 1000


if __name__ == "__main__":
    unittest.main()
