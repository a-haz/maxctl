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
| eGPU | PCI detection → iGPU capped, budget to CPU |

Presets: **Silent 12W**, **Balanced 28W**, **Sport 35W**, **Turbo 40W**, **eGPU 28W**. 28–35W is the sweet spot on this chassis. GPD advertises 35W; 54W is AMD cTDP max and runs hot.

### eGPU mode

The HX 370 shares one STAPM / PPT budget between the cores and the 890M. With an external GPU (OCuLink or USB4) doing the rendering, the **eGPU** profile pins the iGPU to 600–1000 MHz (`pp_od_clk_voltage`, DPM table becomes 600 / 780 / 1000) and keeps the Balanced 28W tier (EPP `balance_performance`), so the budget is spent on the CPU. It stays at 28W on purpose: the 35W / EPP-performance tier is where this chassis's SMU has hung (black screen); raise the TDP slider only once that is proven stable. 1000 MHz still composites the internal panel plus HDMI without stutter; raise **Max clock** under *iGPU* if you drive the built-in display hard.

Detection is a PCI display-class device that is not the APU (`/sys/bus/pci/devices/*/class` = `0x03…`). The helper applies the eGPU profile at boot when a dock is already attached (OCuLink is not hot-plug) and on attach / detach edges when it is; on detach it falls back to the AC / battery profile. Config keys: `egpu_switch` (default `true`), `egpu_profile` (default `egpu`).

```bash
maxctl egpu            # detected card + auto-switch state
maxctl egpu off        # keep AC / battery switching only
maxctl egpu on --profile turbo
maxctl profile egpu    # apply by hand
```

## Install

Project path: `~/Workspace/Projects/maxctl`

```bash
cd ~/Workspace/Projects/maxctl
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

## Power user

The **Power user** tab is for raw SMU / clock work. Overview stays the simple 12 / 28 / 35 / 40W path.

- Manual STAPM / Fast PPT / Slow PPT / Skin, plus STAPM and Slow times
- Recipes: **Flat** (all rails = TDP), **Burst** (short Fast above TDP), **Efficiency** (lower sustained, keep a little burst)
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

Project path: `~/Workspace/Projects/maxctl`
