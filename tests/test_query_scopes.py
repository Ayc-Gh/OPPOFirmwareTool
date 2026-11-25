import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oplus_tool import tracker
from oplus_tool.catalog import Catalog, MANIFEST_EVIDENCE
from vendor.oplus_tracker import tomboy_pro as upstream


class QueryScopeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db_path = Path(temp.name) / 'catalog.db'
        self.model = 'PHY110'
        self.default = '10010111'
        self.other = '10011000'
        self.opts = tracker.QueryOptions(self.model, 'cn', '', suffixes='A')
        self.cfg = upstream.QueryConfig('PHY110_11.F.28', self.model, 'cn', 'manual', '0' * 64)
        self.patch_url = 'https://example.invalid/patch.zip'
        self.full_url = 'https://example.invalid/full.zip'
        self.body = {'status': 'published', 'isRecruit': False, 'opex': {
            'opexVersionName': '17.0Patch01', 'opexPackage': [
                {'businessCode': 'ovl_update_1', 'info': {'autoUrl': self.patch_url}}]}}
        self.response = upstream.QueryResult(True, 200, data={
            'version': 'PHY110_17.0', 'ota_version': 'PHY110_11.H.1', 'raw_body': self.body},
            components=[upstream.ComponentInfo('System', '17.0', self.full_url, self.full_url, '42', '', self.full_url)])

    def packages(self, nvid=None):
        return tracker.normalize_result(self.response, replace(self.cfg, nvid=nvid), '正式版', self.opts)

    def test_migrate_v105_and_keep_same_artifact_per_carrier(self):
        default, _ = self.packages()
        other, _ = self.packages(self.other)
        prior = replace(default, component='prior-component')
        prior_body = asdict(prior)
        prior_body['recipe']['nvid'] = None
        with closing(sqlite3.connect(self.db_path)) as legacy, legacy:
            legacy.execute('CREATE TABLE packages (id TEXT PRIMARY KEY, model TEXT, region TEXT, body TEXT)')
            legacy.executemany('INSERT INTO packages VALUES (?,?,?,?)',
                               [('old-id', self.model, 'cn', json.dumps(asdict(other))),
                                ('old-default-id', self.model, 'cn', json.dumps(prior_body))])
        db = Catalog(self.db_path)
        self.assertEqual([p.id for p in db.list(self.model, 'cn', self.other)], [other.id])
        self.assertEqual([p.id for p in db.list(self.model, 'cn')], [prior.id])
        db.put([default, replace(other, url=self.full_url + '?new-signature')])
        reopened = Catalog(self.db_path)
        self.assertEqual({p.id for p in reopened.list(self.model, 'cn', self.default)}, {default.id, prior.id})
        self.assertNotEqual(default.id, other.id)
        self.assertEqual([p.url for p in reopened.list(self.model, 'cn', self.other)], [self.full_url + '?new-signature'])
        exported = self.db_path.with_suffix('.json')
        reopened.export(self.model, 'cn', exported, self.other)
        rows = json.loads(exported.read_text(encoding='utf-8'))
        self.assertEqual([r['recipe']['nvid'] for r in rows], [self.other])

    def test_replay_uses_only_current_effective_nv_and_keeps_matching_history(self):
        historical = [self.packages(nv)[0] for nv in (None, self.other)]
        for requested, expected in (('', self.default), (self.default, self.default), (self.other, self.other)):
            with self.subTest(nv=requested), patch.object(upstream, 'query_update', return_value=upstream.QueryResult(False, 2004)) as query:
                cancel = Mock()
                cancel.is_set.return_value = cancel.wait.return_value = False
                tracker.query_all(replace(self.opts, nvid=requested), cancel, Mock(), Mock(), Mock(), cached=historical)
                calls = [call.args[0] for call in query.call_args_list]
                self.assertEqual({tracker.effective_nvid(cfg.region, cfg.nvid) for cfg in calls}, {expected})
                self.assertEqual(sum(cfg.ota_version == self.cfg.ota_version for cfg in calls), 1)
        profile = dict(model=self.model, region='cn', nvid=self.other, components='System:OLD', ota=self.cfg.ota_version)
        self.assertFalse(any(cfg.components_input for _, cfg in tracker.build_queries(self.opts, [profile])))

    def test_refresh_rejects_foreign_nv_before_contacting_server(self):
        package, _ = self.packages(self.other)
        downgrade = tracker.normalize_downgrade({'packages': [{'downloadUrl': self.full_url}]},
                                                replace(self.opts, nvid=self.other))[0]
        with patch.object(upstream, 'query_update') as query, patch.object(tracker.downgrade_query, 'query_downgrade') as down:
            for p in (package, downgrade):
                with self.assertRaisesRegex(ValueError, 'NV'):
                    tracker.refresh_package(p, self.opts)
            query.assert_not_called()
            down.assert_not_called()

    def test_opex_keeps_server_base_through_query_inspection_and_refresh(self):
        db = Catalog(self.db_path)
        info = SimpleNamespace(is_delta=False, metadata={})
        with patch.object(tracker, 'build_queries', return_value=[('正式版', self.cfg)]), \
             patch.object(upstream, 'query_update', return_value=self.response), \
             patch('oplus_tool.payload.inspect', return_value=info) as inspect:
            tracker.query_all(self.opts, threading.Event(), Mock(), Mock(), db.put)
            self.assertEqual([c.args[0] for c in inspect.call_args_list], [self.full_url])
            patch_pkg = next(p for p in db.list(self.model, 'cn') if p.component == 'ovl_update_1')
            self.assertEqual(patch_pkg.kind, '补丁包')
            self.assertEqual(patch_pkg.base_version, '适用底包: PHY110_17.0')
            before = asdict(patch_pkg)
            self.assertIs(tracker.refine_package(patch_pkg, info), patch_pkg)
            self.assertEqual(asdict(patch_pkg), before)
            old = replace(patch_pkg, kind='全量包', base_version='', evidence=MANIFEST_EVIDENCE)
            fixed = tracker.refresh_package(old, self.opts)
            self.assertEqual((fixed.kind, fixed.base_version), (patch_pkg.kind, patch_pkg.base_version))
            self.assertEqual(tracker.refresh_package(patch_pkg, self.opts).evidence, patch_pkg.evidence)

    def test_requery_repairs_only_the_confirmed_legacy_opex_artifact(self):
        _, patch_pkg = self.packages()
        old = replace(patch_pkg, kind='全量包', evidence=MANIFEST_EVIDENCE, base_version='')
        neighbor = replace(old, component='other-component')
        db = Catalog(self.db_path)
        db.put([old, neighbor])
        db.put([patch_pkg])
        db.put([old])  # A stale in-flight manifest result cannot restore the old classification.
        self.assertEqual({p.id for p in db.list(self.model, 'cn')}, {patch_pkg.id, neighbor.id})

    def test_opex_refresh_recognizes_same_digest_after_cdn_path_rotation(self):
        info = self.body['opex']['opexPackage'][0]['info']
        info['zipHash'] = 'ab12' * 16
        _, cached = self.packages()
        info['autoUrl'] = 'https://new.example.invalid/renewed.zip'
        with patch.object(upstream, 'query_update', return_value=self.response):
            refreshed = tracker.refresh_package(cached, self.opts)
            self.assertEqual((refreshed.kind, refreshed.url), ('补丁包', info['autoUrl']))
            self.body['opex']['opexVersionName'] = '17.0Patch02'
            with self.assertRaisesRegex(RuntimeError, '无法生成'):
                tracker.refresh_package(cached, self.opts)

    def test_normal_query_write_and_stale_manifest_after_cdn_rotation(self):
        db = Catalog(self.db_path)
        _, patch_pkg = self.packages()
        patch_pkg = replace(patch_pkg, sha256='a' * 64, md5='b' * 32)
        old = replace(patch_pkg, kind='全量包', base_version='', evidence=MANIFEST_EVIDENCE,
                      md5='', url=self.full_url, original_url=self.full_url)
        collision = replace(old, md5='c' * 32)  # Same SHA256 but a conflicting published MD5.
        unrelated = replace(old, recipe=old.recipe | {'nvid': self.other})
        unsigned = replace(old, sha256='')
        db.put([old, collision, unrelated, unsigned])
        db.put([patch_pkg])
        db.put([old])
        self.assertEqual({p.id for p in db.list(self.model, 'cn')}, {patch_pkg.id, collision.id, unsigned.id})
        self.assertEqual([p.id for p in db.list(self.model, 'cn', self.other)], [unrelated.id])
        moved = replace(patch_pkg, url=self.full_url, original_url=self.full_url)
        db.put([moved])
        self.assertEqual({p.id for p in db.list(self.model, 'cn')}, {moved.id, collision.id, unsigned.id})
