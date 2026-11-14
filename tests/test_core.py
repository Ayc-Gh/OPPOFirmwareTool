from __future__ import annotations
import hashlib
import json
import re
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
from oplus_tool import adb,payload,tracker,engine
from oplus_tool.catalog import Package,Catalog
from oplus_tool.common import Cancelled
from oplus_tool.network import download,RangeReader
from tests.fixtures import make_payload

@contextmanager
def serve(data,ignore_range=False,bad_range=False):
    stats={'sent':0,'ranges':[]}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_HEAD(self):
            self.send_response(200);self.send_header('Content-Length',str(len(data)))
            if not ignore_range:self.send_header('Accept-Ranges','bytes')
            self.send_header('ETag','"fixture-v1"');self.end_headers()
        def do_GET(self):
            range=self.headers.get('Range','');stats['ranges'].append(range)
            m=re.fullmatch(r'bytes=(\d+)-(\d*)',range)
            if m and not ignore_range:
                start=int(m[1]);end=min(int(m[2]) if m[2] else len(data)-1,len(data)-1)
                if start>=len(data):self.send_response(416);self.end_headers();return
                body=data[start:end+1];self.send_response(206)
                self.send_header('Content-Range',f'bytes {start+int(bad_range)}-{end}/{len(data)}')
            else:body=data;self.send_response(200)
            self.send_header('Content-Length',str(len(body)));self.send_header('Accept-Ranges','bytes');self.send_header('ETag','"fixture-v1"');self.end_headers()
            try:self.wfile.write(body);stats['sent']+=len(body)
            except (ConnectionError,OSError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:yield f'http://127.0.0.1:{server.server_port}/ota.zip',stats
    finally:server.shutdown();server.server_close();worker.join()

def progress(*args):pass

class CoreTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.cancel=threading.Event()
    def tearDown(self):self.tmp.cleanup()
    def test_adb_parse_and_region(self):
        props=adb.parse_props('[ro.product.brand]: [OPPO]\n[ro.product.model]: [PHY110]\n[ro.build.version.ota]: [PHY110_11.H.10_3100_202610291237]\n[ro.boot.slot_suffix]: [_b]\n[ro.build.oplus_nv_id]: [10010111]\n[persist.sys.oplus.region]: [EU]\n[ro.oplus.version.base]: [.hash]\n')
        d=adb.device_from_props('serial',props);self.assertEqual(d.region,'cn');self.assertEqual(d.slot,'b');self.assertEqual(d.components,'system_vendor:.hash')
        self.assertIsNone(adb.device_from_props('s',{'ro.product.brand':'Samsung'}))
    def test_full_local_and_deflated_zip(self):
        for compressed in (False,True):
            archive,images=make_payload(self.root/str(compressed),compressed=compressed)
            for path in (archive,archive.parent/'payload.bin'):
                info=payload.inspect(str(path));self.assertFalse(info.is_delta)
                self.assertEqual([p.name for p in info.partitions],list(images));self.assertEqual(info.partitions[1].sha256,hashlib.sha256(images['init_boot']).hexdigest())
    def test_manifest_rejects_truncated_file_and_unsafe_name(self):
        archive,_=make_payload(self.root)
        p=self.root/'truncated.bin';p.write_bytes((self.root/'payload.bin').read_bytes()[:-100])
        with self.assertRaises(ValueError):payload.inspect(str(p))
        original=(self.root/'payload.bin').read_bytes();p.write_bytes(original.replace(b'boot',b'../x',1))
        with self.assertRaises(ValueError):payload.inspect(str(p))
    def test_remote_inspection_and_subset_only(self):
        archive,images=make_payload(self.root)
        with serve(archive.read_bytes()) as (url,stats):
            info=payload.inspect(url,self.cancel);self.assertEqual(len(info.partitions),3)
            self.assertLess(stats['sent'],len(archive.read_bytes())//2)
            subset=self.root/'subset.bin';payload.stage_subset(info,['init_boot'],subset,self.cancel,progress)
            sub=payload.inspect(str(subset));self.assertEqual([p.name for p in sub.partitions],['init_boot'])
            self.assertLess(subset.stat().st_size,2048)
    def test_range_ignored_and_corrupt_response(self):
        for ignore,bad in ((True,False),(False,True)):
            with serve(b'A'*4096,ignore,bad) as (url,stats):
                with self.assertRaises(RuntimeError):RangeReader(url)
    def test_download_and_resume_with_hash(self):
        data=b'ROM-CONTENT'*400000;md5=hashlib.md5(data).hexdigest();dest=self.root/'download.zip'
        with serve(data) as (url,stats):
            # Cancellation deliberately leaves a resumable partial and sidecar.
            def cancel_progress(done,total,text):
                if done>=1048576:self.cancel.set()
            with self.assertRaises(Cancelled):download(url,dest,self.cancel,cancel_progress,len(data),md5)
            self.assertFalse(dest.exists());self.assertTrue((self.root/'download.zip.part').exists());self.cancel.clear()
            download(url,dest,self.cancel,progress,len(data),md5)
            self.assertEqual(dest.read_bytes(),data);self.assertTrue(any(r.startswith('bytes=1048576-') for r in stats['ranges']))
    def test_download_range_ignored_restarts(self):
        data=b'TEST'*700000;dest=self.root/'restart.zip';md5=hashlib.md5(data).hexdigest()
        with serve(data,ignore_range=True) as (url,stats):
            def stop(d,t,s):self.cancel.set()
            with self.assertRaises(Cancelled):download(url,dest,self.cancel,stop,len(data),md5)
            self.cancel.clear();download(url,dest,self.cancel,progress,len(data),md5)
            self.assertEqual(dest.read_bytes(),data)
    def test_hash_failure_does_not_publish(self):
        dest=self.root/'bad.zip'
        with serve(b'x'*1024) as (url,stats):
            with self.assertRaisesRegex(RuntimeError,'MD5'):download(url,dest,self.cancel,progress,1024,'0'*32)
        self.assertFalse(dest.exists())
    def test_catalog_keeps_full_delta_and_rotating_links(self):
        db=Catalog(self.root/'db.sqlite3');p=Package('PHY110','cn','正式版','全量包','17.0','OTA','System','https://test/file.zip?a=1')
        p.original_url=p.url;db.put([p]);p2=replace(p,url='https://test/file.zip?a=2',original_url='https://test/file.zip?a=2')
        db.put([p2]);delta=replace(p,kind='增量包',base_version='old');db.put([delta]);self.assertEqual(len(db.list('PHY110','cn')),2)
        self.assertEqual(db.list('OTHER','cn'),[])
    def test_query_plan_and_no_credentials_in_recipe(self):
        opts=tracker.QueryOptions('PHY110','cn','PHY110_11.H.10_3100_202610291237',components='system_vendor:HASH')
        jobs=tracker.build_queries(opts);self.assertTrue(any(cfg.components_input for ch,cfg in jobs));self.assertTrue(any(cfg.recruitID for ch,cfg in jobs))
        from vendor.oplus_tracker.tomboy_pro import QueryResult,ComponentInfo
        cfg=jobs[0][1]
        result=QueryResult(True,200,data={'raw_body':{'status':'published','isRecruit':False},'version':'PHY110_17.0.0.100(CN01)','ota_version':'new'},components=[ComponentInfo('System','new','https://test/full.zip','https://test/full.zip','1024','0'*32,'https://test/full.zip')])
        packages=tracker.normalize_result(result,cfg,'尝鲜版',opts);self.assertEqual(packages[0].channel,'正式版');self.assertNotIn('guid',packages[0].recipe)
        from types import SimpleNamespace
        packages[0].base_version=opts.ota
        tracker.refine_package(packages[0],SimpleNamespace(is_delta=True,metadata={'pre-build-incremental':'SOURCEBUILD'}))
        self.assertTrue(packages[0].base_version.startswith(opts.ota));self.assertIn('SOURCEBUILD',packages[0].base_version)
    def test_local_ssut_selected_and_delta(self):
        for delta in (False,True):
            archive,images=make_payload(self.root/str(delta),delta=delta);info=payload.inspect(str(archive))
            old=self.root/'old';old.mkdir(exist_ok=True);(old/'boot.img').write_bytes(images['boot'])
            out=self.root/('out'+str(delta));engine.extract(info,['boot','init_boot'],out,str(old),self.cancel,progress,progress,'ssut')
            self.assertEqual((out/'boot.img').read_bytes(),images['boot']);self.assertEqual((out/'init_boot.img').read_bytes(),images['init_boot']);self.assertFalse((out/'vendor.img').exists())
    def test_delta_baseline_and_unsupported_preflight(self):
        archive,images=make_payload(self.root,delta=True);info=payload.inspect(str(archive))
        with self.assertRaisesRegex(ValueError,'基线'):engine.extract(info,['boot'],self.root/'out','',self.cancel,progress,progress,'ssut')
        old=self.root/'old';old.mkdir();(old/'boot.img').write_bytes(b'X'*4096)
        with self.assertRaisesRegex(ValueError,'SHA256'):engine.extract(info,['boot'],self.root/'out',str(old),self.cancel,progress,progress,'ssut')
        archive,_=make_payload(self.root/'unsupported',unsupported=True);info=payload.inspect(str(archive))
        (old/'boot.img').write_bytes(images['boot'])
        with self.assertRaisesRegex(ValueError,'PUFFDIFF'):engine.extract(info,['boot'],self.root/'out',str(old),self.cancel,progress,progress,'ssut')
    def test_remote_ssut_delta_subset(self):
        archive,images=make_payload(self.root,delta=True);old=self.root/'old';old.mkdir();(old/'boot.img').write_bytes(images['boot'])
        with serve(archive.read_bytes()) as (url,stats):
            info=payload.inspect(url,self.cancel);out=self.root/'remote-out';engine.extract(info,['boot','init_boot'],out,str(old),self.cancel,progress,progress,'ssut')
            self.assertEqual((out/'boot.img').read_bytes(),images['boot']);self.assertLess(stats['sent'],archive.stat().st_size//2)
    def test_xishang_online_and_local(self):
        archive,images=make_payload(self.root)
        for remote in (False,True):
            with serve(archive.read_bytes()) as (url,stats):
                source=url if remote else str(archive);info=payload.inspect(source,self.cancel);out=self.root/('xishang-'+str(remote))
                engine.extract(info,['boot','init_boot'],out,'',self.cancel,progress,progress,'xishang')
                self.assertEqual((out/'boot.img').read_bytes(),images['boot']);self.assertEqual((out/'init_boot.img').read_bytes(),images['init_boot'])
                if remote:self.assertLess(stats['sent'],archive.stat().st_size)
    def test_opex_patch_package_parsing(self):
        from vendor.oplus_tracker.tomboy_pro import QueryResult, QueryConfig
        cfg = QueryConfig('PYB110_11.A', 'PYB110', 'cn', 'manual', '0'*64)
        opts = tracker.QueryOptions('PYB110', 'cn', 'PYB110_11.A')
        raw_body = {
            'status': 'published',
            'isRecruit': False,
            'versionName': 'PYB110_17.0.1.201(CN01)',
            'realOsVersion': 'ColorOS 17.0.0',
            'opex': {
                'opexVersionName': '17.0.1.201Patch01',
                'content': '• 优化部分场景的系统稳定性',
                'opexPackage': [{
                    'businessCode': 'ovl_update_6137',
                    'info': {
                        'zipSize': 1230309,
                        'zipHash': 'e7efa948f57dd039c09aa22682af228d32307ebd225693b24300aa63942c4a06',
                        'autoUrl': 'https://gauss-opexcostauto-cn.allawnfs.com/patch.zip'
                    }
                }]
            }
        }
        res = QueryResult(success=True, response_code=200, data={'version':'PYB110_17.0.1.201(CN01)', 'ota_version':'PYB110_11.A.44', 'raw_body': raw_body}, components=[])
        packages = tracker.normalize_result(res, cfg, '正式版', opts)
        self.assertEqual(len(packages), 1)
        p = packages[0]
        self.assertEqual(p.kind, '补丁包')
        self.assertEqual(p.version, '17.0.1.201Patch01')
        self.assertEqual(p.component, 'ovl_update_6137')
        self.assertEqual(p.size, 1230309)
        self.assertEqual(p.sha256, 'e7efa948f57dd039c09aa22682af228d32307ebd225693b24300aa63942c4a06')
        self.assertEqual(p.url, 'https://gauss-opexcostauto-cn.allawnfs.com/patch.zip')
        self.assertIn('ovl_update_6137', p.evidence)

if __name__=='__main__':unittest.main(verbosity=2)
