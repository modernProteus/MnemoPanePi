#!/usr/bin/env python3
"""
MnemoPane display loop for Raspberry Pi + Pimoroni Inky Impression (E673 / Spectra 6).

Now supports:
- Two albums: photos/original/landscape and photos/original/portrait
- Album-specific playlists: state/playlist_landscape.json, state/playlist_portrait.json
- Crop modes: auto/fill/fit (auto picks best defaults per album)
- Gentle “e-paper friendly” filters to improve contrast + clarity
- Calm refresh logic: only updates the panel when something actually requires it
"""

import json
import time
from pathlib import Path
from datetime import datetime

from inky.auto import auto
from PIL import Image, ImageOps, ImageEnhance, ImageFilter

HOME = Path.home()
MNEMO = HOME / "mnemopane"
STATE = MNEMO / "state"
ORIG = MNEMO / "photos" / "original"

SETTINGS_FILE = STATE / "settings.json"
LAST_DISPLAY_FILE = STATE / "last_display.json"

REFRESH_FLAG = STATE / "refresh_now.flag"

ALBUMS = ("landscape", "portrait")

DEFAULT_SETTINGS = {
	"mode": "static",
	"interval_min": 60,
	"album": "landscape",
	"current_image": "",
	"crop_mode": "auto",
	"crop_y": 0.35,
	"filters": {
		"autocontrast_cutoff": 2,
		"color": 1.08,
		"contrast": 1.10,
		"sharpness": 1.15,
		"unsharp_radius": 1.2,
		"unsharp_percent": 120,
		"unsharp_threshold": 3
	}
}

UPLOAD_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# e-paper friendly: don’t slideshow faster than 15 minutes unless forced
MIN_INTERVAL_MIN = 15


def playlist_file(album: str) -> Path:
	return STATE / f"playlist_{album}.json"


def slideshow_index_file(album: str) -> Path:
	return STATE / f"slideshow_index_{album}.txt"


def _read_json(path: Path, default):
	try:
		if not path.exists():
			return default
		return json.loads(path.read_text())
	except Exception:
		return default


def _write_json(path: Path, obj):
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(obj, indent=2))


def ensure_dirs():
	STATE.mkdir(parents=True, exist_ok=True)
	ORIG.mkdir(parents=True, exist_ok=True)
	for a in ALBUMS:
		(ORIG / a).mkdir(parents=True, exist_ok=True)


def load_settings():
	ensure_dirs()
	if not SETTINGS_FILE.exists():
		_write_json(SETTINGS_FILE, DEFAULT_SETTINGS)
		return dict(DEFAULT_SETTINGS)
	s = _read_json(SETTINGS_FILE, dict(DEFAULT_SETTINGS))

	changed = False
	for k, v in DEFAULT_SETTINGS.items():
		if k not in s:
			s[k] = v
			changed = True
	if "filters" not in s or not isinstance(s["filters"], dict):
		s["filters"] = dict(DEFAULT_SETTINGS["filters"])
		changed = True
	else:
		for k, v in DEFAULT_SETTINGS["filters"].items():
			if k not in s["filters"]:
				s["filters"][k] = v
				changed = True
	if s.get("album") not in ALBUMS:
		s["album"] = "landscape"
		changed = True
	if s.get("crop_mode") not in ("auto", "fill", "fit"):
		s["crop_mode"] = "auto"
		changed = True
	try:
		s["crop_y"] = float(s.get("crop_y", 0.35))
	except Exception:
		s["crop_y"] = 0.35
		changed = True
	s["crop_y"] = max(0.0, min(1.0, s["crop_y"]))

	if changed:
		_write_json(SETTINGS_FILE, s)
	return s


def list_images(album: str):
	d = ORIG / album
	if not d.exists():
		return []
	out = []
	for p in sorted(d.iterdir()):
		if p.is_file() and p.suffix.lower() in UPLOAD_EXTS:
			out.append(p.name)
	return out


def load_playlist(album: str):
	return _read_json(playlist_file(album), [])


def save_playlist(album: str, pl):
	_write_json(playlist_file(album), pl)


def load_index(album: str) -> int:
	p = slideshow_index_file(album)
	try:
		return int(p.read_text().strip())
	except Exception:
		return 0


def save_index(album: str, idx: int):
	slideshow_index_file(album).write_text(str(idx))


def pop_refresh_flag() -> bool:
	if REFRESH_FLAG.exists():
		try:
			REFRESH_FLAG.unlink()
		except Exception:
			pass
		return True
	return False


def album_defaults(album: str):
	# Opinionated defaults:
	# landscape: fill; portrait: fit
	if album == "portrait":
		return ("fit", 0.35)
	return ("fill", 0.35)


def render_to_panel(img: Image.Image, target_w: int, target_h: int,
					crop_mode: str = "fill",
					crop_y: float = 0.35) -> Image.Image:
	img = ImageOps.exif_transpose(img).convert("RGB")

	if crop_mode == "fit":
		fitted = ImageOps.contain(img, (target_w, target_h))
		canvas = Image.new("RGB", (target_w, target_h), (255, 255, 255))
		x = (target_w - fitted.width) // 2
		y = (target_h - fitted.height) // 2
		canvas.paste(fitted, (x, y))
		return canvas

	crop_y = max(0.0, min(1.0, float(crop_y)))
	return ImageOps.fit(
		img,
		(target_w, target_h),
		method=Image.Resampling.LANCZOS,
		centering=(0.5, crop_y),
	)


def apply_epaper_filters(img: Image.Image, filters: dict | None) -> Image.Image:
	if not filters:
		return img

	try:
		cutoff = int(filters.get("autocontrast_cutoff", 2))
	except Exception:
		cutoff = 2
	if cutoff > 0:
		img = ImageOps.autocontrast(img, cutoff=cutoff)

	def _f(key, default):
		try:
			return float(filters.get(key, default))
		except Exception:
			return float(default)

	color = _f("color", 1.08)
	if abs(color - 1.0) > 0.01:
		img = ImageEnhance.Color(img).enhance(color)

	contrast = _f("contrast", 1.10)
	if abs(contrast - 1.0) > 0.01:
		img = ImageEnhance.Contrast(img).enhance(contrast)

	sharp = _f("sharpness", 1.15)
	if abs(sharp - 1.0) > 0.01:
		img = ImageEnhance.Sharpness(img).enhance(sharp)

	try:
		radius = float(filters.get("unsharp_radius", 1.2))
		percent = int(filters.get("unsharp_percent", 120))
		threshold = int(filters.get("unsharp_threshold", 3))
	except Exception:
		radius, percent, threshold = 1.2, 120, 3

	if percent > 0:
		img = img.filter(ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=threshold))

	return img


def choose_next_slideshow(album: str, imgs: list, pl: list, forced_advance: bool) -> str:
	"""
	Return the filename to display for slideshow.
	If forced_advance is True, move to next item immediately.
	"""
	if not pl:
		# seed playlist to current library order
		pl = imgs[:]
		save_playlist(album, pl)

	# remove missing
	pl = [x for x in pl if x in set(imgs)]
	save_playlist(album, pl)
	if not pl:
		return ""

	idx = load_index(album)
	idx = idx % len(pl)

	if forced_advance:
		idx = (idx + 1) % len(pl)
		save_index(album, idx)

	return pl[idx]


def pick_static(album: str, imgs: list, current_image: str) -> str:
	if current_image and current_image in set(imgs):
		return current_image
	return imgs[0] if imgs else ""


def write_last_display(album: str, filename: str):
	_write_json(LAST_DISPLAY_FILE, {
		"album": album,
		"filename": filename,
		"ts": datetime.now().isoformat(timespec="seconds")
	})


def main():
	inky = auto(ask_user=False, verbose=True)
	w, h = inky.WIDTH, inky.HEIGHT
	print(f"[display] Detected panel {w}x{h}")

	last_render_key = None
	last_show_time = 0.0
	last_tick_time = 0.0

	# On boot, allow immediate render of whatever settings say
	first = True

	while True:
		s = load_settings()
		album = s.get("album", "landscape")
		if album not in ALBUMS:
			album = "landscape"

		mode = s.get("mode", "static")
		interval_min = int(s.get("interval_min", 60))
		interval_min = max(1, interval_min)
		interval_min = max(interval_min, MIN_INTERVAL_MIN)

		imgs = list_images(album)

		forced = pop_refresh_flag()  # only true when admin explicitly asked for refresh/settings change
		now = time.time()

		# Determine whether slideshow tick is due
		tick_due = False
		if mode == "slideshow":
			if last_tick_time == 0.0:
				last_tick_time = now
			if (now - last_tick_time) >= (interval_min * 60):
				tick_due = True
				last_tick_time = now

		# Choose target image
		target = ""
		playlist = load_playlist(album)

		if mode == "slideshow":
			# forced refresh in slideshow advances one step
			target = choose_next_slideshow(album, imgs, playlist, forced_advance=forced or tick_due)
		else:
			target = pick_static(album, imgs, s.get("current_image", ""))

		# Resolve crop defaults
		crop_mode = s.get("crop_mode", "auto")
		crop_y = float(s.get("crop_y", 0.35))
		if crop_mode == "auto":
			crop_mode, default_y = album_defaults(album)
			crop_y = default_y

		# Build a stable "render key" so we only update when something *meaningful* changes
		render_key = json.dumps({
			"album": album,
			"filename": target,
			"mode": mode,
			"crop_mode": crop_mode,
			"crop_y": round(crop_y, 3),
			"filters": s.get("filters", {}),
			"forced": bool(forced),
			"tick": bool(tick_due),
		}, sort_keys=True)

		should_update = first or forced or tick_due or (render_key != last_render_key)

		# Extra calm guard: don’t hammer show() due to any bug loop
		# allow forced updates any time
		if should_update and not forced:
			# if we updated very recently, chill a bit
			if (now - last_show_time) < 5.0:
				should_update = False

		print(f"[display] target={album}/{target} | mode={mode} | forced={forced} | tick_due={tick_due} | update={should_update}")

		if should_update and target:
			try:
				src = ORIG / album / target
				with Image.open(src) as im:
					im = render_to_panel(im, w, h, crop_mode=crop_mode, crop_y=crop_y)
					im = apply_epaper_filters(im, s.get("filters", {}))

				inky.set_image(im)
				inky.show()

				write_last_display(album, target)
				last_render_key = render_key
				last_show_time = time.time()
				first = False
				print(f"[display] Displayed {album}/{target} at {datetime.now().isoformat(timespec='seconds')}")
			except Exception as e:
				print(f"[display] ERROR rendering {album}/{target}: {e}")

		# Sleep: keep loop responsive to admin refresh flag, but not busy
		time.sleep(2.0)


if __name__ == "__main__":
	main()