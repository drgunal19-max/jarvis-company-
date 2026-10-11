
# Integration checklist

## Application API
1. Set a strong `APP_API_TOKEN` in `.env`.
2. Restart the app and enter that token in the dashboard for the current browser tab.
3. Calls other than `/api/health` require `Authorization: Bearer <APP_API_TOKEN>`.

## Public RSS / Atom
1. Create a task, then add an HTTPS RSS/Atom URL in “Sources & connections”.
2. The URL must use port 443 and resolve to public IP addresses. Local/private targets and redirects are blocked; requests pin to a public address for TLS validation.
3. Run the associated task to collect the feed. Per-feed status and failures are visible in Sources and execution history.
4. Do not use private, signed, or token-bearing feed URLs; the app stores the configured URL. Respect feed terms and update intervals.

## Zoho
1. Confirm exact products (CRM, Mail, Recruit, Desk, Projects, etc.).
2. Create/configure an OAuth client using current official Zoho documentation.
3. Request only the scopes required.
4. Implement a connector adapter and test read-only access.
5. Add writes only with approval controls and audit records.

## Email
1. Confirm Google Workspace, Microsoft 365, Zoho Mail, or other provider.
2. Use OAuth and official APIs; never collect the user's mailbox password.
3. Start read-only and store only needed metadata/content.
4. Add labels, thread summaries, follow-up extraction and digest delivery.

## Chat, web search, and browser voice
1. Configure `LLM_API_KEY` and the OpenAI-compatible base URL/model to enable chat.
2. Configure `TAVILY_API_KEY` to enable the explicit per-message web-search option. Search terms and returned excerpts are sent to Tavily and then to the configured language model.
3. Browser voice input uses the browser's Web Speech implementation and requires user microphone permission; speech input is transcribed into the message box and is not sent until the user reviews and submits it.
4. The chat can create a local discovery task when explicitly requested. It never executes the task automatically.

These capabilities do not grant general access to the user's browser, Google account, or third-party apps.

## Google Workspace read-only
1. Create a Google Cloud OAuth Web application client. Enable Gmail API, Calendar API, and Drive API; register the exact `GOOGLE_REDIRECT_URI`.
2. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, and a generated Fernet `GOOGLE_TOKEN_ENCRYPTION_KEY`. Generate a key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
3. Restart the app and connect through its dashboard. OAuth uses a short-lived browser state cookie; the callback URI must match Google Cloud exactly. Use HTTPS in deployment.
4. The app requests `gmail.readonly`, `calendar.readonly`, and `drive.metadata.readonly`. It loads Gmail headers, upcoming Calendar event metadata, and Drive file metadata only when the user requests it. It does not modify Google data or persist fetched Google records.
5. OAuth access/refresh tokens are Fernet-encrypted in SQLite. Back up `GOOGLE_TOKEN_ENCRYPTION_KEY` securely and keep it stable; losing it requires reconnecting Google. Disconnect removes the stored token row.
6. Google can require consent-screen setup, test-user configuration, and verification for sensitive/restricted scopes before an account can authorize.

## Instagram professional account (read-only)
1. In Meta for Developers, enable **Instagram API with Instagram Login** and configure access for a Business or Creator account. Register the exact `INSTAGRAM_REDIRECT_URI`.
2. Set `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_REDIRECT_URI`, and a separately generated Fernet `INSTAGRAM_TOKEN_ENCRYPTION_KEY`; restart the app.
3. Connect from the dashboard. OAuth uses the single `instagram_business_basic` permission and a short-lived, HTTP-only state cookie. The authorization callback must match exactly; use HTTPS for deployment.
4. Select **Load profile and recent media** to fetch basic profile fields and up to 12 media metadata records. No messages, publishing, likes, comments, or personal-account access are implemented.
5. The short-lived login token is exchanged for a long-lived token and encrypted in SQLite; the app refreshes it before expiry. Keep the encryption key stable; disconnect removes the local token. Meta app review/account-role approval may be required.

## WhatsApp Business Cloud API
1. Create/configure a Meta app with WhatsApp Business Cloud API, a business phone number, and an authorized access token. Record the phone number ID; the app sends using the official Graph API.
2. Set `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN` (a long random value you choose), `META_APP_SECRET`, and `META_GRAPH_API_VERSION` in the app's environment. Do not commit credentials.
3. Register the HTTPS callback `<public-app-url>/api/webhooks/whatsapp` with the same verify token, then subscribe to the `messages` field. Webhook delivery is authenticated by checking Meta's `X-Hub-Signature-256` HMAC over the raw request body.
4. The dashboard supports explicit, user-submitted text sends after confirming recipient opt-in. Delivery status and inbound text are stored locally; inbound media is not fetched and no auto-reply is sent.
5. The sender implements text messages only. Follow Meta/WhatsApp consent, template, rate, and 24-hour customer-service-window requirements; this integration does not bypass them. Use a public HTTPS reverse proxy for Meta callbacks, never an exposed development server.

## LinkedIn / job sources
Use permitted APIs, supported integrations, saved-search alerts, public feeds or authorized exports. Respect terms, authentication, rate limits, and privacy restrictions. Do not bypass anti-bot controls.

## n8n
Use `docker compose up --build` for a local n8n instance, then visit <http://127.0.0.1:5678>. Create the owner account, import `n8n/JARVIS_Backend_Manual_Discovery.json`, and execute it manually. Set the same value for `APP_API_TOKEN` and n8n's `JARVIS_API_TOKEN`; the compose service passes this environment variable through to n8n. Ensure your self-hosted n8n policy permits its `$env.JARVIS_API_TOKEN` expression. The workflow calls backend task/run APIs; the backend remains the only scheduler.

To ingest results from an n8n source workflow, send the normalized finding batch to `POST /api/n8n/findings` with the bearer token. The endpoint validates task IDs and HTTP(S) URLs, limits batches, and deduplicates on task/source/source ID. Keep provider credentials in n8n credentials, not workflow JSON or source control.

`n8n/JARVIS_Job_Discovery_Starter.json` is only a public-feed demonstration and does not save data in the app.
