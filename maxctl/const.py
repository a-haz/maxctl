"""Hardware limits and defaults for GPD Win Max 2 2025 (G1619-05, HX 370)."""

from __future__ import annotations

DMI_PRODUCT = "G1619-05"
DEVICE_NAME = "GPD Win Max 2"
CPU_NAME = "AMD Ryzen AI 9 HX 370"
GPU_NAME = "Radeon 890M"

# GPD advertises 35W. AMD cTDP for HX 370 is 15–54W. HHD is conservative
# (28W). We expose 8–40W by default and 8–54W when unlocked.
TDP_MIN = 8
TDP_DEFAULT = 28
TDP_RECOMMENDED_MAX = 35
TDP_SAFE_MAX = 40
TDP_UNLOCK_MAX = 54

TCTL_MIN = 70
TCTL_MAX = 100
TCTL_DEFAULT = 90

GPU_SCLK_MIN = 600
GPU_SCLK_MAX = 2900

# ALIB / SMU command IDs used by AMD ACPI \_SB.ALIB 0x0C (same as HHD Adjustor).
ALIB_STAPM_LIMIT = 0x05
ALIB_FAST_LIMIT = 0x06
ALIB_SLOW_LIMIT = 0x07
ALIB_STAPM_TIME = 0x01
ALIB_SLOW_TIME = 0x08
ALIB_TEMP_TARGET = 0x03
ALIB_SKIN_LIMIT = 0x2E

SOCKET_PATH = "/run/maxctl/maxctl.sock"
STATE_PATH = "/var/lib/maxctl/state.json"
CONFIG_PATH = "/etc/maxctl/config.json"
USER_CONFIG_PATH = "~/.config/maxctl/config.json"

FAN_DISABLE = 0
FAN_MANUAL = 1
FAN_AUTO = 2

EPP_MODES = (
    "performance",
    "balance_performance",
    "balance_power",
    "power",
)

PPD_MODES = ("power-saver", "balanced", "performance")
