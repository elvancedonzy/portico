#!/usr/bin/env python3
"""
Updated Seam Dashboard addon server with /api/ha-summary endpoint.
The new endpoint aggregates code data into a HA-friendly JSON shape:
{
  "rooms": {
    "Rosehill Pl Room 1": {
      "device_id": "...",
      "online": true,
      "battery": 0.85,
      "locked": true,
      "active_guest": {
        "name": "John Smith",
        "code": "1234",
        "starts_at": "2026-05-14T16:00:00Z",
        "ends_at": "2026-05-15T14:30:00Z"
      },
      "next_guest": { ... } | null,
      "occupied_now": true,
      "minutes_until_checkin": 0,
      "minutes_until_checkout": 245
    },
    ...
  },
  "front_door": { same shape },
  "summary": {
    "total_codes": 24,
    "occupied_rooms": 1,
    "todays_checkins": [...],
    "todays_checkouts": [...]
  }
}
"""
import json
import re
import subprocess
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

from shared_config import (
    BASEMENT_DEVICE_ID,
    SKIP_CODES,
    ROOM_TO_DOOR_PREFIX,
    DOOR_TO_ROOM_PREFIX,
    SITE_NAME,
)

PORT = 8765
SEAM_API = "https://connect.getseam.com"

SYNC_STATUS_FILE = "/data/sync_status.json"
SYNC_LOG_FILE = "/data/seam_sync.log"

ROOM_AC_SWITCHES = {
    "Rosehill Pl Room 1": "switch.room_1_ac",
    "Rosehill Pl Room 2": "switch.room_2_ac",
    "Rosehill Pl Room 3": "switch.room_3_ac",
    "Rosehill Pl Room 3 New": "switch.room_3_ac",
    "Room 3": "switch.room_3_ac",
}

# Cached AC states pushed by HA via POST /api/ac-update
_ac_states = {"room_1": None, "room_2": None, "room_3": None}
PERMANENT_CODES = set(SKIP_CODES)


def redact(s):
    """Strip anything that looks like a Seam API key before it ever reaches a log line."""
    return re.sub(r"seam_[A-Za-z0-9_]{6,}", "seam_***", str(s))

try:
    with open("/data/options.json") as f:
        opts = json.load(f)
    K1 = opts.get("account1_api_key", "")
    K2 = opts.get("account2_api_key", "")
except Exception as e:
    print("Config error:", redact(e), flush=True)
    K1 = ""
    K2 = ""

print("K1 set:", bool(K1), flush=True)
print("K2 set:", bool(K2), flush=True)

MANIFEST = json.dumps({
    "name": f"{SITE_NAME} Locks",
    "short_name": "Locks",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0a0a0c",
    "theme_color": "#0a0a0c",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}],
}).encode()

ICON_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="115" fill="#0a0a0c"/>
<path fill="#5dcaa5" d="M256 128c-49.7 0-90 40.3-90 90v20H148v164h216V238h-18v-20c0-49.7-40.3-90-90-90zm0 36c29.8 0 54 24.2 54 54v20H202v-20c0-29.8 24.2-54 54-54zm0 110c17.7 0 32 14.3 32 32 0 12.9-7.6 24-18.7 29.3V368h-26.6v-32.7C231.6 330 224 318.9 224 306c0-17.7 14.3-32 32-32z"/>
</svg>"""

INJECT = b"""<script>
(function(){
  var base = location.pathname.replace(/\\/+$/, '');
  var orig = window.fetch.bind(window);
  window.fetch = function(url, opts) {
    if (typeof url === 'string' && url.startsWith('/') && !url.startsWith(base)) {
      url = base + url;
    }
    return orig(url, opts);
  };
})();
</script>"""


def tail_lines(path, n=200, max_bytes=131072):
    """Return the last n lines of a (possibly huge) text file without reading it all."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        return data.decode("utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


def seam_request(api_key, method, path, body=None):
    """Call Seam API via curl, return parsed JSON."""
    cmd = [
        "curl", "-s", "-X", method,
        "-H", "Authorization: Bearer " + api_key,
        "-H", "Content-Type: application/json",
        "-H", "Accept: application/json",
        "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "--compressed",
    ]
    if body:
        cmd += ["--data-raw", json.dumps(body)]
    cmd.append(SEAM_API + path)
    result = subprocess.run(cmd, capture_output=True, timeout=25)
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"error": "parse failed", "raw": result.stdout.decode("utf-8", errors="replace")}


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def build_ha_summary():
    """Aggregate everything HA needs into one JSON response."""
    now = datetime.now(timezone.utc)
    devices = {}

    # Get devices from both accounts
    for acct, key in [(1, K1), (2, K2)]:
        if not key:
            continue
        r = seam_request(key, "GET", "/devices/list")
        for d in r.get("devices", []):
            devices[d["device_id"]] = {**d, "_acct": acct, "_key": key}

    rooms = {}
    front_door = None
    basement = {"guest_present": False, "guest_name": ""}
    todays_checkins = []
    todays_checkouts = []
    occupied_count = 0
    total_codes = 0

    for device_id, d in devices.items():
        codes_resp = seam_request(d["_key"], "GET", f"/access_codes/list?device_id={device_id}")
        codes = codes_resp.get("access_codes", []) or []
        total_codes += len(codes)

        # Basement lock: extract guest state, include in activity/counts but not room cards
        if device_id == BASEMENT_DEVICE_ID:
            for c in codes:
                name = (c.get("name") or "").strip()
                if not name or name in PERMANENT_CODES or c.get("type") != "time_bound":
                    continue
                starts = parse_iso(c.get("starts_at"))
                ends = parse_iso(c.get("ends_at"))
                if not starts or not ends:
                    continue
                if starts <= now <= ends:
                    basement = {"guest_present": True, "guest_name": name}
                    occupied_count += 1
                    if starts.date() == now.date():
                        todays_checkins.append({
                            "name": name,
                            "device": "Basement",
                            "time": c.get("starts_at"),
                        })
                    if ends.date() == now.date():
                        todays_checkouts.append({
                            "name": name,
                            "device": "Basement",
                            "time": c.get("ends_at"),
                        })
                    break
            continue

        guest_codes = []
        for c in codes:
            name = c.get("name") or ""
            if not name:
                continue
            # Skip whole-house room copies and permanent codes
            if name.startswith("h~ "):
                continue
            if name in PERMANENT_CODES:
                continue
            if "Airbnb Backup" in name:
                continue
            if c.get("type") != "time_bound":
                continue
            starts = parse_iso(c.get("starts_at"))
            ends = parse_iso(c.get("ends_at"))
            if not ends or ends < now:
                continue
            display_name = name
            if display_name.startswith("s~ "):
                display_name = display_name[3:]
            guest_codes.append({
                "name": display_name,
                "code": c.get("code"),
                "starts_at": c.get("starts_at"),
                "ends_at": c.get("ends_at"),
                "starts_dt": starts,
                "ends_dt": ends,
            })

        guest_codes.sort(key=lambda x: x["starts_dt"] or now)

        active = None
        upcoming = None
        for g in guest_codes:
            if g["starts_dt"] and g["starts_dt"] <= now <= g["ends_dt"]:
                active = g
                break

        for g in guest_codes:
            if g["starts_dt"] and g["starts_dt"] > now:
                upcoming = g
                break

        # Today's check-ins/outs from this device
        for g in guest_codes:
            if g["starts_dt"] and g["starts_dt"].date() == now.date():
                todays_checkins.append({
                    "name": g["name"],
                    "device": d.get("properties", {}).get("name"),
                    "time": g["starts_at"],
                })
            if g["ends_dt"] and g["ends_dt"].date() == now.date():
                todays_checkouts.append({
                    "name": g["name"],
                    "device": d.get("properties", {}).get("name"),
                    "time": g["ends_at"],
                })

        def strip_dt(g):
            if not g:
                return None
            return {k: v for k, v in g.items() if not k.endswith("_dt")}

        props = d.get("properties", {})
        device_data = {
            "device_id": device_id,
            "online": props.get("online", False),
            "battery": props.get("battery", {}).get("level"),
            "locked": props.get("locked"),
            "active_guest": strip_dt(active),
            "next_guest": strip_dt(upcoming),
            "occupied_now": active is not None,
            "minutes_until_checkin": int((upcoming["starts_dt"] - now).total_seconds() / 60) if upcoming and upcoming["starts_dt"] else None,
            "minutes_until_checkout": int((active["ends_dt"] - now).total_seconds() / 60) if active and active["ends_dt"] else None,
            "code_count": len(codes),
        }

        device_name = props.get("name", device_id)
        if d["_acct"] == 1:
            if active:
                occupied_count += 1
            rooms[device_name] = device_data
        else:
            front_door = device_data
            front_door["name"] = device_name

    # Sort today's lists
    todays_checkins.sort(key=lambda x: x["time"] or "")
    todays_checkouts.sort(key=lambda x: x["time"] or "")

    # Add AC state from HA-pushed cache (populated by POST /api/ac-update)
    name_to_key = {
        "Rosehill Pl Room 1": "room_1", "Room 1": "room_1",
        "Rosehill Pl Room 2": "room_2", "Room 2": "room_2",
        "Rosehill Pl Room 3": "room_3", "Rosehill Pl Room 3 New": "room_3", "Room 3": "room_3",
    }
    for device_name, room_data in rooms.items():
        key = name_to_key.get(device_name)
        state = _ac_states.get(key) if key else None
        room_data["ac_on"] = (state == "on") if state is not None else None

    return {
        "rooms": rooms,
        "front_door": front_door,
        "basement": basement,
        "summary": {
            "total_codes": total_codes,
            "occupied_rooms": occupied_count,
            "total_rooms": len(rooms),
            "todays_checkins": todays_checkins,
            "todays_checkouts": todays_checkouts,
            "generated_at": now.isoformat(),
        },
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, f, *a):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.html()
        elif self.path.startswith("/api/ha-summary"):
            self.ha_summary()
        elif self.path == "/api/config":
            self.app_config()
        elif self.path == "/api/sync-status":
            self.sync_status()
        elif self.path.startswith("/api/sync-log"):
            self.sync_log()
        elif self.path == "/manifest.json":
            self._static(MANIFEST, "application/manifest+json")
        elif self.path == "/icon.svg":
            self._static(ICON_SVG, "image/svg+xml")
        else:
            self.proxy("GET")

    def do_POST(self):
        if self.path == "/api/ac-update":
            self.ac_update()
        elif self.path == "/api/sync-trigger":
            self.sync_trigger()
        else:
            self.proxy("POST")

    def ac_update(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            for key in ("room_1", "room_2", "room_3"):
                if key in body:
                    _ac_states[key] = body[key]
            print(f"[AC] states updated: {_ac_states}", flush=True)
            self.send_response(200)
            self.send_cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", 2)
            self.end_headers()
            self.wfile.write(b"{}")
        except Exception as e:
            print("[EXC] ac_update:", redact(e), flush=True)
            self.send_response(400)
            self.end_headers()

    def _json(self, obj, status=200):
        payload = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def app_config(self):
        self._json({
            "skip_codes": sorted(PERMANENT_CODES),
            "room_to_door_prefix": ROOM_TO_DOOR_PREFIX,
            "door_to_room_prefix": DOOR_TO_ROOM_PREFIX,
            "basement_device_id": BASEMENT_DEVICE_ID,
            "site_name": SITE_NAME,
        })

    def sync_status(self):
        try:
            with open(SYNC_STATUS_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {"last_run": None, "issues": []}
        self._json(data)

    def sync_log(self):
        self._json({"lines": tail_lines(SYNC_LOG_FILE, n=200)})

    def sync_trigger(self):
        try:
            running = subprocess.run(["pgrep", "-f", "seam_sync.py"], capture_output=True)
            if running.returncode == 0:
                self._json({"started": False, "reason": "already running"})
                return
            subprocess.Popen([
                "sh", "-c",
                ". /data/runtime_env.sh; /usr/local/bin/python3 /seam_sync.py >> /data/seam_sync.log 2>&1",
            ])
            self._json({"started": True})
        except Exception as e:
            print("[EXC] sync_trigger:", redact(e), flush=True)
            self._json({"error": str(e)}, status=500)

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Account")

    def _static(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "max-age=3600")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def html(self):
        with open("/dashboard.html", "rb") as f:
            d = f.read()
        patched = d.replace(b"<head>", b"<head>" + INJECT, 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", len(patched))
        self.end_headers()
        self.wfile.write(patched)

    def ha_summary(self):
        try:
            data = build_ha_summary()
        except Exception as e:
            print("[EXC] ha_summary:", redact(e), flush=True)
            # Return safe defaults so HA sensors never get a 500 / missing keys
            data = {
                "summary": {"occupied_rooms": 0, "total_rooms": 3, "total_codes": 0,
                             "todays_checkins": [], "todays_checkouts": []},
                "front_door": {"online": False, "locked": None, "battery": None,
                               "active_guest": None, "next_guest": None, "code_count": 0},
                "basement": {"guest_present": False, "guest_name": ""},
                "rooms": {},
                "error": str(e),
            }
        try:
            payload = json.dumps(data, default=str).encode()
            self.send_response(200)
            self.send_cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(payload))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e2:
            print("[EXC] ha_summary write:", redact(e2), flush=True)

    def _is_ingress(self):
        """True if this request came through HA supervisor's ingress proxy.

        Supervisor sets `X-Hass-Source: core.ingress` on ingress requests and
        also adds `X-Ingress-Path`. Direct calls (LAN, other add-ons on
        hassio_network) have neither. We require this on state-changing
        proxy calls so a rogue add-on on the same network can't create or
        delete lock codes even if the port ever leaks.
        """
        return (
            self.headers.get("X-Hass-Source") == "core.ingress"
            or "X-Ingress-Path" in self.headers
        )

    def proxy(self, method):
        if not self._is_ingress():
            client = self.client_address[0] if self.client_address else "?"
            print(f"[REJECT] non-ingress {method} {self.path} from {client}", flush=True)
            self.send_response(403)
            self.send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"ingress-only endpoint"}')
            return
        try:
            acct = self.headers.get("X-Account", "1")
            key = K1 if acct == "1" else K2
            if not key:
                raise Exception("No key for account " + acct)
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n) if n else None
            url = SEAM_API + self.path
            print("[REQ]", method, url, flush=True)

            cmd = [
                "curl", "-s", "-X", method,
                "-H", "Authorization: Bearer " + key,
                "-H", "Content-Type: application/json",
                "-H", "Accept: application/json",
                "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "-H", "Accept-Language: en-US,en;q=0.9",
                "--compressed",
            ]
            if body:
                cmd += ["--data-raw", body.decode()]
            cmd.append(url)

            result = subprocess.run(cmd, capture_output=True)
            data = result.stdout
            print("[OK] curl exit:", result.returncode, redact(data[:200]), flush=True)

            self.send_response(200)
            self.send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            print("[EXC]", redact(e), flush=True)
            self.send_response(500)
            self.send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())


print("Seam Dashboard starting on port", PORT, flush=True)
HTTPServer(("0.0.0.0", PORT), H).serve_forever()
