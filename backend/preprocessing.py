"""
Preprocessing engine. Every function that mutates data returns
(new_df, log_lines) — nothing is ever changed silently.
"""
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler, OneHotEncoder, StandardScaler

from schema import infer_feature_type


def suggest_actions(df: pd.DataFrame) -> Dict[str, Any]:
    """Produce the 'Suggested actions' panel: one suggestion per column
    plus dataset-level duplicate/outlier summary."""
    n_rows = len(df)
    dup_count = int(df.duplicated().sum())

    column_suggestions = []
    outlier_total = 0

    for col in df.columns:
        s = df[col]
        ftype = infer_feature_type(s)
        missing = int(s.isna().sum())
        suggestion: Dict[str, Any] = {
            "column": col,
            "feature_type": ftype,
            "missing_count": missing,
            "missing_pct": round(100 * missing / n_rows, 2) if n_rows else 0.0,
        }

        if ftype == "numeric":
            suggestion["missing_strategy"] = "median" if missing else "none"
            q1, q3 = s.quantile(0.25), s.quantile(0.75)
            iqr = q3 - q1
            if iqr > 0:
                lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                n_outliers = int(((s < lo) | (s > hi)).sum())
            else:
                n_outliers = 0
            outlier_total += n_outliers
            suggestion["outlier_count"] = n_outliers
            suggestion["outlier_pct"] = round(100 * n_outliers / n_rows, 2) if n_rows else 0.0
            suggestion["outlier_action"] = "winsorize" if n_outliers else "none"
            suggestion["scale"] = "standard"
        elif ftype in ("categorical", "boolean"):
            suggestion["missing_strategy"] = "most_frequent" if missing else "none"
            nunique = int(s.nunique(dropna=True))
            suggestion["encode"] = "onehot" if nunique <= 15 else "label"
        elif ftype == "identifier":
            suggestion["missing_strategy"] = "none"
            suggestion["note"] = "Looks like an identifier — usually excluded from scaling/encoding."
        else:  # text / datetime
            suggestion["missing_strategy"] = "none"

        column_suggestions.append(suggestion)

    return {
        "n_rows": n_rows,
        "duplicate_rows": dup_count,
        "duplicate_pct": round(100 * dup_count / n_rows, 2) if n_rows else 0.0,
        "outlier_rows_estimate": outlier_total,
        "columns": column_suggestions,
    }


def apply_preprocessing(
    df: pd.DataFrame, config: Dict[str, Any]
) -> Tuple[pd.DataFrame, List[str]]:
    """
    config = {
      "drop_duplicates": bool,
      "columns": {
         col_name: {
            "missing_strategy": "median"|"mean"|"most_frequent"|"constant"|"drop_rows"|"none",
            "constant_value": Any (if missing_strategy == "constant"),
            "outlier_action": "winsorize"|"remove"|"keep",
            "encode": "onehot"|"label"|"none",
            "scale": "standard"|"minmax"|"none",
         }, ...
      }
    }
    """
    log: List[str] = []
    out = df.copy()

    # 1. Duplicates
    if config.get("drop_duplicates"):
        before = len(out)
        out = out.drop_duplicates().reset_index(drop=True)
        removed = before - len(out)
        if removed:
            log.append(f"✓ {removed} duplicate rows removed")

    col_configs = config.get("columns", {})

    # 2. Missing values
    for col, cfg in col_configs.items():
        if col not in out.columns:
            continue
        strategy = cfg.get("missing_strategy", "none")
        missing_before = int(out[col].isna().sum())
        if strategy == "none" or missing_before == 0:
            continue
        if strategy == "median":
            val = out[col].median()
            out[col] = out[col].fillna(val)
            log.append(f"✓ {col}: {missing_before} missing values → median ({round(float(val), 3)})")
        elif strategy == "mean":
            val = out[col].mean()
            out[col] = out[col].fillna(val)
            log.append(f"✓ {col}: {missing_before} missing values → mean ({round(float(val), 3)})")
        elif strategy == "most_frequent":
            mode = out[col].mode(dropna=True)
            val = mode.iloc[0] if not mode.empty else None
            out[col] = out[col].fillna(val)
            log.append(f"✓ {col}: {missing_before} missing values → most frequent ('{val}')")
        elif strategy == "constant":
            val = cfg.get("constant_value")
            out[col] = out[col].fillna(val)
            log.append(f"✓ {col}: {missing_before} missing values → constant ('{val}')")
        elif strategy == "drop_rows":
            before = len(out)
            out = out[out[col].notna()].reset_index(drop=True)
            log.append(f"✓ {col}: {before - len(out)} rows with missing values dropped")

    # 3. Outliers (numeric columns only, IQR-based)
    for col, cfg in col_configs.items():
        if col not in out.columns:
            continue
        action = cfg.get("outlier_action", "none")
        if action in ("none", "keep") or not pd.api.types.is_numeric_dtype(out[col]):
            if action == "keep":
                q1, q3 = out[col].quantile(0.25), out[col].quantile(0.75)
                iqr = q3 - q1
                if iqr > 0:
                    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                    n = int(((out[col] < lo) | (out[col] > hi)).sum())
                    if n:
                        log.append(f"⚠ {col}: {n} potential outliers detected → user chose KEEP")
            continue
        q1, q3 = out[col].quantile(0.25), out[col].quantile(0.75)
        iqr = q3 - q1
        if iqr <= 0:
            continue
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        mask = (out[col] < lo) | (out[col] > hi)
        n = int(mask.sum())
        if n == 0:
            continue
        if action == "winsorize":
            out[col] = out[col].clip(lower=lo, upper=hi)
            log.append(f"✓ {col}: {n} outliers winsorized to [{round(float(lo),3)}, {round(float(hi),3)}]")
        elif action == "remove":
            out = out[~mask].reset_index(drop=True)
            log.append(f"✓ {col}: {n} outlier rows removed")

    # 4. Encoding
    for col, cfg in col_configs.items():
        if col not in out.columns:
            continue
        encode = cfg.get("encode", "none")
        if encode == "none":
            continue
        if encode == "onehot":
            dummies = pd.get_dummies(out[col], prefix=col, dtype=int)
            out = pd.concat([out.drop(columns=[col]), dummies], axis=1)
            log.append(f"✓ {col}: OneHotEncoder applied ({dummies.shape[1]} new columns)")
        elif encode == "label":
            codes, uniques = pd.factorize(out[col])
            out[col] = codes
            log.append(f"✓ {col}: LabelEncoder applied ({len(uniques)} categories)")

    # 5. Scaling
    for col, cfg in col_configs.items():
        if col not in out.columns or not pd.api.types.is_numeric_dtype(out[col]):
            continue
        scale = cfg.get("scale", "none")
        if scale == "none":
            continue
        values = out[[col]].astype(float)
        if scale == "standard":
            out[col] = StandardScaler().fit_transform(values)
            log.append(f"✓ {col}: StandardScaler applied")
        elif scale == "minmax":
            out[col] = MinMaxScaler().fit_transform(values)
            log.append(f"✓ {col}: MinMaxScaler applied")

    return out, log
