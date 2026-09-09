"""AMD ACPI ALIB TDP control — same SMU path Handheld Daemon / Adjustor uses."""

from __future__ import annotations

from pathlib import Path

from maxctl.const import (
    ALIB_FAST_LIMIT,
    ALIB_SKIN_LIMIT,
    ALIB_SLOW_LIMIT,
    ALIB_SLOW_TIME,
    ALIB_STAPM_LIMIT,
    ALIB_STAPM_TIME,
    ALIB_TEMP_TARGET,
)

ACPI_CALL = Path("/proc/acpi/call")


def available() -> bool:
    return ACPI_CALL.exists()


def encode_alib(params: list[tuple[int, int]]) -> bytes:
    """Build the ALIB 0x0C payload: length_le16 + (cmd, value_le32)*n."""
    body = bytearray()
    length = 2
    for cmd, value in params:
        length += 5
        body.append(cmd & 0xFF)
        body.extend(int(value).to_bytes(4, "little", signed=False))
    return length.to_bytes(2, "little", signed=False) + body


def format_call(payload: bytes) -> str:
    return f"\\_SB.ALIB 0x0c b{payload.hex()}"


def tdp_params(tdp_w: int, tctl_c: int, boost: bool, limits=None) -> list[tuple[int, int]]:
    if limits is not None:
        stapm = limits.stapm_w * 1000
        fast = limits.fast_w * 1000
        slow = limits.slow_w * 1000
        skin = limits.skin_w * 1000
        tctl_c = limits.tctl_c
        slow_time = limits.slow_time
        stapm_time = limits.stapm_time
    else:
        stapm = tdp_w * 1000
        if boost:
            fast = int(tdp_w * (32 / 28) * 1000)
            slow = min((tdp_w + 2) * 1000, fast)
        else:
            fast = stapm
            slow = stapm
        skin = stapm
        slow_time = 10
        stapm_time = 100
    return [
        (ALIB_STAPM_LIMIT, stapm),
        (ALIB_FAST_LIMIT, fast),
        (ALIB_SLOW_LIMIT, slow),
        (ALIB_SKIN_LIMIT, skin),
        (ALIB_TEMP_TARGET, tctl_c),
        (ALIB_SLOW_TIME, slow_time),
        (ALIB_STAPM_TIME, stapm_time),
    ]


def apply_tdp(tdp_w: int, tctl_c: int, boost: bool, limits=None) -> str:
    if not available():
        raise RuntimeError("acpi_call is not loaded (/proc/acpi/call missing)")
    payload = encode_alib(tdp_params(tdp_w, tctl_c, boost, limits=limits))
    cmd = format_call(payload)
    ACPI_CALL.write_bytes(cmd.encode())
    return cmd
