"""Configuration loaded from HA add-on options.

Values come from /data/options.json, populated by HA Supervisor from the
schema in config.yaml. Edit them in HA → Settings → Add-ons → Portico →
Configuration.

Empty defaults here — the add-on will refuse to sync anything until the
required options are set. This is intentional: this file must not ship
site-specific device IDs.
"""
import json


def _load_options():
    try:
        with open("/data/options.json") as f:
            return json.load(f) or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_opts = _load_options()

# ── Site branding ─────────────────────────────────────────────────────────
SITE_NAME = _opts.get("site_name") or "My STR"

# ── Room locks — list of {name, device_id} ────────────────────────────────
_rooms = _opts.get("rooms") or []
ROOM_DEVICE_IDS = {
    r["name"]: r["device_id"]
    for r in _rooms
    if r.get("name") and r.get("device_id")
}

# ── Main entry locks (same Seam account as each other) ───────────────────
FRONT_DOOR_DEVICE_ID = _opts.get("front_door_device_id") or ""
BASEMENT_DEVICE_ID   = _opts.get("basement_device_id")   or ""

# ── Permanent-resident codes — never touched by sync or cleanup ──────────
SKIP_CODES = _opts.get("skip_codes") or []

# ── Prefixes for room<->door mirror codes (owned by seam_sync) ───────────
ROOM_TO_DOOR_PREFIX = _opts.get("room_to_door_prefix") or "s~ "
DOOR_TO_ROOM_PREFIX = _opts.get("door_to_room_prefix") or "h~ "
