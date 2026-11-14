"""Build a self-contained Windows x64 folder, portable ZIP and editable source ZIP."""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parent

def zip_dir(source,dest,include=None):
    with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(source.rglob('*')):
            if p.is_file() and (include is None or include(p.relative_to(source))):z.write(p,p.relative_to(source))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--reuse-build',action='store_true')
    parser.add_argument('--release-dir',default='release');args=parser.parse_args()
    release=(ROOT/args.release_dir).resolve();target=(release/'OPPOFirmwareTool').resolve()
    if not release.is_relative_to(ROOT.resolve()) or release==ROOT.resolve() or target.parent!=release:
        raise RuntimeError('构建输出目录超出项目范围')
    if not args.reuse_build:
        env=os.environ.copy()
        # Qt uses the Windows ICU API. A development Poppler installation on
        # PATH exports incompatible, version-suffixed symbols under icuuc.dll.
        env['PATH']=os.pathsep.join(p for p in env.get('PATH','').split(os.pathsep) if 'poppler' not in p.lower())
        subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--windowed','--onedir',
                        '--name','OPPOFirmwareTool','--distpath',str(release),'--workpath',str(ROOT/'.build/work'),
                        '--specpath',str(ROOT/'.build'),'--exclude-module','PySide6.QtWebEngineCore',
                        '--exclude-module','PySide6.QtWebEngineWidgets','--add-data',str(ROOT/'oplus_tool/data')+';oplus_tool/data',
                        str(ROOT/'app.py')],cwd=ROOT,check=True,env=env)
    # Clean only diagnostic files created by this project's release validation.
    for path in (target/'DebugConsole.exe',target/'_internal/icuuc.dll.disabled',target/'_internal/icudt78.dll.disabled'):
        path.unlink(missing_ok=True)
    (target/'tools').mkdir(exist_ok=True)
    for name in ['adb.exe','AdbWinApi.dll','AdbWinUsbApi.dll','payload-dumper-go.exe','payload-dumper-xishang.exe','versions.json']:
        shutil.copy2(ROOT/'tools'/name,target/'tools'/name)
    for name in ('README.md','THIRD_PARTY_NOTICES.md','LICENSE'):
        shutil.copy2(ROOT/name,target/name)
    shutil.copytree(ROOT/'licenses',target/'licenses',dirs_exist_ok=True)
    # Supply all installed Python dependency license texts without including caches.
    licenses=target/'licenses/python-packages';licenses.mkdir(parents=True,exist_ok=True)
    for dist in importlib.metadata.distributions():
        if dist.metadata['Name'].lower() not in {'pyside6','pyside6-essentials','pyside6-addons','shiboken6','requests','cryptography','cffi','pycparser','urllib3','certifi','charset-normalizer','idna','pyinstaller','pyinstaller-hooks-contrib'}:continue
        for file in dist.files or []:
            if ('license' in str(file).lower() or 'copying' in file.name.lower() or 'notice' in file.name.lower()) and not str(file).endswith('.py'):
                src=Path(dist.locate_file(file))
                if src.is_file():
                    dest=licenses/dist.metadata['Name']/file;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dest)
    python_license=Path(sys.base_prefix)/'LICENSE.txt'
    if python_license.exists():shutil.copy2(python_license,target/'licenses/Python-LICENSE.txt')
    subprocess.run([str(target/'OPPOFirmwareTool.exe'),'--self-check',str(ROOT/'docs/packaged-check.json')],cwd=target,check=True,timeout=90)
    if not json.loads((ROOT/'docs/packaged-check.json').read_text(encoding='utf-8')).get('ok'):
        raise RuntimeError('打包程序自检失败')
    shutil.copytree(ROOT/'docs',target/'docs',dirs_exist_ok=True)
    # Source ZIP allows modifying/rebuilding vendored LGPL components.
    def include(p):
        if any(part in {'release','debug-release','.build','__pycache__','.git','verification','.venv'} for part in p.parts):return False
        if p.parts[0]=='docs' and p.name in ('debug-check.json','source-check.json'):return False
        if p.parts[0]=='tools':return p.name in {'versions.json','adb.exe','AdbWinApi.dll','AdbWinUsbApi.dll','payload-dumper-go.exe','payload-dumper-xishang.exe'}
        return p.suffix not in {'.pyc','.part'}
    source=release/'OPPOFirmwareTool-Source.zip';zip_dir(ROOT,source,include)
    # Corresponding source archives for unmodified upstream executables.
    for path in (ROOT/'upstream-source').glob('*.zip'):shutil.copy2(path,target/path.name)
    zip_dir(target,release/'OPPOFirmwareTool-Windows-x64.zip')
    hashes=[]
    for p in sorted(release.glob('*.zip')):hashes.append(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name)
    (release/'SHA256SUMS.txt').write_text('\n'.join(hashes)+'\n',encoding='utf-8')
    print('READY:',target/'OPPOFirmwareTool.exe')

if __name__=='__main__':main()
