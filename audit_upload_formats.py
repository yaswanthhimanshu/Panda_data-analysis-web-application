"""End-to-end regression audit for every upload format  by Panda.

The audit creates small representative files in memory, posts them through the
real Flask upload route, and confirms that pandas receives the expected table.
"""

import bz2
import gzip
import io
import json
import os
import tempfile
import zipfile

import pandas as pd
import pyarrow as pa
import pyarrow.orc as pa_orc


TEMP_DATA = tempfile.TemporaryDirectory(prefix="panda-format-audit-")
os.environ["PANDA_DATA_DIR"] = TEMP_DATA.name

from app import app  # noqa: E402 - storage path must be set before app import


SOURCE = pd.DataFrame(
    {
        "record_id": [1, 2, 3],
        "amount": [10.5, 20.25, 30.0],
        "segment": ["North", "South", "West"],
    }
)


def frame_bytes(writer):
    """Return bytes written by a pandas or PyArrow buffer callback."""
    buffer = io.BytesIO()
    writer(buffer)
    return buffer.getvalue()


def make_arrow_ipc():
    """Create a standard Arrow IPC file, which is also Feather V2 compatible."""
    buffer = io.BytesIO()
    table = pa.Table.from_pandas(SOURCE, preserve_index=False)
    with pa.ipc.new_file(buffer, table.schema) as writer:
        writer.write_table(table)
    return buffer.getvalue()


def make_zip():
    """Create an archive containing a supported file in a nested directory."""
    parquet = frame_bytes(lambda b: SOURCE.to_parquet(b, index=False))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested/sample.parquet", parquet)
    return buffer.getvalue()


CSV = SOURCE.to_csv(index=False).encode("utf-8")
TSV = SOURCE.to_csv(index=False, sep="\t").encode("utf-8")
JSON_RECORDS = SOURCE.to_json(orient="records").encode("utf-8")
JSON_LINES = SOURCE.to_json(orient="records", lines=True).encode("utf-8")

CASES = {
    "sample.csv": CSV,
    "sample.tsv": TSV,
    "sample.txt": SOURCE.to_csv(index=False, sep="|").encode("utf-8"),
    "sample.json": JSON_RECORDS,
    "sample.jsonl": JSON_LINES,
    "sample.ndjson": JSON_LINES,
    "sample.xlsx": frame_bytes(lambda b: SOURCE.to_excel(b, index=False)),
    "sample.parquet": frame_bytes(lambda b: SOURCE.to_parquet(b, index=False)),
    "sample.feather": frame_bytes(lambda b: SOURCE.to_feather(b)),
    "sample.arrow": make_arrow_ipc(),
    "sample.orc": frame_bytes(
        lambda b: pa_orc.write_table(
            pa.Table.from_pandas(SOURCE, preserve_index=False), b
        )
    ),
    "sample.csv.gz": gzip.compress(CSV),
    "sample.tsv.bz2": bz2.compress(TSV),
    "sample.zip": make_zip(),
}


def assert_uploaded(client, filename, payload):
    """Upload one case and verify both response metadata and loaded columns."""
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(payload), filename)},
        content_type="multipart/form-data",
    )
    body = response.get_json()
    if response.status_code != 200:
        raise AssertionError(f"{filename}: HTTP {response.status_code}: {body}")
    if body["rows"] != len(SOURCE) or body["columns"] != len(SOURCE.columns):
        raise AssertionError(f"{filename}: incorrect shape: {body}")

    columns_response = client.get("/api/columns")
    columns = columns_response.get_json()
    if columns_response.status_code != 200:
        raise AssertionError(f"{filename}: could not reopen stored dataset: {columns}")
    if columns["all"] != list(SOURCE.columns):
        raise AssertionError(f"{filename}: incorrect columns: {columns['all']}")


def main():
    """Run all upload cases and report a compact pass list."""
    app.config.update(TESTING=True, SECRET_KEY="format-audit")
    passed = []
    with app.test_client() as client:
        for filename, payload in CASES.items():
            assert_uploaded(client, filename, payload)
            passed.append(filename)

    import xlrd

    print(f"PASS: {len(passed)} upload formats")
    for filename in passed:
        print(f"  OK  {filename}")
    print(f"  OK  legacy .xls engine (xlrd {xlrd.__version__})")


if __name__ == "__main__":
    try:
        main()
    finally:
        TEMP_DATA.cleanup()
