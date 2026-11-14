from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from urllib.parse import urlsplit
from .common import DATA

def date_from_version(version):
    m = re.search(r'_(20\d{10})(?:\D|$)', version)
    if m:
        try:
            return datetime.strptime(m[1], '%Y%m%d%H%M').isoformat(sep=' ')
        except ValueError:
            pass
    return ''

def major_from_version(version):
    m = re.search(r'(?:ColorOS\s*|[_Vv])(\d{1,2})\.\d', version, re.I)
    return m[1] if m else '未知'

@dataclass
class Package:
    model: str
    region: str
    channel: str
    kind: str
    version: str
    ota: str
    component: str
    url: str
    original_url: str = ''
    size: int = 0
    published: str = ''
    date_source: str = ''
    major: str = '未知'
    base_version: str = ''
    md5: str = ''
    sha256: str = ''
    recipe: dict = field(default_factory=dict)
    fetched: str = ''
    evidence: str = ''
    release_evidence: str = ''
    public_query: bool = False
    @property
    def id(self):
        # Signed query parameters rotate. Preserve distinct components, source
        # versions, delivery channels, hashes and full/delta packages.
        path = urlsplit(self.original_url or self.url)
        key = [self.model,self.region,self.channel,self.kind,self.ota,self.component,
               self.base_version,self.md5,self.sha256,path.hostname,path.path]
        return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
    @property
    def sort_key(self):
        return (self.published or '', self.ota)

class Catalog:
    def __init__(self, path=None):
        self.path = path or DATA / 'catalog.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS packages (id TEXT PRIMARY KEY, model TEXT, region TEXT, body TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS baselines (id TEXT PRIMARY KEY, model TEXT, region TEXT, ota TEXT, body TEXT)')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=20)
        try:
            with db:yield db
        finally:db.close()
    def put(self, packages):
        with self.connect() as db:
            for package in packages:
                package.fetched = datetime.now(timezone.utc).isoformat()
                db.execute('INSERT OR REPLACE INTO packages VALUES (?,?,?,?)',
                           (package.id, package.model, package.region, json.dumps(asdict(package), ensure_ascii=False)))
    def list(self, model, region):
        with self.connect() as db:
            rows = db.execute('SELECT body FROM packages WHERE model=? AND region=?',(model,region)).fetchall()
        return sorted([Package(**json.loads(row[0])) for row in rows], key=lambda p:p.sort_key, reverse=True)
    def replace(self, old_id, package):
        with self.connect() as db:
            db.execute('DELETE FROM packages WHERE id=?',(old_id,))
        self.put([package])
    def export(self, model, region, path):
        # Links may expire. Device credentials are deliberately absent.
        path.write_text(json.dumps([asdict(p) for p in self.list(model,region)],ensure_ascii=False,indent=2),encoding='utf-8')
    def remember_baseline(self, model, region, ota, components, nvid='', source='ADB 设备属性'):
        # Only public firmware component versions are recorded. No device IDs.
        if not model or not region or not ota.startswith(model+'_') or not components:
            return False
        pairs=[]
        for pair in components.split(','):
            name,sep,value=pair.partition(':')
            if not sep or not name.strip() or not value.strip():return False
            pairs.append((name.strip(),value.strip()))
        components=','.join(name+':'+value for name,value in sorted(set(pairs)))
        body={'model':model,'region':region,'ota':ota,'components':components,'nvid':nvid,'source':source}
        key=hashlib.sha256(json.dumps([model,region,ota,components,nvid]).encode()).hexdigest()
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO baselines VALUES (?,?,?,?,?)',
                       (key,model,region,ota,json.dumps(body,ensure_ascii=False)))
        return True
    def baselines(self, model, region):
        with self.connect() as db:
            rows=db.execute('SELECT body FROM baselines WHERE model=? AND region=? ORDER BY ota DESC',(model,region)).fetchall()
        return [json.loads(row[0]) for row in rows]
    def import_known_baselines(self, model, region):
        # Firmware metadata extracted from hash-verified official full packages.
        # The data contains no device credentials and is scoped to model/region.
        path=Path(__file__).resolve().parent/'data/verified_baselines.json'
        try:profiles=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):return 0
        count=0
        for profile in profiles:
            if profile.get('model')!=model or profile.get('region')!=region:continue
            count+=self.remember_baseline(model,region,profile.get('ota',''),profile.get('components',''),
                                         profile.get('nvid',''),'官方全量包 my_manifest（已验证基线）')
        return count
