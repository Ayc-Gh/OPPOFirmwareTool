import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from oplus_tool import push

class PushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake_pkg = self.root / 'downgrade.ozip'
        self.fake_pkg.write_bytes(b'PK\x03\x04' + b'0' * 1024)

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_assistant_apk(self):
        apk_17 = push.get_assistant_apk(is_legacy=False)
        self.assertTrue(apk_17.is_file())
        self.assertEqual(apk_17.name, 'OTA_17.apk')

        apk_113 = push.get_assistant_apk(is_legacy=True)
        self.assertTrue(apk_113.is_file())
        self.assertEqual(apk_113.name, 'OTA_113.apk')

    def test_check_assistant_installed(self):
        with patch('oplus_tool.push.command', return_value='package:com.color.otaassistant'):
            self.assertTrue(push.check_assistant_installed('SERIAL'))

        with patch('oplus_tool.push.command', return_value=''):
            self.assertFalse(push.check_assistant_installed('SERIAL'))

    def test_storage_check(self):
        # 模拟 df 输出，剩余 20GB
        df_out = "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/block 64000000 44000000 20000000 68% /sdcard"
        with patch('oplus_tool.push.command', return_value=df_out):
            space = push.get_device_free_space('SERIAL')
            self.assertEqual(space, 20000000 * 1024)

    def test_trigger_downgrade_new_and_legacy(self):
        path = "/sdcard/OPPOFirmwareTool-" + "a" * 32 + "/firmware.zip"
        with patch.object(push.LocalUpdatePicker, "handoff", return_value="PHY110_1.2 | 8.00 GB") as handoff:
            for is_legacy, entry in [(False, push.MODERN_ENTRY), (True, push.LEGACY_ENTRY)]:
                result = push.trigger_downgrade("SERIAL", path, is_legacy=is_legacy, expected_size=8 * 1024**3)
                self.assertEqual(result, "PHY110_1.2 | 8.00 GB")
                handoff.assert_called_with(path, entry, 8 * 1024**3, "", None)

    def test_full_push_and_install_workflow(self):
        events = []
        def fake_command(args, custom='', timeout=15):
            cmd_str = ' '.join(args)
            if args[3:] == ['getprop']:
                return '[ro.product.brand]: [OPPO]\n[ro.product.model]: [PHY110]\n[ro.build.version.release]: [14]'
            if 'pm list' in cmd_str:
                return 'package:com.color.otaassistant'
            if args[3:4] == ['df']:
                return "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev 64000000 44000000 20000000 68% /sdcard"
            if args[3:4] == ['stat']:
                return str(self.fake_pkg.stat().st_size)
            events.append(cmd_str)
            return ''

        digest = 'a' * 64
        def popen(args, **kwargs):
            proc = MagicMock()
            proc.poll.return_value = proc.returncode = 0
            stdout = digest + '  ' + args[-1] if 'sha256sum' in args else ''
            proc.communicate.return_value = (stdout, '')
            return proc

        with patch.object(push, 'command', side_effect=fake_command), \
             patch.object(push, 'uuid4', return_value=MagicMock(hex='df' * 16)), \
             patch.object(push, 'validate_push_source', return_value=digest), \
             patch.object(push, 'trigger_downgrade', return_value='PHY110_1.2 | 1.00 KB') as preview, \
             patch('subprocess.Popen', side_effect=popen):
            detail = push.push_and_install('SERIAL', self.fake_pkg, log=lambda s: events.append('LOG:' + s))

        # 验证完整流程执行无报错
        self.assertTrue(any('LOG:' in e for e in events))
        self.assertEqual(detail, preview.return_value)
        self.assertEqual(preview.call_args.kwargs['expected_size'], self.fake_pkg.stat().st_size)
        self.assertIn('/firmware.ozip', preview.call_args.args[1])

    def test_two_handoffs_cannot_drive_the_same_picker(self):
        path = "/sdcard/OPPOFirmwareTool-" + "a" * 32 + "/firmware.zip"
        def concurrent(*args):
            with self.assertRaisesRegex(RuntimeError, "另一项任务"):
                push.trigger_downgrade("SERIAL", path)
            raise TimeoutError("timeout")
        with patch.object(push.LocalUpdatePicker, "handoff", side_effect=concurrent):
            with self.assertRaises(TimeoutError):
                push.trigger_downgrade("SERIAL", path)
        with patch.object(push.LocalUpdatePicker, "handoff", return_value="ready"):
            self.assertEqual(push.trigger_downgrade("SERIAL", path), "ready")

    def test_inspect_package_safety(self):
        # 1. 小文件测试
        small_pkg = self.root / 'small.zip'
        small_pkg.write_bytes(b'PK\x03\x04test')
        warns, errs = push.inspect_package_safety(small_pkg, 'PHY110')
        self.assertTrue(any('文件过小' in e for e in errs))

        # 2. 机型不匹配测试
        mismatch_pkg = self.root / 'PKB110_11.A_test.zip'
        with mismatch_pkg.open('wb') as data:
            data.truncate(60 * 1024 * 1024)
        warns, errs = push.inspect_package_safety(mismatch_pkg, 'PHY110')
        self.assertFalse(warns)
        self.assertTrue(any('机型不匹配' in e for e in errs))
