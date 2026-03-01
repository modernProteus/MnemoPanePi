#!/usr/bin/env bash
systemctl list-units --type=service --all | grep -i mnemopane || true