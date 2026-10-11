
# Architecture

Browser dashboard -> FastAPI -> SQLite database
                               -> discovery service -> public Arbeitnow job API
n8n manual workflow -> authenticated FastAPI task/run APIs
n8n source workflows -> authenticated /api/n8n/findings ingestion API

## Implemented local MVP
- Task CRUD, terminal cancellation, manual execution, and opt-in interval polling.
- Rule-based task command extraction; optional configured OpenAI-compatible extraction.
- Persistent preferences/memory, findings review states, source health, run history, briefing, and audit log.
- Finding persistence with `(task_id, source, source_id)` uniqueness and deduplication.
- Global emergency stop and optional single-instance backend scheduler.
- Optional shared bearer-token API protection and n8n finding ingestion.
- Per-task public RSS/Atom sources with HTTPS-only URLs, public IP validation and pinned connections, bounded XML parsing, and per-feed health.
- JSON data export and deliberate task deletion with retained audit records.

## Not connected / production gaps
- Zoho APIs, mailbox/calendar providers, LinkedIn APIs/alerts, telephony, and external messaging.
- OAuth flows, user accounts, roles, tenant isolation, PostgreSQL, and distributed queue/locking.
- Production notification delivery, automated approval actions, and cloud deployment.

The app does not claim coverage for sources without a configured and authorized connector. The scheduler should run on only one app instance until distributed locking/queueing is added.
