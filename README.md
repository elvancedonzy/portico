# Portico

A Home Assistant add-on that keeps your **Airbnb / Booking.com / VRBO** reservations in sync with your **smart locks** — automatically. When a guest extends their stay, the door code extends too. When a booking cancels, the code expires. All within 2 minutes, without you touching anything.

Built for short-term rental hosts who are tired of updating lock codes twice — once in the booking platform, once on the lock.

Works with Schlage, Yale, Kwikset, and any lock supported by [Seam](https://seam.co).

## What it does

- **Auto-extend on Airbnb changes.** Guest extends their stay → the Seam access code's `ends_at` shifts by the same delta. PIN preserved. End-to-end in ~2 min.
- **Booking.com email intake.** New Booking.com reservation email → time-bound access code created automatically with a strong random PIN.
- **Lock-to-lock mirroring.** Per-room codes automatically appear on the front door as `s~ <name>`; whole-house codes appear on every room lock as `h~ <name>`. seam_sync runs every minute.
- **Baseline-tracked delta logic.** Airbnb ICS is date-only (midnight), but your code has a real check-out hour. Portico records the calendar baseline on first bind and only applies the *delta* on future extensions — preserving your chosen hour-of-day.
- **Safety caps.** Won't extend more than 30 days in one delta. Won't shorten more than 2 days. Never touches codes named in `skip_codes` (owners, cleaners).
- **Dashboard.** Dark-mode, mobile-friendly, HA Ingress. See all locks, all codes, all guests at a glance.

## Requirements

- Home Assistant OS or Supervised (add-ons required)
- One or two [Seam](https://seam.co) accounts with your locks paired
- Airbnb calendar ICS URLs added to HA as `calendar.*_airbnb` entities *(optional but required for auto-extend)*
- Booking.com reservation email inbox with an app password *(optional, only for Booking intake)*

## Install

1. In HA: **Settings → Add-ons → Add-on Store → three-dot menu → Repositories**
2. Paste: `https://github.com/elvancedonzy/portico`
3. Refresh the store, find **Portico**, click Install
4. Open the add-on's **Configuration** tab, fill in:
   - `account1_api_key` — Seam key for your room locks
   - `account2_api_key` — Seam key for your main-entry locks (can be the same as account1)
   - `rooms` — list of `{name, device_id}` for each room lock
   - `front_door_device_id` — main entry lock's Seam device ID
   - `skip_codes` — names of permanent codes never to touch (owners, cleaners)
5. Start the add-on
6. Open the sidebar panel to see your locks

## How it works

Three cron jobs run inside the add-on:

- `seam_sync.py` — every minute. Bidirectional lock-to-lock replication using name prefixes.
- `airbnb_extend.py` — every 2 minutes. Reads HA Airbnb calendar entities. On first sight of a reservation, records its date window as a baseline. On later polls, if the calendar shifted, applies the *same shift* to the Seam code.
- `booking_email_sync.py` — every 15 minutes. Polls Gmail IMAP for Booking.com reservation confirmations and creates time-bound codes.

State lives in `/data/` (persisted across restarts). Nothing about your reservations leaves your HA host — Portico calls Seam and (optionally) Gmail; that's it.

## Security

Before enabling Booking.com intake, please read **[SECURITY.md](SECURITY.md)** — an attacker who forges a Booking.com email that reaches your inbox can create an access code on your locks. There are mitigations, but understand the model.

Known issues in v0.2:
- Booking.com email intake trusts email that reaches the configured inbox — a forged email = an unlock. Disable this feature or use a dedicated inbox.
- Add-on options (API keys) are stored in `/data/options.json` in plaintext inside the container.

Fixed in v0.2:
- Port 8765 is no longer published to the LAN — only Ingress and same-host add-ons on `hassio_network` can reach it. The proxy also requires the Ingress header on state-changing calls.

Report vulnerabilities to `elvancedonzy@gmail.com`. Please do not open public issues for security problems.

## Status

**v0.2 — released 2026-07-30.** Runs the author's real short-term rental. Extension flow tested end-to-end. Not yet on HACS; install via add-on repository as described above.

### Changelog

- **v0.2.0** (2026-07-30) — Removed LAN port publication; the proxy now requires the HA Ingress header. Bumped add-on version.
- **v0.1.0** (2026-07-30) — Initial public release.

## License

Apache-2.0. See [LICENSE](LICENSE).

Portico is not affiliated with Seam Labs, Inc., Airbnb, Booking.com, or Home Assistant.
