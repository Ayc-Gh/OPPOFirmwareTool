import subprocess
import threading
import unittest
from unittest.mock import patch
from oplus_tool import adb
from oplus_tool.common import Cancelled

GUID='a1'*32
DUID='B2'*32
GUID_XML=f'<openid_config><guid name="GUID" value="{GUID}"/><auid value="do-not-use"/></openid_config>'
DUID_XML=f'<map><string name="DUID">{DUID}</string><long name="DUID_TIME" value="123"/></map>'

class RootIdentifierTests(unittest.TestCase):
    def test_normal_scan_never_requests_root(self):
        props='[ro.product.brand]: [OPPO]\n[ro.product.model]: [PHY110]\n'
        with patch.object(adb,'command',side_effect=['List of devices attached\nSERIAL device\n',props]) as cmd:
            devices,notes=adb.scan()
        self.assertEqual(len(devices),1);self.assertEqual(notes,[])
        self.assertTrue(all('su' not in call.args[0] for call in cmd.call_args_list))
    def test_reads_named_identifiers_for_selected_device(self):
        with patch.object(adb,'command',side_effect=['uid=0(root)',GUID_XML,DUID_XML]) as cmd:
            result=adb.read_root_identifiers('SELECTED','custom-adb.exe')
        self.assertEqual((result.guid,result.duid),(GUID,DUID));self.assertEqual(result.notes,[])
        self.assertNotIn(GUID,repr(result));self.assertNotIn(DUID,repr(result))
        self.assertEqual(cmd.call_count,3)
        for call in cmd.call_args_list:
            self.assertEqual(call.args[0][:5],['-s','SELECTED','shell','su','-c']);self.assertEqual(call.args[1],'custom-adb.exe')
        self.assertIn(adb.GUID_FILE,cmd.call_args_list[1].args[0][-1]);self.assertIn(adb.DUID_FILE,cmd.call_args_list[2].args[0][-1])
    def test_partial_results_and_invalid_values(self):
        for text in ('<broken', '<openid_config><guid value="no"/></openid_config>',f'<openid_config><guid value="{"0"*64}"/></openid_config>'):
            with patch.object(adb,'command',side_effect=['uid=0(root)',text,DUID_XML]):
                result=adb.read_root_identifiers('SERIAL')
            self.assertEqual(result.guid,'');self.assertEqual(result.duid,DUID);self.assertEqual(len(result.notes),1)
        self.assertEqual(adb.parse_root_identifier('<map><string name="other">'+GUID+'</string></map>','duid'),'')
    def test_denied_or_nonroot_never_reads_private_files(self):
        for response in (RuntimeError('permission denied'),'uid=2000(shell)'):
            with patch.object(adb,'command',side_effect=response if isinstance(response,Exception) else [response]) as cmd:
                with self.assertRaises(RuntimeError):adb.read_root_identifiers('SERIAL')
                self.assertEqual(cmd.call_count,1)
    def test_root_authorization_timeout(self):
        with patch.object(adb,'command',side_effect=subprocess.TimeoutExpired('su',30)):
            with self.assertRaisesRegex(RuntimeError,'授权超时'):adb.read_root_identifiers('SERIAL')
    def test_cancellation_before_or_during_read(self):
        cancel=threading.Event();cancel.set()
        with patch.object(adb,'command') as cmd:
            with self.assertRaises(Cancelled):adb.read_root_identifiers('SERIAL',cancel=cancel)
            cmd.assert_not_called()
        cancel.clear()
        def grant(*args,**kwargs):cancel.set();return 'uid=0(root)'
        with patch.object(adb,'command',side_effect=grant) as cmd:
            with self.assertRaises(Cancelled):adb.read_root_identifiers('SERIAL',cancel=cancel)
            self.assertEqual(cmd.call_count,1)

if __name__=='__main__':unittest.main(verbosity=2)
