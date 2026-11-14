"""Extract only init_boot from a real cached full OTA using both engines."""
import json
import threading
from datetime import datetime
from pathlib import Path
from oplus_tool.catalog import Catalog
from oplus_tool.payload import inspect
from oplus_tool.engine import extract
from oplus_tool.common import HOME,file_hash

def main():
    p=Catalog().list('PHY110','cn')[0];info=inspect(p.url)
    output=HOME/'verification'/datetime.now().strftime('%Y%m%d-%H%M%S');results=[]
    for engine in ('ssut','xishang'):
        dest=output/engine
        extract(info,['init_boot'],dest,'',threading.Event(),lambda d,t,s:print(s,flush=True),lambda s:print(s,flush=True),engine)
        image=dest/'init_boot.img';results.append({'engine':engine,'size':image.stat().st_size,'sha256':file_hash(image),'path':str(image)})
    assert results[0]['sha256']==results[1]['sha256']
    report={'firmware':p.version,'ota':p.ota,'package_size':p.size,'partition':'init_boot','manifest_sha256':next(x.sha256 for x in info.partitions if x.name=='init_boot'),'results':results}
    (HOME/'docs/real-extraction.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print('VERIFIED',report,flush=True)

if __name__=='__main__':main()
