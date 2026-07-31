#!/usr/bin/env python3
import subprocess, sys
try:
    import seam
except ImportError:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'seam', '--break-system-packages', '-q'])
try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'requests', '--break-system-packages', '-q'])
    import requests

import logging, os
from datetime import datetime, timezone, timedelta
import json

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

from shared_config import (
    ROOM_DEVICE_IDS,
    FRONT_DOOR_DEVICE_ID,
    BASEMENT_DEVICE_ID,
    SKIP_CODES,
    ROOM_TO_DOOR_PREFIX,
    DOOR_TO_ROOM_PREFIX,
)

# Account 1 — Rooms (Room 1 + Room 2 + Room 3)
# Read API keys from add-on options (single source of truth)
try:
    with open("/data/options.json") as _f:
        _opts = json.load(_f)
    ROOMS_API_KEY = _opts["account1_api_key"]
    FRONT_DOOR_API_KEY = _opts["account2_api_key"]
except Exception as _e:
    raise RuntimeError(f"Cannot read Seam API keys from /data/options.json: {_e}")

BASEMENT_API_KEY = FRONT_DOOR_API_KEY

HA_URL = "http://supervisor/core/api"

# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SNAPSHOT_FILE = "/data/seam_known_codes.json"
SYNC_STATUS_FILE = "/data/sync_status.json"

# Reset at the start of each sync() run; populated by log_action_failure() so the
# dashboard can show which codes are stuck waiting on lock confirmation vs actually failed.
_sync_issues = []


def parse_dt(val):
    """Parse a datetime string or object into a UTC datetime."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val
    return datetime.fromisoformat(str(val).replace("Z", "+00:00"))


def to_api_dt(val):
    """Parse into a UTC datetime, then render as an ISO string the Seam API can serialize."""
    dt = parse_dt(val)
    return dt.isoformat() if dt else None


def log_action_failure(action_desc, e):
    """Log a create/update/delete failure, distinguishing a pending device
    confirmation (lock hasn't ack'd yet, will resolve or get retried next sync)
    from an actual rejection by the lock/API. Also records the issue so the
    dashboard can surface it (see SYNC_STATUS_FILE)."""
    if isinstance(e, seam.SeamActionAttemptTimeoutError):
        aa = e.action_attempt
        log.warning(f"  PENDING (no device confirmation yet): {action_desc} "
                    f"— action_attempt_id={aa.action_attempt_id}, will re-check next sync")
        _sync_issues.append({
            "status": "pending",
            "action": action_desc,
            "action_attempt_id": aa.action_attempt_id,
        })
    elif isinstance(e, seam.SeamActionAttemptFailedError):
        aa = e.action_attempt
        log.error(f"  FAILED: {action_desc} — {aa.error.type}: {aa.error.message}")
        _sync_issues.append({
            "status": "failed",
            "action": action_desc,
            "reason": f"{aa.error.type}: {aa.error.message}",
        })
    else:
        log.error(f"  FAILED: {action_desc} — {e}")
        _sync_issues.append({"status": "failed", "action": action_desc, "reason": str(e)})


def dates_differ(a, b, tolerance_seconds=60):
    """Return True if two datetime values differ by more than tolerance."""
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return abs((parse_dt(a) - parse_dt(b)).total_seconds()) > tolerance_seconds




def _load_snapshot():
    try:
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _report_external_changes(label, current_pins, prev_snapshot):
    """Log codes that appeared/disappeared on a device since the last sync run."""
    prev = prev_snapshot.get(label)
    if prev is None:
        return
    curr_names = set(current_pins.keys())
    prev_names = set(prev.keys())
    for name in sorted(curr_names - prev_names):
        log.warning(f"  [EXTERNAL] Code added on {label}: '{name}' PIN={current_pins[name]}")
    for name in sorted(prev_names - curr_names):
        log.warning(f"  [EXTERNAL] Code removed from {label}: '{name}' (was PIN={prev.get(name, '?')})")


def _save_post_sync_snapshot(rooms_client, door_client):
    """Re-fetch all codes post-sync and save snapshot for next run's external-change detection."""
    snap = {}
    for label, did in ROOM_DEVICE_IDS.items():
        try:
            codes = rooms_client.access_codes.list(device_id=did)
            snap[label] = {c.name: (c.code or "") for c in codes if c.name}
        except Exception as e:
            log.warning(f"  Snapshot: could not fetch {label}: {e}")
    try:
        codes = door_client.access_codes.list(device_id=FRONT_DOOR_DEVICE_ID)
        snap["Front Door"] = {c.name: (c.code or "") for c in codes if c.name}
    except Exception as e:
        log.warning(f"  Snapshot: could not fetch Front Door: {e}")
    if BASEMENT_DEVICE_ID:
        try:
            codes = door_client.access_codes.list(device_id=BASEMENT_DEVICE_ID)
            snap["Basement"] = {c.name: (c.code or "") for c in codes if c.name}
        except Exception as e:
            log.warning(f"  Snapshot: could not fetch Basement: {e}")
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snap, f)
    except Exception as e:
        log.warning(f"  Snapshot: write failed: {e}")


def delete_expired_codes(seam_client, device_map):
    """Delete time_bound codes that expired more than 24 hours ago."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for device_label, device_id in device_map.items():
        try:
            codes = list(seam_client.access_codes.list(device_id=device_id))
        except Exception as e:
            log.warning(f"  Could not list codes for {device_label}: {e}")
            continue
        for c in codes:
            name = (c.name or "").strip()
            if not name or name in SKIP_CODES or c.type != "time_bound":
                continue
            ends = parse_dt(c.ends_at)
            if ends and ends < cutoff:
                try:
                    seam_client.access_codes.delete(access_code_id=c.access_code_id)
                    log.info(f"  EXPIRED: deleted '{name}' from {device_label} (expired {ends.strftime('%Y-%m-%d %H:%M')} UTC)")
                except Exception as e:
                    log_action_failure(f"delete expired '{name}' from {device_label}", e)


def cleanup_duplicate_names(seam_client, device_map):
    """Find codes with duplicate names on the same device, keep the latest end date one."""
    for device_label, device_id in device_map.items():
        try:
            codes = list(seam_client.access_codes.list(device_id=device_id))
        except Exception as e:
            log.warning(f"  Could not list codes for {device_label} during cleanup: {e}")
            continue

        by_name = {}
        for c in codes:
            name = (c.name or "").strip()
            if not name:
                continue
            if name in SKIP_CODES:
                continue
            if c.type != "time_bound":
                continue
            by_name.setdefault(name, []).append(c)

        for name, group in by_name.items():
            if len(group) < 2:
                continue
            pins = set(c.code for c in group if c.code)
            if len(pins) != 1:
                log.warning(f"  Found {len(group)} codes named '{name}' on {device_label} with different PINs {pins} - skipping cleanup")
                continue

            def end_key(c):
                if c.ends_at is None:
                    return datetime.min.replace(tzinfo=timezone.utc)
                return parse_dt(c.ends_at)
            group.sort(key=end_key, reverse=True)
            keep = group[0]
            for stale in group[1:]:
                try:
                    seam_client.access_codes.delete(access_code_id=stale.access_code_id)
                    log.info(f"  CLEANUP: removed duplicate '{name}' on {device_label} (ends_at={stale.ends_at}, kept ends_at={keep.ends_at})")
                except Exception as e:
                    log_action_failure(f"CLEANUP remove duplicate '{name}' on {device_label}", e)


def convert_unmanaged_codes(seam_client, device_id, device_label):
    """Convert any unmanaged codes on a device to managed."""
    try:
        unmanaged = seam_client.access_codes.unmanaged.list(device_id=device_id)
        for code in unmanaged:
            name = code.name or "(unnamed)"
            try:
                seam_client.access_codes.unmanaged.convert_to_managed(
                    access_code_id=code.access_code_id
                )
                log.info(f"  CONVERTED unmanaged code '{name}' on {device_label}")
            except Exception as e:
                log.warning(f"  Could not convert '{name}' on {device_label}: {e}")
    except Exception as e:
        log.warning(f"  Could not list unmanaged codes for {device_label}: {e}")


def get_active_codes(seam_client, device_id, device_label="device"):
    """Returns dict of {name: code_object} for active/scheduled codes."""
    codes = seam_client.access_codes.list(device_id=device_id)
    result = {}
    for code in codes:
        is_future = (code.type == "time_bound" and code.ends_at and parse_dt(code.ends_at) > datetime.now(timezone.utc))
        if code.status in ("set", "setting") or (code.status == "unset" and code.is_scheduled_on_device) or is_future:
            name = code.name or ""
            if name:
                result[name] = code
    log.info(f"  {device_label}: {len(result)} active code(s)")
    return result


def _get_supervisor_token():
    """Get SUPERVISOR_TOKEN from env or s6 envdir (crond doesn't inherit container env)."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        try:
            with open("/run/s6/container_environment/SUPERVISOR_TOKEN") as f:
                token = f.read().strip()
        except Exception:
            pass
    if not token:
        try:
            with open("/run/s6/container_environment/HASSIO_TOKEN") as f:
                token = f.read().strip()
        except Exception:
            pass
    return token


def _ha_service(domain, service, data):
    """Call an HA service via the supervisor API."""
    token = _get_supervisor_token()
    if not token:
        log.warning("SUPERVISOR_TOKEN not set — skipping HA update")
        return
    try:
        r = requests.post(
            f"{HA_URL}/services/{domain}/{service}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=data,
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning(f"  HA service {domain}.{service} failed: {e}")


def sync_basement_guest(seam_client):
    """Read the active guest code from the basement lock and update HA presence + guest name."""
    if not BASEMENT_DEVICE_ID:
        log.info("Basement: BASEMENT_DEVICE_ID not set — skipping (add device ID after connecting Kwikset to Seam)")
        return

    now_utc = datetime.now(timezone.utc)
    try:
        codes = seam_client.access_codes.list(device_id=BASEMENT_DEVICE_ID)
    except Exception as e:
        log.warning(f"  Basement: could not fetch codes: {e}")
        return

    active_guest = None
    for c in codes:
        name = (c.name or "").strip()
        if not name or name in SKIP_CODES:
            continue
        if c.type != "time_bound":
            continue
        starts = parse_dt(c.starts_at)
        ends = parse_dt(c.ends_at)
        if starts and ends and starts <= now_utc <= ends:
            active_guest = name
            break

    if active_guest:
        log.info(f"  Basement: active guest '{active_guest}' — presence ON")
    else:
        log.info("  Basement: no active guest code — presence OFF")
    # HA state (input_boolean.basement_guest_present + input_text.basement_guest_name)
    # is synced via the /api/ha-summary REST sensor + basement_guest_sync_from_seam automation
    # (direct HA API calls require homeassistant_api: true in config.yaml — pending reinstall)


def _write_sync_status():
    try:
        with open(SYNC_STATUS_FILE, "w") as f:
            json.dump({
                "last_run": datetime.now(timezone.utc).isoformat(),
                "issues": _sync_issues,
            }, f)
    except Exception as e:
        log.warning(f"  Could not write sync status: {e}")


def sync():
    _sync_issues.clear()

    log.info("=" * 60)
    log.info("Starting Seam bidirectional sync")

    from seam import Seam
    # Default wait_for_action_attempt timeout is 5s — too short for a Z-Wave
    # lock (e.g. Schlage) to confirm a code add/delete. Give it real time before
    # treating it as failed/pending.
    ACTION_WAIT = {"timeout": 20.0, "polling_interval": 2.0}
    rooms_seam = Seam(api_key=ROOMS_API_KEY, wait_for_action_attempt=ACTION_WAIT)
    front_door_seam = Seam(api_key=FRONT_DOOR_API_KEY, wait_for_action_attempt=ACTION_WAIT)

    # ── 0. Convert unmanaged codes on room locks ──────────────────────────────
    log.info("Checking for unmanaged codes on room locks...")
    for room_name, device_id in ROOM_DEVICE_IDS.items():
        convert_unmanaged_codes(rooms_seam, device_id, room_name)

    # ── 0b. Convert unmanaged codes on Front Door ─────────────────────────────
    log.info("Checking for unmanaged codes on Front Door...")
    convert_unmanaged_codes(front_door_seam, FRONT_DOOR_DEVICE_ID, "Front Door")

    # ── 0c. Clean up duplicate-name codes on room locks ───────────────────────
    import time
    time.sleep(5)
    cleanup_duplicate_names(rooms_seam, ROOM_DEVICE_IDS)
    cleanup_duplicate_names(front_door_seam, {"Front Door": FRONT_DOOR_DEVICE_ID})

    # ── 1. Fetch all room codes ───────────────────────────────────────────────
    log.info("Fetching room codes...")
    room_codes_by_device = {}
    all_room_codes = {}

    for room_name, device_id in ROOM_DEVICE_IDS.items():
        codes = get_active_codes(rooms_seam, device_id, room_name)
        room_codes_by_device[device_id] = codes
        for name, code in codes.items():
            existing = all_room_codes.get(name)
            if existing is None:
                all_room_codes[name] = code
            else:
                # If name collision, keep the one with the latest end date
                from datetime import datetime as _dt, timezone as _tz
                def _ends(c):
                    if c.type != "time_bound" or not c.ends_at: return _dt.min.replace(tzinfo=_tz.utc)
                    return parse_dt(c.ends_at)
                if _ends(code) > _ends(existing):
                    all_room_codes[name] = code

    log.info(f"Total unique codes across all rooms: {len(all_room_codes)}")

    # ── 2. Fetch Front Door codes ─────────────────────────────────────────────
    log.info("Fetching Front Door codes...")
    front_door_all = front_door_seam.access_codes.list(device_id=FRONT_DOOR_DEVICE_ID)

    door_managed_room = {}    # s~ codes: synced from rooms to door
    door_whole_house = {}     # whole-house codes: need to sync to all rooms
    door_managed_whole = {}   # h~ codes: already synced to rooms
    door_permanent = {}       # permanent codes (SKIP_CODES)

    for code in front_door_all:
        name = code.name or ""
        if not name:
            continue
        if name.startswith(ROOM_TO_DOOR_PREFIX):
            door_managed_room[name.removeprefix(ROOM_TO_DOOR_PREFIX)] = code
        elif name.startswith(DOOR_TO_ROOM_PREFIX):
            door_managed_whole[name.removeprefix(DOOR_TO_ROOM_PREFIX)] = code
        elif name in SKIP_CODES:
            door_permanent[name] = code
        else:
            door_whole_house[name] = code

    log.info(f"  Room→Door (s~): {len(door_managed_room)}, Whole-house: {len(door_whole_house)}, Door→Room (h~): {len(door_managed_whole)}, Permanent: {len(door_permanent)}")

    # ── External change detection ─────────────────────────────────────────────
    _prev_snap = _load_snapshot()
    if _prev_snap:
        _did_to_label = {v: k for k, v in ROOM_DEVICE_IDS.items()}
        for _did, _codes in room_codes_by_device.items():
            _lbl = _did_to_label.get(_did, _did)
            _report_external_changes(_lbl, {n: (c.code or "") for n, c in _codes.items()}, _prev_snap)
        _fd_pins = {c.name: (c.code or "") for c in front_door_all if c.name}
        _report_external_changes("Front Door", _fd_pins, _prev_snap)

    # ══════════════════════════════════════════════════════════════════════════
    #  DIRECTION 1: Rooms → Front Door (s~ prefix)
    # ══════════════════════════════════════════════════════════════════════════
    log.info("--- Syncing Rooms → Front Door ---")

    room_names = set(all_room_codes.keys())
    managed_room_names = set(door_managed_room.keys())

    to_add_to_door = room_names - managed_room_names - set(door_whole_house.keys()) - set(door_permanent.keys())
    to_remove_from_door = managed_room_names - room_names
    in_sync_door = room_names & managed_room_names

    log.info(f"To ADD: {len(to_add_to_door)}, To REMOVE: {len(to_remove_from_door)}, In sync: {len(in_sync_door)}")

    # Add new codes
    for name in to_add_to_door - set(SKIP_CODES):
        if name.startswith(DOOR_TO_ROOM_PREFIX): continue
        source = all_room_codes[name]
        try:
            kwargs = dict(
                device_id=FRONT_DOOR_DEVICE_ID,
                name=ROOM_TO_DOOR_PREFIX + name,
                code=source.code,
            )
            if source.type == "time_bound":
                kwargs["starts_at"] = to_api_dt(source.starts_at)
                kwargs["ends_at"] = to_api_dt(source.ends_at)
            front_door_seam.access_codes.create(**kwargs)
            log.info(f"  ADDED to Front Door: '{name}' (code: {source.code})")
        except Exception as e:
            log_action_failure(f"add '{name}' to Front Door", e)

    # Update dates if stay was extended
    for name in in_sync_door:
        source = all_room_codes[name]
        door_code = door_managed_room[name]
        if source.type == "time_bound" and door_code.type == "time_bound":
            if dates_differ(source.ends_at, door_code.ends_at) or dates_differ(source.starts_at, door_code.starts_at):
                try:
                    front_door_seam.access_codes.update(
                        access_code_id=door_code.access_code_id,
                        starts_at=to_api_dt(source.starts_at),
                        ends_at=to_api_dt(source.ends_at),
                    )
                    log.info(f"  UPDATED dates for '{name}' on Front Door (stay extended)")
                except Exception as e:
                    log_action_failure(f"update dates for '{name}' on Front Door", e)

    # Remove stale codes
    for name in to_remove_from_door:
        try:
            front_door_seam.access_codes.delete(
                access_code_id=door_managed_room[name].access_code_id
            )
            log.info(f"  REMOVED from Front Door: '{name}'")
        except Exception as e:
            log_action_failure(f"remove '{name}' from Front Door", e)

    # ══════════════════════════════════════════════════════════════════════════
    #  DIRECTION 2: Front Door → All Rooms (h~ prefix)
    # ══════════════════════════════════════════════════════════════════════════
    log.info("--- Syncing Front Door → All Rooms ---")

    whole_house_names = set(door_whole_house.keys())
    # h~ codes live on ROOM locks, not Front Door
    managed_whole_names = set()
    for _did, _codes in room_codes_by_device.items():
        for _n in _codes:
            if _n.startswith(DOOR_TO_ROOM_PREFIX):
                managed_whole_names.add(_n[len(DOOR_TO_ROOM_PREFIX):])

    # A whole-house code is only "in sync" if EVERY room has the prefixed copy —
    # checking for presence on just one room let codes silently stay missing
    # from the others (e.g. after a manual delete on a single room lock).
    fully_synced_names = {
        name for name in whole_house_names
        if all((DOOR_TO_ROOM_PREFIX + name) in room_codes_by_device[did] for did in ROOM_DEVICE_IDS.values())
    }

    to_add_to_rooms = whole_house_names - fully_synced_names
    to_remove_from_rooms = managed_whole_names - whole_house_names
    in_sync_rooms = fully_synced_names

    log.info(f"Whole-house to ADD: {len(to_add_to_rooms)}, To REMOVE: {len(to_remove_from_rooms)}, In sync: {len(in_sync_rooms)}")

    # Add whole-house codes to all rooms
    for name in to_add_to_rooms - set(SKIP_CODES):
        # Skip Airbnb Backup codes — they belong to specific listings, not whole-house
        if 'Airbnb Backup' in name:
            continue
        source = door_whole_house[name]
        for room_name, device_id in ROOM_DEVICE_IDS.items():
            existing = room_codes_by_device[device_id]
            prefixed = DOOR_TO_ROOM_PREFIX + name
            # Skip if any existing managed code on this lock has the same PIN
            if any(c.code == source.code for c in existing.values()):
                log.info(f"  Skipping '{name}' on {room_name}: PIN {source.code} already exists")
                continue
            if name in existing or prefixed in existing:
                continue
            try:
                kwargs = dict(
                    device_id=device_id,
                    name=prefixed,
                    code=source.code,
                )
                if source.type == "time_bound":
                    kwargs["starts_at"] = to_api_dt(source.starts_at)
                    kwargs["ends_at"] = to_api_dt(source.ends_at)
                rooms_seam.access_codes.create(**kwargs)
                log.info(f"  ADDED to {room_name}: '{name}' (code: {source.code})")
            except Exception as e:
                log_action_failure(f"add '{name}' to {room_name}", e)

    # Update dates on room locks if whole-house stay extended
    for name in in_sync_rooms:
        source = door_whole_house[name]
        if source.type != "time_bound":
            continue
        prefixed = DOOR_TO_ROOM_PREFIX + name
        for room_name, device_id in ROOM_DEVICE_IDS.items():
            existing = room_codes_by_device[device_id]
            room_code = existing.get(prefixed)
            if room_code is None or room_code.type != "time_bound":
                continue
            if dates_differ(source.ends_at, room_code.ends_at) or dates_differ(source.starts_at, room_code.starts_at):
                try:
                    rooms_seam.access_codes.update(
                        access_code_id=room_code.access_code_id,
                        starts_at=to_api_dt(source.starts_at),
                        ends_at=to_api_dt(source.ends_at),
                    )
                    log.info(f"  UPDATED dates for '{name}' on {room_name} (stay extended)")
                except Exception as e:
                    log_action_failure(f"update dates for '{name}' on {room_name}", e)

    # Remove whole-house codes from rooms when deleted from Front Door
    for name in to_remove_from_rooms:
        for room_name, device_id in ROOM_DEVICE_IDS.items():
            existing = room_codes_by_device[device_id]
            prefixed = DOOR_TO_ROOM_PREFIX + name
            if prefixed in existing:
                try:
                    rooms_seam.access_codes.delete(
                        access_code_id=existing[prefixed].access_code_id
                    )
                    log.info(f"  REMOVED from {room_name}: '{name}'")
                except Exception as e:
                    log_action_failure(f"remove '{name}' from {room_name}", e)

    # ── 3. Cleanup expired codes ──────────────────────────────────────────────
    log.info("--- Cleaning up expired codes ---")
    delete_expired_codes(rooms_seam, ROOM_DEVICE_IDS)
    delete_expired_codes(front_door_seam, {"Front Door": FRONT_DOOR_DEVICE_ID})
    if BASEMENT_DEVICE_ID:
        delete_expired_codes(front_door_seam, {"Basement": BASEMENT_DEVICE_ID})

    # ── 4. Basement guest presence sync ──────────────────────────────────────
    log.info("--- Syncing basement guest presence ---")
    sync_basement_guest(front_door_seam)

    _save_post_sync_snapshot(rooms_seam, front_door_seam)
    _write_sync_status()
    log.info("Sync complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        sync()
    except Exception as e:
        log.critical(f"Sync crashed: {e}", exc_info=True)
        sys.exit(1)

