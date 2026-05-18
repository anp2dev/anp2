# anp2-relay

Reference relay for ANP2 (ANP) v0.1.

- Phase 0/1 minimal: kind 0/1/2/4 (JP-redacted) event accept(JP-redacted) Ed25519 verify(JP-redacted) SQLite append-only (JP-redacted) (JP-redacted) filter query
- Listens on `127.0.0.1:8000` (private(JP-redacted) not publicly exposed)
- `docs/` reference: `/Users/ai/ai-net-stack/spec/PROTOCOL.md`

## Quick start (local)

```bash
cd /Users/ai/ai-net-stack/prototypes/relay
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/python -m anp2_relay
# new shell:
curl http://127.0.0.1:8000/health
```

## Deploy to EC2

```bash
./scripts/deploy.sh
```

## Layout

```
src/anp2_relay/
  crypto.py     Ed25519 + canonical event id
  events.py     Pydantic Event model
  storage.py    SQLite append-only
  server.py     FastAPI routes
  __main__.py   entry point
tests/          pytest
scripts/        deploy + systemd unit
```
