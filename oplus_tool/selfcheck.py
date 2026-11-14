"""Offline extraction and bundled-runtime check used for release validation."""
import hashlib
import json
import struct
import tempfile
import threading
from pathlib import Path
from . import adb,payload,engine,__version__
from .common import atomic_json

def run(window,report):
    # Test a deterministic single-partition image through the actual executables.
    data=b'OPPOFirmwareTool release self-check'.ljust(4096,b'\0');digest=hashlib.sha256(data).digest()
    pf=payload.pack_fields
    extent=pf([(1,0,0),(2,0,1)])
    operation=pf([(1,0,0),(2,0,0),(3,0,len(data)),(6,2,extent),(7,0,len(data)),(8,2,digest)])
    part=pf([(1,2,b'boot'),(7,2,pf([(1,0,len(data)),(2,2,digest)])),(8,2,operation)])
    manifest=pf([(3,0,4096),(13,2,part)])
    results={}
    from vendor.oplus_tracker import tomboy_pro as upstream
    key=b'K'*32;iv=b'I'*16
    assert upstream.aes_ctr_decrypt(upstream.aes_ctr_encrypt(data,key,iv),key,iv)==data
    assert upstream.generate_protected_key(key,upstream.PUBLIC_KEYS['cn'])
    with tempfile.TemporaryDirectory(prefix='oplus-release-check-') as tmp:
        tmp=Path(tmp);source=tmp/'payload.bin';source.write_bytes(b'CrAU'+struct.pack('>QQI',2,len(manifest),0)+manifest+data)
        info=payload.inspect(str(source))
        for name in ('ssut','xishang'):
            dest=tmp/name
            engine.extract(info,['boot'],dest,'',threading.Event(),lambda *a:None,lambda *a:None,name)
            results[name]=hashlib.sha256((dest/'boot.img').read_bytes()).hexdigest()
    devices,notes=adb.scan()
    profiles=json.loads((Path(__file__).resolve().parent/'data/verified_baselines.json').read_text(encoding='utf-8'))
    assert profiles and all(p.get('ota','').startswith(p.get('model','')+'_') for p in profiles)
    atomic_json(Path(report),{'ok':all(h==digest.hex() for h in results.values()),'version':__version__,
        'bundled_baseline_profiles':len(profiles),
        'root_policy':'manual-button-only','root_button':window.root_button.text(),
        'crypto':'AES-CTR and RSA-OAEP verified',
        'tabs':[window.tabs.tabText(i) for i in range(window.tabs.count())],
        'engines':results,'devices':[{'name':d.name,'model':d.model,'slot':d.slot,'region':d.region} for d in devices],
        'adb_notes':notes})
