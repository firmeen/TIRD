"""No-network live session detection and polling regression tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tird_collector as api
from tird_live import (
    ADVISOR_IDS, INSIGHTS_IDS, LABELS, classify_catalog,
    catalog_signature, known_scope_for_id, fetch_live_snapshot,
    snapshot_documents,
)
from tird_scopes import ParallelArchive

# A live browser is NEVER opened by any of these tests.


class LiveCatalogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = api.Config(
            authorization="TEST_ONLY", cookie="TEST=FAKE",
            referer="https://tirdmember.tird.insure/Content?_mId=shared",
            curl_executable="curl.exe",
            output=Path(self.folder.name), parallel=3, delay=0,
        )

    def tearDown(self):
        self.folder.cleanup()

    def test_verified_reference_sets_are_disjoint_and_complete(self):
        self.assertEqual(len(ADVISOR_IDS), 3)
        self.assertEqual(len(INSIGHTS_IDS), 36)
        self.assertFalse(ADVISOR_IDS & INSIGHTS_IDS)
        self.assertEqual(
            known_scope_for_id("70daccca-c63a-4e62-9acc-062c931534df"),
            "advisor",
        )
        self.assertEqual(known_scope_for_id("33"), "insights")

    def test_classifies_by_id_not_referer_or_count(self):
        advisor = [{"Service_id": x} for x in ADVISOR_IDS]
        insights = [{"Service_id": x} for x in INSIGHTS_IDS]
        self.assertEqual(classify_catalog(advisor).scope, "advisor")
        self.assertEqual(classify_catalog(insights).scope, "insights")
        self.assertEqual(classify_catalog(insights[:3]).scope, "insights")
        future = advisor + [{"Service_id": "new-advisor-id"}]
        self.assertEqual(classify_catalog(future).scope, "advisor")

    def test_unknown_and_mixed_id_sets_are_not_downloadable(self):
        self.assertFalse(classify_catalog([{"Service_id": "brand-new-id"}]).trusted)
        mixed = [
            {"Service_id": next(iter(ADVISOR_IDS))},
            {"Service_id": next(iter(INSIGHTS_IDS))},
        ]
        self.assertFalse(classify_catalog(mixed).trusted)
        scope, documents, classification = snapshot_documents(self.base, mixed)
        self.assertIsNone(scope)
        self.assertTrue(all(d.scope == "unknown" for d in documents))
        self.assertEqual(classification.scope, "unknown")

    def test_live_snapshot_preserves_credential_values_and_folder(self):
        sample = [{"Service_id": next(iter(INSIGHTS_IDS))}]
        scope, docs, result = snapshot_documents(self.base, sample)
        self.assertEqual(scope.slug, "insights")
        self.assertEqual(result.scope, "insights")
        self.assertEqual(scope.config.output, self.base.output / "insights")
        self.assertEqual(scope.config.referer, self.base.referer)
        self.assertEqual(scope.config.cookie, self.base.cookie)
        self.assertEqual(scope.config.authorization, self.base.authorization)
        self.assertEqual(docs[0].scope, "insights")

    def test_identical_catalog_does_not_trigger_repaint(self):
        sample = [{"Service_id": "1", "Service_name": "Title"}]
        self.assertEqual(catalog_signature(sample), catalog_signature(list(sample)))
        self.assertNotEqual(catalog_signature(sample), catalog_signature([
            {"Service_id": "1", "Service_name": "Updated title"}
        ]))

    def test_one_catalog_request_only_without_details_or_download(self):
        calls = []
        class FakeClient:
            def __init__(self, config, stop):
                calls.append(("init", config.referer))
            def get_services(self, *, quiet=False):
                calls.append(("get_services", quiet))
                return [{"Service_id": next(iter(ADVISOR_IDS))}]
            def get_service_key(self, *a):
                self.fail("No detail requests should be made")
        with patch.object(api, "TIRDClient", FakeClient):
            scope, docs, identity = fetch_live_snapshot(self.base)
        self.assertEqual([x[0] for x in calls], ["init", "get_services"])
        self.assertTrue(calls[-1][1])
        self.assertEqual(scope.slug, "advisor")
        self.assertEqual(len(docs), 1)

    def test_legacy_root_manifest_uses_verified_category(self):
        # Previously saved wrong-category documents remain valid and resume
        # in their ACTUAL category without moving or redownloading the PDF.
        existing = self.base.output / "legacy.pdf"
        existing.write_bytes(b"%PDF-1.4\nmock")
        legacy = api.Manifest(self.base.output)
        service_id = next(iter(INSIGHTS_IDS))
        legacy.upsert({
            "service_id": service_id, "status": "success",
            "file_path": "legacy.pdf", "file_size": existing.stat().st_size,
        })
        from tird_live import live_scope
        scopes = [live_scope(self.base, "advisor"), live_scope(self.base, "insights")]
        archive = ParallelArchive(scopes, self.base)
        self.assertTrue(archive.already_completed("insights", service_id))
        self.assertFalse(archive.already_completed("advisor", service_id))


class LiveUIContractTests(unittest.TestCase):
    def test_textual_live_ui_must_never_override_internal_log(self):
        from tird_ui import TIRDApp, LIVE_REFRESH_SECONDS
        self.assertEqual(LIVE_REFRESH_SECONDS, 1.0)
        args = type("Args", (), {"output": Path("TIRD_Newsletter")})()
        app = TIRDApp(args, api)
        self.assertTrue(callable(app.log.system))
        self.assertTrue(callable(app.write_log))

    def test_poll_pause_uses_freeze_snapshot_and_no_browser_driver(self):
        src = (Path(__file__).resolve().parents[1] / "tird_ui.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("self.set_interval(LIVE_REFRESH_SECONDS", src)
        self.assertIn("if self.busy or self.poll_paused or self.refreshing", src)
        self.assertIn("self.pending_download = snapshot", src)
        self.assertIn("self._start_download(selected)", src)
        self.assertNotIn("playwright", src.lower())
        self.assertNotIn("selenium", src.lower())


if __name__ == "__main__":
    unittest.main()
