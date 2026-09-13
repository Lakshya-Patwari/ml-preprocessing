import io
from pathlib import Path
import json
from typing import List, Optional
import os
import shutil
import subprocess
import tempfile

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

import session as sess
from preprocessing import apply_preprocessing, suggest_actions
from schema import detect_relationships, profile_dataframe

app = FastAPI(title="DAG-MILE API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local MVP only — lock this down for real deployment
    allow_credentials=False,  # wildcard origin + credentials is rejected by browsers
    allow_methods=["*"],
    allow_headers=["*"],
)


# The frontend lives next to the backend folder.
PROJECT_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_DIR / "frontend"


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/style.css", include_in_schema=False)
def style():
    return FileResponse(FRONTEND_DIR / "style.css", media_type="text/css")


@app.get("/app.js", include_in_schema=False)
def javascript():
    return FileResponse(FRONTEND_DIR / "app.js", media_type="application/javascript")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _read_any(filename: str, raw: bytes) -> pd.DataFrame:
    name = filename.lower()

    # CSV
    if name.endswith(".csv"):
        return pd.read_csv(io.BytesIO(raw))

    # TSV
    if name.endswith(".tsv"):
        return pd.read_csv(io.BytesIO(raw), sep="\t")

    # JSON
    if name.endswith(".json"):
        return pd.read_json(io.BytesIO(raw))

    # Excel / spreadsheet formats
    if name.endswith((".xlsx", ".xlsm", ".xlsb", ".ods")):
        try:
            return pd.read_excel(
                io.BytesIO(raw),
                engine="calamine"
            )
        except Exception as e:
            raise ValueError(
                f"Could not read spreadsheet '{filename}': {str(e)}"
            )

    # Old .xls files
    if name.endswith(".xls"):

        # Try Calamine first
        try:
            return pd.read_excel(
                io.BytesIO(raw),
                engine="calamine"
            )
        except Exception:
            pass

        # Try xlrd
        try:
            return pd.read_excel(
                io.BytesIO(raw),
                engine="xlrd"
            )
        except Exception:
            pass

        # Sometimes .xls files are actually HTML tables
        try:
            tables = pd.read_html(io.BytesIO(raw))

            if tables:
                return tables[0]

        except Exception:
            pass

        # Sometimes an .xls file is actually an .xlsx file
        try:
            return pd.read_excel(
                io.BytesIO(raw),
                engine="openpyxl"
            )
        except Exception:
            pass

        raise ValueError(
            f"File '{filename}' has a .xls extension, "
            "but its internal format could not be recognized. "
            "The file may be corrupted, mislabeled, or exported "
            "in an unsupported format."
        )

    raise ValueError(f"Unsupported file type: {filename}")


def _read_xls_leniently(filename: str, raw: bytes) -> pd.DataFrame:
    """
    A LOT of real-world '.xls' files aren't actually the legacy binary
    Excel format at all — they're HTML tables (very common from
    older reporting tools/government software) or a renamed .xlsx,
    both just saved with a .xls extension. xlrd only understands the
    true binary format and fails with a cryptic 'little-endian marker'
    error on anything else, so try the likely real formats in order
    before giving up.
    """
    attempts = []

    try:
        return pd.read_excel(io.BytesIO(raw), engine="xlrd")
    except Exception as e:
        attempts.append(f"as legacy .xls (xlrd): {e}")

    try:
        tables = pd.read_html(io.BytesIO(raw))
        if tables:
            return tables[0]
    except Exception as e:
        attempts.append(f"as HTML table: {e}")

    try:
        return pd.read_excel(io.BytesIO(raw), engine="openpyxl")
    except Exception as e:
        attempts.append(f"as renamed .xlsx (openpyxl): {e}")

    raise ValueError(
        f"'{filename}' has a .xls extension but doesn't match any format "
        f"we can read (legacy Excel, HTML table, or .xlsx). It may be "
        f"corrupted, password-protected, or an unsupported export format. "
        + " | ".join(attempts)
    )


def _file_summary(filename: str, df: pd.DataFrame) -> dict:
    return {
        "filename": filename,
        "rows": int(df.shape[0]),
        "cols": int(df.shape[1]),
        "columns": list(df.columns.astype(str)),
    }


# --------------------------------------------------------------------------- #
# Upload + Analysis
# --------------------------------------------------------------------------- #

@app.post("/api/upload")
async def upload(files: List[UploadFile] = File(...), session_id: Optional[str] = Form(None)):
    if not session_id:
        session_id = sess.new_session()
    else:
        try:
            sess.get_session(session_id)
        except KeyError:
            session_id = sess.new_session()

    summaries = []
    errors = []
    for f in files:
        raw = await f.read()
        try:
            df = _read_any(f.filename, raw)
        except Exception as e:
            errors.append({"filename": f.filename, "error": str(e)})
            continue
        sess.add_file(session_id, f.filename, df)
        sess.log(session_id, f.filename, f"File loaded: {df.shape[0]} rows x {df.shape[1]} columns")
        summaries.append(_file_summary(f.filename, df))

    return {"session_id": session_id, "files": summaries, "errors": errors}


@app.get("/api/analyze/{session_id}")
def analyze(session_id: str):
    try:
        s = sess.get_session(session_id)
    except KeyError:
        raise HTTPException(404, "Session not found")

    if not s["files"]:
        raise HTTPException(400, "No files uploaded in this session yet")

    profiles = {fname: profile_dataframe(df) for fname, df in s["files"].items()}
    relationships = detect_relationships(s["files"]) if len(s["files"]) > 1 else []
    s["conflicts"] = relationships

    return {
        "session_id": session_id,
        "profiles": profiles,
        "relationships": relationships,
    }


class ResolveConflictBody(BaseModel):
    file_a: str
    column_a: str
    file_b: str
    column_b: str
    decision: str  # "keep_separate" | "rename_a" | "rename_b"
    new_name: Optional[str] = None


@app.post("/api/resolve-conflict/{session_id}")
def resolve_conflict(session_id: str, body: ResolveConflictBody):
    try:
        sess.get_session(session_id)
    except KeyError:
        raise HTTPException(404, "Session not found")

    if body.decision == "keep_separate":
        sess.log(session_id, body.file_a,
                  f"⚠ '{body.column_a}' kept separate from '{body.column_b}' in {body.file_b} "
                  f"(different meaning, same name)")
        return {"status": "ok", "action": "kept_separate"}

    if body.decision in ("rename_a", "rename_b") and body.new_name:
        target_file = body.file_a if body.decision == "rename_a" else body.file_b
        target_col = body.column_a if body.decision == "rename_a" else body.column_b
        df = sess.get_df(session_id, target_file)
        if target_col not in df.columns:
            raise HTTPException(400, f"Column '{target_col}' not found in {target_file}")
        df = df.rename(columns={target_col: body.new_name})
        sess.set_df(session_id, target_file, df)
        sess.log(session_id, target_file, f"✓ Column renamed: '{target_col}' → '{body.new_name}'")
        return {"status": "ok", "action": "renamed", "file": target_file, "new_name": body.new_name}

    raise HTTPException(400, "Unrecognized decision / missing new_name")


class MergeBody(BaseModel):
    operation: str  # "join" | "concat"
    file_a: str
    file_b: str
    column_a: Optional[str] = None
    column_b: Optional[str] = None
    how: Optional[str] = "inner"  # for join: inner/left/outer
    new_name: str


@app.post("/api/merge/{session_id}")
def merge(session_id: str, body: MergeBody):
    try:
        sess.get_session(session_id)
    except KeyError:
        raise HTTPException(404, "Session not found")

    df_a = sess.get_df(session_id, body.file_a)
    df_b = sess.get_df(session_id, body.file_b)

    if body.operation == "join":
        if not body.column_a or not body.column_b:
            raise HTTPException(400, "column_a and column_b are required for a join")
        merged = df_a.merge(
            df_b, left_on=body.column_a, right_on=body.column_b,
            how=body.how or "inner", suffixes=("_a", "_b"),
        )
        note = (f"JOIN of {body.file_a} and {body.file_b} "
                f"on {body.column_a}/{body.column_b} ({body.how}) → {merged.shape[0]} rows")
    elif body.operation == "concat":
        merged = pd.concat([df_a, df_b], axis=0, ignore_index=True)
        note = f"CONCAT of {body.file_a} and {body.file_b} → {merged.shape[0]} rows"
    else:
        raise HTTPException(400, "operation must be 'join' or 'concat'")

    sess.add_file(session_id, body.new_name, merged)
    sess.log(session_id, body.new_name, f"✓ {note}")
    sess.log(session_id, body.file_a, f"✓ Used in {body.operation.upper()} → {body.new_name}")
    sess.log(session_id, body.file_b, f"✓ Used in {body.operation.upper()} → {body.new_name}")

    return {"status": "ok", "new_file": _file_summary(body.new_name, merged)}


# --------------------------------------------------------------------------- #
# Preprocessing
# --------------------------------------------------------------------------- #

@app.get("/api/preprocess-suggestions/{session_id}/{filename:path}")
def preprocess_suggestions(session_id: str, filename: str):
    try:
        df = sess.get_df(session_id, filename)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return suggest_actions(df)


class PreprocessBody(BaseModel):
    drop_duplicates: bool = True
    columns: dict = {}


@app.post("/api/preprocess/{session_id}/{filename:path}")
def preprocess(session_id: str, filename: str, body: PreprocessBody):
    try:
        df = sess.get_df(session_id, filename)
    except KeyError as e:
        raise HTTPException(404, str(e))

    new_df, log_lines = apply_preprocessing(df, body.dict())
    sess.set_df(session_id, filename, new_df)
    for line in log_lines:
        sess.log(session_id, filename, line)

    return {
        "status": "ok",
        "filename": filename,
        "shape": list(new_df.shape),
        "log": log_lines,
        "profile": profile_dataframe(new_df),
    }


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

@app.get("/api/download/{session_id}/{filename:path}")
def download(session_id: str, filename: str):
    try:
        df = sess.get_df(session_id, filename)
    except KeyError as e:
        raise HTTPException(404, str(e))
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    out_name = filename.rsplit(".", 1)[0] + "_cleaned.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )


@app.get("/api/download-log/{session_id}")
def download_log(session_id: str):
    try:
        s = sess.get_session(session_id)
    except KeyError:
        raise HTTPException(404, "Session not found")
    lines = ["DAG-MILE Transformation Log", "=" * 40, ""]
    for fname, entries in s["log"].items():
        lines.append(f"[{fname}]")
        lines.extend(f"  {e}" for e in entries)
        lines.append("")
    text = "\n".join(lines)
    return PlainTextResponse(
        text,
        headers={"Content-Disposition": 'attachment; filename="transformation_log.txt"'},
    )


@app.get("/api/session/{session_id}")
def session_state(session_id: str):
    try:
        s = sess.get_session(session_id)
    except KeyError:
        raise HTTPException(404, "Session not found")
    return {
        "session_id": session_id,
        "files": [_file_summary(f, df) for f, df in s["files"].items()],
        "log": s["log"],
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
