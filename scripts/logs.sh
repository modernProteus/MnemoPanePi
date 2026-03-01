#!/usr/bin/env bash
UNIT="${1:-mnemopane-netmode.service}"
sudo journalctl -u "$UNIT" -n 100 --no-pager