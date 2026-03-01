import os
import time
import json
import subprocess
from pathlib import Path

from flask import Flask, request, abort, redirect, url_for, render_template, send_from_directory, flash
from werkzeug.utils import secure_filename

from PIL import Image, ImageOps

# ---- Hard path to avoid "~" ambiguity (root vs modernproteus) ----
MNEMO_DIR = Path("/home/modernproteus/mnemopane")
STATE_DIR = MNEMO_DIR / "state"
PHOTO_ORIG = MNEMO_DIR / "photos" / "original"
PHOTO_PROC = MNEMO_DIR / "photos" / "processed"
THUMB_DIR = PHOTO_PROC / "thumbs"

TOKEN_FILE = STATE_DIR / "admin_token.txt"
ADMIN_UNTIL_FILE = STATE_DIR / "admin_enabled_until.txt"
SETTINGS_FILE = STATE_DIR / "settings.json"
PLAYLIST_FILE = STATE_DIR / "playlist.json"
LAST_DISPLAY_FILE = STATE_DIR / "last_display.json"
AP_STATE_FILE = STATE_DIR / "ap_enabled.txt"
WIFI_SCAN_CACHE = STATE_DIR / "wifi_scan.json"

DEFAULT_SETTINGS = {
	"mode": "static",           # static | slideshow
	"interval_min": 60,         # slideshow interval
	"current_image": ""         # filename (optional)
}

UPLOAD_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
THUMB_SIZE = (360, 240)

AP_URL_HINT = "http://192.168.4.1:8080/"
AP_SSID = "MnemoPane-Setup"
AP_CON_NAME = "mnemoPane-AP"   # NetworkManager connection name

app = Flask(__name__)
app.secret_key = os.environ.get("MNEMOPANE_FLASK_SECRET", "mnemopane-dev-secret")  # ok for local LAN
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25MB


# ----------------------------
# Helpers
# ----------------------------

def ensure_dirs():
	STATE_DIR.mkdir(parents=True, exist_ok=True)
	PHOTO_ORIG.mkdir(parents=True, exist_ok=True)
	PHOTO_PROC.mkdir(parents=True, exist_ok=True)
	THUMB_DIR.mkdir(parents=True, exist_ok=True)


def load_settings():
	ensure_dirs()
	if not SETTINGS_FILE.exists():
		SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, indent=2))
		return DEFAULT_SETTINGS.copy()
	try:
		data = json.loads(SETTINGS_FILE.read_text())
		if not isinstance(data, dict):
			return DEFAULT_SETTINGS.copy()
		merged = DEFAULT_SETTINGS.copy()
		merged.update(data)
		return merged
	except Exception:
		return DEFAULT_SETTINGS.copy()


def save_settings(s: dict):
	ensure_dirs()
	SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def load_playlist():
	ensure_dirs()
	if not PLAYLIST_FILE.exists():
		return []
	try:
		data = json.loads(PLAYLIST_FILE.read_text())
		return data if isinstance(data, list) else []
	except Exception:
		return []


def save_playlist(items: list):
	ensure_dirs()
	PLAYLIST_FILE.write_text(json.dumps(items, indent=2))


def list_images():
	ensure_dirs()
	files = []
	for p in sorted(PHOTO_ORIG.iterdir(), key=lambda x: x.name.lower()):
		if p.is_file() and p.suffix.lower() in UPLOAD_EXTS:
			files.append(p)
	return files


def read_token():
	if not TOKEN_FILE.exists():
		abort(500, "Admin token missing. Create ~/mnemopane/state/admin_token.txt")
	return TOKEN_FILE.read_text().strip()


def admin_enabled() -> bool:
	try:
		until = float(ADMIN_UNTIL_FILE.read_text().strip())
		return time.time() < until
	except Exception:
		return False


def require_access():
	# allow health endpoint without token
	if request.path == "/health":
		return

	# Admin time window gate
	if not admin_enabled():
		abort(404)

	# Token gate (?t= or header)
	token = read_token()
	supplied = request.args.get("t") or request.headers.get("X-Admin-Token")
	if supplied != token:
		abort(403)


def touch_refresh_flag(reason: str = ""):
	"""
	One-way signal to the display loop:
	- write timestamp
	- optional reason for debugging
	"""
	ensure_dirs()
	payload = {"ts": time.time(), "reason": reason}
	(STATE_DIR / "refresh_now.flag").write_text(json.dumps(payload))


def safe_int(v, default=60, lo=15, hi=24 * 60):
	try:
		n = int(v)
		return max(lo, min(hi, n))
	except Exception:
		return default


def read_last_display():
	if not LAST_DISPLAY_FILE.exists():
		return None
	try:
		return json.loads(LAST_DISPLAY_FILE.read_text())
	except Exception:
		return None


def now_iso():
	return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# ----------------------------
# Thumbnails
# ----------------------------

def thumb_path_for(filename: str) -> Path:
	base = Path(filename).name
	return THUMB_DIR / f"{base}.jpg"


def ensure_thumb(original_path: Path) -> Path:
	ensure_dirs()
	tpath = thumb_path_for(original_path.name)

	try:
		if tpath.exists() and tpath.stat().st_mtime >= original_path.stat().st_mtime:
			return tpath
	except Exception:
		pass

	try:
		img = Image.open(original_path)
		img = ImageOps.exif_transpose(img)  # fix orientation
		img = img.convert("RGB")
		img = ImageOps.fit(img, THUMB_SIZE, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
		img.save(tpath, "JPEG", quality=84, optimize=True)
	except Exception:
		# if thumb fails, we just won't have it
		pass

	return tpath


# ----------------------------
# NetworkManager helpers
# ----------------------------

def run_cmd(args, timeout=10) -> tuple[int, str]:
	"""
	Returns (returncode, stdout+stderr)
	"""
	try:
		p = subprocess.run(
			args,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			text=True,
			timeout=timeout,
			check=False
		)
		return p.returncode, (p.stdout or "").strip()
	except Exception as e:
		return 999, str(e)


def nmcli(args, sudo=False, timeout=15) -> tuple[int, str]:
	base = ["nmcli"]
	if sudo:
		base = ["sudo", "nmcli"]
	return run_cmd(base + args, timeout=timeout)


def wifi_status() -> dict:
	"""
	Returns dict with:
	  connected: bool
	  ssid: str|None
	  ip: str|None
	  ap_active: bool
	  ap_ip: str|None
	"""
	# Wi-Fi connected?
	rc, out = nmcli(["-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"])
	connected = False
	if rc == 0:
		for line in out.splitlines():
			parts = line.split(":")
			if len(parts) >= 3 and parts[1] == "wifi" and parts[2] == "connected":
				connected = True
				break

	# SSID
	ssid = None
	rc, out = nmcli(["-t", "-f", "ACTIVE,SSID", "dev", "wifi"])
	if rc == 0:
		for line in out.splitlines():
			if line.startswith("yes:"):
				ssid = line.split(":", 1)[1]
				break

	# Current wlan0 IP
	ip = None
	rc, out = run_cmd(["bash", "-lc", "ip -4 addr show dev wlan0 | awk '/inet /{print $2}' | head -n1"])
	if rc == 0 and out:
		ip = out.strip()

	# Is our AP connection active?
	ap_active = False
	rc, out = nmcli(["-t", "-f", "NAME", "con", "show", "--active"])
	if rc == 0:
		ap_active = any(line.strip() == AP_CON_NAME for line in out.splitlines())

	ap_ip = None
	if ap_active:
		# If AP is active, we expect 192.168.4.1/24
		ap_ip = ip

	return {
		"connected": connected,
		"ssid": ssid,
		"ip": ip,
		"ap_active": ap_active,
		"ap_ip": ap_ip,
	}


def wifi_scan(force=False) -> dict:
	"""
	Returns cached scan results unless force=True.
	Cache file: state/wifi_scan.json
	"""
	ensure_dirs()
	if WIFI_SCAN_CACHE.exists() and not force:
		try:
			data = json.loads(WIFI_SCAN_CACHE.read_text())
			if isinstance(data, dict) and (time.time() - float(data.get("ts", 0))) < 30:
				return data
		except Exception:
			pass

	# Ask NM to rescan, then list
	nmcli(["dev", "wifi", "rescan"], timeout=20)

	rc, out = nmcli(["-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"], timeout=20)
	nets = []
	if rc == 0:
		seen = set()
		for line in out.splitlines():
			parts = line.split(":")
			if not parts:
				continue
			ssid = (parts[0] or "").strip()
			if not ssid:
				continue
			if ssid in seen:
				continue
			seen.add(ssid)
			signal = parts[1].strip() if len(parts) > 1 else ""
			sec = parts[2].strip() if len(parts) > 2 else ""
			nets.append({"ssid": ssid, "signal": signal, "security": sec})

	nets.sort(key=lambda x: int(x["signal"] or "0"), reverse=True)

	payload = {"ts": time.time(), "iso": now_iso(), "networks": nets}
	WIFI_SCAN_CACHE.write_text(json.dumps(payload, indent=2))
	return payload


def wifi_connect(ssid: str, password: str | None) -> tuple[bool, str]:
	ssid = (ssid or "").strip()
	if not ssid:
		return False, "SSID missing"

	args = ["dev", "wifi", "connect", ssid]
	if password:
		args += ["password", password]

	# requires sudoers rule: modernproteus NOPASSWD: /usr/bin/nmcli
	rc, out = nmcli(args, sudo=True, timeout=40)
	if rc == 0:
		return True, out or "Connected"
	return False, out or "Failed"


def ap_enable() -> tuple[bool, str]:
	rc, out = nmcli(["con", "up", AP_CON_NAME], sudo=True, timeout=30)
	if rc == 0:
		AP_STATE_FILE.write_text(now_iso())
		return True, out or "Hotspot enabled"
	return False, out or "Failed"


def ap_disable() -> tuple[bool, str]:
	rc, out = nmcli(["con", "down", AP_CON_NAME], sudo=True, timeout=20)
	if rc == 0:
		if AP_STATE_FILE.exists():
			AP_STATE_FILE.unlink()
		return True, out or "Hotspot disabled"
	return False, out or "Failed"


@app.before_request
def _gate():
	require_access()


# ----------------------------
# Routes
# ----------------------------

@app.get("/health")
def health():
	return {"ok": True, "admin_enabled": admin_enabled()}


@app.get("/")
def index():
	s = load_settings()
	all_files = list_images()
	existing_names = [p.name for p in all_files]
	existing_set = set(existing_names)

	# Keep playlist clean, but DO NOT churn it unless needed
	raw_playlist = load_playlist()
	cleaned_playlist = [f for f in raw_playlist if f in existing_set]
	if cleaned_playlist != raw_playlist:
		save_playlist(cleaned_playlist)
	playlist = cleaned_playlist

	token = request.args.get("t", "")
	cards = []
	pl_set = set(playlist)
	for p in all_files:
		ensure_thumb(p)
		cards.append({
			"name": p.name,
			"thumb_url": url_for("thumb", filename=p.name, t=token),
			"in_playlist": p.name in pl_set,
		})

	enabled_until = ADMIN_UNTIL_FILE.read_text().strip() if ADMIN_UNTIL_FILE.exists() else ""
	last_display = read_last_display()
	net = wifi_status()

	scan = None
	if request.args.get("scan") == "1":
		scan = wifi_scan(force=True)
	else:
		# cached (fast) if available
		try:
			scan = wifi_scan(force=False)
		except Exception:
			scan = {"ts": 0, "iso": "", "networks": []}

	return render_template(
		"index.html",
		settings=s,
		enabled_until=enabled_until,
		files=existing_names,
		cards=cards,
		playlist=playlist,
		last_display=last_display,
		net=net,
		scan=scan,
		ap_url_hint=AP_URL_HINT,
		ap_ssid=AP_SSID,
		token=token,
	)


@app.get("/thumb/<path:filename>")
def thumb(filename):
	p = PHOTO_ORIG / Path(filename).name
	if not p.exists():
		abort(404)
	ensure_thumb(p)
	tpath = thumb_path_for(p.name)
	if not tpath.exists():
		return send_from_directory(PHOTO_ORIG, p.name)
	return send_from_directory(THUMB_DIR, tpath.name)


@app.post("/settings")
def update_settings():
	s = load_settings()
	mode = (request.form.get("mode") or s["mode"]).strip().lower()
	interval_min = safe_int(request.form.get("interval_min", s["interval_min"]), default=s["interval_min"])
	current_image = (request.form.get("current_image") or s.get("current_image", "")).strip()

	if mode not in ("static", "slideshow"):
		abort(400, "invalid mode")

	if current_image and not (PHOTO_ORIG / current_image).exists():
		current_image = ""

	changed = (
		s.get("mode") != mode or
		int(s.get("interval_min", 0)) != interval_min or
		s.get("current_image", "") != current_image
	)

	s["mode"] = mode
	s["interval_min"] = interval_min
	s["current_image"] = current_image
	save_settings(s)

	# We do NOT auto-refresh on settings save (keeps e-paper calm).
	# User can hit "Refresh Display Now" if they really want it.
	return redirect(url_for("index", t=request.args.get("t")))


@app.post("/upload")
def upload():
	if "file" not in request.files:
		abort(400, "No file part")
	f = request.files["file"]
	if not f.filename:
		abort(400, "No selected file")

	name = secure_filename(f.filename)
	ext = os.path.splitext(name)[1].lower()
	if ext not in UPLOAD_EXTS:
		abort(400, f"Unsupported file type {ext}")

	ensure_dirs()
	path = PHOTO_ORIG / name
	f.save(path)

	ensure_thumb(path)
	flash("Uploaded successfully.")

	# No auto-refresh here either.
	return redirect(url_for("index", t=request.args.get("t")))


@app.post("/refresh")
def refresh_now():
	touch_refresh_flag("manual_refresh")
	flash("Refresh signal sent to display loop.")
	return redirect(url_for("index", t=request.args.get("t")))


@app.post("/close")
def close_admin():
	if ADMIN_UNTIL_FILE.exists():
		ADMIN_UNTIL_FILE.unlink()
	return "Admin closed."


# ----------------------------
# Delete image (new)
# ----------------------------

@app.post("/delete")
def delete_image():
	fname = (request.form.get("filename") or "").strip()
	if not fname:
		return redirect(url_for("index", t=request.args.get("t")))

	img_path = PHOTO_ORIG / fname
	if not img_path.exists():
		flash("File not found.")
		return redirect(url_for("index", t=request.args.get("t")))

	# What is currently displayed?
	last_display = read_last_display() or {}
	displayed_now = (last_display.get("filename") or "").strip()

	# Remove from playlist
	playlist = [f for f in load_playlist() if f != fname]
	save_playlist(playlist)

	# If static selected image was deleted, clear it
	s = load_settings()
	if s.get("current_image") == fname:
		s["current_image"] = ""
		save_settings(s)

	# Remove original and thumb
	try:
		img_path.unlink()
	except Exception:
		pass

	try:
		tpath = thumb_path_for(fname)
		if tpath.exists():
			tpath.unlink()
	except Exception:
		pass

	# IMPORTANT:
	# Only signal a refresh if we deleted the image that is actually on-screen.
	if fname == displayed_now:
		touch_refresh_flag("deleted_currently_displayed")
		flash("Deleted currently displayed image — refresh requested.")
	else:
		flash("Deleted image.")

	return redirect(url_for("index", t=request.args.get("t")))


# ----------------------------
# Playlist controls
# ----------------------------

@app.post("/playlist/add")
def playlist_add():
	fname = (request.form.get("filename") or "").strip()
	if not fname or not (PHOTO_ORIG / fname).exists():
		return redirect(url_for("index", t=request.args.get("t")))

	playlist = load_playlist()
	if fname not in playlist:
		playlist.append(fname)
		save_playlist(playlist)
		flash("Added to playlist.")

	return redirect(url_for("index", t=request.args.get("t")))


@app.post("/playlist/remove")
def playlist_remove():
	fname = (request.form.get("filename") or "").strip()
	playlist = [f for f in load_playlist() if f != fname]
	save_playlist(playlist)
	flash("Removed from playlist.")
	return redirect(url_for("index", t=request.args.get("t")))


@app.post("/playlist/move")
def playlist_move():
	fname = (request.form.get("filename") or "").strip()
	direction = (request.form.get("direction") or "").strip().lower()

	playlist = load_playlist()
	if fname not in playlist:
		return redirect(url_for("index", t=request.args.get("t")))

	idx = playlist.index(fname)
	if direction == "up" and idx > 0:
		playlist[idx - 1], playlist[idx] = playlist[idx], playlist[idx - 1]
		flash("Moved up.")
	elif direction == "down" and idx < len(playlist) - 1:
		playlist[idx + 1], playlist[idx] = playlist[idx], playlist[idx + 1]
		flash("Moved down.")

	save_playlist(playlist)
	return redirect(url_for("index", t=request.args.get("t")))


# ----------------------------
# Quick static selection
# ----------------------------

@app.post("/static/set")
def static_set():
	fname = (request.form.get("filename") or "").strip()
	if not fname or not (PHOTO_ORIG / fname).exists():
		return redirect(url_for("index", t=request.args.get("t")))

	s = load_settings()
	s["mode"] = "static"
	s["current_image"] = fname
	save_settings(s)

	# This one DOES feel like it should change the display; still user-controlled,
	# but we’ll do a refresh because it’s an explicit action.
	touch_refresh_flag("set_static_image")
	flash("Static image selected. Refresh requested.")

	return redirect(url_for("index", t=request.args.get("t")))


# ----------------------------
# Wi-Fi setup
# ----------------------------

@app.post("/wifi/scan")
def wifi_scan_route():
	wifi_scan(force=True)
	flash("Wi-Fi scan updated.")
	return redirect(url_for("index", t=request.args.get("t"), scan=1))


@app.post("/wifi/connect")
def wifi_connect_route():
	ssid = (request.form.get("ssid") or "").strip()
	password = (request.form.get("password") or "").strip() or None

	ok, msg = wifi_connect(ssid, password)
	flash(msg if msg else ("Connected." if ok else "Failed."))

	return redirect(url_for("index", t=request.args.get("t")))


@app.post("/wifi/hotspot/on")
def wifi_hotspot_on():
	ok, msg = ap_enable()
	flash(msg)
	return redirect(url_for("index", t=request.args.get("t")))


@app.post("/wifi/hotspot/off")
def wifi_hotspot_off():
	ok, msg = ap_disable()
	flash(msg)
	return redirect(url_for("index", t=request.args.get("t")))


if __name__ == "__main__":
	app.run(host="0.0.0.0", port=8080)