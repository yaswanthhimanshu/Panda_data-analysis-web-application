"""
app.py — Panda Backend
No-code Pandas analysis engine.
All routes, all analysis operations, upload handling in one file.
"""

import os, io, uuid, base64, warnings, secrets, time
import chardet
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from werkzeug.utils import secure_filename
from flask import (Flask, request, session, jsonify,
                   send_from_directory, redirect, send_file)

warnings.filterwarnings("ignore")

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder=".")
app.config.update(
    SECRET_KEY         = os.environ.get("SECRET_KEY", secrets.token_hex(32)),
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024,
)

# ── In-memory store ────────────────────────────────────────────────────────
# { session_token → {"df": DataFrame, "filename": str, "encoding": str, "last_access": float} }
# No files written to disk. Every user's data is completely isolated.
_STORE: dict = {}
SESSION_TTL_SECONDS = 2 * 60 * 60

ALLOWED = {"csv", "xlsx", "xls"}

# ── Chart style ────────────────────────────────────────────────────────────────

CHART_RC = {
    "figure.facecolor": "#0e0e14", "axes.facecolor": "#16161f",
    "axes.edgecolor": "#2e2e3e",   "axes.labelcolor": "#c8c8d8",
    "xtick.color": "#8888a8",      "ytick.color": "#8888a8",
    "text.color": "#c8c8d8",       "grid.color": "#2e2e3e",
    "grid.linestyle": "--",        "grid.alpha": 0.5,
}
ACCENT = "#7c6af7"; PINK = "#e05c8a"; GREEN = "#3ec9a7"; AMBER = "#f5c842"
PALETTE = [ACCENT, PINK, GREEN, AMBER, "#a78bfa", "#34d399", "#f87171", "#fbbf24"]

def fig_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=130, facecolor=fig.get_facecolor())
    buf.seek(0); b64 = base64.b64encode(buf.read()).decode(); plt.close(fig); return b64

def apply_rc(): plt.rcParams.update(CHART_RC)

import json as _json

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
    # Second pass: json round-trip catches any nan that slipped through
    return _json.loads(
        _json.dumps(records, allow_nan=False,
                    default=lambda v: None)   # safety net for any other non-serialisable
    )

# ── Aggregation map ────────────────────────────────────────────────────────────
# Maps our display names → pandas agg function strings
NATIVE_AGGS = {
    "mean": "mean", "sum": "sum", "count": "count",
    "min": "min",   "max": "max", "median": "median",
    "std": "std",   "variance": "var",
}
ALL_CALCS = list(NATIVE_AGGS.keys()) + ["mode"]

# ── Upload helpers ─────────────────────────────────────────────────────────────

def allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED

def detect_encoding_bytes(raw: bytes) -> str:
    r = chardet.detect(raw[:100_000])
    enc = r.get("encoding") or "utf-8"
    return enc if (r.get("confidence") or 0) >= 0.6 else "utf-8"

def _sid():
    """Get or create a unique private token for this browser session."""
    if "_sid" not in session:
        session["_sid"] = secrets.token_hex(16)
    return session["_sid"]

def cleanup_store():
    """Remove inactive in-memory datasets so RAM does not grow forever."""
    now = time.time()
    expired = [
        key for key, data in _STORE.items()
        if now - data.get("last_access", now) > SESSION_TTL_SECONDS
    ]
    for key in expired:
        _STORE.pop(key, None)

def load_df():
    """Retrieve this user's DataFrame — completely isolated from other users."""
    cleanup_store()
    key = session.get("_sid")
    if not key or key not in _STORE:
        return None, "No dataset loaded."
    _STORE[key]["last_access"] = time.time()
    return _STORE[key]["df"], None

def apply_selection(df, params):
    cols = params.get("columns")
    if cols and cols != "all" and isinstance(cols, list):
        valid = [c for c in cols if c in df.columns]
        if valid: df = df[valid]
    fcol = params.get("filter_col"); fop = params.get("filter_op"); fval = params.get("filter_val")
    if fcol and fop and fval is not None and fcol in df.columns:
        s = df[fcol]
        try:
            if fop == "eq":         df = df[s.astype(str) == str(fval)]
            elif fop == "ne":       df = df[s.astype(str) != str(fval)]
            elif fop == "contains": df = df[s.astype(str).str.contains(str(fval), case=False, na=False)]
            elif fop == "isin":     df = df[s.astype(str).isin([v.strip() for v in str(fval).split(",")])]
            else:
                n = float(fval); num = pd.to_numeric(s, errors="coerce")
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

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index(): return send_from_directory(".", "index.html")

@app.route("/clear")
def clear():
    key = session.get("_sid")
    if key: _STORE.pop(key, None)   # remove ONLY this user's DataFrame
    session.clear()
    return redirect("/")

# ── Upload ─────────────────────────────────────────────────────────────────────

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or f.filename == "": return jsonify({"error": "No file."}), 400
    if not allowed(f.filename): return jsonify({"error": "Only CSV / XLSX / XLS supported."}), 400
    filename = secure_filename(f.filename); ext = filename.rsplit(".", 1)[1].lower()
    raw = f.read()                              # read entirely into memory — no disk write
    encoding = "utf-8"
    try:
        if ext == "csv":
            encoding = detect_encoding_bytes(raw)
            try:    df = pd.read_csv(io.BytesIO(raw), encoding=encoding)
            except: df = pd.read_csv(io.BytesIO(raw), encoding="latin-1"); encoding = "latin-1"
        else:
            df = pd.read_excel(io.BytesIO(raw))
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400
    cleanup_store()
    _STORE[_sid()] = {"df": df, "filename": filename, "encoding": encoding, "last_access": time.time()}
    return jsonify({"ok": True, "filename": filename, "encoding": encoding})

# ── Column metadata ────────────────────────────────────────────────────────────

@app.route("/api/columns")
def columns():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
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
    return jsonify({
        "all":                df.columns.tolist(),
        "numeric":            numeric_cols,
        "categorical":        categorical_cols,
        "recommended_gb":     recommended_group,   # kept for backwards-compat
        "recommended_group":  recommended_group,
        "recommended_metric": recommended_metric,
        "other_cols":         other,
    })

# ── 1. Overview ────────────────────────────────────────────────────────────────

@app.route("/api/overview")
def overview():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    total = df.shape[0] * df.shape[1]; miss = int(df.isnull().sum().sum())
    cols = [{"column": c, "dtype": str(df[c].dtype),
             "missing": int(df[c].isnull().sum()),
             "missing_pct": round(df[c].isnull().sum() / len(df) * 100, 2) if len(df) else 0,
             "unique": int(df[c].nunique())} for c in df.columns]
    return jsonify({
        "rows": df.shape[0], "columns": df.shape[1], "missing_total": miss,
        "missing_pct": round(miss / total * 100, 2) if total else 0,
        "memory_kb": round(df.memory_usage(deep=True).sum() / 1024, 2),
        "numeric_cols": int(df.select_dtypes(include="number").shape[1]),
        "object_cols":  int(df.select_dtypes(include="object").shape[1]),
        "col_info": cols,
        "code": "df.shape\ndf.info()\ndf.describe(include='all')"
    })

# ── 2. Preview ─────────────────────────────────────────────────────────────────

@app.route("/api/preview", methods=["POST"])
def preview():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    mode = d.get("mode", "head"); n = int(d.get("n", 10))
    if mode == "tail":     res = df.tail(n)
    elif mode == "sample": res = df.sample(min(n, len(df)), random_state=42)
    else:                  res = df.head(n)
    return jsonify({"rows": json_safe_records(res), "cols": res.columns.tolist(),
                    "total": len(df), "code": f"df.{mode}({n})"})

# ── 3. Statistical Summary ─────────────────────────────────────────────────────

@app.route("/api/summary", methods=["POST"])
def summary():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    num = df.select_dtypes(include="number")
    if num.empty: return jsonify({"error": "No numeric columns."}), 400
    desc = num.describe().round(4)
    rows = [{k: desc.loc[k, c] for k in desc.index} | {"column": c} for c in desc.columns]
    return jsonify({"rows": rows, "code": "df.describe()"})

# ── 4. Central Tendency ────────────────────────────────────────────────────────

@app.route("/api/central-tendency", methods=["POST"])
def central_tendency():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    num = df.select_dtypes(include="number")
    if d.get("columns"): num = num[[c for c in d["columns"] if c in num.columns]]
    if num.empty: return jsonify({"error": "No numeric columns."}), 400
    rows = []
    for c in num.columns:
        s = num[c].dropna(); m = s.mode()
        rows.append({"column": c, "mean": round(float(s.mean()), 4),
                     "median": round(float(s.median()), 4),
                     "mode": round(float(m.iloc[0]), 4) if not m.empty else None})
    return jsonify({"rows": rows, "code": "df[cols].mean()\ndf[cols].median()\ndf[cols].mode()"})

# ── 5. Value Counts ────────────────────────────────────────────────────────────

@app.route("/api/value-counts", methods=["POST"])
def value_counts():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    col = d.get("column"); top_n = int(d.get("top_n", 20))
    if not col or col not in df.columns: return jsonify({"error": "column required."}), 400
    vc = df[col].value_counts().head(top_n)
    pct = df[col].value_counts(normalize=True).head(top_n).mul(100).round(2)
    rows = [{"value": str(k), "count": int(v), "pct": float(pct[k])} for k, v in vc.items()]
    apply_rc()
    fig, ax = plt.subplots(figsize=(8, max(3, len(rows) * 0.32)))
    ax.barh([r["value"] for r in rows][::-1], [r["count"] for r in rows][::-1], color=ACCENT, alpha=0.85)
    ax.set_xlabel("Count"); ax.set_title(f"Value Counts — {col}", color="#c8c8d8"); fig.tight_layout()
    return jsonify({"rows": rows, "chart": fig_to_b64(fig), "column": col,
                    "code": f'df["{col}"].value_counts().head({top_n})'})

# ── 6. Frequency Analysis ──────────────────────────────────────────────────────

@app.route("/api/frequency", methods=["POST"])
def frequency():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    col = d.get("column"); top_n = int(d.get("top_n", 20))
    if not col or col not in df.columns: return jsonify({"error": "column required."}), 400
    vc = df[col].value_counts().head(top_n)
    pct = df[col].value_counts(normalize=True).head(top_n).mul(100).round(2)
    rows = [{"value": str(k), "count": int(v), "pct": float(pct[k]), "cum_pct": 0} for k, v in vc.items()]
    cum = 0
    for r in rows: cum += r["pct"]; r["cum_pct"] = round(cum, 2)
    apply_rc()
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, max(3.5, len(rows) * 0.32)))
    ax1.barh([r["value"] for r in rows][::-1], [r["count"] for r in rows][::-1], color=ACCENT, alpha=0.85)
    ax1.set_title("Count", color="#c8c8d8")
    ax2.barh([r["value"] for r in rows][::-1], [r["pct"] for r in rows][::-1], color=PINK, alpha=0.85)
    ax2.set_xlabel("%"); ax2.set_title("Percentage", color="#c8c8d8")
    fig.suptitle(f"Frequency — {col}", color="#c8c8d8"); fig.tight_layout()
    return jsonify({"column": col, "rows": rows, "chart": fig_to_b64(fig),
                    "code": f'df["{col}"].value_counts()\ndf["{col}"].value_counts(normalize=True)'})

# ── 7. Distribution ────────────────────────────────────────────────────────────

@app.route("/api/distribution", methods=["POST"])
def distribution():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    col = d.get("column")
    if not col or col not in df.columns: return jsonify({"error": "column required."}), 400
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    if s.empty: return jsonify({"error": "No numeric values."}), 400
    sk = float(s.skew()); kt = float(s.kurtosis()); mean_v = float(s.mean()); med_v = float(s.median())
    mode_v = float(s.mode().iloc[0]) if not s.mode().empty else None
    sk_lbl = "Right Skewed" if sk > 0.5 else "Left Skewed" if sk < -0.5 else "Nearly Symmetric"
    kt_lbl = "Heavy Tails" if kt > 1 else "Flat Distribution" if kt < -1 else "Normal-like Tails"
    apply_rc()
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(s, bins=30, color=ACCENT, alpha=0.75, edgecolor="#0e0e14")
    ax.axvline(mean_v, color=PINK, linestyle="--", lw=1.5, label=f"Mean {mean_v:.2f}")
    ax.axvline(med_v, color=GREEN, linestyle="--", lw=1.5, label=f"Median {med_v:.2f}")
    if mode_v is not None: ax.axvline(mode_v, color=AMBER, linestyle="--", lw=1.5, label=f"Mode {mode_v:.2f}")
    ax.legend(fontsize=9); ax.set_title(f"Distribution — {col}", color="#c8c8d8"); fig.tight_layout()
    return jsonify({"column": col, "mean": round(mean_v, 4), "median": round(med_v, 4),
                    "mode": round(mode_v, 4) if mode_v else None, "std": round(float(s.std()), 4),
                    "skewness": round(sk, 4), "skew_label": sk_lbl,
                    "kurtosis": round(kt, 4), "kurt_label": kt_lbl, "chart": fig_to_b64(fig),
                    "code": f'df["{col}"].hist(bins=30)\ndf["{col}"].skew()  # {round(sk,4)}\ndf["{col}"].kurtosis()  # {round(kt,4)}'})

# ── 8. Group Analysis Studio ───────────────────────────────────────────────────
# Unified endpoint replacing groupby, segment, multi-segment, agg-studio.
# Supports N group columns × M calculations.

@app.route("/api/group-analysis", methods=["POST"])
def group_analysis():
    df, err = load_df()
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

    has_mode    = "mode" in calcs
    native_calcs = [c for c in calcs if c in NATIVE_AGGS]

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

        # Sort by first calc if single group (most useful ordering)
        if len(group_cols) == 1 and calc_cols:
            result = result.sort_values(calc_cols[0], ascending=False)

    except Exception as e:
        return jsonify({"error": str(e)}), 400

    # ── Chart ─────────────────────────────────────────────────────────────────
    # Human-readable label for a calc column (avoids "count of tenure" phrasing)
    def calc_label(c):
        if c == "record_count": return "Record Count"
        return c.replace("_", " ").title()

    def metric_title(c, m):
        """Chart title: "Record Count by X"  or  "Mean of tenure by X"."""
        if c == "record_count": return "Record Count"
        return f"{calc_label(c)} of {m}"

    chart = None
    try:
        apply_rc()
        fig = None

        if len(group_cols) == 1:
            g = group_cols[0]
            if len(calc_cols) == 1:
                # Horizontal bar — sorted, colored
                fig, ax = plt.subplots(figsize=(8, max(4, len(result) * 0.35)))
                colors = [PALETTE[i % len(PALETTE)] for i in range(len(result))]
                ax.barh(result[g].astype(str).tolist()[::-1],
                        result[calc_cols[0]].tolist()[::-1],
                        color=colors[::-1], alpha=0.85)
                ax.set_xlabel(calc_label(calc_cols[0]))
                ax.set_title(f"{metric_title(calc_cols[0], metric)} by {g}", color="#c8c8d8")
            else:
                # Grouped bar — one group of bars per group value
                n = len(calc_cols); x = np.arange(len(result)); w = 0.8 / n
                fig, ax = plt.subplots(figsize=(max(8, len(result) * 0.8 + 2), 5))
                for i, c in enumerate(calc_cols):
                    if c in result.columns:
                        ax.bar(x + i * w - (n - 1) * w / 2,
                               result[c], width=w, label=calc_label(c),
                               color=PALETTE[i % len(PALETTE)], alpha=0.85)
                ax.set_xticks(x)
                ax.set_xticklabels(result[g].astype(str), rotation=30, ha="right")
                ax.legend(fontsize=9)
                pretty_calcs = ", ".join(calc_label(c) for c in calc_cols)
                ax.set_title(f"{metric} — {pretty_calcs} by {g}", color="#c8c8d8")

        elif len(group_cols) == 2 and calc_cols:
            # Heatmap of first calculation across the two group dimensions
            try:
                pv = result.pivot_table(index=group_cols[1], columns=group_cols[0],
                                         values=calc_cols[0], fill_value=0)
                fig, ax = plt.subplots(figsize=(max(6, len(pv.columns) + 2),
                                                 max(4, len(pv) * 0.7 + 2)))
                cmap = sns.light_palette(ACCENT, as_cmap=True)
                sns.heatmap(pv, annot=True, fmt=".1f", cmap=cmap, linewidths=0.5,
                            linecolor="#0e0e14", annot_kws={"size": 9}, ax=ax,
                            cbar_kws={"shrink": 0.8})
                ax.set_title(
                    f"{metric_title(calc_cols[0], metric)}  ({group_cols[0]} × {group_cols[1]})",
                    color="#c8c8d8")
            except Exception:
                fig = None

        # 3+ groups: no chart, table is the answer
        if fig:
            fig.tight_layout()
            chart = fig_to_b64(fig)

    except Exception:
        chart = None

    # ── Pandas code ───────────────────────────────────────────────────────────
    # Map display names back to real pandas agg names for the code snippet
    _code_calcs = ["count" if c == "record_count" else c for c in calcs]
    gc = f'"{group_cols[0]}"' if len(group_cols) == 1 else str(group_cols)
    if len(_code_calcs) == 1:
        code = f'df.groupby({gc})["{metric}"].{_code_calcs[0]}()'
    else:
        code = f'df.groupby({gc})["{metric}"].agg({_code_calcs})'

    return jsonify({
        "rows":         json_safe_records(result),
        "cols":         result.columns.tolist(),
        "chart":        chart,
        "group_cols":   group_cols,
        "metric_col":   metric,
        "calculations": calcs,
        "calc_cols":    calc_cols,
        "code":         code,
    })

# ── 9. Pivot Table ─────────────────────────────────────────────────────────────

@app.route("/api/pivot", methods=["POST"])
def pivot():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    idx = d.get("index", ""); cols = d.get("columns", "")
    vals = d.get("values", ""); agg = d.get("agg", "mean")
    try:
        pt = pd.pivot_table(df, index=idx, columns=cols, values=vals,
                            aggfunc=agg, fill_value=0)
        return jsonify({"html": pt.round(4).to_html(classes="panda-table", border=0),
                        "code": f'pd.pivot_table(df, index="{idx}", columns="{cols}", values="{vals}", aggfunc="{agg}")'})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ── 10. Crosstab ───────────────────────────────────────────────────────────────

@app.route("/api/crosstab", methods=["POST"])
def crosstab():
    df, err = load_df()
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
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    num = df.select_dtypes(include="number")
    if num.shape[1] < 2: return jsonify({"error": "Need ≥2 numeric columns."}), 400
    method = d.get("method", "pearson"); corr = num.corr(method=method).round(4)
    apply_rc()
    fig, ax = plt.subplots(figsize=(max(6, len(corr) * 0.9), max(5, len(corr) * 0.8)))
    cmap = sns.diverging_palette(260, 340, s=80, l=55, n=256, as_cmap=True)
    mask = np.triu(np.ones_like(corr, dtype=bool))
    sns.heatmap(corr, mask=mask, annot=True, fmt=".2f", cmap=cmap, linewidths=0.5,
                linecolor="#0e0e14", annot_kws={"size": 9}, ax=ax, vmin=-1, vmax=1,
                cbar_kws={"shrink": 0.8})
    ax.set_title(f"Correlation Matrix ({method.title()})", color="#c8c8d8"); fig.tight_layout()
    cols = corr.columns.tolist()
    pairs = [{"col1": cols[i], "col2": cols[j], "corr": round(corr.iloc[i, j], 4)}
             for i in range(len(cols)) for j in range(i + 1, len(cols))]
    pairs.sort(key=lambda x: abs(x["corr"]), reverse=True)
    return jsonify({"chart": fig_to_b64(fig), "method": method,
                    "top_positive": [p for p in pairs if p["corr"] > 0][:5],
                    "top_negative": [p for p in pairs if p["corr"] < 0][:5],
                    "code": f'df.corr(method="{method}")'})

# ── 12. Ranking ────────────────────────────────────────────────────────────────

@app.route("/api/ranking", methods=["POST"])
def ranking():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    col = d.get("column"); n = int(d.get("n", 10))
    if not col or col not in df.columns: return jsonify({"error": "column required."}), 400
    return jsonify({"column": col, "n": n,
                    "largest":  json_safe_records(df.nlargest(n, col)),
                    "smallest": json_safe_records(df.nsmallest(n, col)),
                    "cols": df.columns.tolist(),
                    "code": f'df.nlargest({n}, "{col}")\ndf.nsmallest({n}, "{col}")'})

# ── 13. Std / Variance ─────────────────────────────────────────────────────────

@app.route("/api/std-variance", methods=["POST"])
def std_variance():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    num = df.select_dtypes(include="number")
    if d.get("columns"): num = num[[c for c in d["columns"] if c in num.columns]]
    if num.empty: return jsonify({"error": "No numeric columns."}), 400
    rows = []
    for c in num.columns:
        s = num[c].dropna(); std = float(s.std()); var = float(s.var()); mean = float(s.mean())
        cv = (std / mean * 100) if mean != 0 else None
        rows.append({"column": c, "mean": round(mean, 4), "std": round(std, 4),
                     "variance": round(var, 4), "cv_pct": round(cv, 2) if cv else None,
                     "spread": "Low" if cv and cv < 15 else "High" if cv and cv > 35 else "Medium"})
    return jsonify({"rows": rows, "code": "df[cols].std()\ndf[cols].var()"})

# ── 14. Covariance ─────────────────────────────────────────────────────────────

@app.route("/api/covariance", methods=["POST"])
def covariance():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    num = df.select_dtypes(include="number")
    if num.shape[1] < 2: return jsonify({"error": "Need ≥2 numeric columns."}), 400
    cov = num.cov().round(4); cols = cov.columns.tolist()
    pairs = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = round(cov.iloc[i, j], 4)
            pairs.append({"col1": cols[i], "col2": cols[j], "covariance": v,
                          "direction": "Positive" if v > 0 else "Negative" if v < 0 else "Near Zero"})
    pairs.sort(key=lambda x: abs(x["covariance"]), reverse=True)
    return jsonify({"html": cov.to_html(classes="panda-table", border=0),
                    "pairs": pairs[:20], "code": "df.cov()"})

# ── 15. Skewness & Kurtosis ────────────────────────────────────────────────────

@app.route("/api/skewness-kurtosis", methods=["POST"])
def skewness_kurtosis():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    num = df.select_dtypes(include="number")
    if d.get("columns"): num = num[[c for c in d["columns"] if c in num.columns]]
    if num.empty: return jsonify({"error": "No numeric columns."}), 400
    rows = []
    for c in num.columns:
        s = num[c].dropna(); sk = float(s.skew()); kt = float(s.kurtosis())
        rows.append({"column": c, "skewness": round(sk, 4),
                     "skew_label": "Right Skewed" if sk > 0.5 else "Left Skewed" if sk < -0.5 else "Nearly Symmetric",
                     "kurtosis": round(kt, 4),
                     "kurt_label": "Heavy Tails" if kt > 1 else "Flat Distribution" if kt < -1 else "Normal-like Tails"})
    return jsonify({"rows": rows, "code": "df[cols].skew()\ndf[cols].kurtosis()"})

# ── 16. Quantile Analysis ──────────────────────────────────────────────────────

@app.route("/api/quantile", methods=["POST"])
def quantile():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    col = d.get("column")
    if not col or col not in df.columns: return jsonify({"error": "column required."}), 400
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    if s.empty: return jsonify({"error": "No numeric values."}), 400
    qpts = [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99]
    rows = [{"percentile": f"P{int(q * 100)}", "value": round(float(s.quantile(q)), 4)} for q in qpts]
    Q1 = s.quantile(0.25); Q3 = s.quantile(0.75); IQR = Q3 - Q1
    apply_rc()
    fig, ax = plt.subplots(figsize=(8, 4))
    labels = [r["percentile"] for r in rows]; vals = [r["value"] for r in rows]
    colors = [PINK if l in ["P25", "P50", "P75"] else ACCENT for l in labels]
    ax.bar(labels, vals, color=colors, alpha=0.85)
    ax.set_title(f"Quantile Distribution — {col}", color="#c8c8d8"); fig.tight_layout()
    return jsonify({"column": col, "rows": rows, "q1": round(float(Q1), 4),
                    "q3": round(float(Q3), 4), "iqr": round(float(IQR), 4),
                    "median": round(float(s.median()), 4), "chart": fig_to_b64(fig),
                    "code": f'df["{col}"].quantile([0.01,0.05,0.25,0.50,0.75,0.95,0.99])'})

# ── 17. Column Profiler ────────────────────────────────────────────────────────

@app.route("/api/column-profiler", methods=["POST"])
def column_profiler():
    df, err = load_df()
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
        apply_rc()
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 3.5))
        ax1.hist(sn, bins=25, color=ACCENT, alpha=0.75, edgecolor="#0e0e14")
        ax1.axvline(profile["mean"], color=PINK, linestyle="--", lw=1.5, label="Mean")
        ax1.axvline(profile["median"], color=GREEN, linestyle="--", lw=1.5, label="Median")
        ax1.legend(fontsize=8); ax1.set_title("Distribution", color="#c8c8d8")
        ax2.boxplot(sn, vert=False, patch_artist=True,
                    boxprops=dict(facecolor=ACCENT, alpha=0.6, color=ACCENT),
                    medianprops=dict(color=PINK, linewidth=2),
                    whiskerprops=dict(color="#8888a8"), capprops=dict(color="#8888a8"),
                    flierprops=dict(marker="o", markerfacecolor=PINK, markersize=4, alpha=0.6))
        ax2.set_title("Boxplot", color="#c8c8d8")
        fig.suptitle(col, color="#c8c8d8", fontsize=11); fig.tight_layout()
        profile["chart"] = fig_to_b64(fig)
        profile["code"] = f's = df["{col}"]\ns.describe()\ns.skew()  # {profile["skewness"]}\ns.kurtosis()  # {profile["kurtosis"]}'
    else:
        vc = s.value_counts().head(10)
        profile["top_values"] = [{"value": str(k), "count": int(v),
                                   "pct": round(v / len(s) * 100, 2)} for k, v in vc.items()]
        apply_rc()
        fig, ax = plt.subplots(figsize=(8, max(3, len(vc) * 0.35)))
        ax.barh([str(k) for k in vc.index[::-1]], vc.values[::-1], color=ACCENT, alpha=0.85)
        ax.set_title(f"Top Values — {col}", color="#c8c8d8"); fig.tight_layout()
        profile["chart"] = fig_to_b64(fig)
        profile["code"] = f's = df["{col}"]\ns.dtype  # {s.dtype}\ns.nunique()  # {profile["unique"]}\ns.value_counts().head(10)'
    return jsonify(profile)

# ── 18. Sort ───────────────────────────────────────────────────────────────────

@app.route("/api/sort", methods=["POST"])
def sort_data():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    col = d.get("column"); asc = d.get("ascending", True); n = int(d.get("n", 100))
    if not col or col not in df.columns: return jsonify({"error": "column required."}), 400
    res = df.sort_values(col, ascending=asc).head(n)
    return jsonify({"rows": json_safe_records(res), "cols": res.columns.tolist(),
                    "total": len(df), "showing": min(n, len(df)), "sort_col": col,
                    "code": f'df.sort_values("{col}", ascending={asc}).head({n})'})

# ── 19. Slice / Filter Builder ─────────────────────────────────────────────────

@app.route("/api/slice", methods=["POST"])
def slice_data():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    filters = d.get("filters", []); n = int(d.get("n", 200))
    applied = []; code_lines = []
    for f in filters:
        col = f.get("col"); op = f.get("op"); val = f.get("val")
        if col not in df.columns: continue
        try:
            s = df[col]; ops_map = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
            if op == "eq":         df = df[s.astype(str) == str(val)]; code_lines.append(f'df[df["{col}"]=="{val}"]')
            elif op == "ne":       df = df[s.astype(str) != str(val)]; code_lines.append(f'df[df["{col}"]!="{val}"]')
            elif op == "contains": df = df[s.astype(str).str.contains(str(val), case=False, na=False)]; code_lines.append(f'df[df["{col}"].str.contains("{val}")]')
            elif op == "isin":
                vals = [v.strip() for v in str(val).split(",")]
                df = df[s.astype(str).isin(vals)]; code_lines.append(f'df[df["{col}"].isin({vals})]')
            elif op in ops_map:
                fv = float(val); num = pd.to_numeric(s, errors="coerce")
                df = df[eval(f"num{ops_map[op]}fv")]; code_lines.append(f'df[df["{col}"]{ops_map[op]}{val}]')
            applied.append(f"{col} {op} {val}")
        except Exception: continue
    return jsonify({"rows": json_safe_records(df.head(n)), "cols": df.columns.tolist(),
                    "total_matched": len(df), "showing": min(n, len(df)),
                    "filters_applied": applied,
                    "code": "\n".join(code_lines) if code_lines else "# No filters applied"})

# ── 20. Outlier Detection ──────────────────────────────────────────────────────

@app.route("/api/outliers", methods=["POST"])
def outliers():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    col = d.get("column")
    if not col or col not in df.columns: return jsonify({"error": "column required."}), 400
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    if s.empty: return jsonify({"error": "No numeric values."}), 400
    Q1 = s.quantile(.25); Q3 = s.quantile(.75); IQR = Q3 - Q1
    lo = Q1 - 1.5 * IQR; hi = Q3 + 1.5 * IQR; out = s[(s < lo) | (s > hi)]
    apply_rc()
    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.boxplot(s, vert=False, patch_artist=True,
               boxprops=dict(facecolor=ACCENT, alpha=0.6, color=ACCENT),
               medianprops=dict(color=PINK, linewidth=2),
               whiskerprops=dict(color="#8888a8"), capprops=dict(color="#8888a8"),
               flierprops=dict(marker="o", markerfacecolor=PINK, markersize=5, alpha=0.7))
    ax.set_title(f"Boxplot — {col}", color="#c8c8d8"); ax.set_xlabel(col); fig.tight_layout()
    return jsonify({"column": col, "q1": round(float(Q1), 4), "q3": round(float(Q3), 4),
                    "iqr": round(float(IQR), 4), "lower": round(float(lo), 4), "upper": round(float(hi), 4),
                    "outlier_count": int(len(out)), "outlier_pct": round(len(out) / len(s) * 100, 2),
                    "chart": fig_to_b64(fig),
                    "code": f'Q1=df["{col}"].quantile(.25)\nQ3=df["{col}"].quantile(.75)\nIQR=Q3-Q1\ndf[(df["{col}"]<Q1-1.5*IQR)|(df["{col}"]>Q3+1.5*IQR)]'})

# ── 21. Scatter Plot ───────────────────────────────────────────────────────────

@app.route("/api/scatter", methods=["POST"])
def scatter():
    df, err = load_df()
    if err: return jsonify({"error": err}), 400
    d = request.get_json(silent=True) or {}; df = apply_selection(df, d)
    xc = d.get("x_col", ""); yc = d.get("y_col", "")
    for c in [xc, yc]:
        if c not in df.columns: return jsonify({"error": f"Column '{c}' not found."}), 400
    x = pd.to_numeric(df[xc], errors="coerce"); y = pd.to_numeric(df[yc], errors="coerce")
    m = x.notna() & y.notna(); x, y = x[m], y[m]
    corr = round(float(x.corr(y)), 4)
    apply_rc()
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(x, y, alpha=0.45, s=16, color=ACCENT, edgecolors="none")
    z = np.polyfit(x, y, 1); p = np.poly1d(z); xl = np.linspace(x.min(), x.max(), 200)
    ax.plot(xl, p(xl), color=PINK, lw=1.5, linestyle="--", label="Trend")
    ax.legend(fontsize=9); ax.set_xlabel(xc); ax.set_ylabel(yc)
    ax.set_title(f"Scatter — {xc} vs {yc}  (r={corr})", color="#c8c8d8"); fig.tight_layout()
    return jsonify({"x_col": xc, "y_col": yc, "correlation": corr, "chart": fig_to_b64(fig),
                    "code": f'df.plot.scatter("{xc}", "{yc}")\ndf[["{xc}","{yc}"]].corr()'})

# ── 22. Export Excel ───────────────────────────────────────────────────────────

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

# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
