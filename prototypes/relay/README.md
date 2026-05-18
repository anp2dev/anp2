# anp2-relay

Reference relay for ANP2 (ANP2) v0.1.

- Phase 0/1 minimal: accepts events of kinds 0/1/2/4 etc., Ed25519 verification, SQLite append-only storage, simple filter queries
- Listens on `127.0.0.1:8000` (private, not publicly exposed)
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
