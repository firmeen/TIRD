# TIRD Newsletter Collector

Desktop terminal collector for **Actuarial Advisory** and **Business Insights** research publications. It uses the website's existing, authorized session, `curl.exe` for HTTP, and **Textual/Rich** for the UI. Parallel PDF download uses `ThreadPoolExecutor`, a shared request interval, per-section CSV manifests, PDF-signature checks and SHA256.

## Set up (Windows)

1. Copy `.env.example` to `.env` in this directory.
2. Open TIRD in Chrome and sign in normally.
3. Open **Actuarial Advisory → Article / Research**. In Chrome `F12 → Network → Fetch/XHR`, select `GetService` and copy the `Authorization`, `Cookie`, and **Referer** request headers to `TIRD_AUTHORIZATION`, `TIRD_COOKIE`, `TIRD_REFERER` in `.env`.
4. Open **Business Insights → Article / Research**, select that page's `GetService` request and copy its Referer to `TIRD_INSIGHTS_REFERER`. If that request uses a different session, also set `TIRD_INSIGHTS_COOKIE` and `TIRD_INSIGHTS_AUTHORIZATION`.
5. In PowerShell, execute:

   ```powershell
   python -m pip install -r requirements.txt
   python tird_collector.py
   ```

   You can also double-click `START_TIRD.bat` to open the UI.

**Important:** Two separate pages or different Referers do not necessarily imply two different API results. If both configurations return the same service IDs, the app reports this instead of mislabeling documents. Use DevTools to confirm each section's request/response. If `TIRD_INSIGHTS_REFERER` is missing, the UI still displays Advisor but marks Insights as not configured. The program cannot invent section IDs or acquire credentials automatically.

## Terminal UI

- **Category**: All sections / Actuarial Advisory / Business Insights.
- **Search + year**: filter displayed documents.
- **Enter**: select or deselect a row.
- **Download Selected**: collect selected visible rows.
- **Download All**: collect all visible filtered rows from one or both sections.
- **Stop**: stop pending work; an in-flight request may finish first.
- **Open Folder**: inspect PDF output and manifests.

Activity, section counts, PDF statuses and progress appear in the terminal.

## CLI commands

```powershell
python tird_collector.py --test-only
python tird_collector.py --list --scope both
python tird_collector.py --headless --scope both --parallel 3
python tird_collector.py --headless --scope insights --year 2569 --parallel 3
python tird_collector.py --headless --scope advisor --limit 1
python tird_collector.py --ui
python -m unittest discover -s tests -v
```

`--parallel` defaults to **3**; supports **1–6**. `--delay` defaults to **0.2 seconds minimum between HTTP request starts across all workers**. Do not increase concurrency excessively on this service. `--force` redownloads previously completed PDFs.

## Files

```text
TIRD_Newsletter/
  advisor/
    manifest.csv
    YYYY/MM/*.pdf
  insights/
    manifest.csv
    YYYY/MM/*.pdf
```

Old v4 PDFs under the previous output layout are untouched and recognized from the root manifest as completed Advisor documents. Each category has its own resume manifest and each PDF is validated before being marked saved. The app does not bypass authentication or TLS verification.

**Security:** Never commit `.env`, curl commands containing live cookies, or personal access tokens. Refresh the browser session if credentials have previously been shared. The live two-section API requests and PDF transfers must be verified on your own authorized Windows session.
