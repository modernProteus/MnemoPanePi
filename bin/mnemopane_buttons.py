#!/usr/bin/env python3
import time
from pathlib import Path

import gpiod

GPIO = 24               # Button D (BCM)
HOLD_SECONDS = 1.5      # hold to enable admin
ADMIN_MINUTES = 15
POLL_SEC = 0.02

STATE_DIR = Path.home() / "mnemopane/state"
LEASE_FILE = STATE_DIR / "admin_enabled_until.txt"

def write_lease():
	until = time.time() + ADMIN_MINUTES * 60
	STATE_DIR.mkdir(parents=True, exist_ok=True)
	LEASE_FILE.write_text(str(until))
	print(f"[btn] Admin enabled for {ADMIN_MINUTES} minutes (until {until}).", flush=True)

def main():
	chip = gpiod.Chip("/dev/gpiochip0")

	# libgpiod v2 Python API: request_lines + LineSettings lives in gpiod.line
	# We'll keep it resilient across minor API variations.
	try:
		from gpiod.line import Direction, Bias, LineSettings
		settings = LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP)
		req = chip.request_lines(consumer="mnemo-admin-btn", config={GPIO: settings})
		get_value = lambda: req.get_value(GPIO)
	except Exception:
		# Fallback: request without bias if this build doesn't support it cleanly
		req = chip.request_lines(consumer="mnemo-admin-btn", config={GPIO: gpiod.LineSettings()})
		get_value = lambda: req.get_value(GPIO)

	print(f"[btn] Listening on GPIO{GPIO} (polling). Hold {HOLD_SECONDS}s to enable admin.", flush=True)

	pressed_since = None
	fired = False

	while True:
		# Button is typically active-low with pull-up: 0 == pressed, 1 == released
		try:
			v = get_value()
		except Exception as e:
			print(f"[btn] ERROR reading GPIO{GPIO}: {e}", flush=True)
			time.sleep(1)
			continue

		now = time.time()

		if v == 0:  # pressed
			if pressed_since is None:
				pressed_since = now
				fired = False
			elif (not fired) and (now - pressed_since >= HOLD_SECONDS):
				write_lease()
				fired = True
		else:       # released
			pressed_since = None
			fired = False

		time.sleep(POLL_SEC)

if __name__ == "__main__":
	main()
