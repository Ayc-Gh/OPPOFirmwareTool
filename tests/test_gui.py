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
from oplus_tool import payload,adb,tracker
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
    def choose_model(self,code):
        self.win.target_mode.setCurrentIndex(1);self.win.model_search.clear()
        self.win.model_combo.setCurrentIndex(self.win.model_combo.findData(code));self.win.apply_model_button.click()
    def test_manual_model_clears_device_identity_and_never_requests_root(self):
        self.connect_device();self.win.params['guid'].setText('ab'*32);self.win.params['duid'].setText('cd'*32)
        self.win.params['project'].setText('12345');self.win.params['serial'].setText('PRIVATE')
        self.win.components.setText('system:PHY110_11.A');self.win.params['extra_versions'].setText('PHY110_11.A')
        with patch.object(adb,'read_root_identifiers') as root:
            self.choose_model('PKB110')
            self.assertIsNone(self.win.current_device);self.assertFalse(self.win.root_button.isEnabled())
            self.win.root_button.click();root.assert_not_called()
        opts=self.win.get_options();opts.validate()
        self.assertEqual((opts.model,opts.region,opts.nvid),('PKB110','cn','10010111'))
        for field in ('guid','duid','serial','project','ota','components','extra_versions'):self.assertEqual(getattr(opts,field),'')
        self.assertIn('Find X8',self.win.device_labels['name'].text())
        self.assertIn('OP5A3DL1',self.win.device_labels['model'].text())
        jobs=tracker.build_queries(opts)
        self.assertTrue(jobs);self.assertTrue(all(c.ota_version.startswith('PKB110_') and not c.components_input for _,c in jobs))
    def test_adb_refresh_preserves_manual_target_and_can_switch_back(self):
        device=self.connect_device();self.choose_model('PKB110')
        self.win.params['ota'].setText('PKB110_11.A');self.win.devices_found(([device],[]))
        self.assertEqual(self.win.params['model'].text(),'PKB110');self.assertEqual(self.win.params['ota'].text(),'PKB110_11.A')
        self.win.devices_found(([],[]));self.assertEqual(self.win.params['model'].text(),'PKB110')
        with patch.object(self.win,'query_firmware') as query:
            self.win.query_connected_device(device);query.assert_not_called()
        self.win.devices_found(([device],[]));self.win.target_mode.setCurrentIndex(0)
        self.assertIs(self.win.current_device,device);self.assertEqual(self.win.params['model'].text(),'PHY110')
        self.assertTrue(self.win.root_button.isEnabled())
    def test_manual_search_custom_model_and_unknown_region(self):
        self.win.target_mode.setCurrentIndex(1);self.win.model_search.setText('find x8 PKB110')
        self.assertEqual(self.win.model_combo.count(),3)
        self.assertEqual(self.win.model_combo.itemData(1),'PKB110')
        self.win.model_search.clear()
        international=next(p for p in self.win.model_profiles if p.brand=='OPPO' and p.model.startswith('CPH'))
        self.choose_model(international.model)
        self.assertEqual(self.win.params['region'].currentData(),'');self.assertEqual(self.win.params['nvid'].text(),'')
        with self.assertRaises(ValueError):self.win.get_options().validate()
        self.win.params['region'].setCurrentText('eu');self.win.region_changed()
        opts=self.win.get_options();opts.validate();self.assertEqual(opts.nvid,tracker.upstream.REGION_CONFIG['eu']['carrier_id'])
        self.win.model_combo.setCurrentIndex(self.win.model_combo.findData('__custom__'));self.win.apply_manual_model()
        self.assertEqual(self.win.params['model'].text(),'');self.assertEqual(self.win.params['nvid'].text(),'')
        self.win.params['model'].setText('TEST123');self.win.params['region'].setCurrentText('cn');self.win.region_changed()
        self.win.get_options().validate()
    def test_manual_query_to_online_images_is_scoped_to_model(self):
        archive,_=make_payload(self.root);info=payload.inspect(str(archive))
        p=Package('PKB110','cn','正式版','全量包','PKB110_16.0.0','PKB110_11.F','System','https://example.invalid/pkb110.zip',major='16')
        def query(opts,cancel,progress,log,emit,cached,baselines):
            self.assertEqual(opts.model,'PKB110');self.assertEqual(opts.ota,'');emit([p]);return [p]
        self.choose_model('PKB110')
        with patch.object(tracker,'query_all',side_effect=query):self.win.query_firmware();self.wait_job()
        self.assertEqual(self.win.package_table.rowCount(),1)
        with patch.object(tracker,'resolve_url',side_effect=lambda url:url),patch.object(payload,'inspect',return_value=info) as inspect:
            self.win.use_online_package();self.wait_job()
            inspect.assert_called_once();self.assertEqual(inspect.call_args.args[0],p.url)
        self.assertEqual(self.win.image_table.rowCount(),3);self.assertIs(self.win.info,info)
        self.choose_model('PHY110')
        self.assertEqual(self.win.package_table.rowCount(),0);self.assertEqual(self.win.online_combo.count(),1)
        self.assertEqual(self.win.source_edit.text(),'');self.assertIsNone(self.win.info)
    def test_manual_switch_keeps_local_source_and_locks_during_job(self):
        archive,_=make_payload(self.root);self.win.source_edit.setText(str(archive));self.win.info=payload.inspect(str(archive))
        self.choose_model('PKB110');self.assertIsNotNone(self.win.info);self.assertEqual(self.win.source_edit.text(),str(archive))
        self.win.start_job(lambda c,s:None)
        self.assertFalse(self.win.target_mode.isEnabled());self.assertFalse(self.win.model_combo.isEnabled())
        self.wait_job();self.assertTrue(self.win.target_mode.isEnabled());self.assertTrue(self.win.model_combo.isEnabled())

if __name__=='__main__':unittest.main(verbosity=2)
