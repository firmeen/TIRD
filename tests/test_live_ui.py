"""Textual headless UI test for live session switching and download pause."""
from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import tird_collector as api
from tird_live import ADVISOR_IDS, INSIGHTS_IDS, snapshot_documents
from tird_ui import TIRDApp


class LiveTextualIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_switch_then_pause_during_frozen_download(self):
        with tempfile.TemporaryDirectory() as directory:
            base = api.Config(
                authorization="TEST_ONLY",
                cookie="TEST=FAKE",
                referer="https://tirdmember.tird.insure/Content?_mId=shared",
                curl_executable="curl.exe",
                output=Path(directory),
                parallel=3,
                delay=0,
            )
            args = Namespace(
                output=Path(directory), parallel=3, force=False, scope="both",
                verify_existing=False,
            )
            state = {
                "rows": [{"Service_id": next(iter(INSIGHTS_IDS)), "Service_name": "Insight"}],
                "calls": 0,
                "block": False,
            }
            entered = threading.Event()
            release = threading.Event()
            downloads = []

            def fake_fetch(config):
                state["calls"] += 1
                if state["block"]:
                    entered.set()
                    release.wait(timeout=3)
                return snapshot_documents(config, list(state["rows"]))

            def fake_download(self, run_id, documents):
                downloads.append((run_id, documents))

            async def wait_for(condition, timeout=3.0):
                deadline = time.monotonic() + timeout
                while not condition():
                    if time.monotonic() > deadline:
                        self.fail("Textual UI did not reach expected state")
                    await asyncio.sleep(0.05)

            with (
                patch.object(api.Config, "load", return_value=base),
                patch("tird_ui.fetch_live_snapshot", side_effect=fake_fetch),
                patch.object(TIRDApp, "_download_worker", fake_download),
            ):
                app = TIRDApp(args, api)
                try:
                    async with app.run_test(size=(120, 40)):
                        await wait_for(lambda: app.loaded and app.active_scope == "insights")
                        self.assertEqual(len(app.documents), 1)
                        self.assertEqual(app.documents[0].scope, "insights")

                        # Only current Chrome catalog is visible; no accumulation.
                        state["rows"] = [
                            {"Service_id": next(iter(ADVISOR_IDS)), "Service_name": "Advisor"}
                        ]
                        app.action_refresh()
                        await wait_for(lambda: app.loaded and app.active_scope == "advisor")
                        self.assertEqual(len(app.documents), 1)
                        self.assertEqual(app.documents[0].scope, "advisor")

                        # Start a blocking poll, then click Download while it is
                        # in flight. The displayed Advisor snapshot must win.
                        state["block"] = True
                        app.action_refresh()
                        await wait_for(entered.is_set)
                        app.action_download_all()
                        self.assertTrue(app.poll_paused)
                        self.assertIsNotNone(app.pending_download)
                        self.assertFalse(app.busy)
                        release.set()
                        await wait_for(lambda: app.busy and len(downloads) == 1)
                        self.assertEqual(downloads[0][1][0].scope, "advisor")

                        # A timer tick during download must never request catalog.
                        calls_at_start = state["calls"]
                        await asyncio.sleep(1.15)
                        self.assertEqual(state["calls"], calls_at_start)

                        app._download_done(
                            app.run_id,
                            {"downloaded": 1, "skipped": 0, "failed": 0, "cancelled": 0},
                            None,
                        )
                        self.assertFalse(app.poll_paused)
                        self.assertFalse(app.busy)
                        self.assertFalse(app.loaded)
                        state["block"] = False
                        app.action_refresh()
                        await wait_for(lambda: app.loaded and app.active_scope == "advisor")
                finally:
                    release.set()


if __name__ == "__main__":
    unittest.main()
