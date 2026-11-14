import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt,QEvent
from PySide6.QtTest import QTest
import app
from oplus_tool.catalog import Catalog,Package
from oplus_tool import payload,adb
from tests.fixtures import make_payload

class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        with patch.object(app.Window,'scan_devices',lambda self:None):self.win=app.Window(True)
        self.win.catalog=Catalog(self.root/'catalog.db');self.win.params['model'].setText('PHY110');self.win.params['region'].setCurrentText('cn')
    def tearDown(self):
        self.win.catalog_timer.stop();self.win.hide();self.win.deleteLater()
        self.qt.sendPostedEvents(None,QEvent.DeferredDelete);self.qt.processEvents();self.tmp.cleanup()
    def test_filters_copy_same_version_packages_and_source_invalidation(self):
        packages=[Package('PHY110','cn','正式版',kind,'PHY110_17.0.0.100(CN01)','OTA','System','https://test/'+kind+'.zip',major='17',base_version='OLD' if kind=='增量包' else '') for kind in ('全量包','增量包')]
        self.win.catalog.put(packages);self.win.load_catalog();self.assertEqual(self.win.package_table.rowCount(),2)
        self.win.kind_filter.setCurrentText('增量包');self.assertEqual(self.win.package_table.rowCount(),1)
        self.win.copy_package_name(0,0);self.assertEqual(self.qt.clipboard().text(),'OTA')
        self.win.search.setText('missing');self.assertEqual(self.win.package_table.rowCount(),0)
        archive,_=make_payload(self.root);self.win.info=payload.inspect(str(archive));self.win.populate_images();self.win.check_images(True)
        self.assertEqual(len(self.win.image_names()),3);self.win.image_search.setText('boot');self.assertEqual(len(self.win.image_names()),3)
        self.win.source_edit.setText('other.zip');self.assertIsNone(self.win.info);self.assertEqual(self.win.image_table.rowCount(),0)
    def test_large_firmware_progress_uses_64bit_values(self):
        signals=app.JobSignals();signals.progress.connect(self.win.set_progress)
        signals.progress.emit(5*1024**3,10*1024**3,'大型固件下载')
        self.assertEqual(self.win.progress.value(),500)
    def wait_job(self):
        deadline=time.monotonic()+3
        while self.win.job and time.monotonic()<deadline:
            self.qt.processEvents();QTest.qWait(10)
        self.assertIsNone(self.win.job)
    def connect_device(self,serial='SERIAL'):
        device=adb.Device(serial,name='OPPO',model='PHY110',region='cn',ota='PHY110_11.A')
        self.win.devices_found(([device],[]));return device
    def test_root_is_manual_and_scoped_to_selected_device(self):
        guid='a1'*32;duid='B2'*32
        result=adb.RootIdentifiers('SERIAL',guid,duid)
        with patch.object(adb,'read_root_identifiers',return_value=result) as reader:
            self.assertFalse(self.win.root_button.isEnabled());self.connect_device();reader.assert_not_called()
            self.assertTrue(self.win.root_button.isEnabled());self.win.root_button.click();self.wait_job()
            self.assertEqual(reader.call_args.args[0],'SERIAL')
            self.assertEqual(self.win.params['guid'].text(),guid);self.assertEqual(self.win.params['duid'].text(),duid)
            self.assertNotIn(guid,self.win.log_view.toPlainText());self.assertNotIn(duid,self.win.log_view.toPlainText())
            self.connect_device();reader.assert_called_once();self.assertEqual(self.win.params['guid'].text(),guid)
            self.connect_device('OTHER');self.assertEqual(self.win.params['guid'].text(),'');reader.assert_called_once()
    def test_partial_root_read_retains_existing_input(self):
        self.connect_device();original='c3'*32;self.win.params['guid'].setText(original)
        result=adb.RootIdentifiers('SERIAL',duid='B2'*32,notes=['GUID 配置未找到，保留已有输入。'])
        with patch.object(adb,'read_root_identifiers',return_value=result):
            self.win.root_button.click();self.wait_job()
        self.assertEqual(self.win.params['guid'].text(),original);self.assertEqual(self.win.params['duid'].text(),'B2'*32)
    def test_completion_status_uses_available_width(self):
        self.qt.setStyleSheet(app.STYLE)
        text='查询完成：本轮返回 13 条包记录（含重复）；网络或服务器异常 0 次。'
        self.win.set_progress(1,1,text);self.win.tabs.setCurrentIndex(1)
        for width in (1060,1380):
            self.win.resize(width,840);self.win.show();self.qt.processEvents()
            self.assertGreater(self.win.status.width(),width*.55)
            self.assertGreaterEqual(self.win.status.height(),self.win.status.heightForWidth(self.win.status.width()))
            self.assertEqual(self.win.status.text(),text);self.assertGreaterEqual(self.win.progress.height(),18)

if __name__=='__main__':unittest.main(verbosity=2)
