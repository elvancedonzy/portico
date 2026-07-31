#!/usr/bin/env python3
"""
Booking.com email sync — polls Gmail IMAP for new reservation confirmations
and auto-creates Seam access codes (check-in 12 PM / check-out 10 AM Eastern).

Setup (one-time):
  1. Enable IMAP in Gmail: Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP
  2. Create a Google App Password: myaccount.google.com → Security → App passwords
  3. Set gmail_user and gmail_app_password in the addon options (HA UI → Add-ons → Seam Dashboard → Configuration)
  4. Add LISTING_DEVICE_MAP entries below matching your exact Booking.com listing name keywords

Runs every 15 min via cron. Marks processed emails as read.
Logs to /data/booking_sync.log
"""
import imaplib
import email
import json
import re
import logging
import subprocess
import random
import sys
from datetime import datetime, timezone, timedelta
from email.header import decode_header

# ── Config ────────────────────────────────────────────────────────────────────

try:
    with open("/data/options.json") as f:
        _opts = json.load(f)
except Exception:
    _opts = {}

GMAIL_USER  = _opts.get("gmail_user", "")
GMAIL_PASS  = _opts.get("gmail_app_password", "")
ROOMS_KEY   = _opts.get("account1_api_key", "")

SEAM_API = "https://connect.getseam.com"

from shared_config import ROOM_DEVICE_IDS, SKIP_CODES as _SKIP_LIST

# Map keyword patterns (regex, case-insensitive) found in subject/body → device_id.
# The first match wins. Derived from shared_config.ROOM_DEVICE_IDS — add a room there,
# not here, to keep listing keywords and device IDs in sync.
LISTING_DEVICE_MAP = [
    (r"room\s*" + name.split()[-1], device_id)
    for name, device_id in ROOM_DEVICE_IDS.items()
]

SKIP_CODES = set(_SKIP_LIST)

# Weak PINs never used
WEAK_PINS = {"1234", "0000", "1111", "2222", "3333", "4444", "5555",
             "6666", "7777", "8888", "9999", "1212", "0123", "9876"}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [booking] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Date helpers ──────────────────────────────────────────────────────────────

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def eastern_to_utc(date, hour):
    """Convert Eastern local time on a date to a UTC datetime (handles DST)."""
    year = date.year
    # DST: second Sunday in March → first Sunday in November
    mar1 = datetime(year, 3, 1)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    nov1 = datetime(year, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    offset = timedelta(hours=-4 if dst_start.date() <= date < dst_end.date() else -5)
    local_dt = datetime(date.year, date.month, date.day, hour, 0)
    return (local_dt - offset).replace(tzinfo=timezone.utc)


def parse_date(text):
    """Return a date object from a date string, or None."""
    text = text.strip()
    # "25 May 2026" / "25 may 2026"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if m:
        mon = MONTHS.get(m.group(2).lower())
        if mon:
            return datetime(int(m.group(3)), mon, int(m.group(1))).date()
    # "May 25, 2026"
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text)
    if m:
        mon = MONTHS.get(m.group(1).lower())
        if mon:
            return datetime(int(m.group(3)), mon, int(m.group(2))).date()
    # "2026-05-25"
    m = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", text)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
    # "25/05/2026" (try DD/MM/YYYY first)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        a, b, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(yr, b, a).date()
        except ValueError:
            try:
                return datetime(yr, a, b).date()
            except ValueError:
                pass
    return None


# ── Email helpers ─────────────────────────────────────────────────────────────

def decode_str(h):
    parts = decode_header(h or "")
    out = []
    for part, charset in parts:
        if isinstance(part, bytes):
            out.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)


def get_body(msg):
    """Extract best available plain-text body from email."""
    plain = ""
    html  = ""
    for part in msg.walk():
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        decoded = payload.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            plain += decoded + "\n"
        elif part.get_content_type() == "text/html":
            html += decoded + "\n"
    if plain:
        return plain
    # Strip HTML tags as fallback
    return re.sub(r"<[^>]+>", " ", html)


# ── Reservation parser ────────────────────────────────────────────────────────

def parse_reservation(subject, body):
    """
    Parse a Booking.com confirmation email.
    Returns dict(guest_name, checkin_date, checkout_date, device_id) or None.
    """
    text = subject + "\n" + body

    # Determine room from listing name keywords
    device_id = None
    for pattern, did in LISTING_DEVICE_MAP:
        if re.search(pattern, text, re.IGNORECASE):
            device_id = did
            break
    if not device_id:
        log.warning("Could not determine room from email — no matching listing keyword")
        return None

    # Extract guest name
    guest_name = None
    name_patterns = [
        r"(?:guest\s+name|guest|booker)[:\s]+([A-Z\xc0-\xff][A-Za-z\xc0-\xff '\-]{1,40})(?:\n|\r|,|<|\|)",
        r"^(?:Name)[:\s]+([A-Z\xc0-\xff][A-Za-z\xc0-\xff '\-]{1,40})(?:\n|\r|,|<)",
    ]
    for pat in name_patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            guest_name = m.group(1).strip().rstrip(",. ")
            break
    if not guest_name:
        log.warning("Could not extract guest name from email")
        return None

    # Extract dates by scanning each line for arrival/departure keywords
    checkin_date  = None
    checkout_date = None
    for line in text.splitlines():
        ll = line.lower()
        if any(k in ll for k in ("arrival", "check-in", "check in", "checkin")):
            d = parse_date(line)
            if d:
                checkin_date = d
        if any(k in ll for k in ("departure", "check-out", "check out", "checkout")):
            d = parse_date(line)
            if d:
                checkout_date = d

    # Fallback: grab first two dates in order from body
    if not checkin_date or not checkout_date:
        raw_dates = re.findall(
            r"\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}",
            text, re.IGNORECASE
        )
        parsed = [d for d in (parse_date(s) for s in raw_dates) if d]
        if len(parsed) >= 2:
            checkin_date, checkout_date = parsed[0], parsed[1]

    if not checkin_date or not checkout_date:
        log.warning(f"Could not extract dates for '{guest_name}'")
        return None
    if checkout_date <= checkin_date:
        log.warning(f"Invalid dates for '{guest_name}': checkin={checkin_date} checkout={checkout_date}")
        return None

    return {
        "guest_name":    guest_name,
        "checkin_date":  checkin_date,
        "checkout_date": checkout_date,
        "device_id":     device_id,
    }


# ── Seam helpers ──────────────────────────────────────────────────────────────

def _curl(method, path, body=None):
    cmd = ["curl", "-s", "-X", method,
           "-H", f"Authorization: Bearer {ROOMS_KEY}",
           "-H", "Content-Type: application/json"]
    if body:
        cmd += ["--data-raw", json.dumps(body)]
    cmd.append(SEAM_API + path)
    r = subprocess.run(cmd, capture_output=True, timeout=15)
    return json.loads(r.stdout)


def existing_pins(device_id):
    r = _curl("GET", f"/access_codes/list?device_id={device_id}")
    return {c.get("code") for c in r.get("access_codes", []) if c.get("code")}


def generate_pin(used):
    for _ in range(200):
        pin = str(random.randint(1000, 9999))
        if pin not in used and pin not in WEAK_PINS:
            return pin
    raise RuntimeError("Could not generate unique PIN")


def create_code(reservation):
    checkin_utc  = eastern_to_utc(reservation["checkin_date"], 12)
    checkout_utc = eastern_to_utc(reservation["checkout_date"], 10)
    device_id    = reservation["device_id"]
    guest_name   = reservation["guest_name"]

    used = existing_pins(device_id)
    pin  = generate_pin(used)

    code_name = f"{guest_name} Booking.com"
    r = _curl("POST", "/access_codes/create", {
        "device_id": device_id,
        "name":      code_name,
        "code":      pin,
        "starts_at": checkin_utc.isoformat().replace("+00:00", "Z"),
        "ends_at":   checkout_utc.isoformat().replace("+00:00", "Z"),
    })
    if r.get("access_code"):
        log.info(f"Created '{code_name}' PIN={pin} checkin={checkin_utc.date()} checkout={checkout_utc.date()}")
        return True
    log.error(f"Seam create failed: {r}")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not GMAIL_USER or not GMAIL_PASS:
        log.info("Gmail credentials not set — skipping booking sync")
        return
    if not ROOMS_KEY:
        log.error("account1_api_key not set — cannot create codes")
        return

    log.info("Connecting to Gmail IMAP...")
    try:
        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
            imap.login(GMAIL_USER, GMAIL_PASS)
            imap.select("INBOX")

            _, msgids = imap.search(None, 'UNSEEN FROM "booking.com"')
            ids = msgids[0].split()
            if not ids:
                log.info("No new Booking.com emails")
                return

            log.info(f"Found {len(ids)} unread Booking.com email(s)")
            for msgid in ids:
                try:
                    _, data = imap.fetch(msgid, "(RFC822)")
                    msg = email.message_from_bytes(data[0][1])
                    subject = decode_str(msg.get("Subject", ""))
                    log.info(f"Processing: {subject[:80]}")

                    # Skip non-reservation emails
                    sl = subject.lower()
                    if any(k in sl for k in ("cancel", "modif", "message from guest",
                                              "review request", "reminder", "expir")):
                        log.info("Skipping non-reservation email")
                        imap.store(msgid, "+FLAGS", "\\Seen")
                        continue

                    body        = get_body(msg)
                    reservation = parse_reservation(subject, body)
                    if reservation and reservation["guest_name"] in SKIP_CODES:
                        log.warning(f"Guest name '{reservation['guest_name']}' matches a "
                                    f"permanent resident code — skipping, not creating a booking code")
                        imap.store(msgid, "+FLAGS", "\\Seen")
                    elif reservation:
                        ok = create_code(reservation)
                        if ok:
                            imap.store(msgid, "+FLAGS", "\\Seen")
                    else:
                        # Mark read so we don't retry an unparseable email every 15 min
                        log.warning("Could not parse reservation — marking read")
                        imap.store(msgid, "+FLAGS", "\\Seen")

                except Exception as e:
                    log.error(f"Error processing email {msgid}: {e}", exc_info=True)

    except imaplib.IMAP4.error as e:
        log.error(f"Gmail IMAP login failed: {e}")
    except Exception as e:
        log.error(f"Booking sync error: {e}", exc_info=True)


if __name__ == "__main__":
    main()
