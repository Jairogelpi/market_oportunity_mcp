"""OECD Data Explorer, SDMX REST API (SDMX-JSON). Docs: https://www.oecd.org/en/data/insights/data-explainers/2024/09/api.html

The query is "<dataflow>/<key>": dataflow = "AGENCY,ID,VERSION" (e.g. "OECD.SDD.TPS,DSD_SDBSBSC_ISIC4@DF_SDBS_ISIC4,1.0")
and key = dot-separated dimension values in dataflow order, '+' for several, empty for all
(e.g. "A.USA+DEU.ENTR.J._T.ENT"). Options: start / end (year). No lastNObservations: the endpoint answers it
with HTTP 413 even alone (checked live).
404 "NoResultsFound" means an empty selection (checked live). UNIT_MULT is always made explicit: value x 10^mult.
OECD compiles national statistics: secondary evidence, one statistical lineage."""
import re
from urllib.parse import urlencode
from ..http_client import Policy, ConnectorFailure, NotFound
from .base import Connector, Hit, clip

API = 'https://sdmx.oecd.org/public/rest/data/{flow}/{key}'
MAX_HITS = 60
FLOW = re.compile(r'[A-Za-z0-9_.\-]{2,40},[A-Za-z0-9_@.\-]{2,80},\d+(\.\d+){0,2}')
KEY = re.compile(r'[A-Za-z0-9_+\-]*(\.[A-Za-z0-9_+\-]*){0,20}')

def split_query(query):
    flow,_,key=query.partition('/')
    if not FLOW.fullmatch(flow) or not KEY.fullmatch(key):
        raise ValueError('Consulta OECD: "<AGENCIA,DATAFLOW,VERSIÓN>/<clave>", p. ej. '
                         '"OECD.SDD.TPS,DSD_SDBSBSC_ISIC4@DF_SDBS_ISIC4,1.0/A.USA.ENTR.J._T.ENT"')
    if not key.strip('.'):
        raise ValueError('Consulta OECD sin clave: filtra al menos una dimensión (un dataset completo supera el límite de tamaño)')
    return flow,key

class Oecd(Connector):
    evidence_kind = 'secondary'
    OPTIONS = {'start':r'(19|20)\d{2}','end':r'(19|20)\d{2}'}
    QUERY_HELP = ('OECD necesita query="<AGENCIA,DATAFLOW,VERSIÓN>/<clave>", localizado en el OECD Data Explorer '
                  '(p. ej. "OECD.SDD.TPS,DSD_SDBSBSC_ISIC4@DF_SDBS_ISIC4,1.0/A.USA.ENTR.J._T.ENT")')

    def __init__(self):
        self.policy=Policy('oecd',frozenset({'sdmx.oecd.org'}),min_interval=3.0,timeout=60)

    def build_query(self, terms):
        return ''

    def validate_query(self, query):
        split_query(query)

    def search(self, fetch, query, options):
        flow,key=split_query(query)
        params={'dimensionAtObservation':'AllDimensions','format':'jsondata'}
        if 'start' in options: params['startPeriod']=options['start']
        if 'end' in options: params['endPeriod']=options['end']
        try:
            data,_,_=fetch(API.format(flow=flow,key=key),params)
        except NotFound:
            return []  # OECD answers an empty selection with 404 NoResultsFound
        try:
            ds=data['data']['dataSets'][0]
            st=data['data'].get('structure') or data['data']['structures'][0]
            dims=st['dimensions']['observation']
        except (KeyError,IndexError,TypeError):
            raise ConnectorFailure('Respuesta SDMX-JSON de OECD con formato inesperado')
        return self._hits(flow,st,dims,ds.get('observations') or {})

    def _hits(self, flow, st, dims, observations):
        attrs=[(a['id'],a.get('values') or []) for a in (st.get('attributes') or {}).get('observation') or []]
        name=st.get('name') or flow
        keys=sorted(observations,key=lambda k:[int(i) for i in k.split(':')])
        hits=[]
        for k in keys[:MAX_HITS]:
            obs=observations[k]; idx=[int(i) for i in k.split(':')]
            coords=[(d['id'],d.get('name',d['id']),d['values'][i]) for d,i in zip(dims,idx)]
            at={}
            for (aid,values),ai in zip(attrs,obs[1:]):
                if isinstance(ai,int) and 0<=ai<len(values): at[aid]=values[ai].get('id')
            mult=at.get('UNIT_MULT') or '0'
            parts=[f"{dname}: {v.get('name',v['id'])} ({v['id']})" for _,dname,v in coords]
            value=obs[0]
            snippet=(f'{name}. '+'; '.join(parts)+f'; valor: {value}'+(f' × 10^{mult}' if mult!='0' else '')+
                     (f" [estado: {at['OBS_STATUS']}]" if at.get('OBS_STATUS') not in (None,'A') else '')+
                     f'. Fuente: OECD Data Explorer (SDMX), dataflow {flow}.')
            series_key='.'.join(v['id'] for did,_,v in coords if did!='TIME_PERIOD')
            period=next((v['id'] for did,_,v in coords if did=='TIME_PERIOD'),None)
            url=API.format(flow=flow,key=series_key)+('?'+urlencode({'startPeriod':period,'endPeriod':period}) if period else '')
            hits.append(Hit(url=url,title=f'{name} — {series_key} {period or ""}'.strip(),published_at='unknown',
                            snippet=clip(snippet),independence_group='oecd',
                            metadata={'dataflow':flow,'observation':{did:v['id'] for did,_,v in coords},'value':value,
                                      'unit_mult':mult,'obs_status':at.get('OBS_STATUS'),
                                      'total_observations':len(keys),'truncated':len(keys)>MAX_HITS}))
        return hits
