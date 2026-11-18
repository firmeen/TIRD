"""Network-free regression tests for section scoping and concurrent collection."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tird_collector as api
from tird_scopes import (
    ScopedDocument, ParallelArchive, configured_scopes, collect_catalogs,
    diagnose_scope_routing,
)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = api.Config(
            authorization="TEST_ONLY", cookie="FAKE=VALUE",
            referer="https://tirdmember.tird.insure/Content?_mId=advisor",
            curl_executable="curl.exe", output=Path(self.temp.name),
            parallel=3, delay=0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_both_scopes_are_independent(self):
        with patch.dict(os.environ, {
            "TIRD_ADVISOR_REFERER": "https://tirdmember.tird.insure/Content?_mId=A",
            "TIRD_INSIGHTS_REFERER": "https://tirdmember.tird.insure/Content?_mId=I",
            "TIRD_INSIGHTS_COOKIE": "INSIGHTS=SESSION",
        }):
            scopes = configured_scopes(self.base)
        self.assertEqual([s.slug for s in scopes], ["advisor", "insights"])
        self.assertEqual(scopes[0].config.output.name, "advisor")
        self.assertEqual(scopes[1].config.output.name, "insights")
        self.assertEqual(scopes[1].config.cookie, "INSIGHTS=SESSION")
        self.assertNotEqual(scopes[0].config.referer, scopes[1].config.referer)

    def test_missing_insights_is_explained(self):
        with patch.dict(os.environ, {"TIRD_INSIGHTS_REFERER": ""}):
            scopes = configured_scopes(self.base)
        class FakeClient:
            def __init__(self, *args, **kwargs): pass
            def get_services(self): return []
        with patch.object(api, "TIRDClient", FakeClient):
            docs, errors = collect_catalogs(scopes)
        self.assertIsNone(scopes[1].config)
        self.assertEqual(docs, [])
        self.assertIn("Not configured", errors["insights"])

    def test_duplicate_catalog_is_not_mislabeled(self):
        with patch.dict(os.environ, {"TIRD_INSIGHTS_REFERER": "https://tirdmember.tird.insure/Content?_mId=I"}):
            scopes = configured_scopes(self.base)
        class FakeClient:
            def __init__(self, *args, **kwargs): pass
            def get_services(self): return [{"Service_id": "123", "Service_name": "Test"}]
            def get_service_key(self, service_id): return "test-workspace"
        with patch.object(api, "TIRDClient", FakeClient):
            documents, errors = collect_catalogs(scopes)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].scope, "advisor")
        self.assertIn("insights", errors)

    def test_aba_routing_reuses_credentials_and_switches_only_referer(self):
        with patch.dict(os.environ, {
            "TIRD_ADVISOR_REFERER": "https://tirdmember.tird.insure/Content?_mId=A",
            "TIRD_INSIGHTS_REFERER": "https://tirdmember.tird.insure/Content?_mId=I",
            "TIRD_INSIGHTS_COOKIE": "",
            "TIRD_INSIGHTS_AUTHORIZATION": "",
        }):
            scopes = configured_scopes(self.base)
        calls = []
        class FakeClient:
            def __init__(self, config, *args, **kwargs):
                self.config = config
            def get_services(self):
                calls.append((
                    self.config.referer, self.config.cookie,
                    self.config.authorization,
                ))
                if self.config.referer.endswith("=A"):
                    return [{"Service_id": "advisor-1"}, {"Service_id": "advisor-2"}]
                return [{"Service_id": "insights-1"}]
        with patch.object(api, "TIRDClient", FakeClient):
            result = diagnose_scope_routing(scopes)
        self.assertEqual([entry[0][-1] for entry in calls], ["A", "I", "A"])
        self.assertTrue(all(c[1] == "FAKE=VALUE" for c in calls))
        self.assertTrue(all(c[2] == "TEST_ONLY" for c in calls))
        self.assertEqual((result.advisor_first, result.insights, result.advisor_repeat), (2, 1, 2))
        self.assertEqual(result.overlap, 0)
        self.assertTrue(result.consistent)
        self.assertTrue(result.shared_credentials)

    def test_aba_detects_server_side_session_switch(self):
        with patch.dict(os.environ, {
            "TIRD_ADVISOR_REFERER": "https://tirdmember.tird.insure/Content?_mId=A",
            "TIRD_INSIGHTS_REFERER": "https://tirdmember.tird.insure/Content?_mId=I",
        }):
            scopes = configured_scopes(self.base)
        counters = {"A": 0}
        class FakeClient:
            def __init__(self, config, *args, **kwargs):
                self.referer = config.referer
            def get_services(self):
                if self.referer.endswith("=I"):
                    return [{"Service_id": "insights-1"}]
                counters["A"] += 1
                return [{"Service_id": f"advisor-{counters['A']}"}]
        with patch.object(api, "TIRDClient", FakeClient):
            result = diagnose_scope_routing(scopes)
        self.assertFalse(result.advisor_stable)
        self.assertFalse(result.consistent)

    def test_partially_overlapping_catalog_is_not_mislabeled(self):
        with patch.dict(os.environ, {
            "TIRD_ADVISOR_REFERER": "https://tirdmember.tird.insure/Content?_mId=A",
            "TIRD_INSIGHTS_REFERER": "https://tirdmember.tird.insure/Content?_mId=I",
        }):
            scopes = configured_scopes(self.base)
        class FakeClient:
            def __init__(self, config, *args, **kwargs):
                self.referer = config.referer
            def get_services(self):
                ids = ["A", "SHARED"] if self.referer.endswith("=A") else ["I", "SHARED"]
                return [{"Service_id": i} for i in ids]
            def get_service_key(self, service_id):
                return "workspace"
        with patch.object(api, "TIRDClient", FakeClient):
            documents, errors = collect_catalogs(scopes)
        self.assertEqual([item.service["Service_id"] for item in documents], ["A", "SHARED"])
        self.assertIn("overlaps", errors["insights"])

    def test_same_routing_inputs_are_detected_without_duplicate_request(self):
        same = "https://tirdmember.tird.insure/Content?_mId=A"
        with patch.dict(os.environ, {
            "TIRD_ADVISOR_REFERER": same,
            "TIRD_INSIGHTS_REFERER": same,
            "TIRD_INSIGHTS_COOKIE": "",
            "TIRD_INSIGHTS_AUTHORIZATION": "",
        }):
            scopes = configured_scopes(self.base)
        calls = []
        class FakeClient:
            def __init__(self, *args, **kwargs): pass
            def get_services(self):
                calls.append(1)
                return [{"Service_id": "A"}]
            def get_service_key(self, service_id): return "workspace"
        with patch.object(api, "TIRDClient", FakeClient):
            documents, errors = collect_catalogs(scopes)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(documents), 1)
        self.assertIn("same Referer", errors["insights"])

    def test_parallel_download_uses_concurrency_and_separate_manifests(self):
        with patch.dict(os.environ, {"TIRD_INSIGHTS_REFERER": "https://tirdmember.tird.insure/Content?_mId=I"}):
            scopes = configured_scopes(self.base)
        active = 0
        maximum = 0
        lock = threading.Lock()
        class FakeManifest:
            def completed(self, service_id): return False
        class FakeCollector:
            def __init__(self, config, force=False):
                self.config = config
                self.manifest = FakeManifest()
                self.client = type("FakeClient", (), {"transport": type("FakeTransport", (), {})()})()
            def download_worker(self, row):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.06)
                with lock:
                    active -= 1
                return api.DownloadResult(service_id=row["Service_id"], title="Fake", status="success")
            def process_result(self, result): pass
        with patch.object(api, "TIRDCollector", FakeCollector):
            archive = ParallelArchive(scopes, self.base)
            docs = [
                ScopedDocument("advisor", "Advisor", {"Service_id": "A"}),
                ScopedDocument("insights", "Insights", {"Service_id": "I"}),
                ScopedDocument("advisor", "Advisor", {"Service_id": "A2"}),
            ]
            stats = archive.collect(docs)
        self.assertEqual(stats["downloaded"], 3)
        self.assertGreaterEqual(maximum, 2)
        self.assertIs(archive.collectors["advisor"].client.rate_limiter, archive.collectors["insights"].client.rate_limiter)
        self.assertNotEqual(archive.collectors["advisor"].config.output, archive.collectors["insights"].config.output)

    def test_existing_textual_log_api_not_shadowed(self):
        src = (Path(__file__).resolve().parents[1] / "tird_ui.py").read_text(encoding="utf-8")
        self.assertNotIn("def log(", src)
        self.assertNotIn("self.log(", src)
        self.assertIn("def write_log(", src)


if __name__ == "__main__":
    unittest.main()
