#!/usr/bin/env bash
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo $0"
  exit 1
fi
systemctl disable --now maxctl.service 2>/dev/null || true
rm -f /etc/systemd/system/maxctl.service
rm -f /lib/systemd/system-sleep/maxctl
rm -f /usr/local/bin/maxctl /usr/local/bin/maxctl-gui /usr/local/bin/maxctld
rm -f /usr/share/applications/maxctl.desktop
rm -f /usr/share/icons/hicolor/scalable/apps/maxctl.svg
rm -rf /opt/maxctl
systemctl daemon-reload
echo "maxctl removed. Config left in /etc/maxctl and ~/.config/maxctl"
