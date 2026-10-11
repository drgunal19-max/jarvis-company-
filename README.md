
# J.A.R.V.I.S. Company Intelligence & Automation AI

A runnable starter MVP for a personal/company automation assistant. It includes:
- FastAPI backend and SQLite persistence
- Task creation/listing/pause/resume/cancel
- Manual job discovery against the public Arbeitnow job API
- Basic relevance scoring and deduplication
- Findings inbox and a simple dashboard
- Optional scheduled polling worker
- Importable n8n workflow example
- Docker Compose setup

> This is a functional MVP scaffold, not a claim that every external integration is connected. Zoho, email, LinkedIn, messaging and other sources require provider-specific authorization and credentials. LinkedIn-wide crawling is not included; use permitted APIs, alerts or authorized data sources.

## Quick start (Windows / VS Code)

1. Install Python 3.11+.
2. Extract this ZIP and open the `JARVIS_Company_AI` folder in VS Code.
3. In the VS Code terminal:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

4. Open `http://127.0.0.1:8000`.
5. API docs: `http://127.0.0.1:8000/docs`.

If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope Process Bypass` in that terminal, then activate again. Or use `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` and `.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload`.

## Run a job discovery search

- Open the dashboard.
- Create a task such as “Find computer-skilled assistant in Bengaluru”.
- Click **Run discovery** on the task.
- Review findings and open original source links.

The starter uses Arbeitnow's public job API and keyword filtering. API availability and fields can change; check the run history if a source is unavailable.

## n8n

1. Sign in to your n8n instance.
2. Import `n8n/JARVIS_Job_Discovery_Starter.json`.
3. Configure the Manual Trigger workflow and execute it.
4. The starter workflow calls Arbeitnow and filters roles with relevant keywords.
5. To send results into this app, configure the HTTP Request node to call your running backend's authenticated integration endpoint after you deploy and enable that endpoint. Do not expose a local development server publicly without access controls.

## Docker (optional)

```bash
docker compose up --build
```

Then open `http://localhost:8000`.

## Environment variables

Copy `.env.example` to `.env`. Never commit secrets. The MVP works without an LLM API key; ranking uses deterministic keyword matching. Configure an LLM provider only after adding the relevant implementation and credential.

## Important limitations

- The MVP is a foundation to extend, not a complete enterprise product.
- No Zoho or mailbox is connected by default.
- No background website crawling or private-profile access.
- Scheduled checks are optional and should respect source terms/rate limits.
- Do not automatically contact candidates or send external messages without an explicit approval workflow.
