from __future__ import annotations
import hashlib
import io
import struct
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from .common import check_cancel
from .network import RangeReader

# Schema field numbers follow Android update_engine/update_metadata.proto.
# Unknown fields are retained when staging an online subset for ssut.
OP_NAMES={0:'REPLACE',1:'REPLACE_BZ',2:'MOVE',3:'BSDIFF',4:'SOURCE_COPY',5:'SOURCE_BSDIFF',6:'ZERO',7:'DISCARD',8:'REPLACE_XZ',9:'PUFFDIFF',10:'BROTLI_BSDIFF',11:'ZUCCHINI',12:'LZ4DIFF_BSDIFF',13:'LZ4DIFF_PUFFDIFF',14:'ZSTD'}
SOURCE_OPS={2,3,4,5,9,10,11,12,13}
SSUT_UNSUPPORTED={2,3,9,11,12,13}
XISHANG_SUPPORTED={0,1,4,6,8,10,14}

def varint(data,pos):
    value=0
    for shift in range(0,70,7):
        if pos>=len(data):raise ValueError('Protobuf 被截断')
        b=data[pos];pos+=1;value|=(b&127)<<shift
        if b<128:return value,pos
    raise ValueError('Protobuf 整数超出范围')

def encode_varint(n):
    result=bytearray()
    while n>127:result.append((n&127)|128);n>>=7
    result.append(n);return bytes(result)

def fields(data):
    result=[];pos=0
    while pos<len(data):
        tag,pos=varint(data,pos);number,wire=tag>>3,tag&7
        if number==0:raise ValueError('无效 Protobuf 字段')
        if wire==0:value,pos=varint(data,pos)
        elif wire==2:
            length,pos=varint(data,pos)
            if length>len(data)-pos:raise ValueError('Protobuf 字段被截断')
            value=data[pos:pos+length];pos+=length
        elif wire in (1,5):
            length=8 if wire==1 else 4
            if len(data)-pos<length:raise ValueError('Protobuf 字段被截断')
            value=data[pos:pos+length];pos+=length
        else:raise ValueError(f'不支持 Protobuf wire type {wire}')
        result.append((number,wire,value))
    return result

def pack_fields(values):
    result=bytearray()
    for number,wire,value in values:
        result+=encode_varint(number<<3|wire)
        if wire==0:result+=encode_varint(value)
        elif wire==2:result+=encode_varint(len(value))+value
        else:result+=value
    return bytes(result)

def first(values,number,default=None):
    return next((v for n,w,v in values if n==number),default)

@dataclass
class Partition:
    name:str
    size:int
    sha256:str
    old_size:int=0
    old_sha256:str=''
    operations:list=field(default_factory=list,repr=False)
    raw:bytes=field(default=b'',repr=False)
    has_old_info:bool=False
    @property
    def needs_source(self):return any(op['type'] in SOURCE_OPS for op in self.operations)
    @property
    def download_size(self):return sum(op['length'] for op in self.operations)
    @property
    def operations_text(self):return ', '.join(OP_NAMES.get(t,str(t)) for t in sorted({o['type'] for o in self.operations}))

@dataclass
class PayloadInfo:
    source:str
    partitions:list
    metadata:dict
    manifest:bytes=field(repr=False)
    data_offset:int=0
    payload_offset:int=0
    payload_size:int=0
    remote:bool=False
    compressed:bool=False
    minor_version:int=0
    partial_update:bool=False
    @property
    def is_delta(self):return any(p.needs_source for p in self.partitions)

@contextmanager
def open_source(source,cancel=None):
    reader=RangeReader(source,cancel) if source.startswith(('http://','https://')) else open(source,'rb')
    try:yield reader
    finally:reader.close()

def read_exact(reader,length):
    data=reader.read(length)
    if len(data)!=length:raise ValueError('固件文件被截断')
    return data

def inspect(source,cancel=None):
    source=str(source);remote=source.startswith(('http://','https://'))
    with open_source(source,cancel) as reader:
        magic=read_exact(reader,4);reader.seek(0)
        offset=0;compressed=False;metadata={};archive=None;stream=reader
        reader.seek(0,2);payload_size=reader.tell();reader.seek(0)
        try:
            if magic!=b'CrAU':
                if magic[:2]!=b'PK':raise ValueError('仅支持含 payload.bin 的 OTA ZIP 或原始 payload.bin；不支持加密 OFP/OZIP。')
                archive=zipfile.ZipFile(reader)
                entries=[e for e in archive.infolist() if e.filename.replace('\\','/').split('/')[-1]=='payload.bin']
                if len(entries)!=1:raise ValueError('压缩包必须包含唯一 payload.bin。')
                entry=entries[0];payload_size=entry.file_size;compressed=entry.compress_type!=zipfile.ZIP_STORED
                reader.seek(entry.header_offset)
                header=read_exact(reader,30)
                if header[:4]!=b'PK\x03\x04':raise ValueError('ZIP 本地文件头无效')
                name_len,extra_len=struct.unpack('<HH',header[26:30])
                offset=entry.header_offset+30+name_len+extra_len
                stream=archive.open(entry)
                for name in ('META-INF/com/android/metadata','metadata'):
                    try:
                        meta=archive.getinfo(name)
                        if meta.file_size>2*1024*1024:continue
                        metadata=dict(line.split('=',1) for line in archive.read(meta).decode('utf-8','replace').splitlines() if '=' in line)
                        break
                    except KeyError:pass
            header=read_exact(stream,20)
            if header[:4]!=b'CrAU':raise ValueError('payload.bin 魔数错误')
            version,manifest_len=struct.unpack('>QQ',header[4:])
            if version!=2:raise ValueError(f'仅支持 payload v2，当前为 v{version}')
            sig_len=struct.unpack('>I',read_exact(stream,4))[0]
            if not 0<manifest_len<=64*1024*1024 or 24+manifest_len+sig_len>payload_size:
                raise ValueError('payload manifest 长度异常')
            manifest=read_exact(stream,manifest_len)
            data_offset=24+manifest_len+sig_len
            parts=[]
            manifest_fields=fields(manifest)
            for n,w,raw in manifest_fields:
                if n!=13:continue
                pf=fields(raw);name=first(pf,1,b'').decode('utf-8')
                import re
                if not re.fullmatch(r'[a-zA-Z0-9_\-]+',name):raise ValueError('分区名称不安全：'+name)
                new=fields(first(pf,7,b''));old=fields(first(pf,6,b''));ops=[]
                for no,wi,value in pf:
                    if no!=8:continue
                    of=fields(value);t=first(of,1)
                    if t is None:raise ValueError('分区操作缺少类型')
                    off,length=first(of,2,0),first(of,3,0)
                    if data_offset+off+length>payload_size:raise ValueError('操作数据超出 payload 边界')
                    ops.append({'type':t,'offset':off,'length':length,'raw':value})
                parts.append(Partition(name,first(new,1,0),first(new,2,b'').hex(),first(old,1,0),first(old,2,b'').hex(),ops,raw,
                                       has_old_info=first(pf,6) is not None))
            if not parts:raise ValueError('payload 没有分区列表')
            if len({p.name for p in parts})!=len(parts):raise ValueError('payload 分区名重复')
            return PayloadInfo(source,parts,metadata,manifest,data_offset,offset,payload_size,remote,compressed,
                               minor_version=first(manifest_fields,12,0),partial_update=bool(first(manifest_fields,16,0)))
        finally:
            if stream is not reader:stream.close()
            if archive:archive.close()

def stage_subset(info,selected,dest,cancel,progress):
    """Repack selected remote operation blobs, preserving operation/image hashes.

    The temporary payload is an extraction intermediate, never an installable OTA.
    Metadata signatures are discarded because the manifest offsets change.
    """
    if info.compressed:raise ValueError('远程 ZIP 的 payload 被压缩，无法按范围提取，请先完整下载。')
    selected=set(selected);chunks=[];offset=0;part_raw={}
    for part in info.partitions:
        if part.name not in selected:continue
        pf=[]
        for n,w,value in fields(part.raw):
            if n==8:
                of=fields(value);length=first(of,3,0);old_offset=first(of,2,0)
                # Replace or insert the data offset even if zero was omitted.
                of=[(a,b,v) for a,b,v in of if a!=2]
                of.append((2,0,offset))
                value=pack_fields(of)
                if length:
                    chunks.append((info.payload_offset+info.data_offset+old_offset,length))
                    offset+=length
            pf.append((n,w,value))
        part_raw[part.name]=pack_fields(pf)
    mf=[]
    for n,w,value in fields(info.manifest):
        if n in (4,5):continue
        if n==13:
            name=first(fields(value),1,b'').decode()
            if name not in selected:continue
            value=part_raw[name]
        mf.append((n,w,value))
    manifest=pack_fields(mf)
    done=0
    with open_source(info.source,cancel) as reader,Path(dest).open('wb') as out:
        out.write(b'CrAU'+struct.pack('>QQI',2,len(manifest),0)+manifest)
        for start,length in chunks:
            reader.seek(start)
            while length:
                check_cancel(cancel)
                n=min(length,4*1024*1024)
                data=read_exact(reader,n);out.write(data);length-=n;done+=n
                progress(done,offset,'按需获取所选分区的 payload 数据')
    return Path(dest)
