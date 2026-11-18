# TIRD Newsletter Collector

A Python collector for two TIRD sections: **Actuarial Advisory / Article Research**
and **Business Insights / Newsletter**. The user controls Chrome. The
**Textual + Rich terminal UI observes the currently active authorized TIRD
session every 1 second** through one `POST /Content/GetService` request.

## How this version works

1. **You open Business Insights in Chrome yourself.** The terminal shows its
   current catalog (36 documents in the supplied November 18 capture), marked
   **Business Insights**, not Advisor.
2. **Click Download All.** Auto Refresh immediately pauses. If a poll was
   already in flight, the collector waits for it to finish, then downloads the
   *last displayed catalog snapshot* using **3 parallel workers** (1–6
   configurable). No catalog polls run while downloading.
3. When the download finishes/stops, Auto Refresh resumes. You then
   **switch Chrome to Actuarial Advisory yourself**.
4. Within the next successful poll, the terminal **replaces** the visible list
   with Advisor's current catalog (3 documents in the supplied capture).
   Click Download All **a second time** to download the Advisor documents.

There is **no browser automation** and **no accumulation of both catalogs in
the UI**. Files from the two separate download rounds are retained in their
respective output folders and manifests.

### Important routing behavior

The actual TIRD server selects a catalog according to server-side session
context associated with the user's latest navigation in Chrome. Changing
`Referer` in an isolated Python request is **not** sufficient to reliably
change categories. This live UI therefore uses the existing authorized
`TIRD_AUTHORIZATION` + `TIRD_COOKIE` session for **one catalog request at a
time**, then **classifies by verified `Service_id` fingerprints**, never by
Referer, number of documents, or the last category displayed.

The initial reference fingerprints contain the **3 Advisor** and **36
Business Insights** IDs from the supplied Network captures dated November
18, 2026. If the API includes new IDs alongside known IDs, the new items
are included under the recognized category. An entirely new/unrecognized
catalog or a mixed-category response is displayed as **UNKNOWN**, and
downloads remain disabled until it can be identified safely. This avoids
saving Insights under the Advisor label.

One request starts each 1-second tick **only if no previous poll is still
running**. Slow requests are never overlapped. If the poll fails, the UI
marks the last catalog stale and disables downloads until the connection
recovers. This doesn't provide a direct link to Chrome and won't necessarily
detect a change instantly if the API is slow.

## Install and start (Windows)

1. In Chrome, sign in to TIRD using your own authorized session.
2. Copy `.env.example` to `.env` (or keep your existing private `.env`).
3. From Chrome DevTools → Network → `GetService` copy the current
   `Authorization` and `Cookie` headers. Also set `TIRD_REFERER`
   to a valid TIRD `/Content?_mId=...` URL from that browser session.
4. Start the app:

```powershell
python -m pip install -r requirements.txt
python tird_collector.py --ui
```

Or double-click `START_TIRD.bat`. Do not publish or commit `.env`.
`TIRD_ADVISOR_REFERER` and `TIRD_INSIGHTS_REFERER` are **NOT required**
for the **live UI**; they belong to the earlier separate-Referer CLI
diagnostic approach. Section-specific Cookie/Authorization values are also
not required when the two pages share one session.

## Controls

- **Auto Refresh (1s):** always on while idle; no separate setup needed.
- **Refresh:** manual immediate poll.
- **Search / Year:** filter the *current* catalog without changing Chrome.
- **Enter:** select/deselect a row.
- **Download Selected / Download All:** download the current section only.
- **Stop:** stop pending transfers; in-flight transfers may finish.
- **Open Folder:** inspect PDFs and manifests.

Only one section is displayed at a time. Download counts apply to the last
download round; saved file statuses also reflect the manifest. If the user
switches Chrome while a download is running, the backend session may change
outside this program's control; **keep Chrome on the same section until that
download is finished**.

## PDF output and existing downloads

```text
TIRD_Newsletter/
  advisor/
    manifest.csv
    YYYY/MM/*.pdf
  insights/
    manifest.csv
    YYYY/MM/*.pdf
```

Downloads use the existing curl.exe engine, `ThreadPoolExecutor`, shared
request rate limiter, retry/timeout handling, atomic writes, PDF signature
checks and SHA256. Prior PDFs in the old root manifest are recognized by
their **verified service ID** and credited to the *correct* section even if
the previous UI had labeled a 36-item Insights catalog as Advisor. Existing
files are not renamed or moved.

## CLI and tests

```powershell
python tird_collector.py --ui --parallel 3
python tird_collector.py --ui --parallel 6
python -m unittest discover -s tests -v
```

Legacy `--list`, `--test-only`, `--scope`, `--headless` and
`--diagnose-scopes` remain from PR #3, but they still implement the old
two-Referer workflow and must **not** be treated as proof that the
server selected the requested section. For the session-driven two-round
workflow, use `--ui`.

**Credentials:** Only use a session you are authorized to access. Credentials
are read from local `.env`, sent to curl through stdin, and never committed.
Do not disable TLS verification or automate interactions with Chrome.

**Validation:** Tests exercise simulated API responses and UI behavior.
The new live 1-second workflow and both real PDF download rounds still need
to be verified against your authorized Windows browser session.
