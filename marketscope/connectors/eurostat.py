"""Eurostat Statistics API, JSON-stat 2.0.
Docs: https://ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access/api-getting-started/api

The query is a dataset code (find it in the Eurostat Data Browser). Option `filters` ("dim=code,code;dim2=code")
maps to dimension query parameters; time selectors (sinceTimePeriod, untilTimePeriod, lastTimePeriod) and
geoLevel are accepted the same way. Without a geo filter, the study geography is used.
Each observation becomes one hit whose URL reproduces exactly that observation."""
import re
from urllib.parse import urlencode
from ..http_client import Policy, ConnectorFailure
from .base import Connector, Hit, clip, iso_datetime

API = 'https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/'
MAX_HITS = 60
GEO_ALIASES = {'GR':'EL','GB':'UK','EU':'EU27_2020'}
_CODE = r'[A-Za-z0-9_.\-]{1,40}'
_PAIR = rf'[A-Za-z_]{{1,30}}={_CODE}(,{_CODE}){{0,30}}'

def parse_filters(text):
    return [(dim,vals.split(',')) for dim,vals in (p.split('=',1) for p in text.split(';') if p)]

def _fmt(value):
    return str(int(value)) if isinstance(value,float) and value.is_integer() else str(value)

class Eurostat(Connector):
    evidence_kind = 'primary'
    OPTIONS = {'filters':rf'{_PAIR}(;{_PAIR}){{0,15}}'}
    QUERY_HELP = ('Eurostat necesita query=<código de dataset> (p. ej. isoc_eb_ai), localizado en el Data Browser de Eurostat; '
                  'filtra con options {"filters":"indic_is=E_AI_TANY;size_emp=GE10;lastTimePeriod=3"}')

    def __init__(self):
        self.policy=Policy('eurostat',frozenset({'ec.europa.eu'}),min_interval=1.0)

    def build_query(self, terms):
        return ''  # no free-text search: the agent must name the dataset

    def validate_query(self, query):
        if not re.fullmatch(r'[A-Za-z0-9_]{2,40}',query):
            raise ValueError('Código de dataset de Eurostat no válido (solo letras, dígitos y _)')

    def search(self, fetch, query, options):
        filters=parse_filters(options.get('filters',''))
        geo=self.terms.get('geography')
        if geo and geo!='GLOBAL' and not any(d=='geo' for d,_ in filters):
            filters.append(('geo',[GEO_ALIASES.get(geo,geo)]))
        params=[('lang','EN')]+[(d,v) for d,vals in filters for v in vals]
        data,_,_=fetch(API+query,params)
        if not isinstance(data,dict) or not isinstance(data.get('id'),list) or not isinstance(data.get('dimension'),dict):
            raise ConnectorFailure('Respuesta de Eurostat sin estructura JSON-stat')
        return self._hits(query,data)

    def _hits(self, dataset, data):
        ids,sizes,dims=data['id'],data['size'],data['dimension']
        values=data.get('value') or {}
        if isinstance(values,list): values={str(i):v for i,v in enumerate(values) if v is not None}
        status=data.get('status') or {}
        if isinstance(status,list): status={str(i):s for i,s in enumerate(status) if s}
        codes=[{pos:code for code,pos in dims[d]['category']['index'].items()} for d in ids]
        label,updated=data.get('label',dataset),iso_datetime(data.get('updated'))
        keys=sorted(values,key=int)
        hits=[]
        for key in keys[:MAX_HITS]:
            idx,coords=int(key),[]
            for d,size,lookup in reversed(list(zip(ids,sizes,codes))):
                coords.append((d,lookup[idx%size])); idx//=size
            coords.reverse()
            parts=[f"{dims[d]['label']}: {dims[d]['category']['label'].get(c,c)} ({c})" for d,c in coords]
            value=_fmt(values[key]); flag=status.get(key)
            snippet=(f"{label}. {'; '.join(parts)}; valor: {value}"+(f' [estado: {flag}]' if flag else '')+
                     f'. Fuente: Eurostat, dataset {dataset}, actualizado {data.get("updated","")}.')
            hits.append(Hit(url=API+dataset+'?'+urlencode([('lang','EN')]+coords),title=label,published_at=updated,
                            snippet=clip(snippet),independence_group='eurostat',
                            metadata={'dataset':dataset,'observation':dict(coords),'value':values[key],'status':flag,
                                      'total_observations':len(keys),'truncated':len(keys)>MAX_HITS}))
        return hits
