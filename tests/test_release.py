import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

import app
import build
from oplus_tool import __version__, adb, selfcheck
from oplus_tool.catalog import Catalog


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.output = self.root / 'custom-output'
        target = self.output / 'OPPOFirmwareTool'
        target.mkdir(parents=True)
        (self.root / 'licenses').mkdir()
        (self.root / 'docs').mkdir()
        for name in (*build.TOOL_FILES, 'README.md', 'THIRD_PARTY_NOTICES.md', 'LICENSE'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        (self.root / 'README.md').write_text(f'当前版本 **v{__version__}**。', encoding='utf-8')
        (self.root / 'app.py').write_text('release_value = 1\n', encoding='utf-8')
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (self.root / 'tools/apk').iterdir()}
        self.report = {'ok': True, 'version': __version__, 'gui_version_matches': True,
                       'opex_patch_support': True, 'push_controls_present': True, 'bundled_apk_sha256': hashes}
        (self.root / build.REPORT).write_text(json.dumps(self.report), encoding='utf-8')

    def package(self, check, executable_code='release_value = 1\n'):
        with patch.object(build, 'ROOT', self.root), \
                patch('sys.argv', ['build.py', '--reuse-build', '--release-dir', self.output.name]), \
                patch.object(build.subprocess, 'run', side_effect=check), \
                patch.object(build, 'frozen_modules', return_value=[('app.py', compile(executable_code, 'app.py', 'exec'))]), \
                patch.object(build.importlib.metadata, 'distributions', return_value=[]):
            build.main()

    def test_both_archives_ship_the_two_verified_apks_and_new_report(self):
        payload = dict(self.report, evidence='new run')
        def make_report(args, **kwargs):
            report_path = Path(args[-1])
            self.assertFalse(report_path.exists())
            report_path.write_text(json.dumps(payload), encoding='utf-8')
        self.package(make_report)
        for filename in ('OPPOFirmwareTool-Windows-x64.zip', 'OPPOFirmwareTool-Source.zip'):
            with zipfile.ZipFile(self.output / filename) as archive:
                self.assertFalse(any(n.startswith(self.output.name + '/') for n in archive.namelist()))
                self.assertEqual(json.loads(archive.read(build.REPORT)), payload)
                for apk in ('tools/apk/OTA_17.apk', 'tools/apk/OTA_113.apk'):
                    self.assertEqual(archive.read(apk), (self.root / apk).read_bytes())

    def test_missing_legacy_apk_stops_build_before_packaging(self):
        (self.root / 'tools/apk/OTA_113.apk').unlink()
        with self.assertRaises(FileNotFoundError):
            self.package(lambda *a, **kw: self.fail('缺失 APK 时不应开始打包'))

    def test_stale_success_cannot_replace_missing_report(self):
        with self.assertRaises(FileNotFoundError):
            self.package(lambda *a, **kw: None)
        self.assertEqual(list(self.output.glob('*.zip')), [])

    def test_old_executable_version_fails_even_when_ok(self):
        def wrong_version(args, **kwargs):
            Path(args[-1]).write_text(json.dumps(dict(self.report, version='1.0.3')), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, __version__):
            self.package(wrong_version)
        self.assertEqual(list(self.output.glob('*.zip')), [])

    def test_reuse_rejects_same_version_with_different_frozen_code(self):
        def current_version(args, **kwargs):
            Path(args[-1]).write_text(json.dumps(self.report), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'app.py.*不一致'):
            self.package(current_version, executable_code='release_value = 0\n')
        self.assertEqual(list(self.output.glob('*.zip')), [])

    def test_mismatched_readme_stops_before_build(self):
        (self.root / 'README.md').write_text('当前版本 **v1.0.3**。', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'README'):
            self.package(lambda *args, **kwargs: self.fail('版本不一致时不应开始构建'))

    def test_source_selfcheck_checks_runtime_without_enumerating_devices(self):
        qt = QApplication.instance() or QApplication([])
        with patch.object(app, 'DATA', self.root), \
                patch.object(app, 'Catalog', return_value=Catalog(self.root / 'test.db')), \
                patch.object(app.QTimer, 'singleShot') as timer:
            window = app.Window(True, scan_on_start=False)
        timer.assert_not_called()
        target = self.root / 'offline.json'
        command = adb.command
        def version_only(args, custom):
            self.assertEqual(args, ['version'])
            return command(args, custom)
        try:
            with patch.object(adb, 'command', side_effect=version_only) as run:
                self.assertTrue(selfcheck.run(window, target))
            run.assert_called_once()
            actual = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(actual['version'], __version__)
            self.assertEqual(set(actual['bundled_apk_sha256']), {'OTA_17.apk', 'OTA_113.apk'})
            self.assertTrue(actual['gui_version_matches'] and actual['opex_patch_support'])
            self.assertNotIn('devices', actual)
        finally:
            window.catalog_timer.stop()
            window.deleteLater()
            qt.processEvents()
