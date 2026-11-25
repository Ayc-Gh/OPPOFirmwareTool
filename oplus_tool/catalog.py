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
from vendor.oplus_tracker.config import OTA_REGION_CONFIG
from .common import DATA

MANIFEST_EVIDENCE = '已读取 payload manifest 核验'

def effective_nvid(region, nvid):
    """The OTA client sends the region's carrier ID when the override is empty."""
    return nvid or OTA_REGION_CONFIG.get(region, {}).get('carrier_id', '')

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

    def __post_init__(self):
        if self.recipe and not self.recipe.get('nvid'):
            self.recipe = self.recipe | {'nvid': effective_nvid(self.region, '')}

    @property
    def nvid(self):
        return effective_nvid(self.region, self.recipe.get('nvid'))

    def matches_target(self, model, region, nvid):
        return (self.model, self.region, self.nvid) == (model, region, effective_nvid(region, nvid))

    @property
    def _download_path(self):
        path = urlsplit(self.original_url or self.url)
        return path.hostname, path.path

    @property
    def artifact_key(self):
        return json.dumps([self.model, self.region, self.nvid, self.channel, self.ota,
                           self.component, self.version], ensure_ascii=False)

    def same_artifact(self, other):
        if self.artifact_key != other.artifact_key:
            return False
        shared_digest = False
        for attr, length in (('sha256', 64), ('md5', 32)):
            left, right = getattr(self, attr), getattr(other, attr)
            if left and right:
                if left.lower() != right.lower() or not re.fullmatch(f'[a-fA-F0-9]{{{length}}}', left):
                    return False
                shared_digest = True
        if shared_digest:
            return True
        # Without comparable digests only two digest-free records at the same
        # original host/path can be associated. Ignore rotating URL signatures.
        return not (self.sha256 or self.md5 or other.sha256 or other.md5) and self._download_path == other._download_path

    @property
    def id(self):
        # Signed query parameters rotate. Preserve distinct components, source
        # versions, delivery channels, hashes and full/delta packages.
        key = [self.model, self.region, self.nvid, self.channel, self.ota, self.component,
               self.md5, self.sha256, *self._download_path, self.kind, self.base_version]
        return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
    @property
    def sort_key(self):
        return (self.published or '', self.ota)

class Catalog:
    def __init__(self, path=None):
        self.path = path or DATA / 'catalog.sqlite3'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS packages (id TEXT PRIMARY KEY, model TEXT, region TEXT, body TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS baselines (id TEXT PRIMARY KEY, model TEXT, region TEXT, ota TEXT, body TEXT)')
            if 'nvid' not in {row[1] for row in db.execute('PRAGMA table_info(packages)')}:
                db.execute('ALTER TABLE packages ADD COLUMN nvid TEXT')
                db.execute('ALTER TABLE packages ADD COLUMN artifact_key TEXT')
                for old_id, body in db.execute('SELECT id, body FROM packages').fetchall():
                    package = Package(**json.loads(body))
                    db.execute('UPDATE packages SET id=?, nvid=?, artifact_key=?, body=? WHERE id=?',
                               (package.id, package.nvid, package.artifact_key,
                                json.dumps(asdict(package), ensure_ascii=False), old_id))
                db.execute('CREATE INDEX packages_target ON packages (model, region, nvid)')
                db.execute('CREATE INDEX packages_artifact ON packages (artifact_key)')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=20)
        try:
            with db:yield db
        finally:db.close()
    def _put(self, db, package):
        # A fresh OPEX response corrects records misclassified by older clients.
        is_patch = package.kind == '补丁包'
        if is_patch or package.evidence == MANIFEST_EVIDENCE:
            rows = db.execute('SELECT id, body FROM packages WHERE artifact_key=?',
                              (package.artifact_key,)).fetchall()
            for old_id, body in rows:
                old = Package(**json.loads(body))
                if not package.same_artifact(old):
                    continue
                if old.kind == '补丁包' and not is_patch:
                    return
                if is_patch and (old.kind == '补丁包' or old.evidence == MANIFEST_EVIDENCE):
                    db.execute('DELETE FROM packages WHERE id=?', (old_id,))
        package.fetched = datetime.now(timezone.utc).isoformat()
        db.execute('INSERT OR REPLACE INTO packages (id,model,region,nvid,artifact_key,body) VALUES (?,?,?,?,?,?)',
                   (package.id, package.model, package.region, package.nvid, package.artifact_key,
                    json.dumps(asdict(package), ensure_ascii=False)))

    def put(self, packages):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for package in packages:
                self._put(db, package)
    def list(self, model, region, nvid=''):
        with self.connect() as db:
            rows = db.execute('SELECT body FROM packages WHERE model=? AND region=? AND nvid=?',
                              (model, region, effective_nvid(region, nvid))).fetchall()
        return sorted([Package(**json.loads(row[0])) for row in rows], key=lambda p:p.sort_key, reverse=True)
    def replace(self, old_id, package):
        with self.connect() as db:
            db.execute('DELETE FROM packages WHERE id=?',(old_id,))
            self._put(db, package)
    def export(self, model, region, path, nvid=''):
        # Links may expire. Device credentials are deliberately absent.
        path.write_text(json.dumps([asdict(p) for p in self.list(model,region,nvid)],ensure_ascii=False,indent=2),encoding='utf-8')
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
