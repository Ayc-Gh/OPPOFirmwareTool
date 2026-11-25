"""Offline extraction and bundled-runtime check used for release validation."""
import hashlib
import json
import struct
import tempfile
import threading
import zipfile
from pathlib import Path
from PySide6.QtWidgets import QLabel
from . import adb, payload, engine, models, push, tracker, __version__
from .common import HOME, atomic_json, file_hash

def check_bundled_apks():
    apk_dir = HOME / 'tools' / 'apk'
    hashes = {}
    for legacy in (False, True):
        apk = push.get_assistant_apk(is_legacy=legacy, custom_dir=apk_dir)
        if apk.parent.resolve() != apk_dir.resolve():
            raise RuntimeError(f'发行目录缺少伴生 APK: {apk.name}')
        with zipfile.ZipFile(apk) as archive:
            if not archive.read('AndroidManifest.xml'):
                raise ValueError(f'伴生 APK 缺少 Android manifest: {apk.name}')
        hashes[apk.name] = file_hash(apk)
    return hashes

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
    if upstream.aes_ctr_decrypt(upstream.aes_ctr_encrypt(data,key,iv),key,iv)!=data or not upstream.generate_protected_key(key,upstream.PUBLIC_KEYS['cn']):
        raise ValueError('OTA 查询加密自检失败')
    with tempfile.TemporaryDirectory(prefix='oplus-release-check-') as tmp:
        tmp=Path(tmp);source=tmp/'payload.bin';source.write_bytes(b'CrAU'+struct.pack('>QQI',2,len(manifest),0)+manifest+data)
        info=payload.inspect(str(source))
        for name in ('ssut','xishang'):
            dest=tmp/name
            engine.extract(info,['boot'],dest,'',threading.Event(),lambda *a:None,lambda *a:None,name)
            results[name]=hashlib.sha256((dest/'boot.img').read_bytes()).hexdigest()
    bundled_adb = HOME / 'tools' / 'adb.exe'
    if not bundled_adb.is_file():
        raise FileNotFoundError(bundled_adb)
    # The version command runs locally without starting the server or touching a device.
    adb_version = adb.command(['version'], str(bundled_adb)).splitlines()[0]
    apk_hashes = check_bundled_apks()
    raw_body = {'opex': {'opexPackage': [{'businessCode': 'sample', 'info': {'autoUrl': 'https://example.invalid/check.zip'}}]}}
    options = tracker.QueryOptions('PHY110', 'cn', 'PHY110_11.F')
    config = upstream.QueryConfig(options.ota, options.model, options.region, 'manual', '0' * 64)
    patches = tracker.normalize_result(upstream.QueryResult(True, 200, data={'raw_body': raw_body}), config, '正式版', options)
    patch_support = len(patches) == 1 and patches[0].kind == '补丁包' and window.kind_filter.findText('补丁包') >= 0
    subtitle = window.findChild(QLabel, 'subtitle')
    version_matches = subtitle is not None and subtitle.text().endswith('v' + __version__)
    profiles=json.loads((Path(__file__).resolve().parent/'data/verified_baselines.json').read_text(encoding='utf-8'))
    if not profiles or not all(p.get('ota','').startswith(p.get('model','')+'_') for p in profiles):
        raise ValueError('随包基线自检失败')
    model_profiles=models.load_profiles()
    manual_selection = any(p.model=='PHY110' and 'OP565FL1' in p.codenames for p in model_profiles)
    push_controls = bool(window.push_local_button.text() and window.push_selected_button.text())
    checks = [all(h==digest.hex() for h in results.values()), version_matches, manual_selection,
              patch_support, push_controls, adb_version.startswith('Android Debug Bridge version')]
    atomic_json(Path(report),{'ok':all(checks),'version':__version__,
        'bundled_baseline_profiles':len(profiles),
        'bundled_model_profiles':len(model_profiles),'manual_model_selection':manual_selection,
        'gui_version_matches':version_matches,'opex_patch_support':patch_support,
        'push_controls_present':push_controls,'bundled_apk_sha256':apk_hashes,'adb_version':adb_version,
        'root_policy':'manual-button-only','root_button':window.root_button.text(),
        'crypto':'AES-CTR and RSA-OAEP verified',
        'tabs':[window.tabs.tabText(i) for i in range(window.tabs.count())],
        'engines':results})
    return all(checks)
