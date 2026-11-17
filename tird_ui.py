"""Textual front end for the existing TIRD curl.exe collector.

The HTTP/authorization/download implementation stays in tird_collector.py.
The UI reads the local .env file through Config.load; credentials are neither
shown nor written to UI logs. Work runs off the Textual event loop.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, ProgressBar, RichLog, Static


class TIRDApp(App):
    TITLE = "TIRD Collector"
    SUB_TITLE = "Newsletter archive / curl.exe"
    BINDINGS = [
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+d", "download_selected", "Download selected"),
        ("ctrl+o", "open_output", "Open folder"),
        ("ctrl+s", "stop", "Stop"),
        ("ctrl+q", "quit", "Quit"),
    ]

    CSS = """
    Screen { background: #10171e; color: #dbe5ea; }
    Header, Footer { background: #192933; color: #c3d0d7; }
    #main { height: 1fr; padding: 0 2 1 2; }
    #brand { height: 3; margin-top: 1; padding: 0 2;
             background: #1b2c36; border-left: thick #b6a17d;
             color: #eae8df; text-style: bold; content-align: left middle; }
    #status { height: 3; margin-top: 1; padding: 0 2;
              background: #172530; border: solid #2b4250;
              content-align: left middle; }
    #toolbar { height: 3; margin-top: 1; }
    Button { margin-right: 1; color: #f0f4f6;
             background: #315366; text-style: bold; }
    Button:hover { background: #3d6b80; }
    Button:disabled { opacity: 0.45; }
    #download_selected, #download_all { background: #24675c; }
    #stop { background: #70474b; }
    #filters { height: 4; margin-top: 1; }
    .field { width: 1fr; margin-right: 1; }
    .field Label { height: 1; color: #b1c0ca; }
    Input { height: 3; background: #152530; border: tall #2f4553;
            color: #eff4f6; }
    Input:focus { border: tall #b6a17d; }
    #summary { height: 3; margin-top: 1; padding: 0 2;
               background: #1b2b36; border: solid #304653;
               content-align: left middle; }
    #catalog_box { height: 1fr; min-height: 8; margin-top: 1;
                   border: solid #304653; }
    #catalog_heading { height: 2; background: #1b2b36;
                       color: #d0bd99; text-style: bold;
                       padding: 0 1; content-align: left middle; }
    #catalog { height: 1fr; background: #111f28; }
    DataTable > .datatable--header { background: #253a47; text-style: bold; }
    DataTable > .datatable--cursor { background: #385f70; }
    #progress_box { height: 4; margin-top: 1; padding: 0 1;
                    border: solid #304653; }
    #progress_caption { height: 1; }
    #progress { height: 2; }
    #log_box { height: 8; margin-top: 1; border: solid #304653; }
    #log_heading { height: 1; background: #1b2b36;
                   color: #d0bd99; text-style: bold; padding-left: 1; }
    #activity { height: 1fr; background: #111f28; padding: 0 1; }
    """

    def __init__(self, args: Any, collector_api: Any) -> None:
        super().__init__()
        self.args = args
        self.api = collector_api
        self.services: list[dict] = []
        self.selected: set[str] = set()
        self.states: dict[str, str] = {}
        self.loaded = False
        self.busy = False
        self.refreshing = False
        self.stop_event = threading.Event()
        self.run_id = 0
        self.stats = {"downloaded": 0, "skipped": 0, "failed": 0}
        self.output = Path(args.output)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="main"):
            yield Static("TIRD  /  NEWSLETTER COLLECTOR                         PDF ARCHIVE", id="brand")
            yield Static("SESSION  /  NOT CHECKED", id="status")
            with Horizontal(id="toolbar"):
                yield Button("REFRESH", id="refresh")
                yield Button("DOWNLOAD SELECTED", id="download_selected")
                yield Button("DOWNLOAD ALL", id="download_all")
                yield Button("STOP", id="stop")
                yield Button("OPEN FOLDER", id="folder")
            with Horizontal(id="filters"):
                with Vertical(classes="field"):
                    yield Label("SEARCH NEWSLETTERS")
                    yield Input(placeholder="Title, edition, keyword...", id="search")
                with Vertical(classes="field"):
                    yield Label("YEAR FILTER")
                    yield Input(placeholder="All / 2026 / 2569", id="year")
            yield Static("AVAILABLE 0     SELECTED 0     DOWNLOADED 0     SKIPPED 0     FAILED 0", id="summary")
            with Vertical(id="catalog_box"):
                yield Static("DOCUMENT CATALOG     /     PRESS ENTER TO SELECT", id="catalog_heading")
                yield DataTable(id="catalog", cursor_type="row", zebra_stripes=True)
            with Vertical(id="progress_box"):
                yield Static("READY / Waiting for connection check", id="progress_caption")
                yield ProgressBar(id="progress", total=1, show_eta=False, show_percentage=True)
            with Vertical(id="log_box"):
                yield Static("ACTIVITY", id="log_heading")
                yield RichLog(id="activity", markup=True, highlight=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#catalog", DataTable)
        for name, width in (("PICK", 5), ("ISSUE", 8), ("DATE", 12), ("NEWSLETTER", 72), ("STATUS", 14)):
            table.add_column(name, key=name, width=width)
        self._buttons()
        self.log("Terminal ready. Credentials are loaded from local .env only.")
        # Defer network operations until Textual has rendered the screen.
        self.set_timer(0.25, self.action_refresh)

    def log(self, message: str, level: str = "info") -> None:
        colors = {"info": "#b8cbd3", "ok": "#85d6ba", "warn": "#e7c48e", "error": "#f1a1a4"}
        color = colors.get(level, colors["info"])
        self.query_one("#activity", RichLog).write(
            f"[dim]{datetime.now():%H:%M:%S}[/dim] [{color}]{escape(str(message))}[/{color}]"
        )

    def _session(self, state: str, detail: str = "") -> None:
        view = Text("SESSION  /  ", style="#dce6e8")
        style = {"CONNECTED": "bold #85d6ba", "CHECKING": "bold #e7c48e", "ERROR": "bold #f1a1a4"}.get(state, "bold")
        view.append(state, style=style)
        if detail:
            view.append("     " + detail[:150], style="#b9c9d1")
        self.query_one("#status", Static).update(view)

    def _buttons(self) -> None:
        self.query_one("#refresh", Button).disabled = self.busy or self.refreshing
        self.query_one("#download_selected", Button).disabled = self.busy or self.refreshing or not self.loaded
        self.query_one("#download_all", Button).disabled = self.busy or self.refreshing or not self.loaded
        self.query_one("#stop", Button).disabled = not self.busy

    def _filtered(self) -> list[dict]:
        query = self.query_one("#search", Input).value.casefold().strip()
        year_value = self.query_one("#year", Input).value.strip()
        year = None
        if year_value.isdecimal():
            year = int(year_value)
            year = year - 543 if year >= 2400 else year
        rows = []
        for service in self.services:
            subtitle = self.api.service_subtitle_of(service)
            title = self.api.service_title_of(service)
            published = self.api.parse_thai_date(subtitle)
            if year is not None and (published is None or published.year != year):
                continue
            if query and query not in (title + " " + subtitle).casefold():
                continue
            rows.append(service)
        return rows

    def _summary(self) -> None:
        self.query_one("#summary", Static).update(
            f"AVAILABLE {len(self.services)}     VISIBLE {len(self._filtered())}     "
            f"SELECTED {len(self.selected)}     DOWNLOADED {self.stats['downloaded']}     "
            f"SKIPPED {self.stats['skipped']}     FAILED {self.stats['failed']}"
        )

    def _render_catalog(self) -> None:
        table = self.query_one("#catalog", DataTable)
        table.clear()
        for service in self._filtered():
            service_id = self.api.service_id_of(service)
            subtitle = self.api.service_subtitle_of(service)
            published = self.api.parse_thai_date(subtitle)
            table.add_row(
                "●" if service_id in self.selected else "○",
                self.api.parse_edition(subtitle) or "—",
                published.strftime("%Y-%m-%d") if published else "—",
                self.api.service_title_of(service),
                self.states.get(service_id, "READY"),
                key=service_id,
            )
        self._summary()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"search", "year"}:
            self._render_catalog()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        service_id = str(event.row_key.value)
        if service_id in self.selected:
            self.selected.remove(service_id)
        else:
            self.selected.add(service_id)
        table = self.query_one("#catalog", DataTable)
        table.update_cell(service_id, "PICK", "●" if service_id in self.selected else "○")
        self._summary()

    def action_refresh(self) -> None:
        if self.busy or self.refreshing:
            return
        self.refreshing = True
        self.loaded = False
        self._buttons()
        self._session("CHECKING", "GetService + GetServiceById")
        self.log("Checking authorized TIRD session and loading catalog...")
        self._load_worker()

    @work(thread=True, group="catalog", exclusive=True)
    def _load_worker(self) -> None:
        try:
            config = self.api.Config.load(self.args)
            client = self.api.TIRDClient(config)
            services = client.get_services()
            if services:
                client.get_service_key(self.api.service_id_of(services[0]))
            manifest = self.api.Manifest(config.output)
            downloaded = {
                self.api.service_id_of(s): "SAVED"
                for s in services if manifest.completed(self.api.service_id_of(s))
            }
            self.call_from_thread(self._load_done, services, downloaded, config.output, None)
        except Exception as exc:
            self.call_from_thread(self._load_done, [], {}, self.output, f"{type(exc).__name__}: {exc}")

    def _load_done(self, services: list[dict], states: dict[str, str], output: Path, error: str | None) -> None:
        self.refreshing = False
        if error:
            self._session("ERROR", "See activity log; the app remains open")
            self.log(error, "error")
            self.loaded = bool(self.services)
        else:
            self.services = services
            self.states = states
            self.output = output
            self.selected.intersection_update({self.api.service_id_of(s) for s in services})
            self.loaded = True
            self._session("CONNECTED", f"{len(services)} accessible documents / curl.exe")
            self.log(f"GetService: PASS | GetServiceById: PASS | {len(services)} documents", "ok")
            self._render_catalog()
        self._buttons()

    def _begin(self, services: list[dict]) -> None:
        if self.busy or self.refreshing:
            return
        if not self.loaded:
            self.log("Refresh catalog before downloading.", "warn")
            return
        if not services:
            self.log("No documents selected for this action.", "warn")
            return
        self.stop_event.clear()
        self.run_id += 1
        self.busy = True
        self.stats = {"downloaded": 0, "skipped": 0, "failed": 0}
        self._buttons()
        progress = self.query_one("#progress", ProgressBar)
        progress.update(total=len(services), progress=0)
        self.query_one("#progress_caption", Static).update(f"RUNNING / 0 of {len(services)}")
        self.log(f"Downloading {len(services)} newsletters sequentially...")
        self._download_worker(self.run_id, list(services))

    def action_download_selected(self) -> None:
        self._begin([s for s in self._filtered() if self.api.service_id_of(s) in self.selected])

    def action_download_all(self) -> None:
        # Respect filters, so the user can download one year or search result set.
        self._begin(self._filtered())

    def action_stop(self) -> None:
        if self.busy:
            self.stop_event.set()
            self.log("Stop requested. Current request will finish before stopping.", "warn")

    @work(thread=True, group="download", exclusive=True)
    def _download_worker(self, run_id: int, services: list[dict]) -> None:
        try:
            config = self.api.Config.load(self.args)
            collector = self.api.TIRDCollector(self.api.TIRDClient(config), config, force=self.args.force)
            for index, service in enumerate(services, 1):
                if self.stop_event.is_set():
                    self.call_from_thread(self._finish_download, run_id, "STOPPED", None)
                    return
                service_id = self.api.service_id_of(service)
                previous = dict(collector.stats)
                collector.process_service(service)
                state = (
                    "SAVED" if collector.stats["downloaded"] > previous["downloaded"] else
                    "SKIPPED" if collector.stats["skipped"] > previous["skipped"] else
                    "FAILED"
                )
                self.call_from_thread(self._download_item, run_id, service_id, index, len(services), state)
                if index < len(services) and config.delay > 0 and self.stop_event.wait(config.delay):
                    self.call_from_thread(self._finish_download, run_id, "STOPPED", None)
                    return
            self.call_from_thread(self._finish_download, run_id, "DONE", None)
        except Exception as exc:
            self.call_from_thread(self._finish_download, run_id, "ERROR", f"{type(exc).__name__}: {exc}")

    def _download_item(self, run_id: int, service_id: str, index: int, total: int, state: str) -> None:
        if run_id != self.run_id:
            return
        self.states[service_id] = state
        if state == "SAVED":
            self.stats["downloaded"] += 1
        elif state == "SKIPPED":
            self.stats["skipped"] += 1
        else:
            self.stats["failed"] += 1
        self.query_one("#progress", ProgressBar).update(progress=index)
        self.query_one("#progress_caption", Static).update(f"{state} / {index} of {total}")
        self.log(f"[{index}/{total}] {state} | {service_id}", "ok" if state == "SAVED" else "warn" if state == "FAILED" else "info")
        self._render_catalog()

    def _finish_download(self, run_id: int, state: str, error: str | None) -> None:
        if run_id != self.run_id:
            return
        self.busy = False
        self._buttons()
        self.query_one("#progress_caption", Static).update(
            f"{state} / {self.stats['downloaded']} saved · {self.stats['skipped']} skipped · {self.stats['failed']} failed"
        )
        if error:
            self.log(error, "error")
            if "AuthenticationError" in error:
                self._session("ERROR", "Authentication rejected; refresh your authorized session")
        else:
            self.log(f"Collection {state.lower()}. PDF output: {self.output}", "ok" if state == "DONE" else "warn")
        self._summary()

    def action_open_output(self) -> None:
        try:
            self.output.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(self.output))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.output)])
        except OSError as exc:
            self.log(f"Could not open output: {exc}", "error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "refresh": self.action_refresh,
            "download_selected": self.action_download_selected,
            "download_all": self.action_download_all,
            "stop": self.action_stop,
            "folder": self.action_open_output,
        }
        action = actions.get(event.button.id)
        if action:
            action()


def run_app(args: Any, collector_api: Any) -> int:
    """Launch Textual without replacing the proven v4 curl transport."""
    TIRDApp(args, collector_api).run()
    return 0