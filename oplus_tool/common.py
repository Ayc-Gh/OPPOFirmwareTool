from __future__ import annotations
import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
RESOURCE = Path(getattr(sys, '_MEIPASS', ROOT))
HOME = Path(sys.executable).parent if getattr(sys, 'frozen', False) else ROOT
DATA = Path(os.environ.get('LOCALAPPDATA', str(HOME))) / 'OPlusFirmwareTool'
DATA.mkdir(parents=True, exist_ok=True)
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

class Cancelled(Exception):
    pass

def check_cancel(cancel: threading.Event):
    if cancel.is_set():
        raise Cancelled('任务已取消；下载临时文件可继续使用。')

def valid_url(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password:
        raise ValueError('请输入有效的 HTTP/HTTPS 固件链接。')
    return url

def safe_name(value: str) -> str:
    return re.sub(r'[^\w.()\-]+', '_', value, flags=re.UNICODE).strip('._')[:180] or 'firmware'

def size_text(size: int) -> str:
    if not size:
        return '未知'
    n = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f'{n:.2f} {unit}' if unit != 'B' else f'{int(n)} B'
        n /= 1024

def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)

def file_hash(path: Path, algorithm='sha256', cancel=None, progress=None) -> str:
    h = hashlib.new(algorithm)
    total, done = path.stat().st_size, 0
    with path.open('rb') as f:
        while chunk := f.read(4 * 1024 * 1024):
            if cancel:
                check_cancel(cancel)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total, '校验 ' + path.name)
    return h.hexdigest()
