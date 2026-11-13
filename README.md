# Panda

Panda is a local data-analysis workbench for exploring structured files through a point-and-click interface. A user uploads a dataset, narrows the rows or columns, runs an analysis from the sidebar, reviews the result, and exports it. Flask routes call pandas and NumPy for calculations; the browser gathers settings and renders tables and charts.

Panda is useful for repeatable exploratory analysis without writing a separate notebook for each common operation. A notebook or custom Python program is still the right place for work beyond the operations implemented here. For installation steps, see [SETUP.md](SETUP.md).

Quick links: [Features](#features) | [End-to-End Analysis](#end-to-end-analysis) | [Architecture](#architecture-and-data-flow) | [Storage](#dataset-storage-and-resume) | [Tests](#tests) | [Setup](SETUP.md)

## Project Overview

The application brings dataset inspection, common tabular analyses, and visualization controls into one local workspace. It is built around the user's analysis question: select a population, choose an operation and its fields, inspect the returned evidence, and export the result.

The central selection controls are shared across analysis requests. For example, a row filter for one region can scope Group Analysis, a time series, or a chart; each operation still calculates from the uploaded dataset with that selection applied. The app does not chain one result into another automatically.

## Contents

- [Features](#features)
- [Supported Files](#supported-files)
- [Result Exports](#result-exports)
- [End-to-End Analysis](#end-to-end-analysis)
- [Architecture and Data Flow](#architecture-and-data-flow)
- [Technology Stack](#technology-stack)
- [Dataset Storage and Resume](#dataset-storage-and-resume)
- [Implementation Details](#implementation-details)
- [Supported Files](#supported-files)
- [Repository Structure](#repository-structure)
- [Tests](#tests)
- [Current Scope and Limitations](#current-scope-and-limitations)
- [Setup Guide](SETUP.md)
- [Contributing](#contributing)

## Features

### Dataset exploration

- Overview metrics and schema insights, including row/column counts, missing cells, duplicate rows, and detected column roles.
- Column dictionary with per-column type, uniqueness, missingness, and sample information.
- Head, tail, and reproducible sample previews.
- Column profiling, numeric distributions, categorical value counts, and missing-value analysis.
- Data-quality checks and a quality score. These report issues; they do not clean or write changes back to the source dataset.

### Analysis

| Operation | API route | What it returns |
| --- | --- | --- |
| Group analysis | `POST /api/group-analysis` | Grouped counts and selected aggregations, including sum, mean, median, mode, min/max, standard deviation, variance, share, contribution, difference from average, and rank. |
| Contribution analysis | `POST /api/contribution-analysis` | Group totals, shares, cumulative contribution, and ranking information. |
| Variance analysis | `POST /api/variance-analysis` | Period comparisons and absolute/percentage change by the configured grouping. |
| Group comparison | `POST /api/group-comparison` | Aggregated values and comparison with an overall value. |
| Time series | `POST /api/time-series-studio` | Date-period aggregation and trend output. |
| Segment comparison | `POST /api/segment-studio` | Category or quantile segments with group metrics. |
| Pivot table | `POST /api/pivot` | A pandas pivot table returned as HTML. |
| Crosstab | `POST /api/crosstab` | Category combination counts or normalized percentages, returned as HTML. |
| Ranking | `POST /api/ranking` | Largest and smallest records for a selected numeric measure. |
| Correlation | `POST /api/correlation` | Pearson, Spearman, or Kendall correlations and a heatmap recipe. |
| Outliers | `POST /api/outliers` | IQR, z-score, MAD, or percentile threshold results and matching rows (detail rows are capped). |

Additional dataset endpoints provide preview, value counts, distribution, column profiling, scatter analysis, missing-value analysis, and a data-quality report. The browser sidebar is the easiest way to access these operations; the complete route list is in `app.py`.

| Dataset task | API route |
| --- | --- |
| Upload and parse | `POST /upload` |
| Discover columns and inferred roles | `GET /api/columns` |
| Overview and column dictionary | `GET` or `POST /api/overview`; `GET` or `POST /api/column-dictionary` |
| Preview rows | `POST /api/preview` |
| Value counts and numeric distribution | `POST /api/value-counts`; `POST /api/distribution` |
| Column profile and scatter relationship | `POST /api/column-profiler`; `POST /api/scatter` |
| Data-quality report and missingness analysis | `POST /api/data-quality-studio`; `POST /api/missing-analysis` |

### Visualizations

The Visual Studio endpoint builds ECharts recipes from pandas-calculated data. Available chart modes in `build_visual_chart()` include automatic selection, histogram, value-count bar, bar, horizontal bar, line, area, combo, stacked bar, pie, donut, treemap, boxplot, scatter, and correlation heatmap. The browser renders the recipes with ECharts; calculations are performed by the backend. Some chart types limit the displayed categories or series to keep charts readable.

The interface also shows explanatory pandas-code snippets for results. These are displayed as reference text; Panda does not execute arbitrary user-entered Python code.

### Supported files

The upload reader accepts CSV, TSV, TXT, JSON, JSON Lines/NDJSON, XLSX, XLS, Parquet, Feather, Arrow IPC, ORC, gzip/bzip2-compressed text, and ZIP archives containing a supported file. Some formats need their optional reader engine; the supplied requirements install PyArrow and `xlrd` for the supported formats. A ZIP import reads the first supported member.

### Result exports

Result actions export returned analysis rows to CSV or Excel, and charts to PNG. The export reflects the result being viewed; it does not save a reusable analysis project.

## End-to-End Analysis

1. Upload a structured file and let the reader infer its columns and data types.
2. Use Overview, Column Dictionary, Data Quality, and Preview Table to check the dataset before interpreting metrics.
3. Set the top **Rows** filter and range, then choose the columns relevant to the question. The shared backend selection function applies filter, row range, and column projection in that order.
4. Open an analysis page from the sidebar, select its measure, grouping, calculation, or date field, and run it. The current top-bar row and column selections are sent with the analysis request.
5. Review the returned table and chart together. Check the selected population, aggregation, and missing-value behavior before treating a result as a business conclusion.
6. Export the result table or chart using its result actions. For example, a Group Analysis export contains the grouped output, not every underlying row in the filtered dataset.

For a dataset with `country`, `product`, and `revenue` columns, a question such as “Which products contribute most to revenue in France?” can be handled by filtering `country` to France, grouping by `product`, summing `revenue`, reviewing the output, and exporting that grouped result. The exact values depend on the uploaded data.

Analyses reload the persisted dataset for each API request. One analysis result is not automatically used as the input to the next analysis, and quality checks do not modify the stored rows.

## Architecture and Data Flow

```text
Panda.vbs (Windows launcher)
  -> desktop.py (Flask on localhost + PyWebView window)
      -> index.html (controls, browser state, fetch requests, tables, ECharts)
          -> app.py (Flask routes, upload readers, selection, pandas/NumPy calculations)
              -> storage.py (CSV dataset files + SQLite metadata)
          <- JSON/table data and chart recipes
      <- browser-side CSV/PNG downloads; Excel bytes from the Flask API
```

On upload, `/upload` reads the file into a DataFrame, stores its rows as CSV, writes a dataset metadata row to SQLite, and puts the dataset ID in the Flask session. A page control updates browser-side settings; `runOp()` sends the selected fields and current row/column selection to a Flask route. The route reloads the CSV through `storage.py`, calls `apply_selection()`, performs the pandas calculation, and returns JSON (or HTML for pivot/crosstab tables). `index.html` renders the response as result cards, tables, and ECharts visualizations. Result export controls use the returned table rows; Excel bytes are produced by Flask, while CSV and chart PNG downloads are assembled in the browser.

## Dataset Storage and Resume

`storage.py` defaults to a `panda_data` directory beside the application. Dataset rows are CSV files under `panda_data/datasets/`; SQLite metadata is stored in `panda_data/panda.db`. The optional saved-dataset interface lists, opens, and deletes these datasets. It does not persist filters, analysis configurations, charts, or a complete workspace. Opening a dataset restores access to its rows and column metadata; users configure analyses again.

## Repository Structure

| File | Responsibility |
| --- | --- |
| `app.py` | Flask application, upload parsing, selection pipeline, analysis calculations, chart recipes, and server-side exports. |
| `index.html` | Single-page interface, operation forms, API requests, result tables, ECharts rendering, and browser downloads. ECharts and fonts are loaded from CDNs. |
| `storage.py` | Persist dataset rows as CSV and metadata in SQLite; list, load, and delete saved datasets. |
| `desktop.py` | Starts Flask on an available localhost port and opens a PyWebView window with downloads enabled. |
| `Panda.vbs` | Windows no-console launcher that calls `.venv\Scripts\pythonw.exe` and `desktop.py`. |
| `requirements.txt` | Pinned Flask, pandas, NumPy, SciPy, file readers, and supporting packages. |
| `requirements-desktop.txt` | Includes base dependencies and PyWebView. |
| `smoke_workbench.py` | Fast API integration smoke test for upload and representative analyses/charts. |
| `audit_upload_formats.py` | Upload-route checks for supported representative formats using temporary storage. |
| `audit_all_features.py` | Deterministic API/formula, selection, chart, export, and saved-dataset audit. |

## Technology Stack

| Technology | Role in this application |
| --- | --- |
| Python | Runs the desktop wrapper and Flask application. |
| Flask | Serves `index.html`, receives uploads and JSON requests, and returns analysis responses and export files. |
| pandas | Reads structured data, creates DataFrames, performs profiling, grouping, time-based aggregation, correlations, and other analysis. |
| NumPy | Supports numeric conversions and calculations used by analysis and chart helpers. |
| SQLite | Stores dataset lookup metadata; the dataset rows themselves are CSV files. |
| PyWebView | Hosts the localhost UI in a native desktop window and permits downloads. |
| Apache ECharts | Draws interactive charts in the browser from recipes returned by Flask. |
| openpyxl, xlrd, PyArrow | Provide Excel and Arrow-family file readers/writers for formats supported by the upload code. |

SciPy is present in the pinned base requirements, but there is no direct SciPy import in the application modules; this README therefore does not attribute a user-facing operation to it.

## Implementation Details

### Applying the current selection

`app.py::apply_selection()` receives the selection controls with each applicable analysis request. It filters rows first, applies the requested row range with pandas `iloc` (end-exclusive), then projects the selected columns. The calculation works on the resulting request DataFrame; it does not rewrite the saved CSV. The same filter can therefore be used for different sidebar operations, each calculated independently from the stored dataset.

### Returning and exporting results

Analysis routes serialize result records and metadata for the frontend. Chart routes return ECharts-compatible configuration and data; the browser draws the chart. Result exports use the currently returned result rows, while chart PNG is captured from the rendered chart. Exporting a result does not persist the analysis configuration for later resumption.

### Dataset persistence

The Flask session carries a dataset ID rather than a DataFrame. Each request loads the saved CSV from disk. SQLite metadata identifies the dataset, its stored path, dimensions, and column list. This keeps large row data out of the session cookie, while making each analytical request perform a file read.

## Tests

Run the available checks as described in [SETUP.md](SETUP.md). The exhaustive audit creates a deterministic edge-case fixture and compares API responses with independently calculated expectations. The upload-format audit checks generated in-memory samples through the upload route. These scripts are useful regression checks but do not prove correctness for every real dataset or every browser interaction.

## Current Scope and Limitations

- Saved datasets can be reopened, but filters, analysis settings, charts, and results are not saved as a resumable workspace.
- Data-quality features identify issues; Panda does not provide a general data-cleaning/write-back workflow, calculated-column editor, or custom Python execution.
- The app is designed to run locally for one analyst. It does not provide accounts or per-user data access for a hosted multi-user deployment.
- Dataset rows are stored as CSV and types are inferred again when reopened. Check date, identifier, and numeric types before relying on type-sensitive analysis.

## Contributing

Keep calculations in `app.py`, UI state and rendering in `index.html`, and row/metadata persistence in `storage.py`. Reuse the shared selection helper and extend the deterministic audit when changing formulas or API contracts. Do not commit virtual environments, local `panda_data`, uploaded datasets, exports, or secrets.
