"""Fast API smoke test for Panda's main user workflow.

The test client uploads a small in-memory CSV, calls representative analysis and
visualization routes, and asserts that each response succeeds. Run this for a
quick integration check; use ``audit_all_features.py`` for formula-level checks.
The uploaded test dataset is deleted when the run completes.
"""

import io
import os

os.environ["PANDA_LOCAL_STORAGE"] = "1"

import app


RAW = (
    b"date,city,channel,sales,profit,cost,customer_id,flag\n"
    b"2024-01-01,A,Online,10,1,9,C1,1\n"
    b"2024-01-15,B,Store,20,4,16,C2,0\n"
    b"2024-02-01,A,Online,15,2,13,C3,1\n"
    b"2024-02-15,C,Store,5,1,4,C4,0\n"
    b"2024-03-01,A,Store,,3,8,C5,1\n"
    b"2024-03-15,B,Online,90,20,70,C6,0\n"
)


def main():
    """Exercise representative endpoints and fail fast on broken API wiring."""
    client = app.app.test_client()
    upload = client.post(
        "/upload",
        data={"file": (io.BytesIO(RAW), "sample.csv")},
        content_type="multipart/form-data",
    )
    print("upload", upload.status_code)
    data = upload.get_json()
    assert upload.status_code == 200

    checks = [
        ("preview", "/api/preview", {"mode": "head", "n": 2}),
        ("value-counts", "/api/value-counts", {"column": "city"}),
        (
            "group-analysis",
            "/api/group-analysis",
            {"metric_col": "sales", "group_cols": ["city"], "calculations": ["sum", "mean"]},
        ),
        ("visual-bar", "/api/visual-studio", {"chart_type": "bar", "x_col": "city", "y_col": "sales", "agg": "mean"}),
        ("visual-horizontal-bar", "/api/visual-studio", {"chart_type": "horizontal-bar", "x_col": "city", "y_col": "sales", "agg": "sum"}),
        ("visual-stacked-bar", "/api/visual-studio", {"chart_type": "stacked-bar", "x_col": "city", "series_col": "channel", "y_col": "sales", "agg": "sum"}),
        ("visual-line", "/api/visual-studio", {"chart_type": "line", "x_col": "date", "y_col": "sales", "agg": "sum"}),
        ("visual-area", "/api/visual-studio", {"chart_type": "area", "x_col": "date", "y_col": "sales", "agg": "sum"}),
        ("visual-combo", "/api/visual-studio", {"chart_type": "combo", "x_col": "date", "y_col": "sales", "agg": "sum"}),
        ("visual-pie", "/api/visual-studio", {"chart_type": "pie", "x_col": "city", "y_col": "sales", "agg": "sum"}),
        ("visual-donut", "/api/visual-studio", {"chart_type": "donut", "x_col": "city", "y_col": "sales", "agg": "sum"}),
        ("visual-treemap", "/api/visual-studio", {"chart_type": "treemap", "x_col": "city", "y_col": "sales", "agg": "sum"}),
        ("visual-histogram", "/api/visual-studio", {"chart_type": "histogram", "x_col": "sales", "bins": 8}),
        ("visual-topn", "/api/visual-studio", {"chart_type": "topn", "x_col": "city"}),
        ("visual-box", "/api/visual-studio", {"chart_type": "box", "x_col": "city", "y_col": "sales"}),
        ("visual-scatter", "/api/visual-studio", {"chart_type": "scatter", "x_col": "sales", "y_col": "profit"}),
        ("visual-heatmap", "/api/visual-studio", {"chart_type": "heatmap"}),
        ("segment", "/api/segment-studio", {"segment_col": "city", "metric_col": "sales"}),
        ("data-quality", "/api/data-quality-studio", {}),
        ("time-series", "/api/time-series-studio", {"date_col": "date", "metric_col": "sales", "freq": "M", "agg": "sum"}),
        ("group-comparison", "/api/group-comparison", {"group_col": "city", "metric_col": "sales", "agg": "mean"}),
        ("missing-analysis", "/api/missing-analysis", {"group_col": "city", "target_col": "profit"}),
        ("outliers-iqr", "/api/outliers", {"column": "sales", "method": "iqr"}),
        ("outliers-zscore", "/api/outliers", {"column": "sales", "method": "zscore"}),
        ("outliers-mad", "/api/outliers", {"column": "sales", "method": "mad"}),
        ("outliers-percentile", "/api/outliers", {"column": "sales", "method": "percentile"}),
        ("correlation-upgrade", "/api/correlation", {"method": "pearson", "target_col": "profit", "ignore_ids": True}),
    ]
    for name, url, payload in checks:
        response = client.post(url, json=payload)
        body = response.get_json()
        print(name, response.status_code, body.get("export_name"), body.get("result_meta"))
        assert response.status_code == 200, body

    dataset_id = data.get("dataset_id")
    if dataset_id:
        client.delete(f"/api/local-datasets/{dataset_id}")


if __name__ == "__main__":
    main()
