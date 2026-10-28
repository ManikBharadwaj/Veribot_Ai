# VeriBot AI: Talent Verification & ATS for Placement Cells

Upload a batch of resume PDFs, pick a target role, and get for every candidate:
**ATS score · Trust score · Role-fit % · Fraud/inflation flags**, plus optional
marksheet/ID cross-checking and a CSV export of the filtered talent pool.

Built with Python, FastAPI, SQLite + SQLAlchemy, pdfplumber/pypdf, pytesseract and plain HTML/JS with Tailwind (CDN).

---

## 1. Run it locally (step by step)

### Step 0: Install the prerequisites
* **Python 3.10 or newer**: check with `python --version` (Windows: `py --version`).
* **Tesseract OCR** (only needed for reading scanned marksheets / images):
  * Windows: install from <https://github.com/UB-Mannheim/tesseract/wiki>. Default path is `C:\Program Files\Tesseract-OCR\tesseract.exe`.
  * macOS: `brew install tesseract`
  * Ubuntu/Debian: `sudo apt install tesseract-ocr`
  * Without Tesseract everything else still works; documents just show as "unreadable" when they need OCR.

### Step 1: Open a terminal in the project folder
```
cd Veribot_Ai
```

### Step 2: Create a virtual environment (keeps packages tidy)
```
python -m venv venv
```
Activate it:
* Windows (PowerShell): `venv\Scripts\Activate.ps1`
* Windows (cmd): `venv\Scripts\activate.bat`
* macOS / Linux: `source venv/bin/activate`

You should see `(venv)` at the start of your prompt.

### Step 3: Install the packages
```
pip install -r requirements.txt
```

### Step 4: Create your settings file
Create a `.env` file in the project folder and add the settings below.
* **SECRET_KEY** (required for safe logins, and must contain at least 64 characters). Generate one:
  ```
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
  and paste it after `SECRET_KEY=`.
* **ENVIRONMENT**: set to `production` in production. Production also requires `DATABASE_URL`.
* **DATABASE_URL**: Supabase can be used here as the PostgreSQL database. Supabase PostgreSQL does not replace Redis; keep **REDIS_URL** or **UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN** for the job queue and cache.
* **REDIS_URL** or **UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN**: required for asynchronous uploads, status polling, rate limiting, OCR/GitHub caches, and health checks.
* **VERIBOT_UPLOAD_DIR**: private scratch storage shared by the API and worker instances. Keep it on a persistent, access-restricted shared volume when API and worker run on different hosts.
* **GITHUB_TOKEN** (recommended): without it GitHub allows only 60 requests/hour, which a big batch will use up. Create a token with *no scopes* at GitHub → Settings → Developer settings → Personal access tokens.
* **LLM_API_KEY / LLM_BASE_URL / LLM_MODEL** (optional): turns on an AI-written summary and up to 3 extra flags. Leave empty to run rules-only.
* **TESSERACT_CMD** (Windows only, if Tesseract is not found): e.g. `C:\Program Files\Tesseract-OCR\tesseract.exe`

### Step 5: Start the server
```
python -m uvicorn main:app --reload
```
(Use `python -m uvicorn`; plain `uvicorn` is often "not recognized" on Windows.)

In development, the FastAPI server starts and stops a queue worker with its application lifespan. In production, run a separate worker process alongside the API deployment:
```
python -u queue_worker.py
```
Keep the production worker running alongside the web server. The upload screen checks a Redis heartbeat and will explicitly say when the worker is offline instead of showing an unexplained 0% queue wait.
The upload endpoint accepts up to 2,000 resumes and 2,000 supporting documents, with an 8 MB per-file and 128 MB combined upload limit. These limits apply to each request; 20,000-file uploads are not supported as one batch. PDF/OCR work is bounded to 16 worker threads, GitHub and optional LLM requests are bounded to four concurrent calls, and all external requests have timeouts. The worker writes candidate records in one database unit of work and publishes phase progress. Overview totals and averages are computed with database aggregates rather than loading every candidate into application memory. The endpoint responds with a task ID immediately after queueing; API and worker must use the same Redis and `VERIBOT_UPLOAD_DIR`.

### Candidate evidence report
Each candidate is evaluated for the role selected for the batch. The stored `report_json` retains resume field extraction, role scores, a skill evidence matrix, flags, supporting-document checks, and an `investigation` object with claim checks and human-readable findings. The optional LLM review receives role/skill summaries and aggregate GitHub metrics only; raw resume text, names, email addresses, usernames, and repository URLs are not sent. Its findings are advisory and cannot change deterministic scores. The full report is available through the existing `GET /api/candidates/{candidate_id}` endpoint. A single-candidate `/api/verify` endpoint is also available to authenticated clients.

For extracted GitHub links, the analyzer fetches public profile totals and samples up to 30 recently pushed public repositories per profile, then checks language breakdowns and up to 100 recent commits for at most three repositories per profile. Repository reports include sampled commit counts, counts attributed to the candidate's linked GitHub username, first/latest sampled commit dates, and up to eight short commit-message samples. These are bounded samples, not lifetime contribution totals; GitHub only attributes commits when its API associates a commit with a user account. GitHub API data only represents accessible public activity. Upstash caching reduces repeated fetches, but unique profiles still consume GitHub API quota.

LinkedIn links are recorded but profile contents are not scraped. LinkedIn profile verification requires appropriate authorized API access/OAuth. Large batches can take longer than 10–15 seconds: PDF/OCR work, GitHub request quotas, network latency, and optional AI calls make that latency impossible to guarantee for thousands of unique resumes.

The candidate scorecard shows extracted contact and education details, the selected role's relevant skill catalog, claimed-skill evidence, resume consistency flags, and scanned repository summaries. The supported role catalog includes SDE, data science, DevOps, product management, and HR profiles.

### Database migrations
Production schema changes are managed through Alembic; run migrations before starting the API:
```
alembic upgrade head
```
Local development continues to create missing tables on startup.
For an existing database whose tables already match the models, baseline it once with `alembic stamp head` instead of attempting to recreate those tables.

Run the focused integration tests with:
```
python -m pytest
```

### Step 6: Open the app
Go to <http://127.0.0.1:8000>, click **Create an account**, then log in.
Do **not** open `index.html` by double-clicking or with Live Server: it must be served by FastAPI.

### Try it
1. *Upload & Analyze* → choose a role → drop resume PDFs → *Run verification*.
2. Optionally also drop marksheets/ID scans (name them like the resume, or make sure they contain the roll number / name).
3. Open the *Talent Pool*, filter, click a row for the full scorecard, and press **Export CSV**.

---

## 2. Project structure

```
Veribot_Ai/
├── main.py              # FastAPI app: pages, login, API routes, CSV export
├── models.py            # SQLite tables (User, Candidate) with SQLAlchemy
├── agent.py             # The verification engine (no web code in here)
├── templates/
│   ├── auth.html        # Login + register page
│   └── index.html       # Dashboard (Tailwind + vanilla JS)
├── requirements.txt
├── .env.example
└── README.md
```

### How data flows
```
Browser (index.html)
  │  fetch("/api/analyze", FormData: role + resume PDFs + documents)
  ▼
main.py   checks login → validates files (type, size, count)
  │  validates and stores files in private scratch; enqueues task metadata in Redis
  ▼
queue_worker.py  retrieves the task, analyzes it, saves reports, publishes status
  │
  ▼
agent.py  PDF text → regex (email, CGPA, links…) → GitHub API (tech roles)
          → OCR documents → match docs to resumes → compare fields
          → scores + flags
  ▼
main.py   saves each report into SQLite (models.py) and returns JSON
  ▼
Browser   draws tables, score rings and the detail drawer
```

---

## 3. Roles and permissions

| | Placement officer | Recruiter |
|---|---|---|
| Upload & analyze | ✔ | ✔ |
| View / filter / export | ✔ | ✔ |
| Delete candidates | ✔ | ✘ |

Every account sees **only the candidates it uploaded**.

---

## 4. How the scores work

* **ATS score (0-100)**: keywords for the role 40 · standard sections 25 · contact details 10 · readability 15 · measurable results 10.
* **Role fit %**: how well the resume's skills and footprint match the chosen role/department.
* **Evidence score**
  * *Tech roles*: from GitHub: original repos, recent activity, stars, commit depth.
  * *Non-tech roles*: self-reported (certificates, results, LinkedIn link), so it is **capped at 65**. Only a matching document can lift trust higher.
* **Trust score**: with documents: `0.7 × evidence + 0.3 × document points` (verified 100, partial 60, unreadable 40, mismatch 5); without: the evidence score. Then penalties per flag: high −15, medium −5, low −1. Role-fit flags do not reduce trust.
* **Verdict**: *Verified* if trust ≥ 70 and no high flag · *Needs Review* if trust ≥ 40 · otherwise *High Risk*.

### Add a new job role
Open `agent.py`, find `ROLE_PROFILES`, copy an existing entry, give it a new key, a label, `kind` (`"tech"` or `"non_tech"`) and your keyword lists. It appears in the dropdown automatically.

---

## 5. Honest limitations (please read)

* **DigiLocker is a SIMULATION.** It is not connected to the real DigiLocker or any government API. It only checks that an OCR'd document looks like an academic record and that its details match the resume. A "pass" does not prove authenticity. Real verification needs official issuer/partner APIs.
* **LinkedIn is not fetched.** LinkedIn blocks scraping and its terms forbid it. The app only checks that a link exists and uses evidence in the resume itself.
* **GitHub rate limits**: set `GITHUB_TOKEN` for batches.
* **OCR is imperfect**: blurry scans can produce wrong names/numbers; mismatches should be reviewed by a human.
* **Scores are decision support**, not a verdict on a person. Always review flagged candidates manually.
* **Production deployment**: production requires a 64+ character `SECRET_KEY`, `DATABASE_URL`, Redis, HTTPS, and shared protected upload scratch storage for API/worker replicas. The app enables signed CSRF tokens, strict same-site/secure session cookies, login throttling, CSP/security headers, dependency health checks, and asynchronous batch processing. Registration is still open; restrict it or add an invitation flow before public deployment, and define retention/deletion policies for resume data.

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| `uvicorn` not recognized | Use `python -m uvicorn main:app --reload` |
| Page says "Cannot reach backend" / JSON error | Open <http://127.0.0.1:8000>, not Live Server |
| Documents all "unreadable" | Install Tesseract; set `TESSERACT_CMD` on Windows |
| GitHub "rate limit" warnings | Set `GITHUB_TOKEN` in `.env` |
| Logged out after restart | Set a fixed `SECRET_KEY` in `.env` |
| Want a clean database | Stop the server and delete `veribot.db` |
