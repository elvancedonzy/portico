# Contributing

Thanks for looking. Portico is a solo-maintained side project — please help keep contributions small and focused so I can actually review them.

## Before opening a PR

- Open an issue first for anything larger than a typo or a one-line fix. Explain the use case.
- One PR per concern. Big multi-topic PRs get closed.
- Don't reformat unrelated code. Lint changes go in their own PR.
- Keep the diff readable. `git rebase -i` to squash if needed.

## Bug reports

Include:
- Portico version (`config.yaml` `version:` field)
- Home Assistant version
- Which script misbehaved (`seam_sync`, `airbnb_extend`, `booking_email_sync`, dashboard)
- The relevant chunk of `/data/*.log` — **redact API keys and guest names before pasting**
- What you expected vs what happened

## Security

Do not open a public issue for a security problem. See [SECURITY.md](SECURITY.md).

## Testing

There isn't a test suite in v0.1. If you're touching the extension delta logic in `airbnb_extend.py`, that's the highest-priority area for tests — please add coverage in the same PR.

## Style

- Python: standard library only where possible. If you must add a dep, put it in the Dockerfile `pip3 install` line and note the reason in the PR.
- No async. This runs as a cron job; synchronous code is fine.
- Log at `INFO` for normal operation, `WARNING` when skipping a scheduled action, `ERROR` for something the user should look at, `CRITICAL` for exits.
- Comment the *why*, not the *what*. The code shows the what.

## Repo layout

```
portico/                # HA add-on repo root
├── LICENSE
├── README.md
├── SECURITY.md
├── CONTRIBUTING.md
├── repository.yaml     # HA add-on repo manifest (points to portico/)
└── portico/            # the add-on itself
    ├── config.yaml     # add-on manifest + options schema
    ├── Dockerfile
    ├── build.yaml      # per-arch base image
    ├── run.sh          # s6-style entrypoint
    ├── crontab
    ├── shared_config.py
    ├── server.py       # dashboard HTTP proxy
    ├── seam_sync.py    # every-minute lock↔lock sync
    ├── airbnb_extend.py # every-2-min reservation-shift sync
    ├── booking_email_sync.py
    ├── seam_log_trim.sh
    └── dashboard.html
```

## License

By contributing, you agree that your contribution is licensed under the Apache-2.0 license (see LICENSE).
