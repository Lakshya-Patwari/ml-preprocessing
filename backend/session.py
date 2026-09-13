"""
Very simple in-memory session store.

For a real deployment you'd back this with Redis / a temp-dir-on-disk
scheme keyed by session id, plus expiry. For the local MVP, a process-wide
dict is enough — this backend is meant to be run locally by a single user.
"""
import uuid
from datetime import datetime
from typing import Dict, List

import pandas as pd

# session_id -> {
#   "created": datetime,
#   "files": {filename: pd.DataFrame},
#   "log": {filename: [str, ...]},
#   "conflicts": [ {...} ],          # cached conflict analysis
#   "resolutions": { column: decision },
# }
SESSIONS: Dict[str, dict] = {}


def new_session() -> str:
    sid = uuid.uuid4().hex[:12]
    SESSIONS[sid] = {
        "created": datetime.utcnow(),
        "files": {},
        "log": {},
        "conflicts": [],
        "resolutions": {},
    }
    return sid


def get_session(session_id: str) -> dict:
    if session_id not in SESSIONS:
        raise KeyError(f"Unknown session_id: {session_id}")
    return SESSIONS[session_id]


def add_file(session_id: str, filename: str, df: pd.DataFrame) -> None:
    s = get_session(session_id)
    s["files"][filename] = df
    s["log"].setdefault(filename, [])


def log(session_id: str, filename: str, message: str) -> None:
    s = get_session(session_id)
    s["log"].setdefault(filename, []).append(message)


def get_df(session_id: str, filename: str) -> pd.DataFrame:
    s = get_session(session_id)
    if filename not in s["files"]:
        raise KeyError(f"Unknown file '{filename}' in session {session_id}")
    return s["files"][filename]


def set_df(session_id: str, filename: str, df: pd.DataFrame) -> None:
    s = get_session(session_id)
    s["files"][filename] = df


def list_files(session_id: str) -> List[str]:
    return list(get_session(session_id)["files"].keys())
