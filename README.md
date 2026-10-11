# J.A.R.V.I.S. Company Intelligence & Automation

J.A.R.V.I.S. is a locally runnable FastAPI application for monitoring tasks, public job discovery, persisted findings, user review, preferences, and optional scheduled checks. It is a single-user MVP; it is not a hosted or multi-tenant enterprise product.

## Working features

- Responsive dashboard and JSON API.
- Task creation, natural-language task extraction, edit, pause, resume, cancel, and manual discovery.
- Chat with an OpenAI-compatible model, with an explicit optional web-search toggle and cited search results.
- Browser speech-to-text input and optional spoken replies when the browser supports its Web Speech APIs.
- Chat can create a local discovery task when explicitly requested; it does not run the task automatically.
- WhatsApp Business Cloud API text sending, signed webhook verification, inbound text/status handling, and local message history. Requires Meta Business app credentials and recipient opt-in.
- Google OAuth with read-only Gmail search, upcoming Calendar events, and Drive file-metadata search. OAuth tokens are encrypted in the local SQLite database.
- Instagram Login for professional accounts with read-only profile and recent-media metadata. OAuth tokens are encrypted in the local SQLite database.
- Optional LLM-assisted task-field extraction through an OpenAI-compatible API. Without credentials, a deterministic local parser is used for task commands.
- Persistent SQLite storage, including automatic schema migration for existing MVP databases.
- Public Arbeitnow job API checks, keyword/location scoring, source URL validation, match explanations, and idempotent finding storage.
- Per-task public RSS/Atom feeds over HTTPS, with DNS/private-address checks, pinned public-IP connections, redirects disabled, bounded response size, match scoring, deduplication, and independent feed health.
- Finding review states, run history, source health, audit events, morning briefing, saved preferences, and editable key/value memory.
- JSON data export and permanent task deletion with related findings, run history, and configured-source cleanup; deletion is written to the retained audit log.
- Global emergency stop and optional backend-owned polling schedules.
- Optional bearer-token protection for the API, including an authenticated n8n finding-ingestion endpoint.
- Docker Compose for the app and optional local n8n service.

WhatsApp Cloud API, Google Workspace, and Instagram support are implemented but are not live until configured with authorized provider credentials. Instagram access is limited to professional-account profile and media metadata; it does not publish content or read inbox messages. Zoho, LinkedIn, telephony, third-party messaging beyond WhatsApp, and production notifications are **not connected**. JARVIS is not a general computer-control agent and cannot access every app or account automatically. No job discovery provider is guaranteed to be available 24/7.

## Run locally on Windows

Python 3.11 or later is supported. Use PowerShell in VS Code:

```powershell
cd C:\Users\Gunal\Downloads\JARVIS_Company_AI_Package\JARVIS_Company_AI
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uvicorn app.main:app --reload
```

If execution policy prevents activation:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

Open <http://127.0.0.1:8000>. API docs: <http://127.0.0.1:8000/docs>. Health endpoint: <http://127.0.0.1:8000/api/health>.

The database defaults to `jarvis.db` in the project directory. For shared or production use, set `DATABASE_URL` to persistent storage, set a long random `APP_API_TOKEN`, use HTTPS, and do not expose the development server directly to the internet. The dashboard accepts the configured API token in its “Sources & connections” section and stores it only for the current browser tab.

## Deploy to Render

The root `render.yaml` Blueprint deploys the Dockerized JARVIS web app on a paid Render Starter instance with a persistent SQLite disk and a Render-generated `APP_API_TOKEN`. The Docker image excludes `.env`, local databases, and the development virtual environment. The container listens on Render's assigned `PORT`; local Docker runs continue to default to port 8000.

1. Push this project to a GitHub or GitLab repository you control. Do not commit `.env`, credentials, or local database files.
2. In Render, create a **Blueprint** from that repository and apply `render.yaml`. The persistent disk requires a paid service plan.
3. Wait for the `/api/health` health check to pass. Open the created JARVIS service URL and enter the generated `APP_API_TOKEN` in **Sources & connections** (retrieve it from the Render service's environment settings).
4. Add provider secrets using the Render dashboard's environment settings, then redeploy. To enable a provider, set its required credentials and its exact public HTTPS OAuth callback URL: `/api/google/callback` or `/api/instagram/callback`. Register that exact URL in the provider console. Generate and keep stable separate Fernet keys for Google and Instagram token encryption. Register `/api/webhooks/whatsapp` with Meta if inbound WhatsApp events are needed.
5. For hosted web search/chat, add `LLM_API_KEY` and `TAVILY_API_KEY`; provider accounts/scopes and approvals are still required. The Blueprint intentionally does not fabricate credentials or claim external integrations are connected.
6. To use the currently running local n8n with the deployed app, set its `JARVIS_API_BASE_URL` to the deployed HTTPS service URL and its `JARVIS_API_TOKEN` to the same generated app token, then restart n8n. Keep the n8n editor private or protected by its own account login. The imported discovery workflow is manual; activate or run it only after reviewing its effects on active tasks.

This Blueprint deploys JARVIS, not the n8n editor. A deploy cannot be started from this workspace until it is pushed to a Git provider and connected to an authorized Render account. Keep the API token private; the app requires it for API access after deployment.

## Task workflow

1. Add a task using the form or the assistant command.
2. Review the extracted location and keywords before running it. Without `LLM_API_KEY`, extraction is rule-based.
3. Choose **Run discovery** to query the public Arbeitnow API.
4. Review ranked matches, explanations, and original links in Findings.
5. Mark each item relevant, irrelevant, duplicate, contacted, or completed.
6. Use Preferences and Persistent memory to store defaults and notes.
7. Add optional RSS/Atom feeds in “Sources & connections”; each feed is tied to a task and only fetched when that task runs.

### RSS / Atom sources

Only public HTTPS feeds on standard port 443 are accepted. Local/private/reserved IP targets are rejected; the hostname is resolved again and the HTTPS request is pinned to a resolved public IP with the original hostname retained for TLS verification. Redirects are not followed, and responses are capped at 2 MB / 500 entries. If a feed host blocks this app's network, its failure appears in source health and runs. Do not add private or signed feed URLs containing access tokens; the app stores the configured feed URL.

Automatic polling is disabled by default. To enable backend-owned polling:

1. Set `ENABLE_SCHEDULER=true` and choose `POLL_INTERVAL_MINUTES` in `.env`.
2. Set a task's “Run every” interval above zero.
3. Restart the app. The backend checks for due tasks on its polling interval. There is no separate scheduler in n8n, avoiding duplicate scheduling.

The global emergency-stop control blocks manual and scheduled source checks.

## Optional AI task extraction

Set an OpenAI-compatible provider in `.env`:

```dotenv
LLM_API_KEY=your-provider-key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

Restart the app. The integration dashboard reports this as configured but not verified until a request is made. Prompts are sent to the configured provider; do not enable this if your organization cannot share these task instructions with that provider. A failed configured provider call is reported as an error rather than silently falling back. Without a key, the local parser is used.

## Chat, web search, and voice

Set `LLM_API_KEY` (and, if needed, `LLM_BASE_URL` and `LLM_MODEL`) to enable chat. The configured OpenAI-compatible provider receives chat messages and up to the 20 most recent stored conversation messages. Conversation history is stored in the local SQLite database. To enable the chat's **Search the web** option, set `TAVILY_API_KEY`; search queries and result excerpts are sent to Tavily and then included as reference material for the language model. Search is opt-in for each message. The app reports provider errors instead of claiming a search or answer succeeded.

Voice input uses the browser's Web Speech API when supported; browser vendors may process speech according to their own service and privacy policy. Spoken replies use browser speech synthesis. Voice input fills the message box only; review and send the transcript yourself. HTTPS (or localhost) and browser microphone permission may be required.

## WhatsApp Business Cloud API

The dashboard can send user-initiated text messages and display inbound text messages and delivery states. Configure these values in `.env` from your Meta app and WhatsApp Business account:

```dotenv
WHATSAPP_ACCESS_TOKEN=your-system-user-or-provider-token
WHATSAPP_PHONE_NUMBER_ID=your-phone-number-id
WHATSAPP_VERIFY_TOKEN=your-own-long-random-webhook-verification-token
META_APP_SECRET=your-meta-app-secret
META_GRAPH_API_VERSION=v23.0
```

Keep these credentials in a secret manager for deployment; never put them in workflow exports or source control. The app's webhook callback is `/api/webhooks/whatsapp`. Register its public HTTPS URL in Meta's WhatsApp webhook configuration, use the same `WHATSAPP_VERIFY_TOKEN`, and subscribe to the `messages` field. The webhook checks Meta's `X-Hub-Signature-256` against the raw request body using `META_APP_SECRET`. Localhost is not reachable by Meta; use a properly secured HTTPS deployment for inbound webhooks.

Messages and delivery metadata are stored in the local app database. Inbound media is not downloaded. Sending requires an explicit UI submission and an opt-in confirmation; the integration does not auto-reply. This version sends text only. Follow WhatsApp's consent, template, quality, and customer-service-window policies; use approved templates where required.

## Google Workspace (read-only)

Create a Google Cloud OAuth **Web application** client and enable Gmail API, Google Calendar API, and Google Drive API. Register the exact `GOOGLE_REDIRECT_URI` value from `.env` as an authorized redirect URI. Add the client ID and secret to `.env`; also create a Fernet key for encrypted token storage:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the generated value as `GOOGLE_TOKEN_ENCRYPTION_KEY`, restart JARVIS, and use **Connect Google account** in Sources & connections. Google OAuth tokens are encrypted at rest in SQLite; never change or lose the encryption key while the tokens are stored. Disconnecting removes the local tokens.

The app requests only `gmail.readonly`, `calendar.readonly`, and `drive.metadata.readonly`. Gmail search displays message headers (not message bodies); Calendar displays upcoming event metadata; Drive search reads file metadata only. Nothing is fetched until you request a Gmail search, Calendar load, or Drive search. Google may require OAuth consent-screen configuration, test-user setup, or scope verification before authorizing these sensitive scopes. Inbound OAuth callbacks require browser cookies; public deployments must use HTTPS and an exact registered callback URL.

## Instagram professional account (read-only)

In Meta for Developers, configure the **Instagram API with Instagram Login** product for a Business or Creator account. Register the exact `INSTAGRAM_REDIRECT_URI` callback and set `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, and `INSTAGRAM_TOKEN_ENCRYPTION_KEY` in `.env`. Generate a separate token-encryption key using the Fernet command above. Restart JARVIS and select **Connect Instagram professional account**. Meta app review or account-role approval may be required before other accounts can authorize.

The only requested permission is `instagram_business_basic`. The app reads the connected professional account's basic profile and up to 12 recent media records when you select **Load profile and recent media**. It does not read messages, publish, like, comment, or access personal Instagram accounts. Tokens are encrypted in SQLite; keep the encryption key stable and disconnect to remove the local token.

## API protection and n8n

`APP_API_TOKEN` is optional for local development. Set it to a high-entropy secret before sharing the API or enabling n8n ingestion. All `/api/*` routes except health require `Authorization: Bearer <APP_API_TOKEN>` when set. This is single shared-token access, not user accounts or role-based authorization.

### Docker Compose

Copy `.env.example` to `.env` if not present, set a strong `APP_API_TOKEN`, then:

```powershell
docker compose up --build
```

App: <http://127.0.0.1:8000>. n8n: <http://127.0.0.1:5678>. Both ports bind to localhost. n8n uses its persisted volume for its database and credentials.

### Import the backend workflow

1. Configure `APP_API_TOKEN` in the project `.env`.
2. Pass the same value to n8n as `JARVIS_API_TOKEN`. Docker Compose does this automatically.
3. The local Compose stack allows expressions to read `JARVIS_API_TOKEN`. This is convenient for the single-user local demo. For a shared/production n8n instance, prefer an encrypted Header Auth credential and do not globally enable environment access in workflow expressions.
4. Import `n8n/JARVIS_Backend_Manual_Discovery.json`.
5. The workflow calls the app to list active tasks and run their public-source checks. It is manual only; backend scheduling remains authoritative.
6. The supplied workflow uses the Compose service hostname `http://jarvis:8000`. If n8n is not on the same Docker Compose network, edit both workflow URLs to the reachable app base URL.

For a local Windows setup without Docker, run JARVIS as above, then start n8n in a second PowerShell terminal:

```powershell
$env:N8N_HOST = "127.0.0.1"
$env:N8N_LISTEN_ADDRESS = "127.0.0.1"
$env:N8N_PORT = "5678"
$env:N8N_PROTOCOL = "http"
$env:N8N_EDITOR_BASE_URL = "http://127.0.0.1:5678/"
$env:WEBHOOK_URL = "http://127.0.0.1:5678/"
$env:N8N_SECURE_COOKIE = "false"
$env:N8N_BLOCK_ENV_ACCESS_IN_NODE = "false"
$env:JARVIS_API_BASE_URL = "http://127.0.0.1:8000"
$env:JARVIS_API_TOKEN = ""
n8n start
```

Open <http://127.0.0.1:5678>, finish n8n's owner setup, and import `n8n/JARVIS_Backend_Manual_Discovery.json`. The workflow uses `JARVIS_API_BASE_URL` so it can connect to either local JARVIS or the Compose service. The local example has no app API token; set `JARVIS_API_TOKEN` to the same strong token as `APP_API_TOKEN` if you configure API authentication. n8n environment access is enabled for this workflow's token expression; keep both services bound to localhost and use encrypted n8n credentials instead for a shared deployment.

`POST /api/n8n/findings` is also available for authorized workflows that collect elsewhere. Send:

```json
{
  "findings": [{
    "task_id": 1,
    "source": "Authorized source",
    "source_id": "stable-source-id",
    "title": "Opportunity title",
    "url": "https://example.com/item",
    "company": "Example",
    "location": "Bengaluru",
    "summary": "Evidence summary",
    "score": 75
  }]
}
```

The API token is required for that endpoint even when other local API routes are open. Duplicate `(task_id, source, source_id)` records are ignored.

The older `n8n/JARVIS_Job_Discovery_Starter.json` remains as an independent public-feed demonstration; it does not authenticate to this backend or save its output.

## Run tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests use temporary SQLite databases and mocked job feeds. They do not require external API credentials or claim that the public feed is currently reachable.

## Deployment and security notes

- Docker Compose is a local demonstration, not a 24/7 cloud deployment.
- For continuous operation, deploy the app to a managed container service with a persistent disk/database, secret manager, HTTPS reverse proxy, backups, and health monitoring. Set `ENABLE_SCHEDULER=true` only on one app instance unless a distributed scheduler/locking strategy is added.
- Do not run multiple scheduler-enabled replicas; this MVP has no distributed lock or job queue.
- `APP_API_TOKEN` is a shared bearer token, not user/tenant isolation. Use a real identity provider and authorization layer before multi-user use.
- The app defaults to read-only discovery. It does not send email, contact candidates, modify external records, or access private profiles.
- Avoid adding private API credentials to source or workflow JSON. Configure them in environment secrets or n8n credentials.
- Follow each source's terms, permissions, consent requirements, and rate limits. RSS checks occur only when a user runs the associated task or when the optional backend scheduler is enabled.
- SQLite is suitable for a local/single-instance MVP. PostgreSQL, migrations via a migration tool, and backups are not currently implemented.
- LLM calls use the configured provider and have provider-dependent cost and data handling.

## Troubleshooting

- **401 from API:** enter the exact `APP_API_TOKEN` in the dashboard for this browser tab.
- **n8n is unauthorized:** confirm `JARVIS_API_TOKEN` matches `APP_API_TOKEN` and is available to workflow expressions.
- **Source check failed:** review Runs and Sources; public API availability and response formats can change.
- **Changes in `.env` have no effect:** restart Uvicorn; configuration is loaded when Python starts.
- **SQLite locked or missing:** ensure the configured database path is writable and only one scheduler-enabled app instance is running.
