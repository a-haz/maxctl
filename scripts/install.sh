#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${PREFIX:-/opt/maxctl}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo $0"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  python3 \
  python3-gi \
  gir1.2-gtk-4.0 \
  gir1.2-adw-1 \
  power-profiles-daemon \
  cmake \
  make \
  g++ \
  pkg-config \
  libpci-dev \
  git \
  dkms \
  "linux-headers-$(uname -r)" \
  acpi-call-dkms || apt-get install -y --no-install-recommends \
  python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 power-profiles-daemon \
  cmake make g++ pkg-config libpci-dev git dkms "linux-headers-$(uname -r)"

modprobe acpi_call 2>/dev/null || true
if [[ ! -e /proc/acpi/call ]]; then
  echo "acpi_call did not load. Building RyzenAdj as the TDP fallback."
fi

rm -rf "${PREFIX}"
mkdir -p "${PREFIX}"
cp -a "${ROOT}/maxctl" "${ROOT}/bin" "${ROOT}/data" "${PREFIX}/"
chmod 755 "${PREFIX}/bin/"*

if [[ ! -x "${PREFIX}/bin/ryzenadj" ]]; then
  tmp="$(mktemp -d)"
  git clone --depth 1 --branch v0.19.0 https://github.com/FlyGoat/RyzenAdj.git "${tmp}/RyzenAdj" \
    || git clone --depth 1 https://github.com/FlyGoat/RyzenAdj.git "${tmp}/RyzenAdj"
  cmake -S "${tmp}/RyzenAdj" -B "${tmp}/RyzenAdj/build"
  cmake --build "${tmp}/RyzenAdj/build" -j"$(nproc)"
  install -m 0755 "${tmp}/RyzenAdj/build/ryzenadj" "${PREFIX}/bin/ryzenadj"
  rm -rf "${tmp}"
fi

install -d /usr/local/bin
ln -sfn "${PREFIX}/bin/maxctl" /usr/local/bin/maxctl
ln -sfn "${PREFIX}/bin/maxctl-gui" /usr/local/bin/maxctl-gui
ln -sfn "${PREFIX}/bin/maxctld" /usr/local/bin/maxctld

install -Dm644 "${PREFIX}/data/maxctl.desktop" /usr/share/applications/maxctl.desktop
install -Dm644 "${PREFIX}/data/maxctl.svg" /usr/share/icons/hicolor/scalable/apps/maxctl.svg
install -Dm644 "${PREFIX}/data/maxctl.service" /etc/systemd/system/maxctl.service
install -Dm755 "${PREFIX}/data/maxctl-sleep" /lib/systemd/system-sleep/maxctl
mkdir -p /etc/maxctl /var/lib/maxctl /run/maxctl

systemctl daemon-reload
systemctl enable --now maxctl.service
gtk-update-icon-cache -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
update-desktop-database /usr/share/applications >/dev/null 2>&1 || true

echo
echo "maxctl installed."
echo "  GUI:    maxctl-gui"
echo "  CLI:    maxctl status"
echo "  Helper: systemctl status maxctl"
if [[ -e /proc/acpi/call ]]; then
  echo "  TDP:    ACPI ALIB via acpi_call"
else
  echo "  TDP:    RyzenAdj fallback (add iomem=relaxed if it cannot talk to the SMU)"
fi
