# GCS Search Macro — Complete User & Deployment Guide

> Enterprise-grade, self-hosted search service for Google Cloud Storage: find search terms inside Python source files or locate files by name across administrator-approved bucket paths, and receive styled Excel reports — securely, asynchronously, and at scale.

This document is the full guide to the project: what it is, the business problems it solves, how to install and run it, and how to use it day to day. For a condensed overview see [README.md](README.md); for the end-to-end flow diagram see [WORKFLOW_DIAGRAM.md](WORKFLOW_DIAGRAM.md); for hardened GCP deployment specifics see [deploy/README.md](deploy/README.md).

---

## Table of Contents

1. [What Is GCS Search Macro?](#1-what-is-gcs-search-macro)
2. [Business Use Case](#2-business-use-case)
3. [Scope — What It Does and Does Not Do](#3-scope--what-it-does-and-does-not-do)
4. [Key Features](#4-key-features)
5. [Architecture](#5-architecture)
6. [Repository Layout](#6-repository-layout)
7. [Prerequisites](#7-prerequisites)
8. [Installation — Local Development](#8-installation--local-development)
9. [Configuration Reference](#9-configuration-reference)
10. [Using the Application](#10-using-the-application)
11. [The Excel Report Explained](#11-the-excel-report-explained)
12. [Result Caching in BigQuery](#12-result-caching-in-bigquery)
13. [Production Deployment on GCP](#13-production-deployment-on-gcp)
14. [Security Model](#14-security-model)
15. [Operational Limits](#15-operational-limits)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. What Is GCS Search Macro?

GCS Search Macro is a standalone production web service that lets authorized users run **two kinds of searches** across approved Google Cloud Storage (GCS) locations:

| Mode | What it searches | How |
|---|---|---|
| **Content search** | The text *inside* Python (`.py`) files | Downloads each file once and scans every line for your terms (literal text or a safe regex subset), classifying each occurrence as an **exact** or **partial** match |
| **Filename search** | The *names* of objects, any extension | Uses GCS object metadata only — no file contents are ever downloaded — and classifies full case-insensitive filename equality as **exact**, containment as **partial** |

Every search runs as an **asynchronous background job**: the browser UI (or API call) submits a request, a Cloud Tasks queue dispatches it to an isolated worker, and the user polls for completion, then downloads a styled multi-sheet **Excel workbook** of the results. Optionally, matching files can be **copied** to an approved destination bucket, and the report can be **emailed** to up to five colleagues.

The service is designed for corporate environments: it sits behind a VPN, an HTTPS load balancer, Cloud Armor, and Google's Identity-Aware Proxy (IAP), enforces administrator-owned search scopes, isolates every job to its owner, and audits every search access in BigQuery.

---

## 2. Business Use Case

### The problem

Data platform teams accumulate thousands of pipeline scripts, SQL files, and data assets in GCS buckets. Common questions become slow, manual, and error-prone at that scale:

- *"We're renaming/dropping the `customer_id` column — which pipeline scripts reference it?"* (impact analysis before a schema change)
- *"Which jobs still call this deprecated function or read this legacy table?"* (migration and deprecation planning)
- *"A file named `daily_sales_extract.csv` was supposed to land — did it, where, when, and how big is it?"* (data delivery verification)
- *"Auditors need every script that touches PII field X, plus copies of those scripts for evidence."* (compliance and audit evidence collection)
- *"Which scripts matching this pattern are actually still active in Airflow, and when did they last run?"* (operational cleanup)

Doing this by hand means `gsutil ls`/`gsutil cat` loops, ad-hoc scripts on laptops with broad bucket permissions, no audit trail, and results that are stale the moment they are produced.

### The solution

GCS Search Macro turns those questions into a **governed, repeatable, self-service workflow**:

- **Self-service for analysts** — a simple browser form; no SDKs, no local credentials, no shell scripts.
- **Governed by administrators** — users can only search inside bucket/path roots an administrator has approved per *access profile* ("scope"); copy destinations and email domains are allow-listed the same way.
- **Fast on repeat queries** — results are cached in BigQuery keyed on the exact source definition. A re-run only rescans objects that were added or changed since the last run, and drops deleted ones.
- **Operationally enriched** — content-search results are joined against a DAG status table so each matching script shows its `dag_id`, whether it is active, and when it last executed. A job inventory sheet can be appended for full context.
- **Deliverable outputs** — a styled Excel workbook (summary + per-term sheets), a 15-minute signed download URL from a private bucket, optional email delivery, and optional copies of the matching files into an approved evidence bucket under a per-job folder.
- **Auditable** — every search access writes an administrative audit record to BigQuery with a hashed requester identity.

### Who uses it

| Role | How they use it |
|---|---|
| **Data engineers / analysts** | Run impact analysis before schema or pipeline changes; verify file landings |
| **Platform / DevOps teams** | Deprecation sweeps, dead-code discovery, cleanup planning |
| **Compliance / audit teams** | Locate and collect evidence of data usage across the estate |
| **Administrators** | Define scopes (who can search what), copy targets, exclusions, and limits |

---

## 3. Scope — What It Does and Does Not Do

**In scope**

- Concurrent term search inside `.py` files under approved GCS roots (literal terms and a deliberately restricted regex subset).
- Filename search across objects of **every** extension using metadata only.
- Exact/partial classification for both modes; line numbers and surrounding tokens for content matches.
- Asynchronous, durable, owner-isolated jobs (queued → running → succeeded/failed/cancelled).
- BigQuery-backed incremental result caching and access auditing.
- Excel report generation, private-bucket storage, short-lived signed downloads, optional SMTP email delivery.
- Controlled copying of matched files to administrator-approved target buckets.
- DAG-status and job-inventory enrichment for content searches (loaded fresh on every job, even on cache hits).

**Out of scope (by design)**

- Searching contents of non-Python files (content mode is `.py` only; use filename mode for other assets).
- Full-text indexing — every job evaluates live GCS state (with caching); there is no background indexer.
- Storing file contents anywhere: the cache holds metadata, line numbers, and match tokens, never source text.
- Arbitrary regex: groups `()`, counted repetitions `{}`, and backreferences are rejected to prevent catastrophic backtracking.
- Public or unauthenticated access; user-supplied BigQuery table names; searching or copying outside approved roots.
- Cache administration through the UI — cache/manifest maintenance is an administrator activity and never appears in reports.

This directory is a **complete, standalone build context**. It does not import, mount, or read code or data from any other application version.

---

## 4. Key Features

- **Two search modes** — content (inside Python files) and filename (metadata-only, all extensions).
- **Multi-root search** — users add/remove independent bucket + path rows per job; each row may narrow, but never escape, an approved root.
- **Exact vs. partial matching** — identifier-boundary detection separates `PID` from `PID_OLD`; partial matches report the full surrounding token.
- **Header noise suppression** — content search skips leading module docstrings and boilerplate header lines (`Author:`, `Created on:`, `JIRA/SN #:`, etc.) so metadata never pollutes matches.
- **Built-in stale-file exclusion** — content manifests automatically skip `*_old*.py`, date-suffixed copies (`*_20240101.py`), `old_scripts/`, `_copy` files, and change-ticket folders, plus any administrator-configured keywords/patterns.
- **Incremental BigQuery cache** — identical requests reuse cached results if the GCS manifest is unchanged; otherwise only new/updated objects are rescanned and deleted ones are dropped.
- **Operational enrichment** — DAG id / active flag / last-execution timestamp per matched script; optional job inventory sheet.
- **Styled Excel workbooks** — frozen header rows, auto-filters, sized columns, per-term sheets, summary sheet, copy manifest sheet.
- **Approved copy targets** — matched files are copied under `«approved-prefix»/jobs/«job-id»/` to prevent collisions; each copy's outcome is reported.
- **Email delivery** — up to five recipients within configured organization domains, via your SMTP relay; email failure never fails the job (download stays available).
- **Owner isolation** — users can only see, cancel, and download their own jobs; foreign job IDs return 404 to prevent enumeration.
- **Bounded resource usage** — hard caps on files per job, file size, result rows, terms, and bucket rows; bounded-parallelism GCS reads keep memory flat regardless of scope size.

---

## 5. Architecture

```text
Corporate VPN
    │
HTTPS Load Balancer + Cloud Armor + IAP (user/group IAM)
    │
┌───────────────────┐    Cloud Tasks     ┌──────────────────────┐
│  Cloud Run API    │ ─────────────────► │  Cloud Run Worker    │
│  (FastAPI + UI)   │  (job ID only,     │  (concurrency = 1)   │
└───────────────────┘   OIDC-signed)     └──────────────────────┘
    │                                        │            │
    ▼                                        ▼            ▼
 Firestore                              GCS sources   BigQuery
 (job records,                          + approved    (search cache
  owner-keyed)                          copy targets   + access audit)
    │                                        │
    └──── private reports bucket ◄───────────┘
              │
     15-minute signed download URL
```

**Components**

| Component | Module(s) | Responsibility |
|---|---|---|
| **API service** | `api.py`, `auth.py`, `policy.py`, `models.py` | Serves the browser UI, authenticates the IAP identity, validates requests against administrator policy, creates Firestore job records, enqueues Cloud Tasks |
| **Worker service** | `worker.py`, `executor.py` | Accepts Cloud Tasks-only invocations, revalidates policy, transactionally claims the job, executes the search in a per-job temp directory, finalizes state |
| **Search engine** | `search_engine.py` | Lists GCS manifests, applies exclusions, downloads and scans files with bounded parallelism, classifies matches |
| **Cache** | `cache.py` | Ensures/reads/writes the four BigQuery cache tables; diffs manifests; records access audits |
| **Enrichment** | `enrichment.py` | Loads DAG status and job inventory tables from BigQuery |
| **Reporting** | `reporting.py`, `reports.py` | Builds the styled Excel workbook; uploads it to the private, owner-namespaced reports bucket; signs download URLs |
| **Copying** | `copying.py` | Copies unique matched files to the approved target under a job-scoped prefix |
| **Email** | `emailer.py` | Sends the workbook through the organization SMTP relay |
| **Jobs & queue** | `jobs.py`, `queueing.py`, `services.py` | Firestore job store with transactional claim/cancel; Cloud Tasks queue with deterministic (idempotent) task names |
| **Settings** | `settings.py` | Typed, validated environment configuration, including the scope-policy JSON |

**Job lifecycle**: `queued → running → succeeded | failed`, or `queued → cancelled`. Terminal error codes are `QUEUE_UNAVAILABLE` (task creation failed), `POLICY_REJECTED` (administrator policy changed between submit and execution), and `EXECUTION_FAILED` (any runtime failure). See the state diagram in [WORKFLOW_DIAGRAM.md](WORKFLOW_DIAGRAM.md).

---

## 6. Repository Layout

```text
.
├── src/gcs_search_macro_v4/     # Application package
│   ├── api.py                   # Public FastAPI app (UI + /v1 endpoints)
│   ├── worker.py                # Cloud Tasks-only worker FastAPI app
│   ├── executor.py              # End-to-end job execution
│   ├── search_engine.py         # GCS listing + concurrent content/filename search
│   ├── cache.py                 # BigQuery search cache + audit
│   ├── reporting.py             # Excel workbook writer
│   ├── reports.py               # Private report bucket store + signed URLs
│   ├── copying.py               # Approved-target file copying
│   ├── enrichment.py            # DAG status / job inventory loading
│   ├── emailer.py               # SMTP report delivery
│   ├── policy.py                # Request validation against scope policy
│   ├── auth.py                  # IAP / development identity handling
│   ├── jobs.py                  # Firestore job store
│   ├── queueing.py              # Cloud Tasks queue client
│   ├── services.py              # Lazy service factories
│   ├── settings.py              # Typed environment configuration
│   ├── models.py                # Pydantic API/persistence models
│   └── static/index.html        # Hosted browser UI (no build step)
├── deploy/                      # Cloud Run manifests + hardened deploy guide
│   ├── cloudrun-api.yaml
│   ├── cloudrun-worker.yaml
│   └── README.md
├── Dockerfile                   # Single image for both API and worker
├── pyproject.toml               # Package metadata and dependencies
├── requirements.txt             # Runtime dependencies (mirrors pyproject.toml)
├── .env.example                 # Annotated configuration template
├── README.md                    # Condensed overview
└── WORKFLOW_DIAGRAM.md          # Full Mermaid flow + state diagrams
```

---

## 7. Prerequisites

### For local development

- **Python 3.11 or newer** (the container image uses 3.12).
- `pip` and `venv` (bundled with Python).
- **Google Cloud SDK** (`gcloud`) authenticated with Application Default Credentials if you want to execute real searches locally:

  ```bash
  gcloud auth application-default login
  ```

### For a working deployment (and for end-to-end local runs)

A GCP project with:

- **Cloud Storage** — the source buckets to search, a private reports bucket, and optionally approved copy-target buckets.
- **Firestore** (Native mode) — job records.
- **BigQuery** — a dedicated cache dataset (default name `gcs_search_cache`), plus optional DAG-status and job-inventory tables for enrichment.
- **Cloud Tasks** — a queue for job dispatch.
- **Cloud Run** — two services (API and worker) built from the single `Dockerfile`.
- For production: an HTTPS load balancer with **IAP**, **Cloud Armor**, Artifact Registry, and Secret Manager. Optionally an SMTP relay for email delivery.

---

## 8. Installation — Local Development

### Step 1 — Clone and create a virtual environment

```bash
git clone <your-repo-url>
cd Gcs_Search_Macro_Enterprise

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

### Step 2 — Install the package with development extras

```bash
pip install -e '.[dev]'
```

This installs FastAPI, Uvicorn, Pydantic, the Google Cloud client libraries (Firestore, BigQuery, Storage, Tasks), pandas, and openpyxl, plus development tooling (httpx, pytest).

### Step 3 — Configure the environment

```bash
cp .env.example .env
```

Edit `.env` (see the [Configuration Reference](#9-configuration-reference) for every variable). At minimum for local work:

- Keep `APP_ENV=development`.
- Set `GOOGLE_CLOUD_PROJECT` to your dev project.
- Define at least one scope in `GCS_SEARCH_SCOPE_POLICIES_JSON` — the app refuses to start without one.
- Set `GCS_SEARCH_REPORTS_BUCKET` to a bucket you can write to (needed to finish a real job).

### Step 4 — Run the API (and, for end-to-end runs, the worker)

```bash
# Terminal 1 — public API + browser UI on http://localhost:8000
uvicorn gcs_search_macro_v4.api:app --reload --port 8000

# Terminal 2 — worker on http://localhost:8081 (only needed to execute jobs)
uvicorn gcs_search_macro_v4.worker:app --reload --port 8081
```

Open <http://localhost:8000>. In development mode the app has no IAP in front of it, so identify yourself per request with the `X-Dev-User-Email` header (the UI's `fetch` calls won't set it, so exercise the API with `curl` locally, or use a browser extension that injects the header):

```bash
curl -s http://localhost:8000/v1/me -H 'X-Dev-User-Email: you@example.com'
```

> **Production note:** the development identity header is hard-rejected when `APP_ENV=production`; only IAP's `X-Goog-Authenticated-User-Email` is trusted there.

### Step 5 — Execute a job locally

Job dispatch uses Cloud Tasks, which cannot call `localhost`. For local end-to-end testing, create the job through the API and then invoke the worker directly — in development mode the worker's Cloud Tasks header guard is disabled:

```bash
# 1. Create a job (returns {"job_id": "...", "status": "queued"})
curl -s -X POST http://localhost:8000/v1/jobs \
  -H 'X-Dev-User-Email: you@example.com' \
  -H 'Content-Type: application/json' \
  -d '{
        "scope_id": "analytics",
        "search_type": "content",
        "bucket_paths": [{"bucket": "acme-datalake-scripts", "prefix": "pipelines"}],
        "terms": [{"value": "customer_id", "mode": "literal"}]
      }'

# 2. Trigger execution on the local worker
curl -s -X POST http://localhost:8081/internal/jobs/<JOB_ID>

# 3. Poll status and fetch the signed download URL
curl -s http://localhost:8000/v1/jobs/<JOB_ID> -H 'X-Dev-User-Email: you@example.com'
curl -s http://localhost:8000/v1/jobs/<JOB_ID>/download -H 'X-Dev-User-Email: you@example.com'
```

If Cloud Tasks is unreachable when the job is created, the API returns `503` and marks the job `failed` with `QUEUE_UNAVAILABLE` — point `GCS_SEARCH_TASKS_LOCATION`/`GCS_SEARCH_TASKS_QUEUE` at a real (even empty) queue in your dev project to avoid this.

### Step 6 — Build the container image (optional)

The same image serves both API and worker (the worker overrides the command to serve `gcs_search_macro_v4.worker:app`):

```bash
docker build -t gcs-search .
docker run --rm -p 8080:8080 --env-file .env gcs-search
```

---

## 9. Configuration Reference

All configuration is environment-driven (or `.env` in development) and validated at startup by `settings.py`. Store secrets — the scope-policy JSON and the SMTP password — in **Secret Manager** in real deployments.

### Core settings

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `development` | `development`, `test`, or `production`. Production enforces IAP identity, required config, and the Cloud Tasks worker guard |
| `GOOGLE_CLOUD_PROJECT` | — | GCP project hosting Firestore, Cloud Tasks, and the reports bucket. **Required in production** |
| `GCS_SEARCH_REPORTS_BUCKET` | — | Private bucket where Excel reports are stored (owner-namespaced). **Required in production** |
| `GCS_SEARCH_JOBS_COLLECTION` | `gcs_search_jobs` | Firestore collection for job documents |
| `GCS_SEARCH_TASKS_LOCATION` | `us-central1` | Cloud Tasks queue region |
| `GCS_SEARCH_TASKS_QUEUE` | `gcs-search-jobs` | Cloud Tasks queue name |
| `GCS_SEARCH_WORKER_URL` | — | Worker base URL that Cloud Tasks invokes. **Required in production** |
| `GCS_SEARCH_TASK_SERVICE_ACCOUNT` | — | Service account whose OIDC token authenticates task dispatch to the worker. **Required in production** |
| `GCS_SEARCH_ALLOWED_EMAIL_DOMAINS` | — | Comma-separated domains permitted for both requester identities and email recipients. **Required in production** |
| `GCS_SEARCH_SCOPE_POLICIES_JSON` | `{}` | The administrator-owned scope allowlist (below). **At least one scope is always required** |
| `GCS_SEARCH_CACHE_DATASET` | `gcs_search_cache` | BigQuery dataset for the search cache |
| `GCS_SEARCH_CACHE_TABLE_PREFIX` | *(empty)* | Optional prefix for cache table names |

### Job limits and concurrency

| Variable | Default | Purpose |
|---|---|---|
| `GCS_SEARCH_MAX_TERMS` | `200` | Maximum search terms per job |
| `GCS_SEARCH_MAX_TERM_LENGTH` | `200` | Maximum characters per term |
| `GCS_SEARCH_MAX_BUCKET_PATHS` | `20` | Maximum bucket/path rows per job |
| `GCS_SEARCH_MAX_FILES_PER_JOB` | `100000` | Manifest cap; exceeding it fails the job fast |
| `GCS_SEARCH_MAX_FILE_BYTES` | `10485760` (10 MiB) | Per-file size cap for content search |
| `GCS_SEARCH_MAX_RESULT_ROWS` | `100000` | Report row cap |
| `GCS_SEARCH_SEARCH_WORKERS` | `16` | Concurrent GCS download/scan threads per job |
| `GCS_SEARCH_COPY_WORKERS` | `8` | Concurrent copy threads per job |
| `GCS_SEARCH_MAX_INVENTORY_ROWS` | `100000` | Row cap when loading DAG/inventory enrichment tables |

### SMTP (optional email delivery)

| Variable | Default | Purpose |
|---|---|---|
| `GCS_SEARCH_SMTP_HOST` | *(empty — email disabled)* | Organization SMTP relay host |
| `GCS_SEARCH_SMTP_PORT` | `587` | Relay port |
| `GCS_SEARCH_SMTP_USER` / `GCS_SEARCH_SMTP_PASSWORD` | *(empty)* | Relay credentials (password belongs in Secret Manager) |
| `GCS_SEARCH_SMTP_FROM` | *(empty)* | From address (falls back to `SMTP_USER`) |
| `GCS_SEARCH_SMTP_USE_TLS` | `true` | STARTTLS on/off |

### The scope policy JSON

`GCS_SEARCH_SCOPE_POLICIES_JSON` is a JSON object whose **keys are scope IDs** (the "access profiles" users pick in the UI). It is owned by administrators, never populated from HTTP input, and revalidated by the worker at execution time — so revoking a scope takes effect even for already-queued jobs.

```json
{
  "analytics": {
    "project": "acme-analytics-dev",
    "buckets": [
      { "name": "acme-datalake-scripts", "prefix": "" }
    ],
    "dag_table": "acme-analytics-dev.pipeline_metadata.dag_script_status",
    "job_inventory_table": "acme-analytics-dev.pipeline_metadata.data_lake_job_inventory",
    "exclude_keywords": ["load", "backup", "archive/"],
    "exclude_patterns": [],
    "copy_targets": {
      "review": { "bucket": "acme-review-staging", "prefix": "gcs-search" }
    }
  }
}
```

| Field | Meaning |
|---|---|
| `project` | GCP project used for GCS reads, BigQuery cache, and enrichment queries for this scope |
| `buckets` | Approved search roots. Users may search a root exactly or **narrow** it with a deeper prefix, but can never search a different bucket or a shallower/altered path |
| `dag_table` | Optional BigQuery table joined to content matches to report `dag_id`, `is_active`, `last_executed` |
| `job_inventory_table` | Optional BigQuery table appended to content reports as a `Job_Inventory` sheet |
| `exclude_keywords` | Case-insensitive substrings that drop objects from content manifests (applied on top of built-in stale-file patterns) |
| `exclude_patterns` | Additional regex exclusions for content manifests |
| `copy_targets` | Approved copy destinations. A user-requested copy must land within one of these bucket/prefix roots |

---

## 10. Using the Application

### 10.1 The browser UI

Navigate to the application URL (through your VPN + IAP in production; `http://localhost:8000` in development). The single-page form walks through:

1. **Access profile** — pick a scope. The form pre-fills its approved bucket/path rows and default copy target.
2. **What to search for** — *"A term inside files"* (content) or *"A file name in GCS"* (filename).
3. **Buckets and paths** — add/remove rows freely; each row must stay within the selected profile's approved roots.
4. **Terms** — one per line (content mode also accepts comma-separated). For content searches choose the term mode:
   - **Literal** — the text exactly as typed, e.g. `PID` (case-insensitive).
   - **Safe regex** — patterns such as `pid_\d+` or `col_[a-z]+`. Groups `()`, counted repetitions `{}`, and backreferences are rejected by design. Filename search is always literal.
5. **Copy matches** *(optional)* — tick the checkbox and confirm the destination; it must stay within an approved copy root. Files land under `«prefix»/jobs/«job-id»/` so runs never collide.
6. **Email report to** *(optional)* — up to five recipients, all within the approved organization domains.
7. **Run search** — the UI shows the job reference, polls every 2.5 seconds, and reveals a **Download Excel report** link on success (a 15-minute signed URL; just re-request if it expires).

### 10.2 The REST API

All `/v1` endpoints require the authenticated identity (IAP in production; `X-Dev-User-Email` in development). Interactive OpenAPI docs are intentionally disabled.

| Method & path | Purpose | Notable responses |
|---|---|---|
| `GET /healthz` | Liveness probe | `200` |
| `GET /` | Browser UI | `200` HTML |
| `GET /v1/me` | Echo the authenticated email | `401` no identity, `403` domain not allowed |
| `GET /v1/scopes` | Approved scopes with their bucket roots and copy targets | `200` |
| `POST /v1/jobs` | Create a search job | `202` accepted; `403` unauthorized scope/root/recipient; `422` limit or term violations; `503` queue unavailable |
| `GET /v1/jobs/{job_id}` | Poll an owned job | `404` if not yours (anti-enumeration) |
| `POST /v1/jobs/{job_id}/cancel` | Cancel while still queued | `409` if already running |
| `GET /v1/jobs/{job_id}/download` | 15-minute signed report URL | `409` until the job has succeeded |

**Create-job request body**

```json
{
  "scope_id": "analytics",
  "search_type": "content",
  "bucket_paths": [
    { "bucket": "acme-datalake-scripts", "prefix": "pipelines/finance" }
  ],
  "terms": [
    { "value": "customer_id", "mode": "literal" },
    { "value": "cust_[a-z_]+_key", "mode": "regex" }
  ],
  "copy": { "bucket": "acme-review-staging", "prefix": "gcs-search", "overwrite": false },
  "email_recipients": ["teammate@your-org.com"]
}
```

Omit `bucket_paths` (or send `[]`) to search **every** approved root in the scope. `copy` and `email_recipients` are optional. For filename searches set `"search_type": "filename"` and use literal terms only.

**Job status response**

```json
{
  "job_id": "5f0c…",
  "status": "succeeded",
  "scope_id": "analytics",
  "search_type": "content",
  "created_at": "2026-07-12T14:03:11Z",
  "started_at": "2026-07-12T14:03:14Z",
  "finished_at": "2026-07-12T14:04:02Z",
  "files_scanned": 412,
  "matches_found": 37,
  "has_report": true,
  "error_code": null,
  "error_message": null
}
```

`files_scanned` counts the files actually (re)scanned this run — `0` on a full cache hit. `matches_found` counts unique matching files across all terms.

### 10.3 How matching works

**Content search** (`.py` files only):

- Files are downloaded once each with bounded parallelism and scanned line by line, case-insensitively.
- Leading module docstrings and boilerplate header lines (`Author:`, `Created on:`, `Description:`, JIRA/ticket references, …) are skipped.
- An occurrence is **exact** when the match is not embedded in a larger identifier (`customer_id` in `SELECT customer_id` — exact; inside `customer_id_old` — partial). Partial matches also report the full surrounding token so you can see *what* it was embedded in.
- Stale-looking files (`*_old*.py`, `*_20240101.py`, `old_scripts/`, `*_copy*.py`, `CHG0*` folders) and administrator exclusions are skipped automatically.

**Filename search** (all extensions, metadata only):

- The final path component of every object is compared case-insensitively against each term: full equality → **exact**; containment → **partial**.
- No object contents are downloaded; sizes and timestamps come from GCS metadata.

---

## 11. The Excel Report Explained

Reports are Excel (`.xlsx`) workbooks with styled headers, frozen first rows, and auto-filters on every sheet.

### Content search workbook

| Sheet | Contents |
|---|---|
| `Summary` | Per term: matching file count, files with exact matches, files with partial matches |
| `All_Matches` | Every match row across all terms |
| *(one sheet per term)* | The same rows filtered to that term |
| `Job_Inventory` | Optional appended inventory table from the scope's `job_inventory_table` |
| `Copied_Files` | Present when a copy was requested: `source_uri`, `destination_uri`, `status`, `message` per file |
| `No_Matches` | Replaces the match sheets when nothing was found |

Each match row includes: `search_term`, `match_type` (`exact`, `partial`, or `exact and partial`), `source_bucket`, `file_path`, `gcs_uri`, `exact_lines` and `partial_lines` (line numbers), `partial_matches` (the surrounding tokens), and DAG enrichment (`dag_id`, `is_active`, `last_executed`).

### Filename search workbook

Same structure with `All_Files` instead of `All_Matches`; each row includes `search_term`, `match_type`, `file_name`, `source_bucket`, `blob_name`, `created_or_landed_at_utc`, `last_updated_utc`, `size_bytes`, `size_mib`, and `gcs_uri`.

---

## 12. Result Caching in BigQuery

Every job's *source definition* — search type, project, resolved bucket paths, terms, and (for content) exclusions — is hashed into a **query key**. The dedicated cache dataset holds four tables (optionally name-prefixed):

| Table | Contents |
|---|---|
| `search_cache_run` | Snapshot metadata with `PENDING` / `COMPLETE` / `FAILED` state |
| `search_cache_manifest` | Per-snapshot source-object metadata (CRC32C hash, generation, size, timestamps) |
| `search_cache_result` | Exact/partial match rows per term |
| `search_cache_access` | Per-job audit record with a **hashed** requester identity |

On each run:

1. If no completed snapshot exists for the key → full search, then persist a new snapshot.
2. If one exists and the live GCS manifest is **unchanged** → reuse the cached results entirely (`files_scanned = 0`).
3. If the manifest changed → rescan **only** new/updated objects, drop rows for deleted/affected ones, merge, and persist a fresh append-only snapshot.

The cache stores object metadata, line numbers, and match tokens — **never file contents**. DAG/inventory enrichment is loaded fresh for every job even on cache hits, so operational columns are always current. Apply your organization's retention policy to the dataset; cache maintenance is administrative and invisible to end users.

---

## 13. Production Deployment on GCP

The full hardened runbook lives in [deploy/README.md](deploy/README.md) — treat it as authoritative. The outline:

1. **Identities & storage** — create three service accounts (`gcs-search-api`, `gcs-search-worker`, `gcs-search-tasks`), a private uniform-access reports bucket with a 7–30-day lifecycle delete rule, and put the scope-policy JSON and SMTP password in Secret Manager.
2. **Least-privilege IAM** — the API can enqueue tasks, use Firestore, view report objects, and sign URLs; the worker can use Firestore, create report objects, read only approved source buckets, and read/write only its BigQuery enrichment/cache resources; the tasks account holds `roles/run.invoker` on the **worker only**. The API must *not* be able to read source buckets or BigQuery data.
3. **Build & deploy** — one image, two Cloud Run services:

   ```bash
   gcloud builds submit --tag REGION-docker.pkg.dev/PROJECT/REPO/gcs-search --project PROJECT .
   gcloud run services replace deploy/cloudrun-api.yaml    --region REGION --project PROJECT
   gcloud run services replace deploy/cloudrun-worker.yaml --region REGION --project PROJECT
   ```

   Replace the `PROJECT_ID` / `IMAGE_URI` / `REPORTS_BUCKET` / `WORKER_URL` placeholders (or render via Terraform/Cloud Deploy). Never commit real project IDs, bucket names, or policy JSON.
4. **Queue** — create the Cloud Tasks queue with a finite dispatch rate and a small concurrency ceiling (start ~4) and raise it only after measuring cost and memory. Tasks carry an OIDC token whose audience is the worker URL.
5. **Perimeter** — put the API behind a serverless NEG + HTTPS load balancer, enable **IAP** for the requesting users/groups, apply a **Cloud Armor** policy allowing only corporate VPN egress CIDRs, and keep Cloud Run ingress at `internal-and-cloud-load-balancing` so direct service-URL traffic fails. The app trusts `X-Goog-Authenticated-User-Email` **only** in this topology.
6. **Operations** — keep worker `containerConcurrency: 1` (scale jobs horizontally); monitor queue depth, task failures, download errors, BigQuery bytes, cache hit rate, and report sizes; verify the negative tests (non-IAP user, off-VPN access, foreign job ID, expired signed URL, direct worker call) before go-live.

A Kubernetes-equivalent topology is described at the end of `deploy/README.md` for teams that already operate GKE.

---

## 14. Security Model

- **Network perimeter** — VPN-only source IPs (Cloud Armor) → HTTPS LB → IAP → Cloud Run with load-balancer-only ingress.
- **Identity** — IAP asserts the user; the app additionally enforces an allowed-email-domain check. The development identity header is rejected outright in production.
- **Owner isolation** — every job document is keyed by owner email. Reading, cancelling, or downloading someone else's job returns **404**, not 403, to prevent job-ID enumeration.
- **Administrator-owned scopes** — all searchable roots, enrichment tables, exclusions, and copy targets come from server-side configuration. The browser can narrow within approved roots but can never introduce a bucket, table, or destination. Policy is revalidated by the worker at execution time.
- **Worker isolation** — the worker is invocable only by the Cloud Tasks service account (Cloud Run IAM), with a defense-in-depth check on the `X-CloudTasks-TaskName` header. Tasks carry only a job ID — never search data.
- **Safe regex subset** — groups, counted repetitions, and backreferences are rejected to eliminate catastrophic-backtracking DoS in the hosted engine.
- **Bounded work** — hard caps on terms, rows, paths, files, file sizes, and result rows fail oversized jobs fast and predictably.
- **Private artifacts** — reports live in a private, owner-namespaced bucket; downloads use 15-minute signed URLs; report retention is lifecycle-managed.
- **Least-privilege data plane** — cache stores metadata and match locations only; audit records hash the requester identity; the API service account cannot read source data at all.

---

## 15. Operational Limits

Defaults (all overridable via environment variables — see [Configuration Reference](#9-configuration-reference)):

| Limit | Default |
|---|---|
| Search terms per job | 20 |
| Term length | 200 characters |
| Bucket/path rows per job | 20 |
| Files per job | 100,000 |
| File size (content search) | 10 MiB |
| Report result rows | 100,000 |
| Concurrent search downloads per job | 16 |
| Concurrent copies per job | 8 |
| Enrichment table rows | 100,000 |
| Email recipients per job | 5 |
| Signed download URL lifetime | 15 minutes |

Jobs exceeding a limit fail with a clear message rather than degrading the service.

---

## 16. Troubleshooting

| Symptom | Likely cause & fix |
|---|---|
| Startup error: *"At least one administrator-owned source scope is required"* | `GCS_SEARCH_SCOPE_POLICIES_JSON` is empty or invalid JSON. Define at least one scope |
| Startup error: *"Missing production configuration: …"* | `APP_ENV=production` requires project, allowed domains, worker URL, task service account, and reports bucket. Set them or use `APP_ENV=development` locally |
| `401 Authenticated user identity required` | No identity header. Locally add `X-Dev-User-Email`; in production check IAP is enabled on the load-balancer backend |
| `403 User domain is not authorized` | The email's domain is not in `GCS_SEARCH_ALLOWED_EMAIL_DOMAINS` |
| `403 Bucket or path is not authorized` | The row escapes the scope's approved roots — you may only narrow an approved root, never switch buckets or shallower paths |
| `422 Regex groups, counted repetitions, and backreferences are not allowed` | Rewrite the pattern within the safe subset (e.g. `pid_\d+`, character classes) or use a literal term |
| `503 Job queue is unavailable` / job failed with `QUEUE_UNAVAILABLE` | Cloud Tasks queue missing or unreachable, or the API lacks the Enqueuer role. Verify queue name/region and IAM |
| Job failed with `POLICY_REJECTED` | The scope policy changed between submission and execution; resubmit under a currently valid scope |
| Job failed with `EXECUTION_FAILED` | Any runtime failure (GCS read, limit breach, BigQuery write, report upload). Check worker logs with the job ID; oversized scopes hit the file/size/row caps intentionally |
| `409 Report is not available` on download | The job hasn't succeeded yet (or failed). Poll status first |
| `409 A running job cannot be cancelled` | Cancellation only applies while a job is still queued |
| Report email never arrives, but the job succeeded | Email is best-effort by design. Check SMTP settings and worker logs; the download link always remains available |
| Signed download link expired | Links last 15 minutes — call the download endpoint (or click the UI link) again for a fresh one |
| Direct call to the worker URL fails in production | Intentional: the worker only accepts Cloud Tasks invocations from the dispatcher service account |

---

*GCS Search Macro v4.1.0 — standalone build context; see `pyproject.toml` for exact dependency versions.*
