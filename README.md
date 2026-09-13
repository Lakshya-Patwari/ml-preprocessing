# DAG-MILE — Intelligent Dataset Preparation (MVP)

A working first version of the data-processing engine described in the
spec: upload multiple CSV/XLSX files, get a fast per-file profile, see
cross-file schema conflicts and join/merge suggestions, approve or edit
a cleaning plan (missing values, duplicates, outliers, encoding,
scaling), and download the cleaned data plus a full transformation log.

No LLM/AI layer yet — as planned, this is the deterministic engine the
AI advisor will sit on top of later.

## Stack (as built)

```
frontend/  →  plain HTML + JS (fetch calls to the API)
backend/   →  FastAPI + pandas + NumPy + scikit-learn
```

The spec called for React/Next.js — that's a straightforward swap once
the API contract below feels right. Keeping the frontend framework-free
for the MVP means there's no npm/build step: you can open a single HTML
file and start clicking.

`ydata-profiling` is *not* wired in yet. The `/api/analyze` endpoint
does its own lightweight profiling (missing %, duplicates, dtypes,
per-column stats, top categories) synchronously, which is fast enough
for interactive use. A full `ydata-profiling` HTML report is a natural
next endpoint (`/api/profile/{session_id}/{filename}`) once you want
the deeper report — flagged as a TODO in `backend/app/main.py`.

## Run it

**Backend**

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

The API is now at `http://127.0.0.1:8000` (interactive docs at
`http://127.0.0.1:8000/docs`).

**Frontend**

Just open `frontend/index.html` in a browser — no build step. It talks
to `http://127.0.0.1:8000` by default. To point it elsewhere, set
`window.DAG_MILE_API` before `app.js` loads, or serve `frontend/` with
any static file server (`python3 -m http.server` from inside
`frontend/`).

## How it works

1. **Upload** — drop CSV/XLSX/JSON files. Each one is parsed into a
   pandas DataFrame and kept in an in-memory session (`session.py`).
   This is a single-user local MVP; swap in Redis/disk-backed sessions
   before deploying for multiple users.
2. **Analysis** — `schema.py::profile_dataframe` computes shape,
   missing %, duplicate rows, dtypes, per-column stats/top values.
3. **Schema matching** — `schema.py::detect_relationships` compares
   every pair of files' shared column names and classifies each as a
   likely **join key** (near-unique id, values overlap), the **same
   feature** (overlapping numeric range / categorical values — safe to
   merge/rename), or a **conflict** (same name, different type or
   value range — e.g. `age` meaning *customer age* in one file and
   *product age* in another). You resolve each one (keep separate /
   rename) or use the merge panel to JOIN or CONCAT two files outright.
4. **Preprocessing** — `preprocessing.py::suggest_actions` proposes a
   per-column plan (median/mode imputation, IQR-based outlier
   winsorizing, one-hot/label encoding, standard/minmax scaling). You
   can edit any cell before applying; `apply_preprocessing` executes it
   and returns a human-readable log line per change — nothing happens
   silently.
5. **Final dataset** — download the cleaned CSV and the full
   transformation log (every change, across every file, in order).

## API summary

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/upload` | upload one or more files, returns/extends a `session_id` |
| GET | `/api/analyze/{session_id}` | per-file profile + cross-file relationships |
| POST | `/api/resolve-conflict/{session_id}` | keep-separate / rename a conflicting column |
| POST | `/api/merge/{session_id}` | JOIN or CONCAT two files into a new one |
| GET | `/api/preprocess-suggestions/{session_id}/{filename}` | suggested cleaning plan |
| POST | `/api/preprocess/{session_id}/{filename}` | apply a (possibly edited) cleaning plan |
| GET | `/api/download/{session_id}/{filename}` | cleaned CSV |
| GET | `/api/download-log/{session_id}` | full transformation log (text) |
| GET | `/api/session/{session_id}` | current file list + full log, for the final screen |

## Next steps (not in this MVP)

- Full `ydata-profiling` HTML reports on demand
- Persist sessions to disk/DB instead of an in-process dict, so uploads
  survive a server restart and multiple users don't share memory
- Streaming/chunked upload for very large files
- The LLM advisor layer: given the profile + relationship findings,
  have it draft the cleaning plan and explain *why*, with the same
  human-approval step before anything is applied
- Swap the frontend for React/Next.js once the API contract stabilizes


## Windows — simple direct run

This version does not use a virtual environment and does not require running Uvicorn separately.

Install dependencies:

```powershell
py -3.14 -m pip install -r backend\requirements.txt
```

Start the application:

```powershell
cd backend
py -3.14 main.py
```

Then open `http://127.0.0.1:8000`. The backend serves the frontend from the same process.

You can also double-click `run_backend.bat` from the project root.
