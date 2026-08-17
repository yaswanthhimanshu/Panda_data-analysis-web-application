"""Deterministic end-to-end correctness audit for the Panda workbench.

System workflow: generate a seeded edge-case dataset, upload it through Flask's
test client, independently calculate expected pandas results, then compare every
analysis, chart recipe, selection, saved-dataset action, and export. Contributors
should run this after changing formulas or API contracts; the fixture is stable
so a failure identifies a behavioral regression rather than random input drift.
"""

from __future__ import annotations

import io
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

os.environ["PANDA_LOCAL_STORAGE"] = "1"

import app


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "validation_random_dataset.csv"
TOL = 1e-4


def build_fixture() -> pd.DataFrame:
    """Build stable mixed-type data with missing values, a duplicate, and an outlier."""
    rng = np.random.default_rng(20260719)
    n = 36
    periods = np.repeat(pd.date_range("2024-01-01", periods=6, freq="MS"), 6)
    dates = periods + pd.to_timedelta(np.tile(np.arange(6), 6), unit="D")
    regions = np.resize(np.array(["North", "South", "East"], dtype=object), n)
    channels = np.resize(np.array(["Web", "Store"], dtype=object), n)
    sales = rng.integers(20, 121, size=n).astype(float)
    sales[-1] = 1000.0
    profit = np.round(sales * 0.24 + rng.normal(0, 4, size=n), 2)
    cost = np.round(sales - profit, 2)
    units = rng.integers(1, 10, size=n)
    satisfaction = rng.integers(1, 6, size=n)
    frame = pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "region": regions,
            "channel": channels,
            "sales": sales,
            "profit": profit,
            "cost": cost,
            "units": units,
            "satisfaction": satisfaction,
            "customer_group": np.resize(["New", "Returning", "Loyal"], n),
            "flag": np.resize([0, 1], n),
            "note": np.resize(["alpha", "beta", "gamma", "delta"], n),
        }
    )
    frame.loc[4, "sales"] = np.nan
    frame.loc[7, "profit"] = np.nan
    frame.loc[9, "region"] = None
    frame.loc[11, "note"] = None
    frame = pd.concat([frame, frame.iloc[[12]]], ignore_index=True)
    return frame


def close(a, b, tol=TOL):
    """Compare nullable numeric results with the audit's shared tolerance."""
    if a is None or b is None:
        return a is None and b is None
    if pd.isna(a) or pd.isna(b):
        return pd.isna(a) and pd.isna(b)
    return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)


class Audit:
    """Compare API responses with independently computed pandas expectations."""
    def __init__(self, frame: pd.DataFrame):
        self.df = frame
        self.client = app.app.test_client()
        self.dataset_id = None
        self.passed: list[str] = []

    def ok(self, name, condition, detail=""):
        if not condition:
            raise AssertionError(f"{name}: {detail or 'assertion failed'}")
        self.passed.append(name)

    def request(self, name, url, payload=None, method="post"):
        response = self.client.get(url) if method == "get" else self.client.post(url, json=payload or {})
        body = response.get_json(silent=True)
        if response.status_code != 200:
            raise AssertionError(f"{name}: HTTP {response.status_code}: {body}")
        return body, response

    def upload(self):
        raw = self.df.to_csv(index=False).encode("utf-8")
        response = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(raw), FIXTURE.name)},
            content_type="multipart/form-data",
        )
        body = response.get_json()
        self.ok("upload", response.status_code == 200, str(body))
        self.dataset_id = body.get("dataset_id")
        loaded, error = app.load_dataset(self.dataset_id)
        self.ok("upload row count", error is None and len(loaded) == len(self.df))

    def dataset_features(self):
        cols, _ = self.request("columns", "/api/columns", method="get")
        self.ok("columns inventory", cols["all"] == self.df.columns.tolist())
        self.ok("date detection", "date" in cols["datetime"])

        overview, _ = self.request("overview", "/api/overview", method="get")
        missing = int(self.df.isna().sum().sum())
        duplicates = int(self.df.duplicated().sum())
        total_cells = int(self.df.size)
        expected_score = round(100 - missing / total_cells * 55 - duplicates / len(self.df) * 25, 1)
        self.ok("overview shape", overview["rows"] == len(self.df) and overview["columns"] == len(self.df.columns))
        self.ok("overview missing", overview["missing_total"] == missing)
        self.ok("overview duplicates", overview["duplicate_rows"] == duplicates)
        self.ok("overview quality score", close(overview["data_quality_score"], expected_score))

        dictionary, _ = self.request("column dictionary", "/api/column-dictionary", method="get")
        by_col = {row["column"]: row for row in dictionary["rows"]}
        self.ok("dictionary coverage", set(by_col) == set(self.df.columns))
        self.ok("dictionary sales missing", by_col["sales"]["missing"] == int(self.df["sales"].isna().sum()))
        self.ok("dictionary region unique", by_col["region"]["unique"] == int(self.df["region"].nunique()))

        quality, _ = self.request("data quality", "/api/data-quality-studio", {})
        studio_score = round(
            100
            - missing / total_cells * 55
            - duplicates / len(self.df) * 25
            - min(quality["issue_count"] * 1.5, 20),
            1,
        )
        self.ok("data quality score", close(quality["data_quality_score"], studio_score))
        self.ok("data quality duplicate count", quality["duplicate_rows"] == duplicates)

    def previews_and_selection(self):
        head, _ = self.request("preview head", "/api/preview", {"mode": "head", "n": 4})
        tail, _ = self.request("preview tail", "/api/preview", {"mode": "tail", "n": 3})
        sample, _ = self.request("preview sample", "/api/preview", {"mode": "sample", "n": 5})
        self.ok("preview head values", [r["date"] for r in head["rows"]] == self.df.head(4)["date"].tolist())
        self.ok("preview tail values", [r["date"] for r in tail["rows"]] == self.df.tail(3)["date"].tolist())
        self.ok("preview sample size", len(sample["rows"]) == 5)

        selected, _ = self.request(
            "column selection", "/api/preview",
            {"mode": "head", "n": 50, "columns": ["region", "sales"]},
        )
        self.ok("column selection", selected["cols"] == ["region", "sales"])

        selected_overview, _ = self.request(
            "overview column selection", "/api/overview",
            {"columns": ["region", "sales"]},
        )
        self.ok("overview selected shape", selected_overview["columns"] == 2)
        self.ok(
            "overview selected columns",
            [row["column"] for row in selected_overview["col_info"]] == ["region", "sales"],
        )

        selected_dictionary, _ = self.request(
            "dictionary column selection", "/api/column-dictionary",
            {"columns": ["region", "sales"]},
        )
        self.ok(
            "dictionary selected columns",
            [row["column"] for row in selected_dictionary["rows"]] == ["region", "sales"],
        )

        filtered_projection, _ = self.request(
            "filter outside selected columns", "/api/preview",
            {
                "mode": "head", "n": 100,
                "columns": ["sales"],
                "filter_col": "region", "filter_op": "eq", "filter_val": "North",
            },
        )
        self.ok("filter outside projection columns", filtered_projection["cols"] == ["sales"])
        self.ok("filter outside projection count", filtered_projection["total"] == int(self.df["region"].eq("North").sum()))

        filters = [
            ("eq", "region", "North", self.df["region"].eq("North")),
            ("contains", "note", "alp", self.df["note"].str.contains("alp", case=False, na=False)),
            ("isin", "region", "North,East", self.df["region"].isin(["North", "East"])),
            ("gt", "sales", "100", self.df["sales"] > 100),
        ]
        for op, col, value, mask in filters:
            body, _ = self.request(
                f"filter {op}", "/api/preview",
                {"mode": "head", "n": 100, "filter_col": col, "filter_op": op, "filter_val": value},
            )
            self.ok(f"filter {op} count", body["total"] == int(mask.sum()))
        ranged, _ = self.request("row range", "/api/preview", {"mode": "head", "n": 20, "row_start": 3, "row_end": 9})
        self.ok("row range count", ranged["total"] == 6 and len(ranged["rows"]) == 6)

    def core_analysis(self):
        value_counts, _ = self.request("value counts", "/api/value-counts", {"column": "region"})
        expected_vc = self.df["region"].fillna("(Missing)").astype(str).value_counts()
        got_vc = {r["value"]: r for r in value_counts["rows"]}
        self.ok("value count values", {k: v["count"] for k, v in got_vc.items()} == expected_vc.to_dict())
        self.ok("value count percentages", all(close(got_vc[k]["pct"], v / len(self.df) * 100, 1e-2) for k, v in expected_vc.items()))

        profiler, _ = self.request("column profiler", "/api/column-profiler", {"column": "sales"})
        sales = self.df["sales"].dropna()
        for key, expected in {
            "mean": sales.mean(), "median": sales.median(), "std": sales.std(), "variance": sales.var(),
            "min": sales.min(), "max": sales.max(), "q1": sales.quantile(.25), "q3": sales.quantile(.75),
            "iqr": sales.quantile(.75) - sales.quantile(.25), "skewness": sales.skew(), "kurtosis": sales.kurtosis(),
        }.items():
            self.ok(f"profiler {key}", close(profiler[key], expected), f"got {profiler[key]}, expected {expected}")

        distribution, _ = self.request("distribution", "/api/distribution", {"column": "sales"})
        for key, expected in {"mean": sales.mean(), "median": sales.median(), "std": sales.std(), "skewness": sales.skew(), "kurtosis": sales.kurtosis()}.items():
            self.ok(f"distribution {key}", close(distribution[key], expected))
        self.ok("distribution histogram coverage", sum(distribution["echarts"]["series"][0]["data"]) == len(sales))

        calcs = ["sum", "mean", "median", "min", "max", "std", "variance", "count", "mode", "share_pct", "contribution_pct", "diff_from_avg", "rank"]
        grouped, _ = self.request("group analysis", "/api/group-analysis", {"metric_col": "sales", "group_cols": ["region"], "calculations": calcs})
        got = {r["region"]: r for r in grouped["rows"]}
        expected_groups = self.df.groupby("region")["sales"]
        group_sum = expected_groups.sum()
        grand_total = self.df["sales"].sum()
        grand_mean = self.df["sales"].mean()
        for region, values in expected_groups:
            row = got[region]
            clean = values.dropna()
            expected_map = {
                "sum": clean.sum(), "mean": clean.mean(), "median": clean.median(), "min": clean.min(),
                "max": clean.max(), "std": clean.std(), "variance": clean.var(), "record_count": clean.count(),
                "mode": clean.mode().iloc[0], "share_pct": group_sum[region] / grand_total * 100,
                "contribution_pct": group_sum[region] / grand_total * 100, "diff_from_avg": clean.mean() - grand_mean,
            }
            for key, expected in expected_map.items():
                self.ok(f"group analysis {region} {key}", close(row[key], expected, 1e-2), f"got {row[key]}, expected {expected}")

        comparison_aggs = {
            "mean": expected_groups.mean,
            "sum": expected_groups.sum,
            "count": expected_groups.count,
            "min": expected_groups.min,
            "max": expected_groups.max,
            "median": expected_groups.median,
            "mode": lambda: expected_groups.apply(lambda s: s.dropna().mode().iloc[0]),
            "std": expected_groups.std,
            "variance": expected_groups.var,
        }
        for agg, expected_fn in comparison_aggs.items():
            comparison, _ = self.request(
                f"group comparison {agg}", "/api/group-comparison",
                {"group_col": "region", "metric_col": "sales", "agg": agg},
            )
            comparison_rows = {r["region"]: r for r in comparison["rows"]}
            for region, expected in expected_fn().items():
                self.ok(
                    f"group comparison {agg} {region}",
                    close(comparison_rows[region]["value"], expected),
                )

        singleton, _ = self.request(
            "group comparison singleton std", "/api/group-comparison",
            {"group_col": "date", "metric_col": "sales", "agg": "std"},
        )
        chart_values = singleton["echarts"]["series"][0]["data"]
        self.ok("group comparison singleton std JSON", any(v is None for v in chart_values))

        contribution, _ = self.request("contribution", "/api/contribution-analysis", {"group_col": "region", "metric_col": "sales"})
        contribution_rows = {r["region"]: r for r in contribution["rows"]}
        self.ok("contribution grand total", close(contribution["grand_total"], grand_total))
        for region, expected in group_sum.items():
            self.ok(f"contribution {region} total", close(contribution_rows[region]["total"], expected))
            self.ok(f"contribution {region} pct", close(contribution_rows[region]["contribution_pct"], expected / grand_total * 100, 1e-2))

    def tables_and_time(self):
        pivot, _ = self.request("pivot", "/api/pivot", {"index": "region", "columns": "channel", "values": "sales", "agg": "sum"})
        expected_pivot = pd.pivot_table(self.df, index="region", columns="channel", values="sales", aggfunc="sum", fill_value=0)
        expected_pivot_html = expected_pivot.round(4).to_html(classes="panda-table", border=0)
        self.ok("pivot values", pivot["html"] == expected_pivot_html)

        pivot_mode, _ = self.request(
            "pivot mode", "/api/pivot",
            {"index": "region", "columns": "channel", "values": "sales", "agg": "mode"},
        )
        expected_mode = pd.pivot_table(
            self.df, index="region", columns="channel", values="sales",
            aggfunc=lambda s: s.dropna().mode().iloc[0], fill_value=0,
        )
        self.ok(
            "pivot mode values",
            pivot_mode["html"] == expected_mode.round(4).to_html(classes="panda-table", border=0),
        )

        for normalize in (False, True):
            crosstab, _ = self.request("crosstab", "/api/crosstab", {"row_col": "region", "col_col": "channel", "normalize": normalize})
            expected = pd.crosstab(self.df["region"], self.df["channel"], normalize="all" if normalize else False)
            if normalize:
                expected = (expected * 100).round(2)
            expected_html = expected.to_html(classes="panda-table", border=0)
            self.ok(f"crosstab {'pct' if normalize else 'counts'}", crosstab["html"] == expected_html)

        variance, _ = self.request("variance", "/api/variance-analysis", {"date_col": "date", "metric_col": "sales", "group_col": "region", "freq": "M", "agg": "sum"})
        dated = self.df.assign(_date=pd.to_datetime(self.df["date"]), _metric=self.df["sales"]).dropna(subset=["_metric"])
        dated["_period"] = dated["_date"].dt.to_period("M").dt.to_timestamp()
        trend = dated.groupby("_period")["_metric"].sum()
        self.ok("variance current", close(variance["current_value"], trend.iloc[-1]))
        self.ok("variance previous", close(variance["previous_value"], trend.iloc[-2]))
        self.ok("variance change", close(variance["absolute_change"], trend.iloc[-1] - trend.iloc[-2]))

        series, _ = self.request("time series", "/api/time-series-studio", {"date_col": "date", "metric_col": "sales", "category_col": "region", "freq": "M", "agg": "sum"})
        self.ok("time series chart", bool(series.get("echarts")) and len(series.get("rows", [])) > 0)

        for mode in ("category", "range", "quantile"):
            segment_col = "region" if mode == "category" else "sales"
            segment, _ = self.request(f"segment {mode}", "/api/segment-studio", {"segment_col": segment_col, "segment_mode": mode, "bins": 4, "metric_col": "profit"})
            self.ok(f"segment {mode} row total", sum(r["rows"] for r in segment["rows"]) == len(self.df))
            self.ok(f"segment {mode} shares", close(sum(r["share_pct"] for r in segment["rows"]), 100, 0.05))

    def missing_outliers_correlations(self):
        missing, _ = self.request("missing analysis", "/api/missing-analysis", {"group_col": "region", "target_col": "profit"})
        rows = {r["column"]: r for r in missing["rows"]}
        for col in self.df.columns:
            self.ok(f"missing {col}", rows[col]["missing"] == int(self.df[col].isna().sum()))

        sales = self.df["sales"]
        valid = sales.dropna()
        methods = {}
        q1, q3 = valid.quantile(.25), valid.quantile(.75)
        methods["iqr"] = (q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1))
        mean, std = valid.mean(), valid.std()
        methods["zscore"] = (mean - 3 * std, mean + 3 * std)
        med = valid.median(); mad = np.median(np.abs(valid - med))
        methods["mad"] = (med - 3.5 * mad / .6745, med + 3.5 * mad / .6745)
        methods["percentile"] = (valid.quantile(.01), valid.quantile(.99))
        for method, (lower, upper) in methods.items():
            result, _ = self.request(f"outliers {method}", "/api/outliers", {"column": "sales", "method": method})
            expected_mask = (sales < lower) | (sales > upper)
            self.ok(f"outliers {method} lower", close(result["lower_threshold"], lower))
            self.ok(f"outliers {method} upper", close(result["upper_threshold"], upper))
            self.ok(f"outliers {method} count", result["outlier_count"] == int(expected_mask.sum()))

        numeric = self.df[["sales", "profit", "cost", "units", "satisfaction", "flag"]]
        for method in ("pearson", "spearman", "kendall"):
            corr, _ = self.request(f"correlation {method}", "/api/correlation", {"method": method, "target_col": "profit", "ignore_ids": False})
            rows = {r["column"]: r for r in corr["rows"]}
            expected = numeric.corr(method=method)["profit"]
            for col in expected.index.drop("profit"):
                self.ok(f"correlation {method} {col}", close(rows[col]["corr"], expected[col]))

        scatter, _ = self.request("recommended scatter", "/api/scatter", {"x_col": "sales", "y_col": "profit"})
        mask = self.df["sales"].notna() & self.df["profit"].notna()
        self.ok("scatter correlation", close(scatter["correlation"], self.df.loc[mask, "sales"].corr(self.df.loc[mask, "profit"])))
        self.ok("scatter point count", len(scatter["echarts"]["series"][0]["data"]) == int(mask.sum()))

        ranking, _ = self.request("ranking", "/api/ranking", {"column": "sales"})
        expected = self.df.dropna(subset=["sales"])
        self.ok("ranking max", close(ranking["largest"][0]["sales"], expected["sales"].max()))
        self.ok("ranking min", close(ranking["smallest"][0]["sales"], expected["sales"].min()))

    def visualizations(self):
        grouped = self.df.assign(region=self.df["region"].fillna("(Missing)").astype(str)).groupby("region")["sales"].sum().dropna()
        charts = {
            "auto": {"x_col": "sales", "y_col": "profit"},
            "histogram": {"x_col": "sales", "bins": 8},
            "topn": {"x_col": "region"},
            "bar": {"x_col": "region", "y_col": "sales", "agg": "sum"},
            "horizontal-bar": {"x_col": "region", "y_col": "sales", "agg": "sum"},
            "stacked-bar": {"x_col": "region", "series_col": "channel", "y_col": "sales", "agg": "sum"},
            "line": {"x_col": "date", "y_col": "sales", "agg": "sum"},
            "area": {"x_col": "date", "y_col": "sales", "agg": "sum"},
            "combo": {"x_col": "date", "y_col": "sales", "agg": "sum"},
            "pie": {"x_col": "region", "y_col": "sales", "agg": "sum"},
            "donut": {"x_col": "region", "y_col": "sales", "agg": "sum"},
            "treemap": {"x_col": "region", "y_col": "sales", "agg": "sum"},
            "box": {"x_col": "region", "y_col": "sales"},
            "scatter": {"x_col": "sales", "y_col": "profit"},
            "heatmap": {},
        }
        for chart, args in charts.items():
            body, _ = self.request(f"visual {chart}", "/api/visual-studio", {"chart_type": chart, **args})
            self.ok(f"visual {chart} rendered", body.get("type") == "chart" and bool(body.get("echarts")))
            resolved = body["chart_type"]
            self.ok(f"visual {chart} resolved type", resolved == ("scatter" if chart == "auto" else chart))
            if chart == "histogram":
                self.ok("visual histogram coverage", sum(body["echarts"]["series"][0]["data"]) == int(self.df["sales"].notna().sum()))
            elif chart == "topn":
                self.ok("visual topn coverage", sum(body["echarts"]["series"][0]["data"]) == len(self.df))
            elif chart in ("bar", "horizontal-bar"):
                self.ok(f"visual {chart} total", close(sum(body["echarts"]["series"][0]["data"]), grouped.sum()))
            elif chart == "stacked-bar":
                self.ok("visual stacked total", close(sum(sum(s["data"]) for s in body["echarts"]["series"]), self.df["sales"].sum()))
            elif chart in ("pie", "donut"):
                self.ok(f"visual {chart} total", close(sum(x["value"] for x in body["echarts"]["series"][0]["data"]), grouped.sum()))
            elif chart == "treemap":
                self.ok("visual treemap total", close(sum(x["value"] for x in body["echarts"]["series"][0]["data"]), grouped.sum()))

    def exports_and_storage(self):
        payload = {"rows": [{"region": "North", "sales": 12.5}], "cols": ["region", "sales"], "filename": "audit"}
        excel = self.client.post("/api/export-excel", json=payload)
        self.ok("result Excel export", excel.status_code == 200 and excel.data[:2] == b"PK")

        csv_export = self.client.post("/api/export-filtered", json={"format": "csv", "filter_col": "region", "filter_op": "eq", "filter_val": "North"})
        exported = pd.read_csv(io.BytesIO(csv_export.data))
        self.ok("filtered CSV export", csv_export.status_code == 200 and len(exported) == int(self.df["region"].eq("North").sum()))
        selected_csv = self.client.post(
            "/api/export-filtered",
            json={
                "format": "csv", "columns": ["sales"],
                "filter_col": "region", "filter_op": "eq", "filter_val": "North",
            },
        )
        selected_export = pd.read_csv(io.BytesIO(selected_csv.data))
        self.ok("selected-column CSV export columns", selected_export.columns.tolist() == ["sales"])
        self.ok("selected-column CSV export rows", len(selected_export) == int(self.df["region"].eq("North").sum()))
        xlsx_export = self.client.post("/api/export-filtered", json={"format": "xlsx", "filter_col": "sales", "filter_op": "gt", "filter_val": 100})
        exported_xlsx = pd.read_excel(io.BytesIO(xlsx_export.data))
        self.ok("filtered Excel export", xlsx_export.status_code == 200 and len(exported_xlsx) == int((self.df["sales"] > 100).sum()))

        listing, _ = self.request("saved datasets", "/api/local-datasets", method="get")
        ids = [item["id"] for item in listing["datasets"]]
        self.ok("saved dataset listed", self.dataset_id in ids)
        reopened, _ = self.request("saved dataset reopen", f"/api/local-datasets/{self.dataset_id}/open", {})
        self.ok("saved dataset reopened", reopened.get("ok") is True)

    def cleanup(self):
        if self.dataset_id:
            response = self.client.delete(f"/api/local-datasets/{self.dataset_id}")
            if response.status_code != 200:
                raise AssertionError(f"cleanup failed: {response.get_data(as_text=True)}")

    def run(self):
        """Execute the complete user workflow in dependency order."""
        self.upload()
        self.dataset_features()
        self.previews_and_selection()
        self.core_analysis()
        self.tables_and_time()
        self.missing_outliers_correlations()
        self.visualizations()
        self.exports_and_storage()


def main():
    """Write the reproducible fixture, run the audit, and always clean storage."""
    frame = build_fixture()
    frame.to_csv(FIXTURE, index=False)
    audit = Audit(frame)
    try:
        audit.run()
        print(f"PASS: {len(audit.passed)} correctness assertions")
        for name in audit.passed:
            print(f"  OK  {name}")
    finally:
        audit.cleanup()


if __name__ == "__main__":
    main()
