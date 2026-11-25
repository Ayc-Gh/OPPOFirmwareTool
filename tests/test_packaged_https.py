import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from oplus_tool import query_check
from vendor.oplus_tracker import tomboy_pro as upstream


class PublicQueryTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.report = Path(scratch.name) / 'query.json'

    def call(self, result):
        with patch.object(upstream, 'query_update', return_value=result) as query:
            ok = query_check.check_public_query(self.report)
        query.assert_called_once()
        config = query.call_args.args[0]
        self.assertEqual((config.model, config.region, config.mode, config.guid), ('PHY110', 'cn', 'manual', '0' * 64))
        self.assertTrue(config.ota_version.startswith('PHY110_11.F.'))
        return ok, json.loads(self.report.read_text(encoding='utf-8'))

    def test_public_query_reports_actual_package_without_download_links(self):
        component = upstream.ComponentInfo(name='system', version='sample', link='https://example.org/signed-url',
                                          original_link='', size='10', md5='', auto_url='')
        result = upstream.QueryResult(True, 200, data={'version': 'PHY110_16.0.10.500(CN01)',
                                                       'ota_version': 'PHY110_11.F.28_2280_202608111929'}, components=[component])
        ok, data = self.call(result)
        self.assertTrue(ok)
        self.assertEqual(data['outcome'], 'packages')
        self.assertEqual(data['packages'], [{'component': 'system', 'version': result.data['version'],
                                             'ota': result.data['ota_version']}])
        self.assertNotIn('signed-url', self.report.read_text(encoding='utf-8'))

    def test_explicit_no_update_is_a_successful_query_without_firmware(self):
        ok, data = self.call(upstream.QueryResult(False, 2004))
        self.assertTrue(ok)
        self.assertEqual((data['outcome'], data['response_code'], data['packages']), ('no_visible_update', 2004, []))

    def test_transport_failure_invalid_200_and_rate_limit_fail_the_check(self):
        for result in (upstream.QueryResult(False, 0, error='Connection failed: FileNotFoundError(2)'),
                       upstream.QueryResult(False, 200, error='Processing failed'),
                       upstream.QueryResult(True, 200, data={}), upstream.QueryResult(False, 308)):
            with self.subTest(result=result):
                ok, data = self.call(result)
                self.assertFalse(ok)
                self.assertFalse(data['ok'])
                self.assertEqual(data['response_code'], result.response_code)

    def test_cli_runs_query_without_gui_and_returns_failure_exit_code(self):
        with patch.object(sys, 'argv', ['app.py', '--check-ota-https', str(self.report)]), \
                patch.object(query_check, 'check_public_query', return_value=False) as run, \
                patch.object(app, 'QApplication') as gui, self.assertRaises(SystemExit) as stopped:
            app.main()
        self.assertEqual(stopped.exception.code, 1)
        run.assert_called_once_with(self.report)
        gui.assert_not_called()
