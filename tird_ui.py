"""Textual 8.x terminal for both TIRD sections and bounded parallel PDFs."""
from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, ProgressBar, RichLog, Select, Static

from tird_scopes import configured_scopes, collect_catalogs, diagnose_scope_routing, ParallelArchive, ScopedDocument


class TIRDApp(App):
    TITLE = "TIRD Collector"
    SUB_TITLE = "Advisor + Business Insights / Parallel PDF archive"
    BINDINGS = [
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+t", "verify_routing", "Verify A/B/A"),
        ("ctrl+d", "download_selected", "Download selected"),
        ("ctrl+o", "open_output", "Open folder"),
        ("ctrl+s", "stop", "Stop"),
        ("ctrl+q", "quit", "Quit"),
    ]
    CSS = """
    Screen { background: #10171e; color: #dbe5ea; }
    Header, Footer { background: #192933; color: #c3d0d7; }
    #main { height: 1fr; padding: 0 2 1 2; }
    #brand { height: 3; margin-top: 1; padding: 0 2; background: #1b2c36;
             border-left: thick #b6a17d; color: #eae8df; text-style: bold;
             content-align: left middle; }
    #status { height: 3; margin-top: 1; padding: 0 2; background: #172530;
              border: solid #2b4250; content-align: left middle; }
    #toolbar { height: 3; margin-top: 1; }
    Button { margin-right: 1; color: #f0f4f6; background: #315366; text-style: bold; }
    Button:hover { background: #3d6b80; }
    Button:disabled { opacity: 0.45; }
    #download_selected, #download_all { background: #24675c; }
    #verify { background: #665637; }
    #stop { background: #70474b; }
    #filters { height: 5; margin-top: 1; }
    .field { width: 1fr; margin-right: 1; }
    .field Label { height: 1; color: #b1c0ca; }
    Input { height: 3; background: #152530; border: tall #2f4553; color: #eff4f6; }
    Input:focus { border: tall #b6a17d; }
    Select { height: 3; background: #152530; }
    #summary { height: 3; margin-top: 1; padding: 0 2; background: #1b2b36;
               border: solid #304653; content-align: left middle; }
    #catalog_box { height: 1fr; min-height: 8; margin-top: 1; border: solid #304653; }
    #catalog_heading { height: 2; background: #1b2b36; color: #d0bd99;
                       text-style: bold; padding: 0 1; content-align: left middle; }
    #catalog { height: 1fr; background: #111f28; }
    DataTable > .datatable--header { background: #253a47; text-style: bold; }
    DataTable > .datatable--cursor { background: #385f70; }
    #progress_box { height: 4; margin-top: 1; padding: 0 1; border: solid #304653; }
    #progress_caption { height: 1; }
    #progress { height: 2; }
    #log_box { height: 8; margin-top: 1; border: solid #304653; }
    #log_heading { height: 1; background: #1b2b36; color: #d0bd99;
                   text-style: bold; padding-left: 1; }
    #activity { height: 1fr; background: #111f28; padding: 0 1; }
    """

    def __init__(self, args: Any, collector_api: Any) -> None:
        super().__init__()
        self.args = args
        self.api = collector_api
        self.documents: list[ScopedDocument] = []
        self.scopes = []
        self.errors: dict[str, str] = {}
        self.selected: set[str] = set()
        self.states: dict[str, str] = {}
        self.loaded = False
        self.busy = False
        self.refreshing = False
        self.archive: ParallelArchive | None = None
        self.stop_pending = False
        self.run_id = 0
        self.stats = {"downloaded": 0, "skipped": 0, "failed": 0, "cancelled": 0}
        self.output = Path(args.output)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="main"):
            yield Static("TIRD / NEWSLETTER COLLECTOR                 ADVISOR + BUSINESS INSIGHTS", id="brand")
            yield Static("SESSION / NOT CHECKED", id="status")
            with Horizontal(id="toolbar"):
                yield Button("REFRESH", id="refresh")
                yield Button("VERIFY A/B/A", id="verify")
                yield Button("DOWNLOAD SELECTED", id="download_selected")
                yield Button("DOWNLOAD ALL", id="download_all")
                yield Button("STOP", id="stop")
                yield Button("OPEN FOLDER", id="folder")
            with Horizontal(id="filters"):
                with Vertical(classes="field"):
                    yield Label("CATEGORY")
                    yield Select(
                        [("All sections", "both"), ("Actuarial Advisory", "advisor"), ("Business Insights", "insights")],
                        value="both", allow_blank=False, id="scope_filter",
                    )
                with Vertical(classes="field"):
                    yield Label("SEARCH")
                    yield Input(placeholder="Title, issue, keyword...", id="search")
                with Vertical(classes="field"):
                    yield Label("YEAR")
                    yield Input(placeholder="All / 2026 / 2569", id="year")
            yield Static("ADVISOR 0   INSIGHTS 0   SELECTED 0   SAVED 0   SKIPPED 0   FAILED 0", id="summary")
            with Vertical(id="catalog_box"):
                yield Static("DOCUMENT CATALOG  /  ENTER TO SELECT  /  CATEGORY-SPECIFIC ARCHIVES", id="catalog_heading")
                yield DataTable(id="catalog", cursor_type="row", zebra_stripes=True)
            with Vertical(id="progress_box"):
                yield Static("READY / Waiting for connection check", id="progress_caption")
                yield ProgressBar(id="progress", total=1, show_eta=False, show_percentage=True)
            with Vertical(id="log_box"):
                yield Static("ACTIVITY", id="log_heading")
                yield RichLog(id="activity", markup=True, wrap=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#catalog", DataTable)
        for title, width in (("PICK", 5), ("CATEGORY", 19), ("ISSUE", 7), ("DATE", 12), ("NEWSLETTER", 68), ("STATUS", 13)):
            table.add_column(title, key=title, width=width)
        self._buttons()
        self.write_log("Terminal ready. Both sections use authorized local .env configuration.")
        self.set_timer(0.25, self.action_refresh)

    # Do NOT name this method log(): it would shadow Textual App.log.system.
    def write_log(self, message: str, level: str = "info") -> None:
        colors = {"info": "#b8cbd3", "ok": "#85d6ba", "warn": "#e7c48e", "error": "#f1a1a4"}
        color = colors.get(level, colors["info"])
        self.query_one("#activity", RichLog).write(
            f"[dim]{datetime.now():%H:%M:%S}[/dim] [{color}]{escape(str(message))}[/{color}]"
        )

    def _session(self, state: str, detail: str = "") -> None:
        view = Text("SESSION / ", style="#dce6e8")
        view.append(state, style={"CONNECTED": "bold #85d6ba", "CHECKING": "bold #e7c48e", "PARTIAL": "bold #e7c48e", "ERROR": "bold #f1a1a4"}.get(state, "bold"))
        if detail:
            view.append("     " + detail[:190], style="#b9c9d1")
        self.query_one("#status", Static).update(view)

    def _buttons(self) -> None:
        self.query_one("#refresh", Button).disabled = self.busy or self.refreshing
        self.query_one("#verify", Button).disabled = self.busy or self.refreshing
        self.query_one("#download_selected", Button).disabled = self.busy or self.refreshing or not self.loaded
        self.query_one("#download_all", Button).disabled = self.busy or self.refreshing or not self.loaded
        self.query_one("#stop", Button).disabled = not self.busy

    def _filtered(self) -> list[ScopedDocument]:
        category = self.query_one("#scope_filter", Select).value
        query = self.query_one("#search", Input).value.casefold().strip()
        year_text = self.query_one("#year", Input).value.strip()
        year = int(year_text) if year_text.isdecimal() else None
        if year is not None and year >= 2400:
            year -= 543
        matched = []
        for document in self.documents:
            if category in ("advisor", "insights") and document.scope != category:
                continue
            subtitle = self.api.service_subtitle_of(document.service)
            title = self.api.service_title_of(document.service)
            published = self.api.parse_thai_date(subtitle)
            if year is not None and (published is None or published.year != year):
                continue
            if query and query not in (title + " " + subtitle).casefold():
                continue
            matched.append(document)
        return matched

    def _summary(self) -> None:
        counts = Counter(row.scope for row in self.documents)
        self.query_one("#summary", Static).update(
            f"ADVISOR {counts['advisor']}    INSIGHTS {counts['insights']}    "
            f"VISIBLE {len(self._filtered())}    SELECTED {len(self.selected)}    "
            f"SAVED {self.stats['downloaded']}    SKIPPED {self.stats['skipped']}    "
            f"FAILED {self.stats['failed']}"
        )

    def _render_catalog(self) -> None:
        table = self.query_one("#catalog", DataTable)
        table.clear()
        for document in self._filtered():
            subtitle = self.api.service_subtitle_of(document.service)
            published = self.api.parse_thai_date(subtitle)
            table.add_row(
                "●" if document.key in self.selected else "○",
                document.label,
                self.api.parse_edition(subtitle) or "—",
                published.strftime("%Y-%m-%d") if published else "—",
                self.api.service_title_of(document.service),
                self.states.get(document.key, "READY"),
                key=document.key,
            )
        self._summary()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"search", "year"} and self.is_mounted:
            self._render_catalog()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "scope_filter" and self.is_mounted:
            self._render_catalog()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        document_key = str(event.row_key.value)
        if document_key in self.selected:
            self.selected.remove(document_key)
        else:
            self.selected.add(document_key)
        self.query_one("#catalog", DataTable).update_cell(
            document_key, "PICK", "●" if document_key in self.selected else "○"
        )
        self._summary()

    def action_refresh(self) -> None:
        if self.busy or self.refreshing:
            return
        self.refreshing = True
        self.loaded = False
        self._buttons()
        self._session("CHECKING", "Advisor + Business Insights")
        self.write_log("Fetching section catalogs with curl.exe...")
        self._load_worker()

    def action_verify_routing(self) -> None:
        """Run three live catalog requests without any PDF downloads."""
        if self.busy or self.refreshing:
            return
        self.refreshing = True
        self._buttons()
        self._session("CHECKING", "Advisor -> Insights -> Advisor")
        self.write_log("Verifying the two menu Referers using A/B/A catalog requests...")
        self._verify_worker()

    @work(thread=True, group="routing", exclusive=True)
    def _verify_worker(self) -> None:
        try:
            base = self.api.Config.load(self.args)
            scopes = configured_scopes(base, "both")
            result = diagnose_scope_routing(scopes)
            self.call_from_thread(self._verify_done, result, None)
        except Exception as exc:
            self.call_from_thread(
                self._verify_done, None, f"{type(exc).__name__}: {exc}"
            )

    def _verify_done(self, result: Any | None, error: str | None) -> None:
        self.refreshing = False
        self._buttons()
        if error:
            self._session("ERROR", "A/B/A routing failed; see activity log")
            self.write_log(f"ROUTING TEST ERROR: {error}", "error")
            return
        level = "ok" if result.consistent else "warn"
        state = "CONNECTED" if result.consistent else "PARTIAL"
        self._session(
            state,
            f"A {result.advisor_first} / B {result.insights} / "
            f"A {result.advisor_repeat}, overlap={result.overlap}"
        )
        self.write_log(
            f"A/B/A {'PASS' if result.consistent else 'FAIL'}: "
            f"Advisor {result.advisor_first} -> "
            f"Insights {result.insights} -> "
            f"Advisor {result.advisor_repeat}; "
            f"overlap={result.overlap}; stable={result.advisor_stable}; "
            f"shared_credentials={result.shared_credentials}. No PDFs downloaded.",
            level,
        )
        if not result.consistent:
            self.write_log(
                "Do not assume section labels are correct; confirm each "
                "menu Referer and inspect requests made before GetService.",
                "warn",
            )

    @work(thread=True, group="catalog", exclusive=True)
    def _load_worker(self) -> None:
        try:
            base = self.api.Config.load(self.args)
            scopes = configured_scopes(base, self.args.scope)
            documents, errors = collect_catalogs(scopes)
            states = {}
            legacy_manifest = self.api.Manifest(base.output, verify_existing=base.verify_existing)
            for scope in scopes:
                if scope.config is None:
                    continue
                manifest = self.api.Manifest(scope.config.output, verify_existing=base.verify_existing)
                for document in documents:
                    if document.scope == scope.slug:
                        service_id = self.api.service_id_of(document.service)
                        if manifest.completed(service_id) or (scope.slug == "advisor" and legacy_manifest.completed(service_id)):
                            states[document.key] = "SAVED"
            self.call_from_thread(self._load_done, scopes, documents, states, errors, base.output, None)
        except Exception as exc:
            self.call_from_thread(self._load_done, [], [], {}, {}, self.output, f"{type(exc).__name__}: {exc}")

    def _load_done(self, scopes: list, documents: list, states: dict, errors: dict, output: Path, error: str | None) -> None:
        self.refreshing = False
        if error:
            self._session("ERROR", "See activity log")
            self.write_log(error, "error")
        else:
            self.scopes = scopes
            self.documents = documents
            self.states = states
            self.errors = errors
            self.output = output
            self.selected.intersection_update({d.key for d in documents})
            self.loaded = bool(documents)
            counts = Counter(doc.scope for doc in documents)
            state = "PARTIAL" if errors else "CONNECTED"
            self._session(state, f"Advisor {counts['advisor']} / Insights {counts['insights']} / workers {self.args.parallel}")
            for name, message in errors.items():
                self.write_log(f"{name.upper()}: {message}", "warn")
            self.write_log(f"Catalog ready: {len(documents)} documents from {len(scopes)-len(errors)} sections", "ok" if not errors else "warn")
            self._render_catalog()
        self._buttons()

    def _begin(self, documents: list[ScopedDocument]) -> None:
        if self.busy or self.refreshing:
            return
        if not self.loaded:
            self.write_log("Refresh catalog before downloading.", "warn")
            return
        if not documents:
            self.write_log("No documents selected.", "warn")
            return
        self.run_id += 1
        self.busy = True
        self.stats = {"downloaded": 0, "skipped": 0, "failed": 0, "cancelled": 0}
        self.archive = None
        self.stop_pending = False
        self._buttons()
        self.query_one("#progress", ProgressBar).update(total=len(documents), progress=0)
        self.query_one("#progress_caption", Static).update(f"RUNNING / 0 of {len(documents)} / {self.args.parallel} parallel workers")
        self.write_log(f"Parallel download: {len(documents)} documents / {self.args.parallel} workers")
        self._download_worker(self.run_id, list(documents))

    def action_download_selected(self) -> None:
        self._begin([d for d in self._filtered() if d.key in self.selected])

    def action_download_all(self) -> None:
        self._begin(self._filtered())

    def action_stop(self) -> None:
        if self.busy:
            self.stop_pending = True
            if self.archive is not None:
                self.archive.stop()
            self.write_log("Stop requested; active transfers may complete first.", "warn")

    @work(thread=True, group="downloads", exclusive=True)
    def _download_worker(self, run_id: int, documents: list[ScopedDocument]) -> None:
        try:
            base = self.api.Config.load(self.args)
            archive = ParallelArchive(self.scopes, base, force=self.args.force)
            self.call_from_thread(self._set_archive, run_id, archive)
            def on_result(document: ScopedDocument, state: str, details: str) -> None:
                self.call_from_thread(self._download_result, run_id, document.key, state, details)
            stats = archive.collect(documents, notify=on_result)
            self.call_from_thread(self._download_done, run_id, stats, None)
        except Exception as exc:
            self.call_from_thread(self._download_done, run_id, None, f"{type(exc).__name__}: {exc}")

    def _set_archive(self, run_id: int, archive: ParallelArchive) -> None:
        if run_id == self.run_id:
            self.archive = archive
            if self.stop_pending:
                archive.stop()

    def _download_result(self, run_id: int, key: str, state: str, details: str) -> None:
        if run_id != self.run_id:
            return
        self.states[key] = state
        if state == "SAVED":
            self.stats["downloaded"] += 1
        elif state == "SKIPPED":
            self.stats["skipped"] += 1
        elif state == "FAILED":
            self.stats["failed"] += 1
        elif state == "CANCELLED":
            self.stats["cancelled"] += 1
        completed = sum(self.stats.values())
        self.query_one("#progress", ProgressBar).update(progress=completed)
        self.query_one("#progress_caption", Static).update(f"{state} / {completed} documents processed")
        self.write_log(f"{state}: {key} {details[:140]}", "ok" if state == "SAVED" else "warn" if state in ("FAILED", "CANCELLED") else "info")
        self._render_catalog()

    def _download_done(self, run_id: int, stats: dict | None, error: str | None) -> None:
        if run_id != self.run_id:
            return
        self.busy = False
        self.archive = None
        self._buttons()
        if error:
            self.query_one("#progress_caption", Static).update("ERROR / See activity log")
            self.write_log(error, "error")
        else:
            label = "STOPPED" if stats["cancelled"] else "DONE"
            self.query_one("#progress_caption", Static).update(
                f"{label} / {stats['downloaded']} saved / {stats['skipped']} skipped / {stats['failed']} failed"
            )
            self.write_log(f"PDF archive: {self.output}", "ok")
        self._summary()

    def action_open_output(self) -> None:
        try:
            self.output.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(self.output))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.output)])
        except OSError as exc:
            self.write_log(f"Could not open output: {exc}", "error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "refresh": self.action_refresh,
            "verify": self.action_verify_routing,
            "download_selected": self.action_download_selected,
            "download_all": self.action_download_all,
            "stop": self.action_stop,
            "folder": self.action_open_output,
        }
        action = actions.get(event.button.id)
        if action:
            action()


def run_app(args: Any, collector_api: Any) -> int:
    TIRDApp(args, collector_api).run()
    return 0
