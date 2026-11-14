"""Fetch pinned upstream extraction engines and record artifact SHA256 values."""
from pathlib import Path
import hashlib
import io
import json
import tarfile
import zipfile
import requests

ROOT=Path(__file__).resolve().parent
TOOLS=ROOT/'tools'
ASSETS={
    'ssut':('https://github.com/ssut/payload-dumper-go/releases/download/2.1.0/payload-dumper-go_2.1.0_windows_amd64.tar.gz','payload-dumper-go.exe'),
    'xishang':('https://github.com/xishang0128/payload-dumper-go/releases/download/v0.1.6/payload-dumper-windows-amd64-v3.zip','payload-dumper-xishang.exe'),
}

def main():
    TOOLS.mkdir(exist_ok=True);manifest={}
    for engine,(url,name) in ASSETS.items():
        r=requests.get(url,timeout=(10,120));r.raise_for_status();data=r.content
        archive_hash=hashlib.sha256(data).hexdigest()
        if engine=='ssut':
            checks=requests.get('https://github.com/ssut/payload-dumper-go/releases/download/2.1.0/payload-dumper-go_sha256checksums.txt',timeout=30)
            checks.raise_for_status()
            line=next(line for line in checks.text.splitlines() if line.strip().endswith(url.split('/')[-1]))
            if line.split()[0]!=archive_hash:raise RuntimeError('ssut 官方 SHA256 不匹配')
            with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
                member=next(m for m in archive.getmembers() if m.name.endswith('.exe'))
                binary=archive.extractfile(member).read()
        else:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                member=next(n for n in archive.namelist() if n.endswith('.exe'))
                binary=archive.read(member)
        (TOOLS/name).write_bytes(binary)
        manifest[engine]={'url':url,'archive_sha256':archive_hash,'binary_sha256':hashlib.sha256(binary).hexdigest(),
                          'verification':'upstream checksums' if engine=='ssut' else 'HTTPS GitHub release; locally recorded hash'}
        print(engine,name,len(binary))
    (TOOLS/'versions.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')

if __name__=='__main__':main()
