"""Read-only device/OTA integration check; no flashing or full-ROM downloads."""
import json
import threading
from dataclasses import asdict
from pathlib import Path
from oplus_tool import adb,tracker
from oplus_tool.catalog import Catalog
from oplus_tool.common import HOME

def main():
    devices,notes=adb.scan();print('Devices:',[(d.name,d.model,d.slot,d.region) for d in devices],notes,flush=True)
    if not devices:raise SystemExit('No authorized OPlus device')
    d=devices[0];options=tracker.QueryOptions(d.model,d.region,d.ota,d.components,d.guid,d.duid,d.project,d.hardware_serial,d.nvid)
    db=Catalog();db.remember_baseline(d.model,d.region,d.ota,d.components,d.nvid)
    db.import_known_baselines(d.model,d.region);logs=[];count=0
    def log(s):logs.append(s);print(s,flush=True)
    def emit(packages):db.put(packages)
    tracker.query_all(options,threading.Event(),lambda a,b,s:print(f'[{a}/{b}] {s}',flush=True),log,emit,baselines=db.baselines(d.model,d.region))
    records=db.list(d.model,d.region)
    summary={'device':{'name':d.name,'model':d.model,'codename':d.codename,'firmware':d.firmware,'slot':d.slot,'region':d.region,'coloros':d.coloros},
             'packages':[{'channel':p.channel,'kind':p.kind,'version':p.version,'size':p.size,'published':p.published,'major':p.major,'component':p.component,'evidence':p.evidence,'base_version':p.base_version} for p in records],
             'logs':logs}
    path=HOME/'docs/live-check.json';path.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print('SUMMARY',path,len(records),flush=True)

if __name__=='__main__':main()
