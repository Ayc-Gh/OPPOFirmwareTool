import hashlib
import lzma
import struct
import zipfile
from pathlib import Path
from oplus_tool.payload import pack_fields

def message(*fields):return pack_fields(fields)
def uint(n,v):return (n,0,v)
def blob(n,v):return (n,2,v)
def extent(start,blocks):return message(uint(1,start),uint(2,blocks))
def part_info(data):return message(uint(1,len(data)),blob(2,hashlib.sha256(data).digest()))

def make_payload(directory,delta=False,compressed=False,unsupported=False):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    images={'boot':b'OPPO-BOOT\0'.ljust(4096,b'B'),'init_boot':b'INIT_BOOT\0'.ljust(8192,b'I'),'vendor':bytes(range(256))*32768}
    parts=[];payload_data=b''
    for i,(name,image) in enumerate(images.items()):
        t=4 if delta and name=='boot' else 8 if name=='init_boot' else 0
        if unsupported and name=='boot':t=9
        data=b'' if t in (4,9) else lzma.compress(image) if t==8 else image
        op=[uint(1,t),uint(2,len(payload_data)),uint(3,len(data)),blob(6,extent(0,len(image)//4096)),uint(7,len(image))]
        if data:op.append(blob(8,hashlib.sha256(data).digest()))
        pf=[blob(1,name.encode()),blob(7,part_info(image))]
        if t in (4,9):
            pf.append(blob(6,part_info(image)))
            op.extend([blob(4,extent(0,len(image)//4096)),uint(5,len(image)),blob(9,hashlib.sha256(image).digest())])
        pf.append(blob(8,message(*op)));parts.append(blob(13,message(*pf)));payload_data+=data
    manifest=message(uint(3,4096),uint(12,2 if delta else 0),*parts)
    content=b'CrAU'+struct.pack('>QQI',2,len(manifest),0)+manifest+payload_data
    path=directory/'payload.bin';path.write_bytes(content)
    archive=directory/'ota.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('META-INF/com/android/metadata','pre-build-incremental=PHY110_16.0.0.100(CN01)\npost-build-incremental=PHY110_17.0.0.100(CN01)\npost-timestamp=1793000000\n')
        z.writestr('payload.bin',content,compress_type=zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED)
    return archive,images
