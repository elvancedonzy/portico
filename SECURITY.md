# Security

Portico creates and modifies **physical door codes** based on data from external sources. Please read this before running it in production.

## Reporting a vulnerability

Email `elvancedonzy@gmail.com` with details. Do not open public GitHub issues for security problems. You will get an acknowledgment within 72 hours.

## Threat model

Portico's job is to keep lock codes aligned with reservation state. That means:

- It **holds Seam API keys** with lock-control scope.
- It **reads guest names + arrival/departure dates** from your Airbnb ICS feed and Booking.com email intake.
- It **creates and modifies access codes** on physical hardware.

If any part of the trust chain is compromised, someone gains physical entry. Take the following seriously.

## Fixed in v0.2

### LAN-reachable web UI (fixed)
v0.1 published port 8765 to the host LAN with no auth — anyone on the LAN could hit `POST /access_codes/create` via the proxy and create a lock code. **v0.2 removes the `ports:` mapping**, so port 8765 is only reachable on the internal `hassio_network` (Ingress + HA REST sensors on the same host). As defense-in-depth, the proxy also rejects any request that lacks the `X-Hass-Source: core.ingress` header, so a compromised add-on on the same network still can't call the Seam API through Portico.

## Known issues in v0.2

Ranked by severity.

### 1. Booking.com email intake trusts unauthenticated email
`booking_email_sync.py` reads Gmail via IMAP and creates lock codes from any message that parses as a Booking.com reservation. Booking.com's own emails are not DKIM-signed in a way this parser verifies. **An attacker who can send email into your inbox can create an unlock.** Mitigation:
- Use a dedicated Gmail account that only Booking.com and you know about.
- Enable Gmail's spam and phishing protections.
- Consider disabling this intake and switching to Booking.com's ICS calendar for reservation dates.

### 2. Secrets in plaintext
Seam API keys, Gmail app password, and ICS URLs are stored in `/data/options.json` inside the container in plaintext. Any process in the add-on's namespace can read them. Do not share support logs or a snapshot backup without redacting this file first.

### 3. No signed releases
Users installing from this GitHub repo pull whatever is on `main`. A compromised maintainer account = malicious code shipped. Planned: signed tags and release notes.

## Defensive posture

- **Airbnb extend safety caps** are on by default: won't extend a code more than 30 days in one delta, won't shorten more than 2 days. If Airbnb's ICS ever returns garbage, the script skips the update and logs a warning rather than modifying the code.
- **Codes named in `skip_codes` are never touched.** Put owner and cleaner codes there.
- **Airbnb PINs are never overwritten** by Portico. The extend flow only shifts `starts_at` / `ends_at`.
- **First-bind is a no-op.** The extend flow records a baseline on first observation and makes no code changes until the calendar actually shifts.

## What Portico deliberately does not do

- **Does not send guest data to any third party.** All Seam and Gmail calls originate from your HA host.
- **Does not accept remote unlock commands.** The dashboard's `POST /locks/unlock_door` requires HA Ingress (a logged-in HA user).
- **Does not proxy anything on the internet.** No cloud service, no telemetry.
- **Ingress-only proxy (v0.2).** The `/access_codes/*`, `/locks/*`, `/devices/*` proxy endpoints require the `X-Hass-Source: core.ingress` header. A rogue add-on on the same `hassio_network` cannot use Portico to create or delete lock codes.

## Dependencies

Runtime Python dependencies (Alpine base image + pip):
- `seam` (official Seam Python SDK)

Base image: `ghcr.io/home-assistant/<arch>-base-python:3.11-alpine3.17`

We do not pin `seam` versions yet. If Seam ships a breaking change, `airbnb_extend.py` may crash on next run.
