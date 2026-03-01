#!/usr/bin/env bash
set -euo pipefail

UNITS=(
  mnemopane-admin.service
  mnemopane-netmode.service
  mnemopane-bleprov.service
  mnemopane-display.service
  mnemopane-buttons.service
)

for u in "${UNITS[@]}"; do
  sudo systemctl disable "$u" || true
  sudo rm -f "/etc/systemd/system/$u"
done

sudo systemctl daemon-reload

echo "MnemoPane services removed."