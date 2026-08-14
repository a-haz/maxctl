# maxctl

TDP, power profiles, iGPU clocks, and fan control for the **GPD Win Max 2 2025** (G1619-05, Ryzen AI 9 HX 370) on Ubuntu.

Ubuntu only ships `power-profiles-daemon`, which changes CPU preference. It does not set STAPM / PPT, Tctl, or the GPD EC fan. This app does.

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

Presets: **Silent 12W**, **Balanced 28W**, **Sport 35W**, **Turbo 40W**. 28–35W is the sweet spot on this chassis. GPD advertises 35W; 54W is AMD cTDP max and runs hot.

## Install

```bash
sudo ./scripts/install.sh
maxctl-gui
```

The helper (`maxctld`) stays running so TDP is re-applied every few seconds. Firmware and `power-profiles-daemon` like to reset SMU limits.

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

Values are clamped. Default ceiling is 40W. Unlock 54W only if you accept more heat and fan noise. This talks to the SMU the same way Motion Assistant / HHD do; bad limits can still thermal-throttle or trip the machine.
