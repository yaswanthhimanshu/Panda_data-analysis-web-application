# Panda Setup Guide

For a new installation, follow the steps in order. If Panda already runs on this machine, keep its existing `.venv` and data folder; do not recreate or redirect either one unless you intend to change environments or use a separate dataset store. This guide uses Windows PowerShell for the supplied desktop launcher. Flask can also be started directly for browser use.

The [README](README.md) describes Panda's features, analysis flow, architecture, and current scope. This guide focuses on installing, running, and testing the application from its project folder.

## Contents

- [1. Check Prerequisites](#1-check-prerequisites)
- [2. Prepare the Python Environment](#2-prepare-the-python-environment)
- [3. Install Dependencies](#3-install-dependencies)
- [4. Run the Desktop App](#4-run-the-desktop-app)
- [5. Run in a Browser](#5-run-in-a-browser)
- [6. Configure Local Data](#6-configure-local-data)
- [7. Run the Checks](#7-run-the-checks)
- [8. Troubleshooting and Data Cautions](#8-troubleshooting-and-data-cautions)

## 1. Check Prerequisites

- Windows for the supplied `Panda.vbs` launcher and native PyWebView window.
- Python and the Python launcher (`py`) installed and available in PowerShell.
- Internet access on first setup to install the pinned Python packages. The interface also loads ECharts and web fonts from CDNs at runtime.
- Enough local disk space for the virtual environment and datasets. Uploaded dataset rows are kept locally until deleted.

The commands below use Python 3.15. The repository pins package versions but has no `.python-version` or package metadata enforcing the interpreter version. The full Python compatibility range has not been verified here; use a version for which pip can install the pinned dependencies.

## 2. Prepare the Python Environment

Open PowerShell in the project directory. Check the current Python before creating an environment:

```powershell
cd path\to\panda
py -3.15 --version
```

If this is a new setup and `.venv` does not exist, create it:

```powershell
py -3.15 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

If `.venv` already exists and works, activate and keep using it; do not recreate it just to follow the fresh-install commands. If PowerShell blocks activation, either adjust the current user's execution policy according to your organization's guidance, or invoke `.venv\Scripts\python.exe` and `.venv\Scripts\pip.exe` directly. The VBS launcher specifically expects `.venv\Scripts\pythonw.exe`.

## 3. Install Dependencies

For the desktop application (base packages plus PyWebView):

```powershell
python -m pip install -r requirements-desktop.txt
```

For Flask/browser mode only:

```powershell
python -m pip install -r requirements.txt
```

`requirements-desktop.txt` includes `requirements.txt`. The base requirements include the CSV/JSON/Excel and PyArrow-based readers. If installation fails, check the Python version and the first package error printed by pip; the repository does not provide a separate lockfile or automated version-selection tool.

Check the installed dependency set:

```powershell
python -m pip check
```

## 4. Run the Desktop App

After installing desktop dependencies, either double-click `Panda.vbs` or run:

```powershell
python desktop.py
```

`desktop.py` starts the Flask server bound to `127.0.0.1`, chooses an available port beginning at 5127, waits briefly for the server, then opens a 1440×920 PyWebView window. It enables downloads before opening the window. Close the Panda window to end the desktop process.

The VBS launcher starts `.venv\Scripts\pythonw.exe` without a console. If it does nothing, first run `python desktop.py` from the activated environment so startup errors are visible; confirm the virtual environment exists and contains PyWebView.

## 5. Run in a Browser

Install `requirements.txt`, then start Flask:

```powershell
python app.py
```

Open `http://127.0.0.1:5000`. To enable the saved-dataset list/open/delete controls in this mode, set the environment flag before starting Flask:

```powershell
$env:PANDA_LOCAL_STORAGE = "1"
python app.py
```

The flag exposes dataset management; it does not turn the browser mode into a complete saved-workspace system. The desktop wrapper sets `PANDA_LOCAL_STORAGE=1` and `PANDA_DESKTOP=1` itself. Do not start two browser-mode servers on the same port; the desktop wrapper finds another free port automatically.

## 6. Configure Local Data

By default, `storage.py` creates `panda_data` beside itself, with dataset CSV files in `panda_data/datasets/`, exports in `panda_data/exports/`, and SQLite metadata in `panda_data/panda.db`. These files are runtime data and are excluded by `.gitignore`.

To put runtime data in a separate directory, set `PANDA_DATA_DIR` before starting Python:

```powershell
$env:PANDA_DATA_DIR = Join-Path $env:LOCALAPPDATA "PandaData"
python desktop.py
```

The process must receive this setting before importing `app.py`/`storage.py`. Existing saved datasets remain in their original data directory; changing the variable points Panda at a different storage location, it does not migrate records.

`SECRET_KEY` may be supplied as an environment variable for a stable Flask session signing key. If omitted, `app.py` creates a random key at process startup, so browser session continuity across restarts is not guaranteed. This local application has no account system or authorization layer; do not expose it to untrusted networks.

Saved dataset files remain on disk until the user deletes them from Saved Datasets or removes the configured data directory. Reopening a saved dataset restores the dataset, not prior filters, analyses, chart choices, or result history.

## 7. Run the Checks

Run these from the project folder with the virtual environment active.

### Fast API smoke test

The smoke test uploads an in-memory fixture, calls representative API routes, and deletes its saved dataset at the end. Use a temporary storage directory so it cannot interact with existing local datasets:

```powershell
$env:PANDA_DATA_DIR = Join-Path $env:TEMP ("panda-smoke-" + [guid]::NewGuid().ToString("N"))
python smoke_workbench.py
```

The script sets `PANDA_LOCAL_STORAGE=1` itself. Inspect the printed status lines and confirm the command exits without an assertion error.

### Upload-format audit

This audit builds payloads in memory, stores temporary application data under a temporary directory, and cleans that directory when it completes:

```powershell
python audit_upload_formats.py
```

It checks representative delimited text, JSON variants, Excel, Parquet, Feather, Arrow IPC, ORC, compressed text, and ZIP uploads, plus the legacy XLS reader dependency.

### Deterministic feature audit

`audit_all_features.py` writes `validation_random_dataset.csv` beside the script and uses the application's configured storage. It deletes the test dataset when it completes, but the generated fixture remains. To avoid touching existing runtime data, point storage at a dedicated temporary path and run from a disposable copy of the repository so the fixture cannot overwrite an existing file:

```powershell
$env:PANDA_DATA_DIR = Join-Path $env:TEMP "panda-feature-audit-data"
python audit_all_features.py
```

The audit independently checks dataset summaries, selection behavior, aggregations, pivot/crosstab values, time/segment analysis, outlier and correlation calculations, chart recipes, exports, and saved-dataset operations. Review its output for a final `PASS` line. Tests were not run as part of this documentation update.

## 8. Troubleshooting and Data Cautions

- **`py -3.15` is not found:** check which Python versions the launcher sees with `py -0p`. The repository does not enforce a specific runtime; choose an installed version that can install the pinned dependencies.
- **Reader-engine error:** Parquet, Feather, Arrow, and ORC need PyArrow; XLS needs `xlrd`. Re-run the appropriate requirements install in the active virtual environment.
- **Charts do not appear:** the interface downloads ECharts from jsDelivr. Check network access and browser developer-console errors. Core tabular API calculations do not use ECharts.
- **Fonts differ:** fonts are requested from Google Fonts; offline use falls back to local browser fonts.
- **Saved Datasets is absent:** in browser mode, set `PANDA_LOCAL_STORAGE=1` before starting the server. Desktop mode sets this automatically.
- **Saved data appears missing:** confirm `PANDA_DATA_DIR` points to the same location used when the dataset was uploaded. The default data folder is local to the application directory.
- **Data types differ after reopen:** saved rows are normalized to CSV and reloaded through `pandas.read_csv`, which infers types again. Inspect date/numeric columns after reopening if type fidelity matters.
- **Dataset fails to upload:** confirm the extension is supported and, for ZIP, that it contains at least one supported file. The reader uses the first supported member in the archive.
