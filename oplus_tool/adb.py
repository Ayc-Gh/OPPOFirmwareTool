from __future__ import annotations
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from .common import RESOURCE, HOME, CREATE_NO_WINDOW, Cancelled

GUID_FILE = '/data/system/openid_config.xml'
DUID_FILE = '/data/user/0/com.oplus.ota/shared_prefs/cache.xml'

@dataclass
class RootIdentifiers:
    serial: str
    guid: str = field(default='', repr=False)
    duid: str = field(default='', repr=False)
    notes: list[str] = field(default_factory=list)

def valid_identifier(value):
    value = (value or '').strip()
    if re.fullmatch(r'[0-9a-fA-F]{64}', value) and value.lower() not in ('0' * 64, 'f' * 64):
        return value
    return ''

def parse_root_identifier(text, kind):
    """Read only the named identifier; never treat unrelated IDs as substitutes."""
    root = ET.fromstring(text)
    for node in root.iter():
        if kind == 'guid' and node.tag.rsplit('}', 1)[-1].lower() == 'guid':
            value = valid_identifier(node.get('value') or node.text)
        elif kind == 'duid' and node.get('name') == 'DUID':
            value = valid_identifier(node.text or node.get('value'))
        else:
            continue
        if value:
            return value
    return ''

def read_root_identifiers(serial, custom='', cancel=None):
    """Explicit opt-in entry point. Device scans and normal queries never call it."""
    if not serial:
        raise ValueError('请先选择已连接的设备。')
    def checkpoint():
        if cancel is not None and cancel.is_set():
            raise Cancelled('已取消 root 读取；保留已有 GUID / DUID。')
    def root_command(text):
        return command(['-s', serial, 'shell', 'su', '-c', text], custom, timeout=30)
    checkpoint()
    try:
        identity = root_command('id')
    except subprocess.TimeoutExpired:
        raise RuntimeError('等待 root 授权超时，请在手机上检查授权后重试。') from None
    except Exception:
        raise RuntimeError('未能取得 root 权限，请确认设备已 root 并允许 ADB shell 使用 su。') from None
    checkpoint()
    if not re.search(r'\buid=0\b', identity):
        raise RuntimeError('su 未返回 root 身份；保留已有 GUID / DUID。')
    result = RootIdentifiers(serial)
    for kind, path in (('guid', GUID_FILE), ('duid', DUID_FILE)):
        checkpoint()
        try:
            text = root_command('head -c 1048577 ' + path)
            if len(text.encode('utf-8')) > 1024 * 1024:
                raise ValueError('配置文件过大')
            value = parse_root_identifier(text, kind)
            if value:
                setattr(result, kind, value)
            else:
                result.notes.append(f'{kind.upper()} 配置中没有有效的 64 位十六进制标识，保留已有输入。')
        except ET.ParseError:
            result.notes.append(f'{kind.upper()} 配置格式无法识别，保留已有输入。')
        except Exception:
            result.notes.append(f'{kind.upper()} 配置无法读取，可能尚未生成或当前系统路径不同；保留已有输入。')
    checkpoint()
    return result

@dataclass
class Device:
    serial: str
    name: str = ''
    model: str = ''
    codename: str = ''
    firmware: str = ''
    ota: str = ''
    slot: str = ''
    region: str = ''
    coloros: str = ''
    project: str = ''
    nvid: str = ''
    brand: str = ''
    guid: str = ''
    duid: str = ''
    hardware_serial: str = ''
    components: str = ''
    state: str = 'device'
    props: dict = field(default_factory=dict, repr=False)

    @property
    def label(self):
        return f'{self.name or self.model or "OPlus 设备"} · {self.serial} [{self.state}]'

def adb_path(custom='') -> str:
    candidates = [Path(custom)] if custom else []
    candidates += [RESOURCE / 'tools/adb.exe', HOME / 'tools/adb.exe', HOME.parent / 'platform-tools/adb.exe']
    for path in candidates:
        if path.is_file():
            return str(path)
    path = shutil.which('adb')
    if path:
        return path
    raise FileNotFoundError('未找到 ADB。请在设置中指定 adb.exe，或将 platform-tools 放到 tools 目录。')

def command(args, custom='', timeout=20):
    r = subprocess.run([adb_path(custom), *args], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or 'ADB 命令失败')
    return r.stdout

def parse_props(text):
    return dict(re.findall(r'^\[([^\]]+)\]: \[(.*)\]\r?$', text, re.M))

def device_from_props(serial, p):
    def first(*keys):
        return next((p[k] for k in keys if p.get(k)), '')
    brand = first('ro.product.vendor.brand', 'ro.product.brand', 'ro.product.manufacturer')
    if not any(b in brand.lower() for b in ('oppo', 'oplus', 'oneplus', 'realme')):
        return None
    ota = first('ro.build.version.ota', 'ro.build.display.ota')
    model = ota.split('_')[0] if ota else first('ro.product.model', 'ro.product.vendor.model')
    nvid = first('ro.build.oplus_nv_id', 'ro.boot.oplus_nv_id', 'ro.oppo.oplus_nv_id')
    mark = first('ro.vendor.oplus.regionmark', 'ro.oplus.pipeline.region', 'persist.sys.oplus.region', 'ro.oppo.regionmark').lower()
    region_map = {'cn':'cn', 'china':'cn', 'eu':'eu', 'eea':'eu', 'india':'in', 'in':'in', 'export':'sg', 'global':'gl', 'oversea':'sg'}
    nvid_map = {'10010111':'cn', '10011000':'cn_cmcc', '01000100':'eu', '00011011':'in', '01011010':'sg', '10100111':'gl'}
    region = nvid_map.get(nvid) or region_map.get(mark, mark)
    # Regional SKU, never infer OTA region from the user-selected UI language.
    if not region:
        display = first('ro.build.display.id').upper()
        region = 'cn' if '(CN' in display else 'in' if '(IN' in display else 'eu' if '(EX' in display and 'EEA' in model else ''
    comp = []
    for key, value in sorted(p.items()):
        if key.startswith('ro.oplus.version.') and value:
            name = key.removeprefix('ro.oplus.version.')
            comp.append(f'{"system_vendor" if name == "base" else name}:{value}')
    return Device(serial=serial, name=first('ro.vendor.oplus.market.name', 'ro.oppo.market.name', 'ro.product.marketname', 'ro.product.model'),
                  model=model, codename=first('ro.product.device', 'ro.product.vendor.device'),
                  firmware=first('ro.build.display.id', 'ro.build.display.full_id'), ota=ota,
                  slot=first('ro.boot.slot_suffix', 'ro.boot.slot').lstrip('_') or '未知',
                  region=region, coloros=first('ro.build.version.oplusrom.display', 'ro.build.version.oplusrom', 'ro.build.version.opporom'),
                  project=first('ro.boot.prjname', 'ro.boot.project_name'), nvid=nvid, brand=brand,
                  guid=first('persist.sys.oplus.guid', 'ro.oplus.guid'),
                  duid=first('persist.sys.oplus.duid', 'ro.oplus.duid'),
                  hardware_serial=first('ro.serialno','ro.boot.serialno') or serial, components=','.join(comp), props=p)

def scan(custom=''):
    devices, notes = [], []
    for line in command(['devices', '-l'], custom).splitlines()[1:]:
        fields = line.split()
        if len(fields) < 2:
            continue
        serial, state = fields[:2]
        if state != 'device':
            notes.append(f'{serial}: {state}（请在手机授权 USB 调试或重新连接）')
            continue
        try:
            p = parse_props(command(['-s', serial, 'shell', 'getprop'], custom))
            device = device_from_props(serial, p)
            if device:
                devices.append(device)
        except Exception as e:
            notes.append(f'{serial}: {e}')
    return devices, notes
