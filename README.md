# maxctl

TDP, power profiles, iGPU clocks, and fan control for the GPD Win Max 2 2025 (G1619-05, Ryzen AI 9 HX 370) on Ubuntu.

Ubuntu only ships `power-profiles-daemon`, which changes CPU preference but does not set STAPM / PPT, Tctl or the GPD EC fan. maxctl does.

## What it controls

| Control | How |
|---|---|
| TDP (8–40W, unlock 54W) | AMD ACPI `\_SB.ALIB` (same path as Handheld Daemon / Adjustor), RyzenAdj fallback |
| Temp limit | SMU Tctl |
| CPU boost + EPP | `amd-pstate-epp` |
| Ubuntu power profile | `powerprofilesctl` |
| 890M clocks | `pp_od_clk_voltage` |
| Fan | in-tree `gpdfan` (`pwm1` / `pwm1_enable`) |
| AC / battery | auto profile switch + re-apply after sleep |
| eGPU | PCI detection → iGPU capped, budget to CPU |

The presets are Silent 12W, Balanced 28W, Sport 35W, Turbo 40W and eGPU 28W. 28 to 35W works best on this chassis. GPD advertises 35W; 54W is the AMD cTDP max and runs hot.

### eGPU mode

The HX 370 shares one STAPM / PPT budget between the cores and the 890M. With an external GPU (OCuLink or USB4) doing the rendering, the eGPU profile pins the iGPU to 600 to 1000 MHz (`pp_od_clk_voltage`, DPM table 600 / 780 / 1000) and keeps the Balanced 28W tier (EPP `balance_performance`), so the budget goes to the CPU. It stays at 28W because the SMU on this chassis has hung (black screen) at the 35W / EPP-performance tier. 1000 MHz still drives the internal panel plus HDMI without stutter; raise Max clock under iGPU if you use the built-in display heavily.

Detection is a PCI display-class device that is not the APU (`/sys/bus/pci/devices/*/class` = `0x03…`). The helper applies the eGPU profile at boot when a dock is already attached (OCuLink is not hot-plug) and on attach / detach edges when it is; on detach it falls back to the AC / battery profile. Config keys: `egpu_switch` (default `true`), `egpu_profile` (default `egpu`).

```bash
maxctl egpu            # detected card + auto-switch state
maxctl egpu off        # keep AC / battery switching only
maxctl egpu on --profile turbo
maxctl profile egpu    # apply by hand
```

## Install

From the repository root:

```bash
sudo ./scripts/install.sh
maxctl-gui
```

The helper (`maxctld`) keeps running and re-applies TDP every few seconds, because firmware and `power-profiles-daemon` tend to reset SMU limits.

```bash
maxctl status
maxctl profile silent
maxctl tdp 28
maxctl fan auto
```

## Without install

Sensors work as your user:

```bash
./bin/maxctl status
./bin/maxctl-gui
```

Applying TDP needs root and either `acpi_call` (`acpi-call-dkms`) or `ryzenadj`. If RyzenAdj says it cannot open the SMU, add `iomem=relaxed` to the kernel command line or load a current `ryzen_smu` module.

## Safety

Values are clamped and the default ceiling is 40W. Unlocking 54W means more heat and fan noise. maxctl talks to the SMU the same way Motion Assistant and HHD do, and bad limits can still thermal-throttle or trip the machine.

## IMU

The Win Max 2 has a Bosch BMI260 IMU. The in-tree `bmi270_i2c` driver exposes it under `/sys/bus/iio/devices` once its init blob loads; linux-firmware ships it as `bmi260-init-data.fw`. `maxctl imu` reads it as your user, no root needed.

```bash
maxctl imu log --label desk --seconds 120 --out ~/imu   # imu_desk_<time>.csv
maxctl imu summary ~/imu/imu_desk_20261020_101500.csv
maxctl imu bias ~/imu/imu_desk_20261020_101500.csv --out ~/imu
maxctl imu apply other.csv --bias ~/imu/imu_desk_20261020_101500.bias.json --out ~/imu
```

`log` polls the sysfs channels (default 100 Hz) and writes `t_mono_ns, t_wall_ns, gx, gy, gz` (rad/s), `ax, ay, az` (m/s2) and `temp_c` (on every tenth row). Keep the machine still for `bias`. It drops the first 15 s, flags samples above 1 deg/s or 0.5 m/s2 from the median and drops 0.5 s around them, then reports the gyro bias, noise, split-half and trend checks, Allan deviation from 0.1 to 40 s, and heading drift with and without the bias (including first-half bias applied to the second half). With `--out` it also writes a bias-corrected CSV. `--accel-bias level` subtracts the accelerometer offset too, which only makes sense if the machine sat level; set `--g` to your local gravity.

## Power user

The Power user tab is for raw SMU and clock settings. The Overview tab keeps to the 12 / 28 / 35 / 40W presets. It has:

- Manual STAPM / Fast PPT / Slow PPT / Skin, plus STAPM and Slow times
- Recipes: Flat (all rails = TDP), Burst (short Fast above TDP), Efficiency (lower sustained, a little burst)
- Live SMU readback from `ryzenadj -i` (limit vs current draw). Strix Point often reports NaN for times / TDC / EDC
- CPU min/max MHz, governor, nonlinear min
- iGPU DPM (`auto` / `low` / `high` / `profile_standard` / `profile_peak`)
- RyzenAdj `--power-saving` / `--max-performance`
- Battery charge limit when the kernel exposes `charge_control_end_threshold`
- Fan curve slew and SMU re-apply interval
- Separate AC / battery profiles, plus an eGPU profile applied while an external GPU is attached
- Named presets under `~/.config/maxctl/presets.json`
- Export / import JSON, copy the equivalent `ryzenadj` command
- Diagnose: ALIB, RyzenAdj, `ryzen_smu`, `iomem=relaxed`, `amd_pstate`

Keys: `1` Silent, `2` Balanced, `3` Sport, `4` Turbo, `5` eGPU, `Ctrl+Enter` apply, `Ctrl+E` export, `Ctrl+I` SMU, `Ctrl+D` diagnose.

```bash
maxctl info
maxctl diagnose
maxctl smu
maxctl watch
maxctl watch --csv
maxctl set --stapm 22 --fast 30 --slow 24 --tctl 88
maxctl set --recipe efficiency
maxctl set --governor powersave --cpu-min 400 --dpm low --charge 80
maxctl preset save desk-28
maxctl export ~/maxctl-profile.json
```
