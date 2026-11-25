"""Build a self-contained Windows x64 folder, portable ZIP and editable source ZIP."""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import marshal
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import zipfile
from oplus_tool import __version__

ROOT=Path(__file__).resolve().parent
TOOL_FILES = (
    'tools/adb.exe', 'tools/AdbWinApi.dll', 'tools/AdbWinUsbApi.dll',
    'tools/payload-dumper-go.exe', 'tools/payload-dumper-xishang.exe', 'tools/versions.json',
    'tools/apk/OTA_17.apk', 'tools/apk/OTA_113.apk',
)
REPORT = 'docs/packaged-check.json'
SSL_POLICY = 'codex-rejected-cas.pem'

def zip_dir(source,dest,include=None):
    with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(source.rglob('*')):
            if p.is_file() and (include is None or include(p.relative_to(source))):z.write(p,p.relative_to(source))

def include_source(path, release_dir):
    if path.is_relative_to(release_dir) or any(part in {'release','debug-release','.build','__pycache__','.git','verification','.venv'} for part in path.parts):
        return False
    if path.parts[0] == 'tools':
        return path.as_posix() in TOOL_FILES
    return path.as_posix() not in ('docs/debug-check.json','docs/source-check.json') and path.suffix not in {'.pyc','.part'}

def validate_report(report, expected_apks):
    if (report.get('ok') is not True or report.get('version') != __version__ or
            report.get('bundled_apk_sha256') != expected_apks or
            not all(report.get(name) is True for name in ('gui_version_matches','opex_patch_support','push_controls_present','tls_context_verified'))):
        raise RuntimeError(f'打包程序自检失败：需要 v{__version__}、可用的 TLS 上下文、推送与补丁入口及匹配的两个 APK')

def frozen_modules(executable):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    yield 'app.py', marshal.loads(archive.extract('app'))
    embedded = archive.open_embedded_archive('PYZ.pyz')
    for module, (kind, _, _) in embedded.toc.items():
        if module in ('oplus_tool', 'vendor') or module.startswith(('oplus_tool.', 'vendor.')):
            name = module.replace('.', '/')
            yield (name + '/__init__.py' if kind == 1 else name + '.py'), embedded.extract(module)

def verify_frozen_sources(target, source_zip):
    with zipfile.ZipFile(source_zip) as source:
        for filename, code in frozen_modules(target/'OPPOFirmwareTool.exe'):
            rebuilt = compile(source.read(filename), code.co_filename, 'exec', dont_inherit=True, optimize=0)
            if rebuilt != code:
                raise RuntimeError(f'{filename} 与 EXE 中的代码不一致，请重新构建')
        for name in source.namelist():
            if name.startswith('oplus_tool/data/') and source.read(name) != (target/'_internal'/name).read_bytes():
                raise RuntimeError(f'{name} 与 EXE 随附数据不一致，请重新构建')

def stream_sha256(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(block)
    return digest.hexdigest()

def copy_ssl_runtime_data(target):
    # Some Python builds load this policy relative to ssl.py on every TLS connection.
    # Constructing a context also detects a broken build interpreter before packaging.
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    source = Path(ssl.__file__).with_name(SSL_POLICY)
    member = '_internal/' + SSL_POLICY
    destination = target / member
    if not source.exists():
        destination.unlink(missing_ok=True)
        return {}
    data = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return {member: hashlib.sha256(data).hexdigest()}

def verify_archive(path, report, runtime_digests=None):
    with zipfile.ZipFile(path) as archive:
        if json.loads(archive.read(REPORT)) != report:
            raise RuntimeError(f'{path.name} 中的自检报告与本次构建不一致')
        expected = {'tools/apk/' + name: digest for name, digest in report['bundled_apk_sha256'].items()}
        expected.update(runtime_digests or {})
        names = archive.namelist()
        for name, digest in expected.items():
            if names.count(name) != 1:
                raise RuntimeError(f'{path.name} 中缺少或重复收录了 {name}')
            with archive.open(name) as data:
                if stream_sha256(data) != digest:
                    raise RuntimeError(f'{path.name} 中的 {name} 与构建时的 SHA256 不一致')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--reuse-build',action='store_true')
    parser.add_argument('--release-dir',default='release');args=parser.parse_args()
    release=(ROOT/args.release_dir).resolve();target=(release/'OPPOFirmwareTool').resolve()
    if not release.is_relative_to(ROOT.resolve()) or release==ROOT.resolve() or target.parent!=release:
        raise RuntimeError('构建输出目录超出项目范围')
    declared = re.search(r'^当前版本\s+\*\*v([^*\s]+)\*\*', (ROOT/'README.md').read_text(encoding='utf-8'), re.MULTILINE)
    if not declared or declared[1] != __version__:
        raise RuntimeError(f'README 声明的当前版本须与应用版本 v{__version__} 相同')
    for name in TOOL_FILES:
        if not (ROOT/name).is_file():
            raise FileNotFoundError(ROOT/name)
    expected_apks = {Path(name).name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                     for name in TOOL_FILES if name.endswith('.apk')}
    if not args.reuse_build:
        env=os.environ.copy()
        # Qt uses the Windows ICU API. A development Poppler installation on
        # PATH exports incompatible, version-suffixed symbols under icuuc.dll.
        env['PATH']=os.pathsep.join(p for p in env.get('PATH','').split(os.pathsep) if 'poppler' not in p.lower())
        # Windows Explorer, the GUI and the packaged self-check share one version.
        version_tuple = tuple(map(int, __version__.split('.'))) + (0,)
        version_file = ROOT/'.build/windows-version.txt'
        version_file.parent.mkdir(exist_ok=True)
        version_file.write_text(
            f"VSVersionInfo(ffi=FixedFileInfo(filevers={version_tuple}, prodvers={version_tuple}), kids=["
            f"StringFileInfo([StringTable('080404B0', [StringStruct('FileVersion', '{__version__}'), "
            f"StringStruct('ProductVersion', '{__version__}'), StringStruct('ProductName', 'OPPO 固件工作台')])]), "
            "VarFileInfo([VarStruct('Translation', [2052, 1200])])])", encoding='utf-8')
        subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--windowed','--onedir',
                        '--name','OPPOFirmwareTool','--distpath',str(release),'--workpath',str(ROOT/'.build/work'),
                        '--specpath',str(ROOT/'.build'),'--version-file',str(version_file),'--exclude-module','PySide6.QtWebEngineCore',
                        '--exclude-module','PySide6.QtWebEngineWidgets','--add-data',str(ROOT/'oplus_tool/data')+';oplus_tool/data',
                        str(ROOT/'app.py')],cwd=ROOT,check=True,env=env)
    # Clean only diagnostic files created by this project's release validation.
    for path in (target/'DebugConsole.exe',target/'_internal/icuuc.dll.disabled',target/'_internal/icudt78.dll.disabled'):
        path.unlink(missing_ok=True)
    runtime_digests = copy_ssl_runtime_data(target)
    for name in TOOL_FILES:
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/name, dest)
    for name in ('README.md','THIRD_PARTY_NOTICES.md','LICENSE'):
        shutil.copy2(ROOT/name,target/name)
    shutil.copytree(ROOT/'licenses',target/'licenses',dirs_exist_ok=True)
    # Supply all installed Python dependency license texts without including caches.
    licenses=target/'licenses/python-packages';licenses.mkdir(parents=True,exist_ok=True)
    for dist in importlib.metadata.distributions():
        if dist.metadata['Name'].lower() not in {'pyside6','pyside6-essentials','pyside6-addons','shiboken6','requests','cryptography','asn1crypto','cffi','pycparser','urllib3','certifi','charset-normalizer','idna','pyinstaller','pyinstaller-hooks-contrib'}:continue
        for file in dist.files or []:
            if ('license' in str(file).lower() or 'copying' in file.name.lower() or 'notice' in file.name.lower()) and not str(file).endswith('.py'):
                src=Path(dist.locate_file(file))
                if src.is_file():
                    dest=licenses/dist.metadata['Name']/file;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dest)
    python_license=Path(sys.base_prefix)/'LICENSE.txt'
    if python_license.exists():shutil.copy2(python_license,target/'licenses/Python-LICENSE.txt')
    with tempfile.TemporaryDirectory(dir=release) as tmp:
        staging = Path(tmp)
        report_file = staging/'packaged-check.json'
        # A fresh path prevents a stale success report from satisfying the build.
        env = os.environ | {'LOCALAPPDATA': str(staging/'appdata')}
        subprocess.run([str(target/'OPPOFirmwareTool.exe'),'--self-check',str(report_file)],
                       cwd=target, check=True, timeout=90, env=env)
        report = json.loads(report_file.read_text(encoding='utf-8'))
        validate_report(report, expected_apks)
        shutil.copy2(report_file, ROOT/REPORT)
        shutil.copytree(ROOT/'docs',target/'docs',dirs_exist_ok=True)
        for path in (ROOT/'upstream-source').glob('*.zip'):
            shutil.copy2(path,target/path.name)
        source = staging/'OPPOFirmwareTool-Source.zip'
        portable = staging/'OPPOFirmwareTool-Windows-x64.zip'
        zip_dir(ROOT, source, lambda p: include_source(p, release.relative_to(ROOT)))
        verify_frozen_sources(target, source)
        zip_dir(target, portable)
        hashes = []
        for package in (source, portable):
            verify_archive(package, report, runtime_digests if package == portable else None)
            with package.open('rb') as archive:
                hashes.append(stream_sha256(archive) + '  ' + package.name)
        sums = staging/'SHA256SUMS.txt'
        sums.write_text('\n'.join(hashes)+'\n', encoding='utf-8')
        for artifact in (source, portable, sums):
            artifact.replace(release/artifact.name)
    print('READY:',target/'OPPOFirmwareTool.exe')

if __name__=='__main__':main()
