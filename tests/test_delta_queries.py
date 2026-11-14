import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from oplus_tool import tracker
from oplus_tool.catalog import Catalog
from vendor.oplus_tracker import tomboy_pro as upstream

MODEL='PHY110';OLD='PHY110_11.F.28_2280_202608111929';NEW='PHY110_11.H.10_3100_202610291237'
OLD_COMPONENTS='system_vendor:OLD,my_manifest:'+OLD+'.97.HASH'
NEW_COMPONENTS='system_vendor:NEW,my_manifest:'+NEW+'.97.HASH'

class DeltaQueryTests(unittest.TestCase):
    def options(self):return tracker.QueryOptions(MODEL,'cn',NEW,NEW_COMPONENTS,nvid='10010111')
    def test_historical_queries_keep_each_matching_component_set(self):
        baseline={'model':MODEL,'region':'cn','ota':OLD,'components':OLD_COMPONENTS,'nvid':'10010111'}
        jobs=tracker.build_queries(self.options(),[baseline,baseline])
        delta=[cfg for _,cfg in jobs if cfg.components_input]
        old=[cfg for cfg in delta if cfg.ota_version==OLD];new=[cfg for cfg in delta if cfg.ota_version==NEW]
        self.assertEqual(len(old),5);self.assertEqual(len(new),5)
        self.assertEqual({cfg.components_input for cfg in old},{OLD_COMPONENTS})
        self.assertEqual({cfg.components_input for cfg in new},{NEW_COMPONENTS})
        self.assertIn('server_auto',{cfg.mode for cfg in old})
        baseline['nvid']='10011000'
        self.assertFalse(any(cfg.ota_version==OLD for _,cfg in tracker.build_queries(self.options(),[baseline])))
    def test_delta_base_comes_from_request_not_current_device(self):
        cfg=upstream.QueryConfig(OLD,MODEL,'cn','taste','0'*64,components_input=OLD_COMPONENTS)
        comp=upstream.ComponentInfo('my_manifest','target','https://example.test/delta.zip','https://example.test/delta.zip','1234','a'*32,'https://example.test/delta.zip')
        for packet_type,kind in (('0','增量包'),('1','全量包')):
            raw={'status':'published','isRecruit':False,'components':[{'componentName':'my_manifest','componentPackets':{'type':packet_type}}]}
            result=upstream.QueryResult(True,200,data={'raw_body':raw,'version':'PHY110_17.0.0.100(CN01)','ota_version':NEW},components=[comp])
            package=tracker.normalize_result(result,cfg,'尝鲜版',self.options())[0]
            self.assertEqual(package.kind,kind);self.assertEqual(package.base_version,OLD if packet_type=='0' else '')
            self.assertNotIn('guid',package.recipe)
            self.assertTrue(package.public_query)
    def test_device_guid_keeps_public_delta_queries(self):
        options=self.options();options.guid='a1'*32
        jobs=tracker.build_queries(options)
        delta=[cfg for _,cfg in jobs if cfg.components_input]
        self.assertEqual({cfg.guid for cfg in delta},{'0'*64,options.guid})
        self.assertEqual(len(delta),10)
    def test_refresh_public_delta_does_not_switch_to_device_endpoint(self):
        from oplus_tool.catalog import Package
        options=self.options();options.guid='a1'*32
        cfg=upstream.QueryConfig(OLD,MODEL,'cn','taste','0'*64,components_input=OLD_COMPONENTS)
        from dataclasses import asdict
        recipe=asdict(cfg);recipe.pop('guid')
        package=Package(MODEL,'cn','正式版','增量包','target',NEW,'my_manifest','https://example.test/delta.zip',
                        base_version=OLD,recipe=recipe,public_query=True)
        comp=upstream.ComponentInfo('my_manifest','target',package.url,package.url,'1234','','')
        raw={'status':'published','isRecruit':False,'components':[{'componentName':'my_manifest','componentPackets':{'type':'0'}}]}
        result=upstream.QueryResult(True,200,data={'raw_body':raw,'version':'target','ota_version':NEW},components=[comp])
        with patch.object(upstream,'query_update',return_value=result) as query:
            refreshed=tracker.refresh_package(package,options)
        self.assertEqual(query.call_args.args[0].guid,'0'*64);self.assertEqual(refreshed.base_version,OLD)
    def test_delta_source_is_not_rewritten_by_anti_query_fallback(self):
        full=upstream.QueryConfig('PHY110_11.A.01_0001_197001010000',MODEL,'cn','taste','0'*64)
        delta=upstream.QueryConfig(OLD,MODEL,'cn','taste','0'*64,components_input=OLD_COMPONENTS)
        good=upstream.QueryResult(True,200,data={'fake_ota_version':'PHY110_11.F.10_3100_202610291237','raw_body':{}},components=[])
        empty=upstream.QueryResult(False,2004,error='artifactV1Result is empty')
        with patch.object(tracker,'build_queries',return_value=[('正式版',full),('正式版',delta)]),patch.object(upstream,'query_update',side_effect=[good,empty]) as query:
            tracker.query_all(self.options(),threading.Event(),lambda *a:None,lambda *a:None,lambda *a:None)
        self.assertEqual(query.call_count,2);self.assertEqual(query.call_args_list[-1].args[0].ota_version,OLD)
    def test_baseline_catalog_deduplicates_and_isolates_models_regions(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Catalog(Path(temp)/'catalog.db')
            self.assertTrue(db.remember_baseline(MODEL,'cn',OLD,OLD_COMPONENTS,'10010111','官方全量包 my_manifest'))
            db.remember_baseline(MODEL,'cn',OLD,','.join(reversed(OLD_COMPONENTS.split(','))),'10010111')
            rows=db.baselines(MODEL,'cn');self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['ota'],OLD);self.assertEqual(db.baselines(MODEL,'eu'),[]);self.assertEqual(db.baselines('OTHER','cn'),[])
            self.assertFalse(db.remember_baseline(MODEL,'cn','WRONG_11.A',OLD_COMPONENTS))
            self.assertEqual(set(rows[0]),{'model','region','ota','components','nvid','source'})
    def test_verified_public_profiles_are_explicit_and_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Catalog(Path(temp)/'catalog.db');self.assertEqual(db.baselines(MODEL,'cn'),[])
            self.assertEqual(db.import_known_baselines('OTHER','cn'),0)
            self.assertGreaterEqual(db.import_known_baselines(MODEL,'cn'),2)
            self.assertTrue(any(p['ota']==OLD for p in db.baselines(MODEL,'cn')))
            self.assertEqual(db.baselines(MODEL,'eu'),[])

if __name__=='__main__':unittest.main(verbosity=2)
