# Deployment templates

These are generalized systemd **user** unit templates for Apex's two bounded
workers. They are examples, not an installer.

- Replace `<repo>` in each `.service` with the absolute path of your checkout.
- `%h` is expanded by systemd to your home directory (uv is assumed at
  `%h/.local/bin/uv`).
- Bring PostgreSQL up first (`python scripts/dev.py db-up`); if it is
  unreachable the workers fail closed with a visible error.
- `apex-operating-cycle-worker` fires every 5 minutes;
  `apex-live-observer-worker` fires every minute and uses only public,
  credential-free market data.
- Optional provider credentials are read from `%h/.config/apex/alpaca.env`
  if present; the file is never part of this repository.
- On WSL2, user timers run only while the VM runs; enable linger with
  `loginctl enable-linger "$USER"`.

Nothing here enables LIVE trading; Apex has no LIVE authority.
