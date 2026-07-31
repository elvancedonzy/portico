#!/usr/bin/env python3
"""
airbnb_extend.py — auto-extends Seam access codes when Airbnb reservations shift.

Reads HA calendar entities (calendar.room_N_airbnb, calendar.whole_home_airbnb).
On FIRST binding: records the calendar's DTSTART/DTEND as a baseline. Does NOT touch the Seam code.
On LATER runs: if the calendar's DTSTART or DTEND has moved from the baseline, applies the SAME
delta to the code's starts_at/ends_at. This preserves the actual check-in/out HOUR the code was
originally set for (Airbnb ICS is date-only; the code has the real time).

Safety caps:
- Reject extensions > 30 days in a single delta
- Reject shortenings > 2 days in a single delta
- PIN is never touched.

seam_sync.py auto-propagates the update to other locks within 1 min.

Runs every 2 minutes via crontab.
"""

import json
import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import urllib.request
import urllib.error

from seam import Seam

from shared_config import ROOM_DEVICE_IDS, FRONT_DOOR_DEVICE_ID, SKIP_CODES

STATE_FILE = "/data/airbnb_bindings.json"
LOOKAHEAD_DAYS = 90
LOOKBACK_HOURS = 24
BIND_TOLERANCE_HOURS = 36
MIN_DELTA_SECONDS = 300              # ignore tiny shifts (< 5 min)
MAX_EXTEND_SECONDS = 30 * 86400      # cap: extend by at most 30 days per delta
MAX_SHORTEN_SECONDS = 2 * 86400      # cap: shorten by at most 2 days per delta

# (ha_calendar_entity, seam_device_id, api_key_option_key)
CALENDAR_ROUTES = [
    ("calendar.room_1_airbnb", ROOM_DEVICE_IDS["Room 1"], "account1_api_key"),
    ("calendar.room_2_airbnb", ROOM_DEVICE_IDS["Room 2"], "account1_api_key"),
    ("calendar.room_3_airbnb", ROOM_DEVICE_IDS["Room 3"], "account1_api_key"),
    ("calendar.whole_home_airbnb", FRONT_DOOR_DEVICE_ID, "account2_api_key"),
]

HA_API = "http://supervisor/core/api"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

try:
    with open("/data/options.json") as f:
        _opts = json.load(f)
except Exception:
    _opts = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [airbnb_extend] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def http_get_json(url, headers, timeout=15):
    req = urllib.request.Request(url, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="ignore")
        raise RuntimeError(f"HTTP {e.code} for {url}: {body_text}") from None


def parse_dt(s):
    if not s:
        return None
    if not isinstance(s, str):
        s = str(s)
    if "T" not in s:
        return datetime.fromisoformat(s + "T00:00:00").replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_z(dt):
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_bindings():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_bindings(b):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(b, f, indent=2)
    os.replace(tmp, STATE_FILE)


def get_calendar_events(entity_id):
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN not set — cannot query HA")
    now = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    end = datetime.now(timezone.utc) + timedelta(days=LOOKAHEAD_DAYS)
    url = (f"{HA_API}/calendars/{entity_id}"
           f"?start={quote(to_iso_z(now))}&end={quote(to_iso_z(end))}")
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    try:
        events = http_get_json(url, headers)
    except Exception as e:
        log.warning(f"HA calendar fetch failed for {entity_id}: {e}")
        return []
    out = []
    for e in events or []:
        uid = e.get("uid") or e.get("recurrence_id")
        if not uid:
            continue
        start = e.get("start", {}) if isinstance(e.get("start"), dict) else {}
        end_ = e.get("end", {}) if isinstance(e.get("end"), dict) else {}
        start_s = start.get("dateTime") or start.get("date")
        end_s = end_.get("dateTime") or end_.get("date")
        summary = (e.get("summary") or "").lower()
        if not start_s or not end_s:
            continue
        if "not available" in summary:
            continue
        out.append({
            "uid": uid,
            "summary": e.get("summary") or "",
            "start": parse_dt(start_s),
            "end": parse_dt(end_s),
        })
    return out


_seam_clients = {}


def seam_client(api_key):
    if api_key not in _seam_clients:
        _seam_clients[api_key] = Seam(api_key=api_key)
    return _seam_clients[api_key]


def _code_to_dict(c):
    return {
        "access_code_id": c.access_code_id,
        "name": c.name,
        "type": c.type,
        "starts_at": c.starts_at,
        "ends_at": c.ends_at,
    }


def list_access_codes(api_key, device_id):
    try:
        codes = seam_client(api_key).access_codes.list(device_id=device_id)
    except Exception as e:
        log.error(f"Seam list_access_codes failed for {device_id}: {e}")
        return None  # None = fetch failed (different from empty list)
    return [_code_to_dict(c) for c in codes]


def update_access_code(api_key, code_id, starts_at, ends_at):
    return seam_client(api_key).access_codes.update(
        access_code_id=code_id,
        starts_at=to_iso_z(starts_at),
        ends_at=to_iso_z(ends_at),
    )


def find_matching_code(event, codes):
    candidates = []
    for c in codes:
        if c.get("type") != "time_bound":
            continue
        name = c.get("name") or ""
        if name in SKIP_CODES:
            continue
        if name.startswith("s~ ") or name.startswith("h~ "):
            continue
        c_start = parse_dt(c.get("starts_at"))
        c_end = parse_dt(c.get("ends_at"))
        if c_start is None or c_end is None:
            continue
        d_start = abs((c_start - event["start"]).total_seconds()) / 3600
        d_end = abs((c_end - event["end"]).total_seconds()) / 3600
        if d_start <= BIND_TOLERANCE_HOURS and d_end <= BIND_TOLERANCE_HOURS:
            candidates.append((d_start + d_end, c))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def process_route(entity_id, device_id, api_key_option, bindings):
    api_key = _opts.get(api_key_option, "")
    if not api_key:
        log.warning(f"{entity_id}: missing {api_key_option} in options — skipping")
        return

    events = get_calendar_events(entity_id)
    if not events:
        return

    codes = list_access_codes(api_key, device_id)
    if codes is None:
        # fetch failed, don't touch bindings for this route
        return
    if not codes:
        log.info(f"{entity_id}: no codes on device {device_id[:8]}")
        return

    codes_by_id = {c["access_code_id"]: c for c in codes}

    for ev in events:
        uid = ev["uid"]
        binding_key = f"{entity_id}::{uid}"
        binding = bindings.get(binding_key) or {}
        code_id = binding.get("code_id")
        code = codes_by_id.get(code_id) if code_id else None

        # ── First-time bind or code disappeared: record baseline, DO NOT modify code ──
        if not code:
            match = find_matching_code(ev, codes)
            if not match:
                log.info(f"{entity_id} UID {uid[:12]}: no Seam code within {BIND_TOLERANCE_HOURS}h — skipping")
                continue
            code_id = match["access_code_id"]
            code = match
            bindings[binding_key] = {
                "code_id": code_id,
                "code_name": code.get("name"),
                "bound_at": to_iso_z(datetime.now(timezone.utc)),
                "baseline_calendar_start": to_iso_z(ev["start"]),
                "baseline_calendar_end": to_iso_z(ev["end"]),
                "snapshot_code_starts_at": code.get("starts_at"),
                "snapshot_code_ends_at": code.get("ends_at"),
            }
            log.info(
                f"{entity_id} UID {uid[:12]}: BOUND to '{code.get('name')}' ({code_id[:8]}) "
                f"— baseline cal_end={to_iso_z(ev['end'])}, code_end={code.get('ends_at')} (no changes made)"
            )
            continue

        # ── Existing binding: compare calendar NOW vs baseline ──
        baseline_start = parse_dt(binding.get("baseline_calendar_start"))
        baseline_end = parse_dt(binding.get("baseline_calendar_end"))
        if baseline_start is None or baseline_end is None:
            # Legacy binding without baseline — record it now, don't touch code
            binding["baseline_calendar_start"] = to_iso_z(ev["start"])
            binding["baseline_calendar_end"] = to_iso_z(ev["end"])
            binding["snapshot_code_starts_at"] = code.get("starts_at")
            binding["snapshot_code_ends_at"] = code.get("ends_at")
            log.info(f"{entity_id} UID {uid[:12]}: legacy binding — baseline recorded, no changes made")
            continue

        delta_start = int((ev["start"] - baseline_start).total_seconds())
        delta_end = int((ev["end"] - baseline_end).total_seconds())

        if abs(delta_start) < MIN_DELTA_SECONDS and abs(delta_end) < MIN_DELTA_SECONDS:
            continue  # no meaningful shift

        # Safety caps — refuse and warn if delta is outside expected range
        for label, delta in (("start", delta_start), ("end", delta_end)):
            if delta > MAX_EXTEND_SECONDS:
                log.warning(
                    f"{entity_id} UID {uid[:12]}: calendar {label} moved forward by "
                    f"{delta/86400:.1f}d — exceeds {MAX_EXTEND_SECONDS/86400:.0f}d extend cap, SKIPPING"
                )
                return
            if delta < -MAX_SHORTEN_SECONDS:
                log.warning(
                    f"{entity_id} UID {uid[:12]}: calendar {label} moved earlier by "
                    f"{-delta/86400:.1f}d — exceeds {MAX_SHORTEN_SECONDS/86400:.0f}d shorten cap, SKIPPING"
                )
                return

        c_start = parse_dt(code.get("starts_at"))
        c_end = parse_dt(code.get("ends_at"))
        if c_start is None or c_end is None:
            log.warning(f"{entity_id} UID {uid[:12]}: code has null start/end — skipping")
            continue

        new_c_start = c_start + timedelta(seconds=delta_start)
        new_c_end = c_end + timedelta(seconds=delta_end)

        try:
            update_access_code(api_key, code_id, new_c_start, new_c_end)
            log.info(
                f"{entity_id} UID {uid[:12]}: EXTENDED '{code.get('name')}' "
                f"ends_at {c_end.isoformat()} -> {new_c_end.isoformat()} "
                f"(cal delta: {delta_end/3600:+.1f}h)"
            )
            binding["baseline_calendar_start"] = to_iso_z(ev["start"])
            binding["baseline_calendar_end"] = to_iso_z(ev["end"])
            binding["last_update"] = to_iso_z(datetime.now(timezone.utc))
            binding["snapshot_code_starts_at"] = to_iso_z(new_c_start)
            binding["snapshot_code_ends_at"] = to_iso_z(new_c_end)
        except Exception as e:
            log.error(f"{entity_id} UID {uid[:12]}: FAILED to update code {code_id[:8]}: {e}")


def main():
    log.info("=" * 50)
    log.info("Starting Airbnb extension sync")

    if not SUPERVISOR_TOKEN:
        log.critical("SUPERVISOR_TOKEN missing — cannot call HA API. Exiting.")
        sys.exit(1)

    bindings = load_bindings()
    initial = len(bindings)

    for entity_id, device_id, api_key_option in CALENDAR_ROUTES:
        try:
            process_route(entity_id, device_id, api_key_option, bindings)
        except Exception as e:
            log.error(f"{entity_id}: unhandled error: {e}", exc_info=True)

    save_bindings(bindings)
    log.info(f"Sync complete. Bindings: {initial} -> {len(bindings)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.critical(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
