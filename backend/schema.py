"""
Schema detection, per-file profiling, and cross-file relationship /
conflict detection.

This is a lightweight, dependency-free stand-in for a full ydata-profiling
report — fast enough to run synchronously on upload. A deeper
ydata-profiling HTML report is generated on demand via a separate,
slower endpoint (see main.py: /api/profile/...).
"""
from itertools import combinations
from typing import Any, Dict, List
import re

import numpy as np
import pandas as pd


def infer_feature_type(series: pd.Series) -> str:
    """Coarse feature typing beyond pandas dtype: numeric / categorical /
    datetime / boolean / identifier / text."""
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    if pd.api.types.is_numeric_dtype(series):
        nunique = series.nunique(dropna=True)
        if nunique > 0 and nunique / max(len(series), 1) > 0.95 and len(series) > 20:
            return "identifier"
        return "numeric"
    # object / string columns
    nunique = series.nunique(dropna=True)
    n = max(len(series), 1)
    if nunique / n > 0.95 and n > 20:
        return "identifier"
    avg_len = series.dropna().astype(str).str.len().mean() if series.notna().any() else 0
    if avg_len and avg_len > 40:
        return "text"
    return "categorical"


def profile_dataframe(df: pd.DataFrame) -> Dict[str, Any]:
    n_rows, n_cols = df.shape
    dup_count = int(df.duplicated().sum())
    columns = []
    for col in df.columns:
        s = df[col]
        missing = int(s.isna().sum())
        ftype = infer_feature_type(s)
        col_info: Dict[str, Any] = {
            "name": col,
            "dtype": str(s.dtype),
            "feature_type": ftype,
            "missing_count": missing,
            "missing_pct": round(100 * missing / n_rows, 2) if n_rows else 0.0,
            "unique_count": int(s.nunique(dropna=True)),
        }
        if ftype == "numeric":
            desc = s.describe()
            col_info.update({
                "min": _safe_float(desc.get("min")),
                "max": _safe_float(desc.get("max")),
                "mean": _safe_float(desc.get("mean")),
                "std": _safe_float(desc.get("std")),
                "median": _safe_float(s.median()),
            })
        elif ftype in ("categorical", "boolean"):
            top = s.value_counts(dropna=True).head(5)
            col_info["top_values"] = {str(k): int(v) for k, v in top.items()}
        columns.append(col_info)

    return {
        "n_rows": n_rows,
        "n_cols": n_cols,
        "duplicate_rows": dup_count,
        "duplicate_pct": round(100 * dup_count / n_rows, 2) if n_rows else 0.0,
        "missing_cells_pct": round(
            100 * df.isna().sum().sum() / (n_rows * n_cols), 2
        ) if n_rows and n_cols else 0.0,
        "columns": columns,
    }


def _safe_float(x):
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return None
    return round(float(x), 4)


def _normalize_name(name: str) -> str:
    """Normalize a column name. Empty/Excel-generated names are ignored."""
    name = str(name).lower().strip()
    if name.startswith("unnamed:"):
        return ""
    name = re.sub(r"[_\-/]+", " ", name)
    name = re.sub(r"[^a-z0-9\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _numeric_overlap(a: pd.Series, b: pd.Series) -> float:
    """Rough overlap of two numeric ranges, 0..1."""
    a, b = pd.to_numeric(a, errors="coerce").dropna(), pd.to_numeric(b, errors="coerce").dropna()
    if a.empty or b.empty:
        return 0.0
    lo = max(a.min(), b.min())
    hi = min(a.max(), b.max())
    if hi <= lo:
        return 0.0
    span = max(a.max(), b.max()) - min(a.min(), b.min())
    return float((hi - lo) / span) if span > 0 else 1.0


def _categorical_overlap(a: pd.Series, b: pd.Series) -> float:
    """Jaccard similarity of up to 50 observed categories."""
    sa = set(a.dropna().astype(str).unique()[:50])
    sb = set(b.dropna().astype(str).unique()[:50])
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _representative_pair(items):
    """Pick two files containing the same normalized feature."""
    return items[0], items[1]


def detect_relationships(files: Dict[str, pd.DataFrame]) -> List[Dict[str, Any]]:
    """
    Compact schema matching.

    IMPORTANT: We do NOT create a card for every file-to-file comparison.
    Instead, columns are grouped by normalized feature name across the whole
    upload. One feature gets at most one schema card.

    Excel-generated Unnamed:* columns are ignored completely.
    """
    # normalized feature -> [(filename, original_column, series, type)]
    groups: Dict[str, list] = {}

    for filename, df in files.items():
        # Avoid duplicate normalized columns within one file.
        seen = set()
        for col in df.columns:
            norm = _normalize_name(col)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            series = df[col]
            groups.setdefault(norm, []).append(
                (filename, str(col), series, infer_feature_type(series))
            )

    findings: List[Dict[str, Any]] = []

    # Only show features that occur in at least two files.
    for norm, items in groups.items():
        if len(items) < 2:
            continue

        a, b = _representative_pair(items)
        f1, c1, s1, t1 = a
        f2, c2, s2, t2 = b

        finding = {
            "file_a": f1,
            "column_a": c1,
            "type_a": t1,
            "file_b": f2,
            "column_b": c2,
            "type_b": t2,
            "files_count": len(items),
        }

        type_set = {item[3] for item in items}

        # Same name but incompatible types across the uploaded files.
        if len(type_set) > 1:
            finding.update({
                "relationship": "conflict",
                "recommendation": "KEEP SEPARATE",
                "confidence": 0.90,
                "note": (
                    f"'{c1}' appears in {len(items)} files, but its inferred "
                    f"types are {', '.join(sorted(type_set))}. Review the schema "
                    "before aligning these columns."
                ),
            })

        elif t1 == "identifier":
            # Compare only the representative pair, not every file pair.
            overlap = _categorical_overlap(s1.astype(str), s2.astype(str))
            finding.update({
                "relationship": "join_key",
                "recommendation": "JOIN",
                "confidence": round(min(0.99, 0.55 + overlap * 0.44), 2),
                "note": (
                    f"'{c1}' appears in {len(items)} files and looks like an "
                    f"identifier ({round(overlap * 100)}% value overlap in the "
                    "representative pair) — possible JOIN key."
                ),
            })

        elif t1 == "numeric":
            overlap = _numeric_overlap(s1, s2)
            if overlap >= 0.60:
                finding.update({
                    "relationship": "same_feature",
                    "recommendation": "MERGE / ALIGN",
                    "confidence": round(0.70 + 0.20 * overlap, 2),
                    "note": (
                        f"'{c1}' appears in {len(items)} files with compatible "
                        f"numeric data ({round(overlap * 100)}% range overlap in "
                        "the representative pair) — likely the same feature."
                    ),
                })
            else:
                finding.update({
                    "relationship": "conflict",
                    "recommendation": "KEEP SEPARATE",
                    "confidence": round(0.70 + 0.20 * (1 - overlap), 2),
                    "note": (
                        f"'{c1}' appears in {len(items)} files but the numeric "
                        "ranges differ substantially in the representative pair."
                    ),
                })

        elif t1 == "categorical":
            overlap = _categorical_overlap(s1, s2)
            if overlap >= 0.50:
                finding.update({
                    "relationship": "same_feature",
                    "recommendation": "MERGE / ALIGN",
                    "confidence": round(0.70 + 0.20 * overlap, 2),
                    "note": (
                        f"'{c1}' appears in {len(items)} files and shares "
                        f"{round(overlap * 100)}% of categories in the "
                        "representative pair — likely the same feature."
                    ),
                })
            else:
                finding.update({
                    "relationship": "conflict",
                    "recommendation": "KEEP SEPARATE",
                    "confidence": 0.80,
                    "note": (
                        f"'{c1}' appears in {len(items)} files, but the observed "
                        "categories differ substantially."
                    ),
                })

        else:
            finding.update({
                "relationship": "same_feature",
                "recommendation": "MERGE / ALIGN",
                "confidence": 0.75,
                "note": (
                    f"'{c1}' appears in {len(items)} files with the same inferred "
                    "feature type — likely the same feature."
                ),
            })

        findings.append(finding)

    findings.sort(key=lambda f: f["confidence"], reverse=True)
    return findings
