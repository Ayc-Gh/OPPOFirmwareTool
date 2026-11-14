"""Public model identities, independent of any connected device or credentials."""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class ModelProfile:
    brand: str
    model: str
    names: tuple[str, ...]
    codenames: tuple[str, ...]
    region: str = ''

    @property
    def name(self):
        return ' / '.join(self.names) or self.model

    @property
    def label(self):
        return f'{self.name} · {self.model}'

    def matches(self, keyword):
        alias={'OPPO':'欧珀','OnePlus':'一加','realme':'真我'}.get(self.brand,'')
        text=' '.join((self.brand,alias,self.model,*self.names,*self.codenames)).casefold()
        compact=''.join(text.split())
        return all(word in compact for word in keyword.casefold().split())

def load_profiles():
    path=Path(__file__).resolve().parent/'data/model_catalog.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    return [ModelProfile(p['brand'],p['model'],tuple(p['names']),tuple(p['codenames']),p.get('region',''))
            for p in data['models']]
