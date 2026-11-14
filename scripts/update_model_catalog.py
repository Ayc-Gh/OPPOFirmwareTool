"""Refresh factual names/model/device codes from Google's public device list."""
import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import requests

URL='https://storage.googleapis.com/play_public/supported_devices.csv'
DEST=Path(__file__).resolve().parents[1]/'oplus_tool/data/model_catalog.json'

def main():
    response=requests.get(URL,timeout=(10,60));response.raise_for_status();raw=response.content
    text=raw.decode('utf-16' if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig')
    groups={};brands={'oppo':'OPPO','oneplus':'OnePlus','realme':'realme'}
    for row in csv.DictReader(io.StringIO(text)):
        brand=brands.get(row['Retail Branding'].strip().lower());model=row['Model'].strip()
        if not brand or not re.fullmatch(r'[A-Za-z][A-Za-z0-9.\-]{2,23}',model):continue
        key=(brand,model)
        profile=groups.setdefault(key,{'brand':brand,'model':model,'names':set(),'codenames':set(),
            # Mainland P-series codes are distinct from international CPH codes.
            # Other variants require the user to select their actual sales region.
            'region':'cn' if brand in ('OPPO','OnePlus') and re.fullmatch(r'P[A-Z]{2,3}\d{2,3}',model) else ''})
        name=row['Marketing Name'].strip();code=row['Device'].strip()
        if name:profile['names'].add(name if name.lower().startswith(brand.lower()) else brand+' '+name)
        if code:profile['codenames'].add(code)
    models=[]
    for key,profile in sorted(groups.items()):
        profile['names']=sorted(profile['names']);profile['codenames']=sorted(profile['codenames'])
        models.append(profile)
    if not any(p['model']=='PHY110' and p['brand']=='OPPO' for p in models):
        raise RuntimeError('Downloaded model list failed validation; existing catalog retained')
    data={'source':URL,'source_help':'https://support.google.com/googleplay/answer/1727131',
          'retrieved_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
          'source_sha256':hashlib.sha256(raw).hexdigest(),
          'note':'Public identity facts only; inclusion does not guarantee payload OTA availability. Region is a suggested default and is editable.',
          'models':models}
    DEST.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Updated',len(models),'model profiles')

if __name__=='__main__':main()
