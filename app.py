"""Panda's Flask API and pandas analysis engine.

System workflow: accept an uploaded dataset, persist it through ``storage.py``,
reload it for each request, apply row/column selections, and return JSON chart
recipes or tabular results. User workflow: upload -> select data -> choose an
analysis -> inspect/export the result. Keep analytical formulas in this module
so the browser UI remains a presentation layer rather than a second calculator.
"""

import os, io, re, uuid, warnings, secrets, csv, tempfile, zipfile, gzip, bz2
from pathlib import Path
import chardet
import numpy as np
import pandas as pd
from werkzeug.utils import secure_filename
from flask import (Flask, request, session, jsonify,
                   send_from_directory, redirect, send_file)
from storage import (
    delete_dataset, list_datasets, get_dataset, load_dataset, save_dataset,
)

warnings.filterwarnings("ignore")

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder=".")
app.config.update(
    SECRET_KEY         = os.environ.get("SECRET_KEY", secrets.token_hex(32)),
)

# ── Persistent storage ─────────────────────────────────────────────────────
# Every uploaded dataset is written to disk (panda_data/datasets/<id>.csv) via
# storage.py. The Flask session cookie only ever holds the dataset's id
# ("_sid") — never the DataFrame itself — so every request is fully
# stateless: a server restart never loses data and RAM usage stays flat
# because Pandas reloads the saved CSV only when an operation runs.
# LOCAL_STORAGE_ENABLED just toggles the "Saved Datasets" browser (desktop
# mode) that lets a user reopen or delete datasets saved in earlier sessions.
LOCAL_STORAGE_ENABLED = os.environ.get("PANDA_LOCAL_STORAGE") == "1"

ALLOWED = {
    "csv", "tsv", "txt", "json", "jsonl", "ndjson",
    "xlsx", "xls", "parquet", "feather", "arrow", "orc",
    "gz", "bz2", "zip",
}
ARCHIVE_IMPORT_EXTS = {
    "csv", "tsv", "txt", "json", "jsonl", "ndjson",
    "xlsx", "xls", "parquet", "feather", "arrow", "orc",
    "gz", "bz2",  # compressed csv/tsv/txt nested inside a zip, e.g. data.csv.gz
}

# ── Chart colors (Power BI–style palette, shared with the frontend) ───────────

ACCENT = "#7c6af7"; PINK = "#e05c8a"; GREEN = "#3ec9a7"; AMBER = "#f5c842"
PALETTE = [ACCENT, PINK, GREEN, AMBER, "#a78bfa", "#34d399", "#f87171", "#fbbf24"]

# ── ECharts config builders ─────────────────────────────────────────────────
# Every chart-producing route below returns a small JSON "recipe" instead of a
# rendered image. The actual drawing happens in the browser via ECharts
# (see renderECharts() in index.html), which is what gives us interactive
# tooltips, zoom, legends, and smooth resize for free.

def bar_chart(title, categories, series, x_label="", y_label="", horizontal=False, mark_lines=None):
    return {
        "type": "bar", "title": title, "horizontal": horizontal,
        "xAxis": {"name": x_label, "data": categories},
        "yAxis": {"name": y_label},
        "series": series,
        "markLines": mark_lines or [],
    }

def histogram_chart(title, s, bins=30, x_label="", mark_stats=True):
    s = np.asarray(s, dtype=float)
    counts, edges = np.histogram(s, bins=bins)
    labels = [f"{edges[i]:.2f}–{edges[i+1]:.2f}" for i in range(len(edges) - 1)]
    cfg = {
        "type": "bar", "title": title,
        "xAxis": {"name": x_label, "data": labels, "rotate": 55},
        "yAxis": {"name": "Frequency"},
        "series": [{"name": "Frequency", "data": counts.tolist(), "color": ACCENT}],
        "markLines": [],
    }
    if mark_stats and len(s):
        mean_v = float(np.mean(s)); med_v = float(np.median(s))
        mean_idx = int(np.clip(np.digitize(mean_v, edges) - 1, 0, len(labels) - 1))
        med_idx = int(np.clip(np.digitize(med_v, edges) - 1, 0, len(labels) - 1))
        cfg["markLines"] = [
            {"name": f"Mean {mean_v:.2f}", "xAxis": labels[mean_idx], "color": PINK},
            {"name": f"Median {med_v:.2f}", "xAxis": labels[med_idx], "color": GREEN},
        ]
    return cfg

def heatmap_chart(title, matrix_df, mask_upper=False, min_v=None, max_v=None, diverging=False):
    cols = matrix_df.columns.tolist(); idx = matrix_df.index.tolist()
    data = []
    for i, r in enumerate(idx):
        for j, c in enumerate(cols):
            if mask_upper and j > i:
                continue
            v = matrix_df.iloc[i, j]
            if pd.notna(v):
                data.append([j, i, round(float(v), 3)])
    vals = [d[2] for d in data] or [0]
    return {
        "type": "heatmap", "title": title,
        "xAxis": {"data": cols}, "yAxis": {"data": idx},
        "series": [{"name": title, "data": data}],
        "visualMap": {
            "min": min_v if min_v is not None else min(vals),
            "max": max_v if max_v is not None else max(vals),
            "diverging": diverging,
        },
    }

def scatter_chart(title, x, y, x_label="", y_label="", trend=True):
    x = np.asarray(x, dtype=float); y = np.asarray(y, dtype=float)
    points = [[round(float(a), 4), round(float(b), 4)] for a, b in zip(x, y)]
    series = [{"name": "Points", "type": "scatter", "data": points, "color": ACCENT}]
    if trend and len(points) > 1 and float(x.min()) != float(x.max()):
        try:
            z = np.polyfit(x, y, 1); p = np.poly1d(z)
            xs = np.linspace(float(x.min()), float(x.max()), 50)
            series.append({"name": "Trend", "type": "line",
                            "data": [[float(a), float(p(a))] for a in xs], "color": PINK})
        except (np.linalg.LinAlgError, ValueError):
            pass  # degenerate fit (e.g. collinear/constant data) — skip trend line, keep points
    return {"type": "scatter", "title": title,
            "xAxis": {"name": x_label, "type": "value"},
            "yAxis": {"name": y_label, "type": "value"},
            "series": series}

def line_chart(title, categories, series, x_label="", y_label=""):
    return {
        "type": "line", "title": title,
        "xAxis": {"name": x_label, "data": categories},
        "yAxis": {"name": y_label},
        "series": series,
    }

def pie_chart(title, labels, values, donut=False):
    return {
        "type": "pie", "title": title, "donut": donut,
        "series": [{
            "name": title,
            "type": "pie",
            "radius": ["45%", "70%"] if donut else "68%",
            "data": [{"name": str(k), "value": round(float(v), 4)} for k, v in zip(labels, values)],
        }],
    }

def treemap_chart(title, labels, values):
    return {
        "type": "treemap", "title": title,
        "series": [{
            "name": title,
            "type": "treemap",
            "data": [{"name": str(k), "value": round(float(v), 4)} for k, v in zip(labels, values)],
        }],
    }

def boxplot_chart(title, categories, groups_data, y_label=""):
    box_data, outlier_points = [], []
    for i, arr in enumerate(groups_data):
        arr = np.asarray(arr, dtype=float)
        if not len(arr): box_data.append([0, 0, 0, 0, 0]); continue
        q1, med, q3 = np.percentile(arr, [25, 50, 75])
        iqr = q3 - q1; lo = q1 - 1.5 * iqr; hi = q3 + 1.5 * iqr
        inliers = arr[(arr >= lo) & (arr <= hi)]
        whisk_lo = float(inliers.min()) if len(inliers) else float(arr.min())
        whisk_hi = float(inliers.max()) if len(inliers) else float(arr.max())
        box_data.append([round(whisk_lo, 3), round(float(q1), 3), round(float(med), 3),
                          round(float(q3), 3), round(whisk_hi, 3)])
        for v in arr[(arr < lo) | (arr > hi)]:
            outlier_points.append([i, round(float(v), 3)])
    series = [{"name": "Box", "type": "boxplot", "data": box_data, "color": ACCENT}]
    if outlier_points:
        series.append({"name": "Outliers", "type": "scatter", "data": outlier_points, "color": PINK})
    return {"type": "boxplot", "title": title, "xAxis": {"data": categories},
            "yAxis": {"name": y_label}, "series": series}

import json as _json

def to_numeric_clean(s):
    """pd.to_numeric with tolerance for thousands-separator commas, stray
    currency symbols, and surrounding whitespace in text columns (e.g.
    "1,234.50" or "$1,234"), so uploads that look "numeric" to a human but
    were read in as strings (commas, $ signs) don't silently become all-NaN.
    Already-numeric columns pass straight through to plain pd.to_numeric."""
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce")
    cleaned = s.astype(str).str.strip().str.replace(r"[,$\s]", "", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")

def normalize_agg(agg):
    return "var" if agg == "variance" else agg

def numeric_like_frame(df):
    out = {}
    for c in df.columns:
        s = to_numeric_clean(df[c])
        if s.notna().sum() >= max(2, int(len(df) * 0.05)):
            out[c] = s
    return pd.DataFrame(out)

def nonnegative_part_values(labels, values):
    pairs = [(str(k), float(v)) for k, v in zip(labels, values) if pd.notna(v) and float(v) >= 0]
    return [p[0] for p in pairs], [p[1] for p in pairs]

def json_safe_records(df):
    """
    Robustly converts a DataFrame to a JSON-safe list of dicts.
    Two-stage approach:
      1. Replace NaN/Inf at the pandas level (handles object columns)
      2. Round-trip through json.dumps(allow_nan=False) to catch any
         floats that survived stage 1 (e.g. numpy float columns that
         pandas re-coerces from None back to nan during to_dict)
    """
    records = df.replace(
        {np.nan: None, np.inf: None, -np.inf: None}
    ).to_dict(orient="records")

    def _default(v):
        # pandas and NumPy date objects are not natively JSON-serializable.
        # Preserve them as ISO strings in previews, API results, and exports.
        if isinstance(v, (pd.Timestamp, np.datetime64)):
            ts = pd.Timestamp(v)
            return None if pd.isna(ts) else ts.isoformat()
        if isinstance(v, pd.Timedelta):
            return None if pd.isna(v) else str(v)
        if hasattr(v, "isoformat"):  # datetime.date / datetime.datetime
            return v.isoformat()
        return None  # genuine "can't serialize this, and don't know what it is"

    # Second pass: json round-trip catches any nan/Timestamp that slipped through
    return _json.loads(
        _json.dumps(records, allow_nan=False, default=_default)
    )

# ── Aggregation map ────────────────────────────────────────────────────────────
# Maps our display names → pandas agg function strings
NATIVE_AGGS = {
    "mean": "mean", "sum": "sum", "count": "count",
    "min": "min",   "max": "max", "median": "median",
    "std": "std",   "variance": "var",
}
# Calcs that aren't plain per-group aggregates — they need the *whole* group
# table (grand total / grand mean) to make sense, so they're always computed
# as a post-processing pass rather than pushed down as a single SQL aggregate.
DERIVED_CALCS = {"share_pct", "contribution_pct", "diff_from_avg", "rank"}
ALL_CALCS = list(NATIVE_AGGS.keys()) + ["mode"] + sorted(DERIVED_CALCS)

DISPLAY_AGGS = {
    "mean": "Average", "sum": "Sum", "count": "Count",
    "record_count": "Record Count", "min": "Minimum", "max": "Maximum",
    "median": "Median", "std": "Standard deviation",
    "variance": "Variance", "mode": "Mode",
    "share_pct": "Share of Total %", "contribution_pct": "Contribution %",
    "diff_from_avg": "Diff from Overall Average", "rank": "Rank",
}

def current_dataset_item():
    dataset_id = session.get("_sid")
    return get_dataset(dataset_id) if dataset_id else None

def alias_map():
    """Future user-alias contract. Falls back strictly to raw dataset fields."""
    item = current_dataset_item()
    raw = (item or {}).get("aliases") if item else None
    return raw if isinstance(raw, dict) else {}

def field_label(name):
    return alias_map().get(name, name)

def agg_label(agg):
    return DISPLAY_AGGS.get(str(agg), str(agg).replace("_", " ").title())

def op_label(metric=None, group_cols=None, agg=None, fallback="Analysis Result"):
    groups = [field_label(g) for g in (group_cols or []) if g]
    if metric and agg:
        base = f"{agg_label(agg)} of {field_label(metric)}"
    elif metric:
        base = field_label(metric)
    else:
        base = fallback
    return f"{base} by {' + '.join(groups)}" if groups else base

def result_meta(total_scanned, result_rows, displayed_rows=None):
    produced = int(result_rows or 0)
    shown = produced if displayed_rows is None else int(displayed_rows or 0)
    return {
        "total_rows_scanned": int(total_scanned or 0),
        "result_rows_produced": produced,
        "rows_currently_displayed": shown,
    }

def with_meta(payload, *, export_name=None, total_scanned=0, result_rows=None,
              displayed_rows=None, full_export=False):
    rows = payload.get("rows")
    if result_rows is None:
        result_rows = len(rows) if isinstance(rows, list) else 0
    payload["export_name"] = export_name or payload.get("title") or "panda_result"
    payload["full_export_supported"] = bool(full_export)
    payload["result_meta"] = result_meta(total_scanned, result_rows, displayed_rows)
    return payload

# ── Upload helpers ─────────────────────────────────────────────────────────────

def file_ext(filename):
    name = filename.lower()
    for compound in (".csv.gz", ".csv.bz2", ".tsv.gz", ".tsv.bz2", ".txt.gz", ".txt.bz2"):
        if name.endswith(compound):
            return compound.split(".", 1)[1]
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""

def allowed(filename):
    return file_ext(filename).split(".")[-1] in ALLOWED

def detect_encoding_bytes(raw: bytes) -> str:
    r = chardet.detect(raw[:100_000])
    enc = r.get("encoding") or "utf-8"
    return enc if (r.get("confidence") or 0) >= 0.6 else "utf-8"

def detect_csv_delimiter(raw: bytes, encoding: str) -> str:
    try:
        sample = raw[:20_000].decode(encoding, errors="replace")
        return csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t", "|"]).delimiter
    except Exception:
        return ","

def read_csv_bytes(raw: bytes, encoding: str, *, sep=None, compression="infer"):
    detected_sep = sep if sep is not None else detect_csv_delimiter(raw, encoding)
    try:
        return pd.read_csv(io.BytesIO(raw), encoding=encoding, sep=detected_sep, compression=compression)
    except Exception:
        return pd.read_csv(io.BytesIO(raw), encoding=encoding, sep=None, engine="python", compression=compression)

def load_dataset_bytes(filename, raw):
    """Parse one supported upload and return ``(DataFrame, encoding, format)``.

    Compressed text is expanded before delimiter/encoding detection. ZIP files
    use the first supported structured-data member and recurse through this same
    parser, keeping all upload-format decisions in one place.
    """
    ext = file_ext(filename)
    base_ext = ext.split(".")[-1]
    encoding = "utf-8"
    lower = filename.lower()

    if base_ext == "zip":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            candidates = [
                n for n in zf.namelist()
                if not n.endswith("/") and file_ext(n).split(".")[-1] in ARCHIVE_IMPORT_EXTS
            ]
            if not candidates:
                raise ValueError("ZIP archive does not contain a supported structured data file.")
            inner = candidates[0]
            df, encoding, source_format = load_dataset_bytes(Path(inner).name, zf.read(inner))
            return df, encoding, f"zip:{source_format}"

    if base_ext in ("csv", "tsv", "txt", "gz", "bz2"):
        # pandas' compression="infer" only works from a filename/path, never
        # from a bare BytesIO, so .gz/.bz2 uploads must be decompressed here
        # before encoding/delimiter sniffing and parsing, or they'd silently
        # be read as raw (garbage) binary text.
        if base_ext == "gz":
            raw = gzip.decompress(raw)
        elif base_ext == "bz2":
            raw = bz2.decompress(raw)
        encoding = detect_encoding_bytes(raw)
        sep = "\t" if lower.endswith((".tsv", ".tsv.gz", ".tsv.bz2")) else None
        try:
            return read_csv_bytes(raw, encoding, sep=sep, compression=None), encoding, ext
        except Exception:
            encoding = "latin-1"
            return read_csv_bytes(raw, encoding, sep=sep, compression=None), encoding, ext

    if base_ext in ("json", "jsonl", "ndjson"):
        encoding = detect_encoding_bytes(raw)
        text = io.BytesIO(raw)
        lines = base_ext in ("jsonl", "ndjson")
        return pd.read_json(text, encoding=encoding, lines=lines), encoding, base_ext

    if base_ext in ("xlsx", "xls"):
        return pd.read_excel(io.BytesIO(raw)), encoding, base_ext

    if base_ext == "parquet":
        return pd.read_parquet(io.BytesIO(raw)), encoding, base_ext

    if base_ext in ("feather", "arrow"):
        return pd.read_feather(io.BytesIO(raw)), encoding, base_ext

    if base_ext == "orc":
        return pd.read_orc(io.BytesIO(raw)), encoding, base_ext

    raise ValueError("Unsupported file type.")

def dataset_quality_profile(df):
    """Compute the lightweight, explainable quality signals used by Overview."""
    rows, cols = df.shape
    total_cells = rows * cols
    missing_cells = int(df.isna().sum().sum())
    duplicate_rows = int(df.duplicated().sum()) if rows else 0
    duplicate_columns = int(pd.Index(df.columns).duplicated().sum())
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    datetime_cols = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    bool_cols = [c for c in df.columns if pd.api.types.is_bool_dtype(df[c])]
    text_cols = [c for c in df.columns if c not in numeric_cols + datetime_cols + bool_cols]
    candidate_keys = []
    for c in df.columns:
        try:
            if rows and df[c].notna().all() and df[c].nunique(dropna=False) == rows:
                candidate_keys.append(c)
        except Exception:
            pass
    missing_penalty = (missing_cells / total_cells * 55) if total_cells else 0
    duplicate_penalty = (duplicate_rows / rows * 25) if rows else 0
    duplicate_col_penalty = min(20, duplicate_columns * 5)
    quality_score = max(0, min(100, round(100 - missing_penalty - duplicate_penalty - duplicate_col_penalty, 1)))
    schema_insights = [
        {"signal": "Numeric columns", "value": len(numeric_cols), "why_it_matters": "Useful as measures, distributions, and correlations."},
        {"signal": "Categorical/text columns", "value": len(text_cols), "why_it_matters": "Useful for grouping, filtering, segmentation, and category comparisons."},
        {"signal": "Candidate key columns", "value": len(candidate_keys), "why_it_matters": "Likely row identifiers for joins, drill-through, and duplicate checks."},
        {"signal": "Duplicate rows", "value": duplicate_rows, "why_it_matters": "Duplicates may inflate counts, sums, and totals."},
        {"signal": "Missing cells", "value": missing_cells, "why_it_matters": "Missingness can bias summaries and reduce chart coverage."},
    ]
    return {
        "quality_score": quality_score,
        "duplicate_rows": duplicate_rows,
        "duplicate_columns": duplicate_columns,
        "candidate_keys": candidate_keys[:20],
        "datetime_cols": datetime_cols,
        "bool_cols": bool_cols,
        "schema_insights": schema_insights,
        "schema_insight_cols": ["signal", "value", "why_it_matters"],
    }

def load_df_from_storage():
    """Loads the current saved dataset with Pandas."""
    dataset_id = session.get("_sid")
    if not dataset_id:
        return None, "No dataset loaded. Please upload a file."
    df, err = load_dataset(dataset_id)
    if err:
        if err == "Dataset not found.":
            session.pop("_sid", None)
        return None, err
    return df, None

def paged_df(df, params):
    rs, re = params.get("row_start"), params.get("row_end")
    if rs is not None and re is not None:
        try:
            start = max(0, int(rs))
            end = max(start, int(re))
            return df.iloc[start:end]
        except Exception:
            pass
    return df

def _eq_mask(s, val, negate=False):
    """Equality mask: tries numeric comparison for numeric columns, else string."""
    try:
        if pd.api.types.is_numeric_dtype(s):
            num = to_numeric_clean(s)
            fv = float(val)
            mask = num == fv
            # Also match string form (e.g. user typed "40.0" for float col)
            mask = mask | (s.astype(str) == str(val))
            return ~mask if negate else mask
    except (ValueError, TypeError):
        pass
    mask = s.astype(str) == str(val)
    return ~mask if negate else mask

def _isin_mask(s, vals_list):
    """isin mask: tries numeric comparison for numeric columns, else string."""
    str_vals = [v.strip() for v in vals_list]
    try:
        if pd.api.types.is_numeric_dtype(s):
            num_vals = []
            for v in str_vals:
                try: num_vals.append(float(v))
                except (ValueError, TypeError): pass
            num = to_numeric_clean(s)
            return num.isin(num_vals) | s.astype(str).isin(str_vals)
    except Exception:
        pass
    return s.astype(str).isin(str_vals)

def build_segment_series(df, col, mode="category", bins=5):
    """Return categorical, equal-width, or quantile segment labels for one column."""
    if col not in df.columns:
        return None, "segment column required."
    s = df[col]
    if mode == "category":
        return s.fillna("(Missing)").astype(str), None
    num = to_numeric_clean(s)
    if num.dropna().empty:
        return None, "Numeric segmenting requires a numeric column."
    bins = max(2, min(int(bins or 5), 20))
    try:
        if mode == "quantile":
            seg = pd.qcut(num, q=bins, duplicates="drop")
        else:
            seg = pd.cut(num, bins=bins)
        return seg.astype(str).replace("nan", "(Missing)"), None
    except Exception as e:
        return None, str(e)

def apply_selection(df, params):
    """Apply selected columns, then a row filter, then an end-exclusive row slice.

    Every analytical route calls this once so tables, metrics, charts, and exports
    all operate on the same selected DataFrame and cannot silently disagree.
    """
    cols = params.get("columns")
    if cols and cols != "all" and isinstance(cols, list):
        valid = [c for c in cols if c in df.columns]
        if valid: df = df[valid]
    fcol = params.get("filter_col"); fop = params.get("filter_op"); fval = params.get("filter_val")
    if fcol and fop and fval is not None and fcol in df.columns:
        s = df[fcol]
        try:
            if fop == "eq":         df = df[_eq_mask(s, fval)]
            elif fop == "ne":       df = df[_eq_mask(s, fval, negate=True)]
            elif fop == "contains": df = df[s.astype(str).str.contains(str(fval), case=False, na=False)]
            elif fop == "isin":     df = df[_isin_mask(s, [v.strip() for v in str(fval).split(",")])]
            else:
                n = float(fval); num = to_numeric_clean(s)
                if fop == "gt":  df = df[num > n]
                elif fop == "lt": df = df[num < n]
                elif fop == "gte": df = df[num >= n]
                elif fop == "lte": df = df[num <= n]
        except Exception: pass
    rs, re = params.get("row_start"), params.get("row_end")
    if rs is not None and re is not None:
        try: df = df.iloc[int(rs):int(re)]
        except Exception: pass
    return df

def detect_datetime_columns(df, sample_size=1000):
    """Identify native or string date columns with at least a 75% parse rate."""
    cols = []
    sample_n = min(len(df), sample_size)
    for c in df.columns:
        s = df[c].dropna().head(sample_n)
        if s.empty:
            continue
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            cols.append(c)
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            continue
        try:
            parsed = pd.to_datetime(s, errors="coerce", format="mixed")
        except (TypeError, ValueError):
            parsed = pd.to_datetime(s, errors="coerce")
        if parsed.notna().mean() >= 0.75:
            cols.append(c)
    return cols

def numeric_frame(df):
    return numeric_like_frame(df)

def likely_id_column(name, s):
    cl = str(name).lower()
    name_hit = any(p == cl or cl.endswith("_" + p) or cl.startswith(p + "_")
                   for p in ("id", "key", "uuid", "guid", "code", "number", "num", "no"))
    unique_pct = s.nunique(dropna=True) / max(len(s.dropna()), 1)
    return bool(name_hit or unique_pct > 0.95)

def strength_label(value):
    v = abs(float(value or 0))
    if v >= 0.7: return "Strong"
    if v >= 0.4: return "Moderate"
    if v >= 0.2: return "Weak"
    return "Very weak"

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index(): return send_from_directory(".", "index.html")

@app.route("/clear")
def clear():
    dataset_id = session.get("_sid")
    if dataset_id:
        # The file stays on disk (it's persistent, on-purpose) —
        # we just forget which dataset this browser session was pointed at.
        session.clear()
    return redirect("/")

@app.route("/api/local-datasets")
def local_datasets():
    if not LOCAL_STORAGE_ENABLED:
        return jsonify({"datasets": [], "local_storage": False})
    return jsonify({"datasets": list_datasets(), "local_storage": True})

@app.route("/api/local-datasets/<dataset_id>/open", methods=["POST"])
def open_local_dataset(dataset_id):
    if not LOCAL_STORAGE_ENABLED:
        return jsonify({"error": "Local storage is not enabled."}), 400
    item = get_dataset(dataset_id)
    if not item:
        return jsonify({"error": "Dataset not found."}), 404
    # Just point this session at the persistent dataset id — no DataFrame
    # is loaded into RAM here; every subsequent request reads from disk.
    session["_sid"] = dataset_id
    return jsonify({"ok": True, "dataset_id": dataset_id,
                    "filename": item.get("original_filename", "dataset.csv")})

@app.route("/api/local-datasets/<dataset_id>", methods=["DELETE"])
def remove_local_dataset(dataset_id):
    if not LOCAL_STORAGE_ENABLED:
        return jsonify({"error": "Local storage is not enabled."}), 400
    ok = delete_dataset(dataset_id)
    if not ok: return jsonify({"error": "Dataset not found."}), 404
    if session.get("_sid") == dataset_id:
        session.pop("_sid", None)
    return jsonify({"ok": True})

# ── Upload ─────────────────────────────────────────────────────────────────────

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or f.filename == "": return jsonify({"error": "No file."}), 400
    if not allowed(f.filename):
        return jsonify({"error": "Supported files: CSV, TSV, TXT, JSON, NDJSON, Excel, Parquet, Feather, Arrow, ORC, compressed CSV/TSV, or ZIP."}), 400
    filename = secure_filename(f.filename)
    raw = f.read()  # Parse upload bytes before saving the normalized DataFrame.
    try:
        df, encoding, source_format = load_dataset_bytes(filename, raw)
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400

    # Save to persistent storage — this is now the only place the data
    # lives, so it always happens (not gated behind LOCAL_STORAGE_ENABLED,
    # which only controls the "Saved Datasets" browser UI).
    saved = save_dataset(df, filename, encoding)
    # Store the persistent ID in the session cookie (NOT the dataframe)
    session["_sid"] = saved["id"]

    return jsonify({
        "ok": True,
        "filename": filename,
        "encoding": encoding,
        "source_format": source_format,
        "rows": int(df.shape[0]),
        "columns": int(df.shape[1]),
        "dataset_id": saved["id"],
        "local_storage": LOCAL_STORAGE_ENABLED,
    })

# ── Column metadata ────────────────────────────────────────────────────────────

@app.route("/api/columns")
def columns():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    item = current_dataset_item() or {}
    id_pats   = ["id", "num", "number", "row", "index", "key", "code", "no", "nr"]
    flag_pats = ["flag", "is_", "has_", "senior", "binary", "bool",
                 "churn", "defaulted", "cancelled", "renewed", "active"]
    numeric_cols     = df.select_dtypes(include="number").columns.tolist()
    categorical_cols = df.select_dtypes(include="object").columns.tolist()
    recommended_group, recommended_metric, other = [], [], []
    for col in df.columns:
        cl       = col.lower()
        n_unique = df[col].nunique()
        is_id       = any(p == cl or cl.endswith("_" + p) or cl.startswith(p + "_") or cl == p
                          for p in id_pats)
        is_low_card = n_unique <= min(50, max(10, len(df) * 0.05))
        is_numeric  = col in numeric_cols
        # Binary 0/1 columns or name-flagged columns → treat as categorical group dim
        is_flag = is_numeric and n_unique <= 5 and any(p in cl for p in flag_pats)
        if is_numeric and n_unique == 2:
            vals = set(df[col].dropna().unique())
            if vals <= {0, 1}:
                is_flag = True
        if is_id:
            other.append(col)
        elif is_flag or (is_numeric and is_low_card):
            # Binary/flag or low-cardinality numeric → better as a group dimension
            recommended_group.append(col)
        elif is_numeric and not is_low_card:
            # High-cardinality numeric → true metric (tenure, salary, charges)
            recommended_metric.append(col)
        elif not is_numeric and is_low_card:
            # Categorical with few unique values → ideal group column
            recommended_group.append(col)
        else:
            other.append(col)
    profile = dataset_quality_profile(df)
    datetime_cols = list(dict.fromkeys(profile["datetime_cols"] + detect_datetime_columns(df)))
    return jsonify({
        "filename":           item.get("original_filename", ""),
        "all":                df.columns.tolist(),
        "numeric":            numeric_cols,
        "categorical":        categorical_cols,
        "datetime":           datetime_cols,
        "boolean":            profile["bool_cols"],
        "recommended_gb":     recommended_group,   # kept for backwards-compat
        "recommended_group":  recommended_group,
        "recommended_metric": recommended_metric,
        "other_cols":         other,
        "candidate_keys":     profile["candidate_keys"],
        "quality_score":      profile["quality_score"],
    })

# ── Dataset understanding: overview and column dictionary ─────────────────────

@app.route("/api/overview")
def overview():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    total = df.shape[0] * df.shape[1]; miss = int(df.isnull().sum().sum())
    profile = dataset_quality_profile(df)
    cols = [{"column": c, "dtype": str(df[c].dtype),
             "missing": int(df[c].isnull().sum()),
             "missing_pct": round(df[c].isnull().sum() / len(df) * 100, 2) if len(df) else 0,
             "unique": int(df[c].nunique())} for c in df.columns]
    return jsonify(with_meta({
        "rows": df.shape[0], "columns": df.shape[1], "missing_total": miss,
        "missing_pct": round(miss / total * 100, 2) if total else 0,
        "data_quality_score": profile["quality_score"],
        "duplicate_rows": profile["duplicate_rows"],
        "duplicate_columns": profile["duplicate_columns"],
        "candidate_keys": ", ".join(profile["candidate_keys"][:5]) if profile["candidate_keys"] else "None detected",
        "memory_kb": round(df.memory_usage(deep=True).sum() / 1024, 2),
        "numeric_cols": int(df.select_dtypes(include="number").shape[1]),
        "object_cols":  int(df.select_dtypes(include="object").shape[1]),
        "col_info": cols,
        "table_rows": cols,
        "table_cols": ["column", "dtype", "unique", "missing", "missing_pct"],
        "schema_insights": profile["schema_insights"],
        "schema_insight_cols": profile["schema_insight_cols"],
        "title": "Dataset Overview",
        "code": "df.shape\ndf.info()\ndf.isna().sum()\ndf.duplicated().sum()"
    }, export_name="dataset_overview", total_scanned=df.shape[0],
       result_rows=len(cols), displayed_rows=len(cols), full_export=True))

# ── Dataset preview ────────────────────────────────────────────────────────────

@app.route("/api/column-dictionary")
def column_dictionary():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    rows = []
    for c in df.columns:
        s = df[c]
        non_null = int(s.notna().sum())
        missing = int(s.isna().sum())
        unique = int(s.nunique(dropna=True))
        role = "Measure" if pd.api.types.is_numeric_dtype(s) else "Dimension"
        if likely_id_column(c, s):
            role = "Key"
        rows.append({
            "column": c,
            "dtype": str(s.dtype),
            "role": role,
            "non_null": non_null,
            "missing": missing,
            "missing_pct": round(missing / len(s) * 100, 2) if len(s) else 0,
            "unique": unique,
            "unique_pct": round(unique / max(non_null, 1) * 100, 2),
            "sample_values": ", ".join(map(str, s.dropna().astype(str).head(3).tolist())) or "",
        })
    cols = [
        "column", "dtype", "role", "non_null", "missing", "missing_pct",
        "unique", "unique_pct", "sample_values",
    ]
    return jsonify(with_meta({
        "title": "Column Dictionary",
        "rows": rows,
        "cols": cols,
        "table_rows": rows,
        "table_cols": cols,
        "code": "df.dtypes\ndf.nunique(dropna=True)\ndf.isna().sum()",
    }, export_name="column_dictionary", total_scanned=df.shape[0],
       result_rows=len(rows), displayed_rows=len(rows), full_export=True))

@app.route("/api/preview", methods=["POST"])
def preview():
    d = request.get_json(silent=True) or {}
    mode = d.get("mode", "head"); n = int(d.get("n", 10))
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    df = apply_selection(df, d)
    total = len(df)
    if mode == "tail":
        res = df.tail(n)
    elif mode == "sample":
        res = df.sample(n=min(max(n, 1), max(total, 1)), random_state=42) if total else df
    else:
        res = df.head(n)
    return jsonify(with_meta({
        "rows": json_safe_records(res),
        "cols": res.columns.tolist(),
        "total": total,
        "title": f"Preview - {mode}",
        "code": f"df.{mode}({n})",
    }, export_name="preview", total_scanned=total, result_rows=total,
       displayed_rows=len(res), full_export=True))

# ── Categorical value counts ───────────────────────────────────────────────────

@app.route("/api/value-counts", methods=["POST"])
def value_counts():
    d = request.get_json(silent=True) or {}
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    df = apply_selection(df, d)
    col = d.get("column")
    if not col or col not in df.columns: return jsonify({"error": "column required."}), 400
    total = len(df)
    vc = df[col].fillna("(Missing)").astype(str).value_counts()
    rows = [
        {"value": str(k), "count": int(v), "pct": round(float(v / total * 100), 2) if total else 0}
        for k, v in vc.items()
    ]
    title = op_label(metric=col, agg="count", fallback="Value Counts")
    echarts = bar_chart(title,
                         [r["value"] for r in rows][::-1],
                         [{"name": "Count", "data": [r["count"] for r in rows][::-1], "color": ACCENT}],
                         y_label="Count", horizontal=True)
    return jsonify(with_meta({
        "rows": rows, "cols": ["value", "count", "pct"],
        "echarts": echarts, "column": col, "title": title,
        "code": f'df["{col}"].value_counts()',
    }, export_name=f"value_counts_{col}", total_scanned=total,
       result_rows=len(rows), displayed_rows=len(rows), full_export=True))

# ── Numeric distribution ───────────────────────────────────────────────────────

@app.route("/api/distribution", methods=["POST"])
def distribution():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    col = d.get("column")
    if not col or col not in df.columns: return jsonify({"error": "column required."}), 400
    s = to_numeric_clean(df[col]).dropna()
    if s.empty: return jsonify({"error": "No numeric values."}), 400
    sk = float(s.skew()); kt = float(s.kurtosis()); mean_v = float(s.mean()); med_v = float(s.median())
    mode_v = float(s.mode().iloc[0]) if not s.mode().empty else None
    sk_lbl = "Right Skewed" if sk > 0.5 else "Left Skewed" if sk < -0.5 else "Nearly Symmetric"
    kt_lbl = "Heavy Tails" if kt > 1 else "Flat Distribution" if kt < -1 else "Normal-like Tails"
    echarts = histogram_chart(f"Distribution — {col}", s, bins=30, x_label=col)
    if mode_v is not None:
        counts, edges = np.histogram(s, bins=30)
        labels = echarts["xAxis"]["data"]
        mode_idx = int(np.clip(np.digitize(mode_v, edges) - 1, 0, len(labels) - 1))
        echarts["markLines"].append({"name": f"Mode {mode_v:.2f}", "xAxis": labels[mode_idx], "color": AMBER})
    return jsonify({"column": col, "mean": round(mean_v, 4), "median": round(med_v, 4),
                    "mode": round(mode_v, 4) if mode_v is not None else None, "std": round(float(s.std()), 4),
                    "skewness": round(sk, 4), "skew_label": sk_lbl,
                    "kurtosis": round(kt, 4), "kurt_label": kt_lbl, "echarts": echarts,
                    "code": f'df["{col}"].hist(bins=30)\ndf["{col}"].skew()  # {round(sk,4)}\ndf["{col}"].kurtosis()  # {round(kt,4)}'})

# ── Group analysis ─────────────────────────────────────────────────────────────
# Unified endpoint replacing groupby, segment, multi-segment, agg-studio.
# Supports N group columns × M calculations.

@app.route("/api/group-analysis", methods=["POST"])
def group_analysis():
    d = request.get_json(silent=True) or {}
    metric = d.get("metric_col", "")
    group_cols = list(dict.fromkeys(d.get("group_cols", [])))
    calcs = d.get("calculations", ["mean"])

    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}
    df = apply_selection(df, d)

    metric     = d.get("metric_col", "")
    group_cols = list(dict.fromkeys(d.get("group_cols", [])))   # dedupe, preserve order
    calcs      = d.get("calculations", ["mean"])

    # Validation
    if not metric or metric not in df.columns:
        return jsonify({"error": f"Metric column '{metric}' not found."}), 400
    if not group_cols:
        return jsonify({"error": "Select at least one group column."}), 400
    for c in group_cols:
        if c not in df.columns:
            return jsonify({"error": f"Group column '{c}' not found."}), 400
    if not calcs:
        return jsonify({"error": "Select at least one calculation."}), 400

    has_mode     = "mode" in calcs
    native_calcs = [c for c in calcs if c in NATIVE_AGGS]
    derived_calcs = [c for c in calcs if c in DERIVED_CALCS]

    try:
        result = None

        # ── Native pandas aggregations ──────────────────────────────────────
        if native_calcs:
            funcs = [NATIVE_AGGS[c] for c in native_calcs]
            agg_result = df.groupby(group_cols)[metric].agg(funcs)

            # agg() on a Series with a list always returns a DataFrame
            if isinstance(agg_result, pd.Series):
                agg_result = agg_result.to_frame(name=native_calcs[0])

            agg_result = agg_result.reset_index()

            # Rename pandas internal names back to our display names
            # e.g. "var" → "variance"
            rename_map = {NATIVE_AGGS[c]: c for c in native_calcs if NATIVE_AGGS[c] != c}
            if rename_map:
                agg_result = agg_result.rename(columns=rename_map)
            result = agg_result

        # ── Mode (requires custom apply) ─────────────────────────────────────
        if has_mode:
            def safe_mode(x):
                m = x.dropna().mode()
                return float(m.iloc[0]) if not m.empty else None
            mode_s = df.groupby(group_cols)[metric].apply(safe_mode).reset_index()
            mode_s.columns = group_cols + ["mode"]
            result = result.merge(mode_s, on=group_cols, how="left") if result is not None else mode_s

        # ── Derived stats: share of total, contribution %, diff from the
        # overall average, and rank. These need the grand total / grand mean
        # across *all* groups, so they're always computed from a fresh
        # sum/mean/count basis rather than reused from whatever calcs the
        # user happened to also check — this keeps them correct even if the
        # user picked e.g. "median" only, and never as a raw dollar total.
        if derived_calcs:
            if result is None:
                result = df[group_cols].drop_duplicates().reset_index(drop=True)
            metric_numeric = to_numeric_clean(df[metric])
            basis = (
                df.assign(_panda_metric=metric_numeric)
                  .groupby(group_cols)["_panda_metric"]
                  .agg(_panda_total="sum", _panda_avg="mean")
                  .reset_index()
            )
            result = result.merge(basis, on=group_cols, how="left")
            grand_total = float(metric_numeric.sum())
            grand_mean = float(metric_numeric.mean()) if metric_numeric.notna().any() else None
            has_negative_total = bool((result["_panda_total"] < 0).any())

            if "share_pct" in derived_calcs or "contribution_pct" in derived_calcs:
                share_base = float(result["_panda_total"].abs().sum()) if has_negative_total else grand_total
                share = (
                    result["_panda_total"].abs() / share_base * 100 if has_negative_total else
                    result["_panda_total"] / share_base * 100
                ).round(2) if share_base else None
                if "share_pct" in derived_calcs:
                    result["share_pct"] = share
                if "contribution_pct" in derived_calcs:
                    # Same basis as share_pct (% of the grand total), but kept as
                    # a distinct column since analysts think of them separately
                    # ("share" = size, "contribution" = what's driving the sum).
                    result["contribution_pct"] = share
            if "diff_from_avg" in derived_calcs:
                result["diff_from_avg"] = (
                    (result["_panda_avg"] - grand_mean).round(4) if grand_mean is not None else None
                )
            if "rank" in derived_calcs:
                rank_basis = result["_panda_total"].abs() if has_negative_total else result["_panda_total"]
                result["rank"] = rank_basis.rank(ascending=False, method="min")

            result = result.drop(columns=["_panda_total", "_panda_avg"])

        if result is None:
            return jsonify({"error": "No valid calculations selected."}), 400

        result = result.round(4)

        # Rename "count" column to "record_count" so the UI shows
        # a meaningful label ("Number of records") instead of the metric name.
        if "count" in result.columns:
            result = result.rename(columns={"count": "record_count"})
            calcs = ["record_count" if c == "count" else c for c in calcs]

        # Columns that actually exist in result (in calc order)
        calc_cols = [c for c in calcs if c in result.columns]
        result = result[group_cols + calc_cols]

        # Sort by rank if present (already the "right" order); otherwise by
        # the first calc if single group (most useful ordering)
        if "rank" in calc_cols:
            result = result.sort_values("rank")
        elif len(group_cols) == 1 and calc_cols:
            result = result.sort_values(calc_cols[0], ascending=False)

    except Exception as e:
        return jsonify({"error": str(e)}), 400

    # ── Chart ─────────────────────────────────────────────────────────────────
    # Human-readable label for a calc column (avoids "count of tenure" phrasing)
    def calc_label(c):
        if c in DISPLAY_AGGS: return DISPLAY_AGGS[c]
        return c.replace("_", " ").title()

    def metric_title(c, m):
        """Chart title: "Record Count by X"  or  "Mean of tenure by X"."""
        if c == "record_count": return "Record Count"
        return f"{calc_label(c)} of {m}"

    chart = None
    try:
        if len(group_cols) == 1:
            g = group_cols[0]
            if len(calc_cols) == 1:
                cats = result[g].astype(str).tolist()[::-1]
                vals = result[calc_cols[0]].tolist()[::-1]
                series_data = [{"value": v, "itemStyle": {"color": PALETTE[i % len(PALETTE)]}}
                                for i, v in enumerate(vals)]
                chart = bar_chart(f"{metric_title(calc_cols[0], metric)} by {g}", cats,
                                   [{"name": calc_label(calc_cols[0]), "data": series_data}],
                                   y_label=calc_label(calc_cols[0]), horizontal=True)
            else:
                cats = result[g].astype(str).tolist()
                series = [{"name": calc_label(c), "data": result[c].round(4).tolist(),
                           "color": PALETTE[i % len(PALETTE)]} for i, c in enumerate(calc_cols)]
                pretty_calcs = ", ".join(calc_label(c) for c in calc_cols)
                chart = bar_chart(f"{metric} — {pretty_calcs} by {g}", cats, series, x_label=g)
        elif len(group_cols) == 2 and calc_cols:
            try:
                pv = result.pivot_table(index=group_cols[1], columns=group_cols[0],
                                         values=calc_cols[0], fill_value=0)
                chart = heatmap_chart(
                    f"{metric_title(calc_cols[0], metric)}  ({group_cols[0]} × {group_cols[1]})", pv)
            except Exception:
                chart = None
        # 3+ groups: no chart, table is the answer
    except Exception:
        chart = None

    # ── Pandas code ───────────────────────────────────────────────────────────
    # Map display names back to real pandas agg names for the code snippet
    _code_calcs = ["count" if c == "record_count" else c for c in calcs if c not in DERIVED_CALCS]
    gc = f'"{group_cols[0]}"' if len(group_cols) == 1 else str(group_cols)
    if _code_calcs:
        code = (f'df.groupby({gc})["{metric}"].{_code_calcs[0]}()' if len(_code_calcs) == 1
                 else f'df.groupby({gc})["{metric}"].agg({_code_calcs})')
    else:
        code = f'g = df.groupby({gc})["{metric}"].sum()'
    if any(c in derived_calcs for c in ("share_pct", "contribution_pct")):
        code += '\nshare_pct = g / g.sum() * 100'
    if "diff_from_avg" in derived_calcs:
        code += '\ndiff_from_avg = g.mean() - df["' + metric + '"].mean()  # per-group avg vs overall avg'
    if "rank" in derived_calcs:
        code += '\nrank = g.rank(ascending=False, method="min")'

    return jsonify({
        "rows":         json_safe_records(result),
        "cols":         result.columns.tolist(),
        "echarts":      chart,
        "group_cols":   group_cols,
        "metric_col":   metric,
        "calculations": calcs,
        "calc_cols":    calc_cols,
        "code":         code,
    })

# ── 9. Pivot Table ─────────────────────────────────────────────────────────────

@app.route("/api/pivot", methods=["POST"])
def pivot():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    idx = d.get("index", ""); cols = d.get("columns", "")
    vals = d.get("values", ""); agg = d.get("agg", "mean")
    aggfunc = normalize_agg(agg)
    try:
        pt = pd.pivot_table(df, index=idx, columns=cols, values=vals,
                            aggfunc=aggfunc, fill_value=0)
        return jsonify({"html": pt.round(4).to_html(classes="panda-table", border=0),
                        "code": f'pd.pivot_table(df, index="{idx}", columns="{cols}", values="{vals}", aggfunc="{aggfunc}")'})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ── 10. Crosstab ───────────────────────────────────────────────────────────────

@app.route("/api/crosstab", methods=["POST"])
def crosstab():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    rc = d.get("row_col", ""); cc = d.get("col_col", "")
    for c in [rc, cc]:
        if c not in df.columns: return jsonify({"error": f"Column '{c}' not found."}), 400
    norm = d.get("normalize", False)
    ct = pd.crosstab(df[rc], df[cc], normalize="all" if norm else False)
    if norm: ct = (ct * 100).round(2)
    norm_str = ', normalize="all"' if norm else ""
    return jsonify({"html": ct.to_html(classes="panda-table", border=0),
                    "code": f'pd.crosstab(df["{rc}"], df["{cc}"]{norm_str})'})

# ── 11. Correlation ────────────────────────────────────────────────────────────

@app.route("/api/correlation", methods=["POST"])
def correlation():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    num = numeric_like_frame(df)
    ignore_ids = bool(d.get("ignore_ids", False))
    id_like = []
    if ignore_ids:
        for c in list(num.columns):
            lc = str(c).strip().lower()
            name_flag = bool(re.search(r'(^|_)(id|uuid|guid)($|_)', lc)) or lc in ("id", "uuid", "guid")
            # A column where every value is unique (and non-null) reads like a
            # row identifier regardless of its name (e.g. "record_no").
            unique_flag = len(num) > 1 and num[c].notna().all() and num[c].nunique(dropna=False) == len(num)
            if name_flag or unique_flag:
                id_like.append(c)
        if id_like:
            num = num.drop(columns=id_like)
    if num.shape[1] < 2:
        msg = "Need ≥2 numeric columns."
        if id_like:
            msg = "Need ≥2 numeric columns after excluding ID-like columns: " + ", ".join(id_like)
        return jsonify({"error": msg}), 400
    method = d.get("method", "pearson"); corr = num.corr(method=method).round(4)
    echarts = heatmap_chart(f"Correlation Matrix ({method.title()})", corr,
                             mask_upper=True, min_v=-1, max_v=1, diverging=True)
    cols = corr.columns.tolist()
    pairs = [{"col1": cols[i], "col2": cols[j], "corr": round(corr.iloc[i, j], 4),
              "strength": strength_label(corr.iloc[i, j])}
             for i in range(len(cols)) for j in range(i + 1, len(cols)) if pd.notna(corr.iloc[i, j])]
    pairs.sort(key=lambda x: abs(x["corr"]), reverse=True)
    target = d.get("target_col", "")
    target_rows = []
    if target in corr.columns:
        target_rows = [{"column": c, "corr": round(float(corr.loc[target, c]), 4),
                        "strength": strength_label(corr.loc[target, c])}
                       for c in corr.columns if c != target and pd.notna(corr.loc[target, c])]
        target_rows.sort(key=lambda r: abs(r["corr"]), reverse=True)
    multi_warn = [p for p in pairs if abs(p["corr"]) >= 0.8][:10]
    return jsonify(with_meta({"echarts": echarts, "method": method,
                    "top_positive": [p for p in pairs if p["corr"] > 0][:5],
                    "top_negative": [p for p in pairs if p["corr"] < 0][:5],
                    "rows": target_rows or pairs[:20],
                    "cols": ["column", "corr", "strength"] if target_rows else ["col1", "col2", "corr", "strength"],
                    "target_col": target or "All pairs",
                    "ignored_id_like_columns": id_like,
                    "multicollinearity_warnings": len(multi_warn),
                    "title": "Correlation Analysis",
                    "code": f'df.corr(method="{method}")'},
                   export_name="correlation_analysis", total_scanned=len(df),
                   result_rows=len(target_rows or pairs), displayed_rows=len(target_rows or pairs[:20])))

# ── Ranking ────────────────────────────────────────────────────────────────────

@app.route("/api/ranking", methods=["POST"])
def ranking():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    col = d.get("column")
    if not col or col not in df.columns: return jsonify({"error": "column required."}), 400
    numeric = to_numeric_clean(df[col])
    work = df.assign(_panda_rank_value=numeric).dropna(subset=["_panda_rank_value"])
    largest = work.sort_values("_panda_rank_value", ascending=False).drop(columns=["_panda_rank_value"])
    smallest = work.sort_values("_panda_rank_value", ascending=True).drop(columns=["_panda_rank_value"])
    return jsonify({"column": col, "n": len(work),
                    "largest":  json_safe_records(largest),
                    "smallest": json_safe_records(smallest),
                    "cols": df.columns.tolist(),
                    "code": f'df.sort_values("{col}", ascending=False)\ndf.sort_values("{col}", ascending=True)'})

# ── Column profiler ────────────────────────────────────────────────────────────

@app.route("/api/column-profiler", methods=["POST"])
def column_profiler():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    col = d.get("column")
    if not col or col not in df.columns: return jsonify({"error": "column required."}), 400
    s = df[col]; is_num = pd.api.types.is_numeric_dtype(s)
    profile = {"column": col, "dtype": str(s.dtype), "total": len(s),
               "missing": int(s.isnull().sum()),
               "missing_pct": round(s.isnull().sum() / len(s) * 100, 2) if len(s) else 0,
               "unique": int(s.nunique()),
               "unique_pct": round(s.nunique() / len(s) * 100, 2) if len(s) else 0,
               "is_numeric": is_num}
    if is_num:
        sn = s.dropna()
        profile.update({"mean": round(float(sn.mean()), 4), "median": round(float(sn.median()), 4),
                         "mode": round(float(sn.mode().iloc[0]), 4) if not sn.mode().empty else None,
                         "std": round(float(sn.std()), 4), "variance": round(float(sn.var()), 4),
                         "min": round(float(sn.min()), 4), "max": round(float(sn.max()), 4),
                         "q1": round(float(sn.quantile(0.25)), 4), "q3": round(float(sn.quantile(0.75)), 4),
                         "iqr": round(float(sn.quantile(0.75) - sn.quantile(0.25)), 4),
                         "skewness": round(float(sn.skew()), 4), "kurtosis": round(float(sn.kurtosis()), 4)})
        echarts_hist = histogram_chart("Distribution", sn, bins=25, x_label=col)
        echarts_box = boxplot_chart("Boxplot", [col], [sn.values])
        profile["echarts_hist"] = echarts_hist
        profile["echarts_box"] = echarts_box
        profile["code"] = f's = df["{col}"]\ns.describe()\ns.skew()  # {profile["skewness"]}\ns.kurtosis()  # {profile["kurtosis"]}'
    else:
        vc = s.value_counts().head(10)
        profile["top_values"] = [{"value": str(k), "count": int(v),
                                   "pct": round(v / len(s) * 100, 2)} for k, v in vc.items()]
        echarts = bar_chart(f"Top Values — {col}", [str(k) for k in vc.index[::-1]],
                             [{"name": "Count", "data": vc.values[::-1].tolist(), "color": ACCENT}],
                             y_label="Count", horizontal=True)
        profile["echarts"] = echarts
        profile["code"] = f's = df["{col}"]\ns.dtype  # {s.dtype}\ns.nunique()  # {profile["unique"]}\ns.value_counts().head(10)'
    return jsonify(profile)

# ── Recommended scatter visualization ──────────────────────────────────────────

@app.route("/api/scatter", methods=["POST"])
def scatter():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    xc = d.get("x_col", ""); yc = d.get("y_col", "")
    for c in [xc, yc]:
        if c not in df.columns: return jsonify({"error": f"Column '{c}' not found."}), 400
    x = to_numeric_clean(df[xc]); y = to_numeric_clean(df[yc])
    m = x.notna() & y.notna(); x, y = x[m], y[m]
    corr = round(float(x.corr(y)), 4)
    echarts = scatter_chart(f"Scatter — {xc} vs {yc}  (r={corr})", x, y, x_label=xc, y_label=yc)
    return jsonify({"x_col": xc, "y_col": yc, "correlation": corr, "echarts": echarts,
                    "code": f'df.plot.scatter("{xc}", "{yc}")\ndf[["{xc}","{yc}"]].corr()'})

# ── Data quality, time, comparison, missingness, outliers, and segments ────────

@app.route("/api/data-quality-studio", methods=["POST"])
def data_quality_studio():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    rows_count = len(df)
    date_cols = detect_datetime_columns(df)
    rows, issues = [], []
    for c in df.columns:
        s = df[c]
        missing = int(s.isna().sum())
        unique = int(s.nunique(dropna=True))
        unique_pct = round(unique / max(rows_count, 1) * 100, 2)
        num = to_numeric_clean(s)
        numeric_parse_pct = round(num.notna().sum() / max(s.notna().sum(), 1) * 100, 2)
        warnings_col = []
        role = "Metric" if pd.api.types.is_numeric_dtype(s) and unique_pct < 95 else "Dimension"
        if likely_id_column(c, s):
            role = "Identifier"; warnings_col.append("ID-like")
        if missing: warnings_col.append("Missing values")
        if unique <= 1: warnings_col.append("Constant")
        elif unique_pct > 80 and role != "Identifier": warnings_col.append("High cardinality")
        if not pd.api.types.is_numeric_dtype(s) and numeric_parse_pct >= 80:
            warnings_col.append("Numbers stored as text")
        if c in date_cols: role = "Date/Time"
        if pd.api.types.is_numeric_dtype(s) or numeric_parse_pct >= 80:
            valid_num = num.dropna()
            if not valid_num.empty and (valid_num < 0).any():
                warnings_col.append("Contains negatives")
        if warnings_col:
            issues.extend({"column": c, "issue": w} for w in warnings_col)
        rows.append({
            "column": c, "dtype": str(s.dtype), "role": role,
            "missing": missing, "missing_pct": round(missing / max(rows_count, 1) * 100, 2),
            "unique": unique, "unique_pct": unique_pct,
            "numeric_parse_pct": numeric_parse_pct,
            "warnings": ", ".join(warnings_col) if warnings_col else "OK",
        })
    duplicate_rows = int(df.duplicated().sum()) if rows_count else 0
    total_cells = max(rows_count * max(len(df.columns), 1), 1)
    score = max(0, min(100, round(100 - (sum(r["missing"] for r in rows) / total_cells * 55)
                                - (duplicate_rows / max(rows_count, 1) * 25)
                                - min(len(issues) * 1.5, 20), 1)))
    return jsonify(with_meta({
        "title": "Data Quality Studio",
        "data_quality_score": score, "duplicate_rows": duplicate_rows,
        "issue_count": len(issues), "date_columns_detected": len(date_cols),
        "rows": rows,
        "cols": ["column", "dtype", "role", "missing", "missing_pct", "unique", "unique_pct", "numeric_parse_pct", "warnings"],
        "schema_insights": issues[:25], "schema_insight_cols": ["column", "issue"],
        "code": "df.isna().sum()\ndf.duplicated().sum()\ndf.nunique()",
    }, export_name="data_quality_studio", total_scanned=rows_count,
       result_rows=len(rows), displayed_rows=len(rows), full_export=True))

@app.route("/api/time-series-studio", methods=["POST"])
def time_series_studio():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    date_col = d.get("date_col", ""); metric = d.get("metric_col", "")
    category = d.get("category_col", ""); freq = d.get("freq", "M"); agg = d.get("agg", "sum")
    if date_col not in df.columns: return jsonify({"error": "Date column required."}), 400
    if metric and metric not in df.columns: return jsonify({"error": "Metric column not found."}), 400
    try: dt = pd.to_datetime(df[date_col], errors="coerce", format="mixed")
    except (TypeError, ValueError): dt = pd.to_datetime(df[date_col], errors="coerce")
    work = df.copy(); work["_date"] = dt; work = work[work["_date"].notna()]
    if work.empty: return jsonify({"error": "No valid dates found."}), 400
    if agg == "none":
        # Raw mode: no period bucketing, no moving average — just the
        # individual dated rows, sorted chronologically.
        cols = [date_col] + ([metric] if metric and metric in work.columns else [])
        res = work.sort_values("_date")[cols]
        rows = json_safe_records(res)
        return jsonify(with_meta({
            "title": f"Raw rows — {date_col}" + (f" & {metric}" if metric else ""),
            "date_col": date_col, "metric_col": metric or None, "frequency": None,
            "rows": rows, "cols": cols, "echarts": None, "is_raw": True,
            "code": f'df.sort_values("{date_col}")[{cols!r}]  # no period bucketing',
        }, export_name="time_series_raw", total_scanned=len(df),
           result_rows=len(rows), displayed_rows=len(rows), full_export=True))
    work["_period"] = work["_date"].dt.to_period(freq).dt.to_timestamp()
    if metric:
        work["_metric"] = to_numeric_clean(work[metric]); work = work[work["_metric"].notna()]
        if work.empty: return jsonify({"error": "No numeric metric values found."}), 400
        grouped = work.groupby("_period")["_metric"]
        series = grouped.sum() if agg == "sum" else grouped.mean() if agg == "mean" else grouped.count()
    else:
        series = work.groupby("_period").size(); agg = "count"
    result = series.reset_index(); result.columns = ["period", "value"]
    result["period"] = result["period"].dt.strftime("%Y-%m-%d")
    result["moving_avg_3"] = result["value"].rolling(3, min_periods=1).mean().round(4)
    result["period_change_pct"] = result["value"].pct_change().replace([np.inf, -np.inf], np.nan).mul(100).round(2)
    rows = json_safe_records(result.round(4))
    echarts = line_chart(f"{agg_label(agg)} of {metric or 'rows'} over time",
                         result["period"].tolist(),
                         [{"name": agg_label(agg), "type": "line", "data": result["value"].round(4).tolist(), "color": ACCENT},
                          {"name": "3-period moving avg", "type": "line", "data": result["moving_avg_3"].round(4).tolist(), "color": PINK}],
                         x_label=date_col, y_label=metric or "Rows")
    return jsonify(with_meta({
        "title": "Time Series Studio", "date_col": date_col,
        "metric_col": metric or "Record count", "frequency": freq,
        "rows": rows, "cols": ["period", "value", "moving_avg_3", "period_change_pct"],
        "echarts": echarts,
        "code": f'df.assign(_date=pd.to_datetime(df["{date_col}"])).groupby(pd.Grouper(key="_date", freq="{freq}"))["{metric}"].{agg}()',
    }, export_name="time_series_studio", total_scanned=len(df),
       result_rows=len(rows), displayed_rows=len(rows), full_export=True))

@app.route("/api/group-comparison", methods=["POST"])
def group_comparison():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    group_col = d.get("group_col", ""); metric = d.get("metric_col", "")
    agg = d.get("agg", "mean")
    if group_col not in df.columns or metric not in df.columns:
        return jsonify({"error": "Group and metric columns are required."}), 400
    if agg == "none":
        # Raw mode: skip the lift/overall-average math entirely and hand
        # back the actual rows, sorted by group, for eyeballing directly.
        res = df[[group_col, metric]].sort_values(group_col)
        rows = json_safe_records(res)
        return jsonify(with_meta({
            "title": f"Raw rows — {group_col} & {metric}", "group_col": group_col,
            "metric_col": metric, "overall": None, "rows": rows,
            "cols": [group_col, metric], "echarts": None, "is_raw": True,
            "code": f'df[["{group_col}", "{metric}"]].sort_values("{group_col}")  # no aggregation',
        }, export_name="group_comparison_raw", total_scanned=len(df),
           result_rows=len(rows), displayed_rows=len(rows), full_export=True))
    work = pd.DataFrame({"group": df[group_col].fillna("(Missing)").astype(str),
                         "metric": to_numeric_clean(df[metric])}).dropna()
    if work.empty: return jsonify({"error": "No numeric metric values found."}), 400
    grouped = work.groupby("group")["metric"]
    vals = grouped.sum() if agg == "sum" else grouped.median() if agg == "median" else grouped.count() if agg == "count" else grouped.mean()
    overall = float(vals.mean()) if agg == "count" else (
        work["metric"].sum() if agg == "sum" else
        work["metric"].median() if agg == "median" else
        work["metric"].mean()
    )
    total_sum = float(work["metric"].sum())
    res = vals.sort_values(ascending=False).reset_index(); res.columns = [group_col, "value"]
    res["overall"] = round(float(overall), 4); res["difference"] = (res["value"] - overall).round(4)
    res["lift_pct"] = ((res["value"] - overall) / overall * 100).replace([np.inf, -np.inf], np.nan).round(2) if overall else None
    denom = float(res["value"].sum())
    res["share_pct"] = res["value"].div(denom).mul(100).round(2) if denom else None
    res["contribution_pct"] = res["value"].div(total_sum).mul(100).round(2) if total_sum and agg == "sum" else None
    res["rank"] = range(1, len(res) + 1)
    rows = json_safe_records(res.round(4))
    echarts = bar_chart(f"{agg_label(agg)} of {metric} by {group_col}",
                        res[group_col].astype(str).tolist()[::-1],
                        [{"name": agg_label(agg), "data": res["value"].round(4).tolist()[::-1], "color": ACCENT}],
                        y_label=agg_label(agg), horizontal=True)
    return jsonify(with_meta({
        "title": "Group Comparison / Lift Analysis", "group_col": group_col,
        "metric_col": metric, "overall": round(float(overall), 4),
        "rows": rows,
        "cols": [group_col, "rank", "value", "overall", "difference", "lift_pct", "share_pct", "contribution_pct"],
        "echarts": echarts,
        "code": f'g=df.groupby("{group_col}")["{metric}"].{agg}()\n(g-g.mean())/g.mean()*100',
    }, export_name="group_comparison", total_scanned=len(df),
       result_rows=len(rows), displayed_rows=len(rows), full_export=True))

@app.route("/api/contribution-analysis", methods=["POST"])
def contribution_analysis():
    """
    "Which segment is driving the metric?" — a Pareto-style contribution
    table. Unlike group-analysis (which reports a stat per group), this
    always ranks by contribution to the grand total and adds a running
    (cumulative) contribution %, so an analyst can see at a glance how many
    groups it takes to explain 80% of the metric — and flags any negative
    contributors (groups that are actually dragging the total down).
    """
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    group_col = d.get("group_col", ""); metric = d.get("metric_col", "")
    if group_col not in df.columns or metric not in df.columns:
        return jsonify({"error": "Group and metric columns are required."}), 400

    work = pd.DataFrame({
        "group": df[group_col].fillna("(Missing)").astype(str),
        "metric": to_numeric_clean(df[metric]),
    }).dropna(subset=["metric"])
    if work.empty: return jsonify({"error": "No numeric metric values found."}), 400

    g = work.groupby("group")["metric"].agg(total="sum", record_count="count").reset_index()
    g = g.rename(columns={"group": group_col})
    grand_total = float(g["total"].sum())
    has_negative = bool((g["total"] < 0).any())
    contribution_base = float(g["total"].abs().sum()) if has_negative else grand_total

    g = g.sort_values("total", key=lambda s: s.abs() if has_negative else s, ascending=False).reset_index(drop=True)
    g["rank"] = range(1, len(g) + 1)
    if contribution_base:
        g["contribution_pct"] = (
            g["total"].abs() / contribution_base * 100 if has_negative else g["total"] / contribution_base * 100
        ).round(2)
    else:
        g["contribution_pct"] = 0.0
    g["cumulative_pct"] = g["contribution_pct"].cumsum().round(2)
    g["direction"] = np.where(g["total"] >= 0, "positive", "negative")

    # Where the classic 80% Pareto cut lands (only meaningful when the
    # metric is mostly non-negative, but harmless to compute either way).
    over80 = g.index[g["cumulative_pct"] >= 80]
    groups_to_80pct = int(g.loc[over80[0], "rank"]) if len(over80) else None

    neg = g[g["total"] < 0]
    top_row = g.iloc[0] if len(g) else None

    rows = json_safe_records(g.round(4))
    cols = [group_col, "rank", "total", "record_count", "contribution_pct", "cumulative_pct", "direction"]

    chart_n = min(len(g), 15)
    chart_df = g.head(chart_n)
    echarts = {
        "type": "bar",
        "title": f"Contribution to {metric} by {group_col} (Pareto)",
        "xAxis": {"name": group_col, "data": chart_df[group_col].astype(str).tolist(), "rotate": 35},
        "yAxis": {"name": "% of total"},
        "series": [
            {"name": "Contribution %", "type": "bar", "data": chart_df["contribution_pct"].round(2).tolist(), "color": ACCENT},
            {"name": "Cumulative %", "type": "line", "data": chart_df["cumulative_pct"].round(2).tolist(), "color": PINK},
        ],
        "markLines": [],
    }

    return jsonify(with_meta({
        "title": "Contribution Analysis (Pareto)", "group_col": group_col, "metric_col": metric,
        "total_groups": int(len(g)), "grand_total": round(grand_total, 4),
        "top_contributor": str(top_row[group_col]) if top_row is not None else None,
        "top_contributor_pct": float(top_row["contribution_pct"]) if top_row is not None else None,
        "contribution_basis": "absolute_total" if has_negative else "grand_total",
        "groups_to_reach_80pct": groups_to_80pct,
        "negative_contributor_count": int(len(neg)),
        "negative_contributor_total": round(float(neg["total"].sum()), 4) if len(neg) else 0.0,
        "rows": rows, "cols": cols, "echarts": echarts,
        "code": (
            f'g = df.groupby("{group_col}")["{metric}"].sum().sort_values(ascending=False)\n'
            f'contribution_pct = g / g.sum() * 100\n'
            f'cumulative_pct = contribution_pct.cumsum()  # how many groups reach 80%?'
        ),
    }, export_name="contribution_analysis", total_scanned=len(df),
       result_rows=len(rows), displayed_rows=len(rows), full_export=True))

@app.route("/api/variance-analysis", methods=["POST"])
def variance_analysis():
    """
    Current period vs. previous period: absolute change, % change, and —
    when a group column is supplied — which groups drove the increase or
    decrease, ranked by their contribution to the net change.
    """
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    date_col = d.get("date_col", ""); metric = d.get("metric_col", "")
    group_col = d.get("group_col", ""); freq = d.get("freq", "M"); agg = d.get("agg", "sum")
    if date_col not in df.columns or metric not in df.columns:
        return jsonify({"error": "Date and metric columns are required."}), 400

    try: dt = pd.to_datetime(df[date_col], errors="coerce", format="mixed")
    except (TypeError, ValueError): dt = pd.to_datetime(df[date_col], errors="coerce")
    work = df.copy(); work["_date"] = dt; work["_metric"] = to_numeric_clean(work[metric])
    work = work[work["_date"].notna() & work["_metric"].notna()]
    if work.empty: return jsonify({"error": "No valid dated numeric rows found."}), 400
    work["_period"] = work["_date"].dt.to_period(freq).dt.to_timestamp()

    periods = sorted(work["_period"].unique())
    if len(periods) < 2:
        return jsonify({"error": "Need at least two periods to compare — try a smaller grain (e.g. Week or Month)."}), 400

    def pick_period(key, fallback):
        raw = d.get(key)
        if not raw: return fallback
        try:
            ts = pd.Timestamp(raw).to_period(freq).to_timestamp()
            return ts if ts in periods else fallback
        except (ValueError, TypeError):
            return fallback

    cur_p = pick_period("current_period", periods[-1])
    prev_p = pick_period("previous_period", periods[-2] if cur_p == periods[-1] else periods[-1])
    if cur_p == prev_p and len(periods) >= 2:
        prev_p = periods[periods.index(cur_p) - 1] if periods.index(cur_p) > 0 else periods[-1]

    def agg_series(s):
        if agg == "mean": return s.mean()
        if agg == "count": return s.count()
        return s.sum()

    cur_total = float(agg_series(work.loc[work["_period"] == cur_p, "_metric"]))
    prev_total = float(agg_series(work.loc[work["_period"] == prev_p, "_metric"]))
    abs_change = cur_total - prev_total
    pct_change = (abs_change / prev_total * 100) if prev_total else None

    rows, cols, echarts = [], [], None
    if group_col and group_col in work.columns:
        gwork = work.assign(_g=work[group_col].fillna("(Missing)").astype(str))
        gcur = gwork.loc[gwork["_period"] == cur_p].groupby("_g")["_metric"].apply(agg_series)
        gprev = gwork.loc[gwork["_period"] == prev_p].groupby("_g")["_metric"].apply(agg_series)
        comp = pd.DataFrame({"previous_value": gprev, "current_value": gcur}).fillna(0).reset_index()
        comp = comp.rename(columns={"_g": group_col})
        comp["absolute_change"] = (comp["current_value"] - comp["previous_value"]).round(4)
        comp["pct_change"] = comp.apply(
            lambda r: round((r["absolute_change"] / r["previous_value"] * 100), 2) if r["previous_value"] else None,
            axis=1,
        )
        total_abs_move = comp["absolute_change"].abs().sum()
        comp["contribution_to_change_pct"] = (
            (comp["absolute_change"].abs() / total_abs_move * 100).round(2) if total_abs_move else 0.0
        )
        comp["direction"] = np.where(comp["absolute_change"] > 0, "increase",
                              np.where(comp["absolute_change"] < 0, "decrease", "flat"))
        comp = comp.sort_values("absolute_change", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)
        comp["rank"] = range(1, len(comp) + 1)
        rows = json_safe_records(comp.round(4))
        cols = [group_col, "rank", "previous_value", "current_value", "absolute_change",
                "pct_change", "contribution_to_change_pct", "direction"]
        top = comp.head(12)
        echarts = bar_chart(
            f"Change in {metric} by {group_col} ({prev_p.strftime('%b %Y')} \u2192 {cur_p.strftime('%b %Y')})",
            top[group_col].astype(str).tolist()[::-1],
            [{"name": "Change", "data": [
                {"value": v, "itemStyle": {"color": GREEN if v >= 0 else PINK}}
                for v in top["absolute_change"].tolist()[::-1]
            ]}],
            y_label=f"Change in {metric}", horizontal=True,
        )

    period_labels = [pd.Timestamp(p).strftime("%Y-%m-%d") for p in periods]
    trend = work.groupby("_period")["_metric"].apply(agg_series).reindex(periods)
    if echarts is None:
        echarts = line_chart(f"{agg_label(agg)} of {metric} — trend", period_labels,
                              [{"name": metric, "data": [round(float(v), 4) for v in trend.tolist()], "color": ACCENT}])

    return jsonify(with_meta({
        "title": "Variance Analysis (Period over Period)", "date_col": date_col, "metric_col": metric,
        "group_col": group_col or None, "frequency": freq, "agg": agg,
        "current_period": cur_p.strftime("%Y-%m-%d"), "previous_period": prev_p.strftime("%Y-%m-%d"),
        "current_value": round(cur_total, 4), "previous_value": round(prev_total, 4),
        "absolute_change": round(abs_change, 4),
        "pct_change": round(pct_change, 2) if pct_change is not None else None,
        "direction": "increase" if abs_change > 0 else ("decrease" if abs_change < 0 else "flat"),
        "rows": rows, "cols": cols, "echarts": echarts,
        "code": (
            f'p = df.assign(_d=pd.to_datetime(df["{date_col}"])).groupby(pd.Grouper(key="_d", freq="{freq}"))["{metric}"].{agg}()\n'
            f'change = p.iloc[-1] - p.iloc[-2]\n'
            f'pct_change = change / p.iloc[-2] * 100'
        ),
    }, export_name="variance_analysis", total_scanned=len(df),
       result_rows=len(rows) if rows else 1, displayed_rows=len(rows) if rows else 1, full_export=True))

@app.route("/api/missing-analysis", methods=["POST"])
def missing_analysis():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    group_col = d.get("group_col", ""); target_col = d.get("target_col", "")
    rows = []
    for c in df.columns:
        miss = df[c].isna()
        rows.append({"column": c, "missing": int(miss.sum()),
                     "missing_pct": round(miss.mean() * 100, 2),
                     "complete": int((~miss).sum())})
    rows.sort(key=lambda r: r["missing_pct"], reverse=True)
    insights = []
    if group_col in df.columns:
        for c in df.columns:
            if c == group_col: continue
            rates = df.assign(_missing=df[c].isna()).groupby(group_col)["_missing"].mean().mul(100).sort_values(ascending=False).head(3)
            if len(rates) and rates.iloc[0] > 0:
                insights.append({"signal": f"Missing {c}", "value": str(rates.index[0]),
                                 "why_it_matters": round(float(rates.iloc[0]), 2)})
    if target_col in df.columns:
        target = to_numeric_clean(df[target_col])
        for c in df.columns:
            miss = df[c].isna()
            if miss.any() and (~miss).any():
                a, b = target[miss].dropna(), target[~miss].dropna()
                if len(a) and len(b):
                    insights.append({"signal": f"{target_col} when {c} is missing",
                                     "value": round(float(a.mean()), 4),
                                     "why_it_matters": round(float(a.mean() - b.mean()), 4)})
    echarts = bar_chart("Missing values by column",
                        [r["column"] for r in rows[:20]][::-1],
                        [{"name": "Missing %", "data": [r["missing_pct"] for r in rows[:20]][::-1], "color": PINK}],
                        y_label="Missing %", horizontal=True)
    return jsonify(with_meta({
        "title": "Missing Value Analysis",
        "rows": rows, "cols": ["column", "missing", "missing_pct", "complete"],
        "echarts": echarts,
        "schema_insights": insights[:30], "schema_insight_cols": ["signal", "value", "why_it_matters"],
        "code": "df.isna().sum().sort_values(ascending=False)",
    }, export_name="missing_analysis", total_scanned=len(df),
       result_rows=len(rows), displayed_rows=len(rows), full_export=True))

@app.route("/api/outliers", methods=["POST"])
def outliers():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    col = d.get("column", ""); method = d.get("method", "iqr")
    if col not in df.columns: return jsonify({"error": "Numeric column required."}), 400
    s = to_numeric_clean(df[col]); valid = s.dropna()
    if valid.empty: return jsonify({"error": "No numeric values."}), 400
    if method == "zscore":
        std = valid.std()
        z = (s - valid.mean()) / std if std else s * 0
        mask = z.abs() > 3
        lower, upper = (valid.mean() - 3 * std, valid.mean() + 3 * std) if std else (valid.mean(), valid.mean())
    elif method == "mad":
        med = valid.median(); mad = np.median(np.abs(valid - med))
        modified = 0.6745 * (s - med) / mad if mad else s * 0
        mask = modified.abs() > 3.5
        lower, upper = (med - 3.5 * mad / 0.6745, med + 3.5 * mad / 0.6745) if mad else (med, med)
    elif method == "percentile":
        lower, upper = valid.quantile(.01), valid.quantile(.99); mask = (s < lower) | (s > upper)
    else:
        q1, q3 = valid.quantile(.25), valid.quantile(.75); iqr = q3 - q1
        lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr; mask = (s < lower) | (s > upper)
    clean_mask = ~mask.fillna(False)
    out = df.loc[mask.fillna(False)].copy()
    impact_mean = float(valid.mean())
    clean_mean = float(s[clean_mask].dropna().mean()) if clean_mask.any() else None
    rows = json_safe_records(out.assign(_outlier_value=s.loc[out.index]).head(100))
    cols = ["_outlier_value"] + [c for c in df.columns if c in out.columns][:20]
    echarts = histogram_chart(f"Outlier thresholds - {col} ({method.upper()})", valid.values,
                              bins=30, x_label=col, mark_stats=False)
    _, edges = np.histogram(valid.values, bins=30)
    labels = echarts["xAxis"]["data"]
    for name, value, color in (("Lower", lower, PINK), ("Upper", upper, GREEN)):
        idx = int(np.clip(np.digitize(float(value), edges) - 1, 0, len(labels) - 1))
        echarts["markLines"].append({
            "name": f"{name} {float(value):.2f}", "xAxis": labels[idx], "color": color,
        })
    code_by_method = {
        "iqr": f'q1=df["{col}"].quantile(.25); q3=df["{col}"].quantile(.75)\n'
               f'iqr=q3-q1\ndf[(df["{col}"] < q1-1.5*iqr) | (df["{col}"] > q3+1.5*iqr)]',
        "zscore": f's=df["{col}"]; z=(s-s.mean())/s.std()\ndf[z.abs() > 3]',
        "mad": f's=df["{col}"]; med=s.median(); mad=(s-med).abs().median()\n'
               f'df[(0.6745*(s-med)/mad).abs() > 3.5]',
        "percentile": f's=df["{col}"]; lo,hi=s.quantile([.01,.99])\ndf[(s < lo) | (s > hi)]',
    }
    return jsonify(with_meta({
        "title": "Outlier Analysis", "column": col, "method": method,
        "lower_threshold": round(float(lower), 4), "upper_threshold": round(float(upper), 4),
        "outlier_count": int(mask.sum()), "outlier_pct": round(float(mask.mean() * 100), 2),
        "mean_with_outliers": round(impact_mean, 4),
        "mean_without_outliers": round(clean_mean, 4) if clean_mean is not None else None,
        "mean_impact": round(impact_mean - clean_mean, 4) if clean_mean is not None else None,
        "rows": rows, "cols": cols, "echarts": echarts,
        "code": code_by_method.get(method, code_by_method["iqr"]),
    }, export_name="outliers", total_scanned=len(df),
       result_rows=int(mask.sum()), displayed_rows=len(rows), full_export=True))

@app.route("/api/segment-studio", methods=["POST"])
def segment_studio():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    seg_col = d.get("segment_col", "")
    mode = d.get("segment_mode", "category")
    bins = int(d.get("bins", 5) or 5)
    metric = d.get("metric_col", "")
    seg, err = build_segment_series(df, seg_col, mode, bins)
    if err: return jsonify({"error": err}), 400

    work = df.copy()
    work["_segment"] = seg
    total = len(work)
    # Metric is optional — only compute metric stats when one was actually chosen.
    numeric_metric = bool(metric) and metric in work.columns and \
        not to_numeric_clean(work[metric]).dropna().empty
    # Missingness is scoped to the columns actually being analyzed (segment
    # column + metric, if any) rather than every column in the dataset —
    # otherwise a segment could look mostly "complete" or mostly "missing"
    # purely because of unrelated columns elsewhere in the table.
    relevant_cols = [c for c in ([seg_col, metric] if numeric_metric else [seg_col]) if c in df.columns]
    rows = []
    for name, g in work.groupby("_segment", dropna=False):
        cell_count = max(len(g), 1) * max(len(relevant_cols), 1)
        row = {
            "segment": str(name),
            "rows": int(len(g)),
            "share_pct": round(len(g) / total * 100, 2) if total else 0,
            "missing_cells_pct": round(g[relevant_cols].isna().sum().sum() / cell_count * 100, 2),
        }
        if numeric_metric:
            s = to_numeric_clean(g[metric]).dropna()
            row.update({
                "metric_mean": round(float(s.mean()), 4) if not s.empty else None,
                "metric_median": round(float(s.median()), 4) if not s.empty else None,
                "metric_min": round(float(s.min()), 4) if not s.empty else None,
                "metric_max": round(float(s.max()), 4) if not s.empty else None,
            })
        rows.append(row)
    rows.sort(key=lambda r: r["rows"], reverse=True)

    echarts = bar_chart(f"Segment Size — {seg_col}",
                         [r["segment"] for r in rows][::-1],
                         [{"name": "Rows", "data": [r["rows"] for r in rows][::-1], "color": ACCENT}],
                         y_label="Rows", horizontal=True)
    cols = ["segment", "rows", "share_pct", "missing_cells_pct"]
    if numeric_metric:
        cols += ["metric_mean", "metric_median", "metric_min", "metric_max"]
    code = f'df.groupby("{seg_col}").size()' if mode == "category" else f'pd.{"qcut" if mode == "quantile" else "cut"}(df["{seg_col}"], {bins})'
    return jsonify({"rows": rows, "cols": cols, "segment_col": seg_col, "mode": mode,
                    "metric_col": metric, "echarts": echarts, "code": code})

# ── Chart builder endpoint ─────────────────────────────────────────────────────

@app.route("/api/visual-studio", methods=["POST"])
def visual_studio():
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    payload, status = build_visual_chart(df, d)
    return jsonify(payload), status
    chart = d.get("chart_type", "auto")
    xcol = d.get("x_col", "")
    ycol = d.get("y_col", "")
    agg = d.get("agg", "mean")
    bins = max(5, min(int(d.get("bins", 30) or 30), 80))
    if chart == "auto":
        if xcol in df.columns and ycol in df.columns:
            chart = "scatter" if pd.api.types.is_numeric_dtype(df[xcol]) and pd.api.types.is_numeric_dtype(df[ycol]) else "bar"
        elif xcol in df.columns and pd.api.types.is_numeric_dtype(df[xcol]):
            chart = "histogram"
        else:
            chart = "topn"

    title = "Panda Visual"
    code = "# Visualization"
    echarts = None
    try:
        if chart == "histogram":
            if xcol not in df.columns: return jsonify({"error": "numeric column required."}), 400
            s = to_numeric_clean(df[xcol]).dropna()
            if s.empty: return jsonify({"error": "No numeric values."}), 400
            title = f"Distribution of {xcol}"
            echarts = histogram_chart(title, s, bins=bins, x_label=xcol)
            code = f'df["{xcol}"].hist(bins={bins})'
        elif chart == "topn":
            if xcol not in df.columns: return jsonify({"error": "category column required."}), 400
            vc = df[xcol].fillna("(Missing)").astype(str).value_counts()
            title = f"All values — {xcol}"
            echarts = bar_chart(title, vc.index[::-1].tolist(),
                                 [{"name": "Count", "data": vc.values[::-1].tolist(), "color": ACCENT}],
                                 y_label="Count", horizontal=True)
            code = f'df["{xcol}"].value_counts()'
        elif chart == "bar":
            if xcol not in df.columns or ycol not in df.columns:
                return jsonify({"error": "x and y columns required."}), 400
            s = to_numeric_clean(df[ycol])
            work = pd.DataFrame({"x": df[xcol].fillna("(Missing)").astype(str), "y": s}).dropna()
            if work.empty: return jsonify({"error": "No numeric values for y column."}), 400
            if agg == "variance":
                grouped = work.groupby("x")["y"].var()
            elif agg == "mode":
                grouped = work.groupby("x")["y"].agg(lambda v: v.mode().iloc[0] if not v.mode().empty else np.nan)
            else:
                grouped = getattr(work.groupby("x")["y"], agg)()
            grouped = grouped.sort_values(ascending=False)
            title = f"{agg.title()} of {ycol} by {xcol}"
            echarts = bar_chart(title, grouped.index[::-1].tolist(),
                                 [{"name": f"{agg}({ycol})", "data": grouped.values[::-1].round(4).tolist(), "color": ACCENT}],
                                 y_label=f"{agg}({ycol})", horizontal=True)
            code = f'df.groupby("{xcol}")["{ycol}"].{agg}().sort_values(ascending=False)'
        elif chart == "box":
            if xcol not in df.columns or ycol not in df.columns:
                return jsonify({"error": "category x and numeric y required."}), 400
            labels, groups = [], []
            for name, g in df.groupby(xcol, dropna=False):
                vals = to_numeric_clean(g[ycol]).dropna()
                if not vals.empty:
                    labels.append(str(name)); groups.append(vals.values)
            if not groups: return jsonify({"error": "No numeric values for y column."}), 400
            title = f"{ycol} distribution by {xcol}"
            echarts = boxplot_chart(title, labels, groups, y_label=ycol)
            code = f'sns.boxplot(data=df, x="{xcol}", y="{ycol}")'
        elif chart == "scatter":
            if xcol not in df.columns or ycol not in df.columns:
                return jsonify({"error": "two numeric columns required."}), 400
            x = to_numeric_clean(df[xcol]); y = to_numeric_clean(df[ycol])
            m = x.notna() & y.notna()
            title = f"{xcol} vs {ycol}"
            echarts = scatter_chart(title, x[m], y[m], x_label=xcol, y_label=ycol, trend=m.sum() > 1)
            code = f'df.plot.scatter("{xcol}", "{ycol}")'
        elif chart == "heatmap":
            num = df.select_dtypes(include="number")
            if num.shape[1] < 2: return jsonify({"error": "Need at least 2 numeric columns."}), 400
            corr = num.corr().round(3)
            title = "Correlation Heatmap"
            echarts = heatmap_chart(title, corr, min_v=-1, max_v=1, diverging=True)
            code = "df.corr(numeric_only=True)"
        else:
            return jsonify({"error": "Unknown chart type."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"chart_type": chart, "title": title, "echarts": echarts, "code": code})

# ── Shared chart calculation engine ────────────────────────────────────────────

def build_visual_chart(df, d):
    """Build an ECharts JSON recipe from a selected DataFrame and chart request.

    Aggregation and chart data are calculated with pandas here; the browser only
    renders the returned recipe, which keeps exported and displayed values aligned.
    """
    chart = d.get("chart_type", "auto")
    xcol = d.get("x_col", "")
    ycol = d.get("y_col", "")
    series_col = d.get("series_col", "")
    agg = d.get("agg", "mean")
    bins = max(5, min(int(d.get("bins", 30) or 30), 80))
    if chart == "auto":
        if xcol in df.columns and ycol in df.columns:
            chart = "scatter" if pd.api.types.is_numeric_dtype(df[xcol]) and pd.api.types.is_numeric_dtype(df[ycol]) else "bar"
        elif xcol in df.columns and pd.api.types.is_numeric_dtype(df[xcol]):
            chart = "histogram"
        else:
            chart = "topn"

    title = "Panda Visual"
    code = "# Visualization"
    echarts = None
    try:
        if chart == "histogram":
            if xcol not in df.columns: return {"error": "numeric column required."}, 400
            s = to_numeric_clean(df[xcol]).dropna()
            if s.empty: return {"error": "No numeric values."}, 400
            title = f"Distribution of {xcol}"
            echarts = histogram_chart(title, s, bins=bins, x_label=xcol)
            code = f'df["{xcol}"].hist(bins={bins})'
        elif chart == "topn":
            if xcol not in df.columns: return {"error": "category column required."}, 400
            vc = df[xcol].fillna("(Missing)").astype(str).value_counts()
            title = f"All values - {xcol}"
            echarts = bar_chart(title, vc.index[::-1].tolist(),
                                 [{"name": "Count", "data": vc.values[::-1].tolist(), "color": ACCENT}],
                                 y_label="Count", horizontal=True)
            code = f'df["{xcol}"].value_counts()'
        elif chart in ("bar", "horizontal-bar", "line", "area", "combo", "pie", "donut", "treemap"):
            if xcol not in df.columns or ycol not in df.columns:
                return {"error": "x and y columns required."}, 400
            work = pd.DataFrame({"x": df[xcol].fillna("(Missing)").astype(str), "y": to_numeric_clean(df[ycol])}).dropna()
            if work.empty: return {"error": "No numeric values for y column."}, 400
            if agg == "variance": grouped = work.groupby("x")["y"].var()
            elif agg == "mode": grouped = work.groupby("x")["y"].agg(lambda v: v.mode().iloc[0] if not v.mode().empty else np.nan)
            else: grouped = getattr(work.groupby("x")["y"], agg)()
            grouped = grouped.dropna()
            grouped = grouped.sort_index() if chart in ("line", "area", "combo") else grouped.sort_values(ascending=False)
            title = f"{agg.title()} of {ycol} by {xcol}"
            cats = grouped.index.astype(str).tolist()
            vals = grouped.values.round(4).tolist()
            if chart == "line":
                echarts = line_chart(title, cats, [{"name": f"{agg}({ycol})", "type": "line", "data": vals, "color": ACCENT}], x_label=xcol, y_label=f"{agg}({ycol})")
                code = f'df.groupby("{xcol}")["{ycol}"].{agg}().plot.line()'
            elif chart == "area":
                echarts = line_chart(title, cats, [{"name": f"{agg}({ycol})", "type": "line", "data": vals, "color": ACCENT, "areaStyle": {}}], x_label=xcol, y_label=f"{agg}({ycol})")
                code = f'df.groupby("{xcol}")["{ycol}"].{agg}().plot.area()'
            elif chart == "combo":
                echarts = bar_chart(title, cats, [
                    {"name": f"{agg}({ycol})", "type": "bar", "data": vals, "color": ACCENT},
                    {"name": "Trend", "type": "line", "data": vals, "color": PINK},
                ], x_label=xcol, y_label=f"{agg}({ycol})")
                code = f'g=df.groupby("{xcol}")["{ycol}"].{agg}(); g.plot.bar(); g.plot.line()'
            elif chart == "pie":
                part_cats, part_vals = nonnegative_part_values(cats, vals)
                if not part_vals or sum(part_vals) <= 0:
                    return {"error": "Pie charts require a positive non-negative total."}, 400
                echarts = pie_chart(title, part_cats[:30], part_vals[:30], donut=False)
                code = f'df.groupby("{xcol}")["{ycol}"].{agg}().plot.pie()'
            elif chart == "donut":
                part_cats, part_vals = nonnegative_part_values(cats, vals)
                if not part_vals or sum(part_vals) <= 0:
                    return {"error": "Donut charts require a positive non-negative total."}, 400
                echarts = pie_chart(title, part_cats[:30], part_vals[:30], donut=True)
                code = f'df.groupby("{xcol}")["{ycol}"].{agg}().plot.pie()'
            elif chart == "treemap":
                part_cats, part_vals = nonnegative_part_values(cats, vals)
                if not part_vals or sum(part_vals) <= 0:
                    return {"error": "Treemaps require a positive non-negative total."}, 400
                echarts = treemap_chart(title, part_cats[:80], part_vals[:80])
                code = f'df.groupby("{xcol}")["{ycol}"].{agg}()  # treemap size'
            else:
                echarts = bar_chart(title, cats[::-1],
                                     [{"name": f"{agg}({ycol})", "data": vals[::-1], "color": ACCENT}],
                                     y_label=f"{agg}({ycol})", horizontal=(chart == "horizontal-bar"))
                code = f'df.groupby("{xcol}")["{ycol}"].{agg}().sort_values(ascending=False)'
        elif chart == "stacked-bar":
            if xcol not in df.columns or ycol not in df.columns or series_col not in df.columns:
                return {"error": "x, y, and series columns required."}, 400
            work = pd.DataFrame({
                "x": df[xcol].fillna("(Missing)").astype(str),
                "series": df[series_col].fillna("(Missing)").astype(str),
                "y": to_numeric_clean(df[ycol]),
            }).dropna()
            if work.empty: return {"error": "No numeric values for y column."}, 400
            if agg == "variance": pivot = work.pivot_table(index="x", columns="series", values="y", aggfunc="var")
            elif agg == "mode": pivot = work.pivot_table(index="x", columns="series", values="y", aggfunc=lambda v: v.mode().iloc[0] if not v.mode().empty else np.nan)
            else: pivot = work.pivot_table(index="x", columns="series", values="y", aggfunc=agg)
            pivot = pivot.fillna(0)
            top_series = pivot.sum().sort_values(ascending=False).head(8).index.tolist()
            pivot = pivot[top_series].head(40)
            title = f"{agg.title()} of {ycol} by {xcol} and {series_col}"
            series = [{"name": str(c), "type": "bar", "stack": "total", "data": pivot[c].round(4).tolist()} for c in pivot.columns]
            echarts = bar_chart(title, pivot.index.astype(str).tolist(), series, x_label=xcol, y_label=f"{agg}({ycol})")
            code = f'df.pivot_table(index="{xcol}", columns="{series_col}", values="{ycol}", aggfunc="{agg}")'
        elif chart == "box":
            if xcol not in df.columns or ycol not in df.columns:
                return {"error": "category x and numeric y required."}, 400
            labels, groups = [], []
            for name, g in df.groupby(xcol, dropna=False):
                vals = to_numeric_clean(g[ycol]).dropna()
                if not vals.empty:
                    labels.append(str(name)); groups.append(vals.values)
            if not groups: return {"error": "No numeric values for y column."}, 400
            title = f"{ycol} distribution by {xcol}"
            echarts = boxplot_chart(title, labels, groups, y_label=ycol)
            code = f'sns.boxplot(data=df, x="{xcol}", y="{ycol}")'
        elif chart == "scatter":
            if xcol not in df.columns or ycol not in df.columns:
                return {"error": "two numeric columns required."}, 400
            x = to_numeric_clean(df[xcol]); y = to_numeric_clean(df[ycol])
            m = x.notna() & y.notna()
            title = f"{xcol} vs {ycol}"
            echarts = scatter_chart(title, x[m], y[m], x_label=xcol, y_label=ycol, trend=m.sum() > 1)
            code = f'df.plot.scatter("{xcol}", "{ycol}")'
        elif chart == "heatmap":
            num = numeric_like_frame(df)
            if num.shape[1] < 2: return {"error": "Need at least 2 numeric columns."}, 400
            corr = num.corr().round(3)
            title = "Correlation Heatmap"
            echarts = heatmap_chart(title, corr, min_v=-1, max_v=1, diverging=True)
            code = "df.corr(numeric_only=True)"
        else:
            return {"error": "Unknown chart type."}, 400
    except Exception as e:
        return {"error": str(e)}, 400
    return {"type": "chart", "chart_type": chart, "title": title, "echarts": echarts, "code": code}, 200

@app.route("/api/export-excel", methods=["POST"])
def export_excel():
    d = request.get_json(silent=True) or {}
    rows = d.get("rows", []); cols = d.get("cols", []); fname = d.get("filename", "panda_export")
    if not rows: return jsonify({"error": "No data to export."}), 400
    result_df = pd.DataFrame(rows)
    if cols: result_df = result_df[[c for c in cols if c in result_df.columns]]
    buf = io.BytesIO(); result_df.to_excel(buf, index=False, engine="openpyxl"); buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"{fname}.xlsx")

# ── Result and selected-dataset exports ────────────────────────────────────────

@app.route("/api/export-filtered", methods=["POST"])
def export_filtered():
    """Re-applies all slice filters server-side and exports ALL matched rows."""
    df, err = load_df_from_storage()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}
    fmt = d.get("format", "csv")
    fname = d.get("filename", "panda_export")
    # Apply column selection + simple filter from selection bar
    df = apply_selection(df, d)
    # Apply stacked slice filters
    filters = d.get("filters", [])
    for f in filters:
        col = f.get("col"); op = f.get("op"); val = f.get("val")
        if col not in df.columns:
            continue
        try:
            s = df[col]
            if op == "eq":
                df = df[_eq_mask(s, val)]
            elif op == "ne":
                df = df[_eq_mask(s, val, negate=True)]
            elif op == "contains":
                df = df[s.astype(str).str.contains(str(val), case=False, na=False)]
            elif op == "isin":
                vals = [v.strip() for v in str(val).split(",")]
                df = df[_isin_mask(s, vals)]
            elif op in ("gt", "lt", "gte", "lte"):
                ops_map = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
                fv = float(val); num = to_numeric_clean(s)
                df = df[eval(f"num{ops_map[op]}fv")]
        except Exception:
            continue
    if df.empty:
        return jsonify({"error": "No rows match the applied filters."}), 400
    buf = io.BytesIO()
    if fmt == "xlsx":
        df.to_excel(buf, index=False, engine="openpyxl")
        buf.seek(0)
        return send_file(buf,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"{fname}.xlsx")
    else:
        df.to_csv(buf, index=False)
        buf.seek(0)
        return send_file(buf, mimetype="text/csv",
                         as_attachment=True, download_name=f"{fname}.csv")

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
