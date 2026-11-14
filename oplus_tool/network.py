from __future__ import annotations
import io
import json
import os
import re
import time
from pathlib import Path
import requests
from .common import valid_url, check_cancel, atomic_json, file_hash

HEADERS={'User-Agent':'OPlusFirmwareTool/1.0','userId':'oplus-ota|00000001','Accept-Encoding':'identity'}

class RangeReader(io.RawIOBase):
    """Seekable HTTP reader; refuses servers that ignore Range requests."""
    def __init__(self,url,cancel=None,headers=None):
        super().__init__()
        self.url=valid_url(url)
        self.cancel=cancel
        self.session=requests.Session()
        self.headers=HEADERS | (headers or {})
        self.pos=0;self.transferred=0;self.etag='';self.modified=''
        try:
            with self.session.get(self.url,headers=self.headers | {'Range':'bytes=0-0'},stream=True,timeout=(8,25)) as r:
                m=re.fullmatch(r'bytes 0-0/(\d+)',r.headers.get('Content-Range',''))
                if r.status_code!=206 or not m:
                    raise RuntimeError(f'服务器不支持正确的 HTTP Range（HTTP {r.status_code}），请完整下载后本地解包。链接过期时请先刷新。')
                self.size=int(m[1]);self.etag=r.headers.get('ETag','');self.modified=r.headers.get('Last-Modified','')
                if len(r.raw.read(1))!=1:
                    raise IOError('远程文件探测返回空数据')
                self.transferred+=1
        except BaseException:
            self.session.close();raise
    def readable(self):return True
    def seekable(self):return True
    def tell(self):return self.pos
    def seek(self,offset,whence=0):
        new=offset if whence==0 else self.pos+offset if whence==1 else self.size+offset if whence==2 else -1
        if new<0:raise ValueError('无效文件偏移')
        self.pos=new;return new
    def read(self,size=-1):
        if self.cancel:check_cancel(self.cancel)
        if size<0:size=self.size-self.pos
        size=min(size,self.size-self.pos)
        if size<=0:return b''
        if size>64*1024*1024:
            raise ValueError('单次远程读取超过 64 MiB，请完整下载后读取此压缩包。')
        start,end=self.pos,self.pos+size-1
        hdr=self.headers | {'Range':f'bytes={start}-{end}'}
        if self.etag and not self.etag.startswith('W/'):
            hdr['If-Range']=self.etag
        elif self.modified:
            hdr['If-Range']=self.modified
        for attempt in range(3):
            try:
                with self.session.get(self.url,headers=hdr,stream=True,timeout=(8,25)) as r:
                    expected=f'bytes {start}-{end}/{self.size}'
                    if r.status_code!=206 or r.headers.get('Content-Range')!=expected:
                        raise RuntimeError(f'Range 响应异常/链接失效（HTTP {r.status_code}）。请刷新链接；不会自动下载整个包。')
                    if self.etag and r.headers.get('ETag',self.etag)!=self.etag:
                        raise RuntimeError('远程固件文件已变化，已停止读取。')
                    chunks=[];remaining=size
                    while remaining:
                        if self.cancel:check_cancel(self.cancel)
                        chunk=r.raw.read(min(1024*1024,remaining))
                        if not chunk:raise IOError('远程数据提前结束')
                        chunks.append(chunk);remaining-=len(chunk);self.transferred+=len(chunk)
                    data=b''.join(chunks)
                self.pos+=size
                return data
            except (requests.RequestException,IOError):
                if attempt==2:raise
                if self.cancel and self.cancel.wait(.5):check_cancel(self.cancel)
    def readinto(self,b):
        data=self.read(len(b));b[:len(data)]=data;return len(data)
    def close(self):
        self.session.close();super().close()

def download(url,dest,cancel,progress,expected_size=0,md5='',sha256=''):
    url=valid_url(url);dest=Path(dest)
    dest.parent.mkdir(parents=True,exist_ok=True)
    partial=dest.with_name(dest.name+'.part');sidecar=partial.with_name(partial.name+'.json')
    from urllib.parse import urlsplit
    p=urlsplit(url)
    identity={'host':p.hostname,'path':p.path,'size':expected_size,'md5':md5,'sha256':sha256}
    info={}
    if sidecar.is_file():
        try:info=json.loads(sidecar.read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
    start=partial.stat().st_size if partial.exists() and info.get('identity')==identity else 0
    # A validator or a cryptographic package hash is required to append safely.
    if not (md5 or sha256 or info.get('etag') or info.get('modified')):start=0
    headers=HEADERS.copy()
    if start:
        headers['Range']=f'bytes={start}-'
        if info.get('etag') and not info['etag'].startswith('W/'):
            headers['If-Range']=info['etag']
        elif info.get('modified'):headers['If-Range']=info['modified']
    if expected_size and start==expected_size:
        total=start
    else:
        with requests.get(url,headers=headers,stream=True,timeout=(8,30)) as r:
            if r.status_code in (401,403,404):
                raise RuntimeError(f'下载 HTTP {r.status_code}；签名可能失效，请刷新链接后继续。')
            r.raise_for_status()
            if start and r.status_code==206:
                match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',r.headers.get('Content-Range',''))
                if not match or int(match[1])!=start:
                    raise RuntimeError('断点响应偏移不匹配，已停止写入。')
                total=int(match[3])
                if info.get('etag') and r.headers.get('ETag',info['etag'])!=info['etag']:
                    raise RuntimeError('远程文件已变化，请改用新文件名重新下载。')
            else:
                start=0;total=int(r.headers.get('Content-Length',0))
            if expected_size and total and total!=expected_size:
                raise RuntimeError(f'文件大小与查询记录不一致：{total} / {expected_size}')
            total=expected_size or total
            atomic_json(sidecar,{'identity':identity,'etag':r.headers.get('ETag',''),'modified':r.headers.get('Last-Modified','')})
            done=start;then=time.monotonic();last=0
            with partial.open('ab' if start else 'wb') as f:
                for chunk in r.iter_content(1024*1024):
                    check_cancel(cancel)
                    if not chunk:continue
                    f.write(chunk);done+=len(chunk)
                    now=time.monotonic()
                    if now-last>.15:
                        progress(done,total,f'下载 {dest.name} · {(done-start)/max(.01,now-then)/1024/1024:.1f} MiB/s')
                        last=now
            if total and done!=total:raise IOError(f'下载未完成：{done}/{total}，保留断点。')
    check_cancel(cancel)
    if sha256 and file_hash(partial,'sha256',cancel,progress).lower()!=sha256.lower():
        raise RuntimeError('SHA256 校验失败；临时文件已保留，请更换文件名重新下载。')
    if md5 and file_hash(partial,'md5',cancel,progress).lower()!=md5.lower():
        raise RuntimeError('MD5 校验失败；临时文件已保留，请更换文件名重新下载。')
    os.replace(partial,dest)
    sidecar.unlink(missing_ok=True)
    progress(total,total,'下载完成'+('，包哈希校验通过' if md5 or sha256 else '（服务器未提供包哈希）'))
    return dest
