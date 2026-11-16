"""US Census Bureau Data API. Docs: https://www.census.gov/data/developers/guidance/api-user-guide.html

Every query needs a free key (checked live: requests without one redirect to missing_key.html), read from
MARKETSCOPE_CENSUS_KEY and redacted from logged URLs. The query is a dataset path (e.g. "2022/cbp",
"2022/abscs"); option `get` lists 1-6 variables (e.g. "NAME,ESTAB,EMP"), `for` / `in` the geography
(default "us:*"), `filters` extra predicates ("NAICS2017=5112;EMPSZES=001"). Variable labels are fetched
(one small request each) so evidence reads "Number of establishments", not "ESTAB"."""
import os
import re
from urllib.parse import urlencode
from ..http_client import Policy, ConnectorFailure, NotFound
from .base import Connector, Hit, clip

API = 'https://api.census.gov/data/{dataset}'
VARIABLE = 'https://api.census.gov/data/{dataset}/variables/{var}.json'
MAX_HITS = 60
MAX_VARS = 6
_PRED = r'[A-Za-z0-9_]{1,40}=[A-Za-z0-9_*:,\-]{1,60}'

class UsCensus(Connector):
    evidence_kind = 'primary'
    OPTIONS = {'get':r'[A-Za-z0-9_]{1,40}(,[A-Za-z0-9_]{1,40}){0,5}','for':r'[a-z ]{2,60}:[0-9*,]{1,60}',
               'in':r'[a-z ]{2,60}:[0-9*,]{1,60}( [a-z ]{2,60}:[0-9*,]{1,60}){0,3}','filters':rf'{_PRED}(;{_PRED}){{0,9}}'}
    REQUIRED_OPTIONS = ('get',)
    QUERY_HELP = 'US Census necesita query=<dataset> (p. ej. "2022/cbp") y options {"get":"NAME,ESTAB,EMP","filters":"NAICS2017=5112"}'

    def __init__(self):
        self.key=os.environ.get('MARKETSCOPE_CENSUS_KEY','').strip()
        self.policy=Policy('us_census',frozenset({'api.census.gov'}),min_interval=0.5,redact_params=frozenset({'key'}))

    def build_query(self, terms):
        return ''

    def validate_query(self, query):
        if not self.key:
            raise ValueError('La API del Census exige clave gratuita: solicítala en https://api.census.gov/data/key_signup.html '
                             'y define MARKETSCOPE_CENSUS_KEY en .env. No se ha enviado ninguna petición.')
        if not re.fullmatch(r'(\d{4}|timeseries)(/[a-z0-9]{2,30}){1,4}',query):
            raise ValueError('Dataset del Census no válido, p. ej. "2022/cbp" o "timeseries/eits/marts"')

    def search(self, fetch, query, options):
        variables=options['get'].split(',')
        params=[('get',options['get']),('for',options.get('for','us:*'))]
        if 'in' in options: params.append(('in',options['in']))
        for pred in filter(None,options.get('filters','').split(';')):
            params.append(tuple(pred.split('=',1)))
        try:
            data,_,_=fetch(API.format(dataset=query),params+[('key',self.key)])
        except ConnectorFailure as ex:
            # The API signals key problems by redirecting to an HTML page (checked live).
            if 'invalid_key' in str(ex):
                raise ConnectorFailure('El Census rechaza la clave (invalid_key): confirma la activación con el enlace '
                                       'del email de alta o revisa MARKETSCOPE_CENSUS_KEY')
            if 'missing_key' in str(ex):
                raise ConnectorFailure('El Census no recibió la clave (missing_key)')
            raise
        if not (isinstance(data,list) and data and isinstance(data[0],list)):
            raise ConnectorFailure('Respuesta del Census con formato inesperado')
        header,rows=data[0],[r for r in data[1:] if isinstance(r,list) and len(r)==len(data[0])]
        labels={}
        for var in variables[:MAX_VARS]:
            if var=='NAME': continue
            try:
                meta,_,_=fetch(VARIABLE.format(dataset=query,var=var),None)
                if isinstance(meta,dict) and isinstance(meta.get('label'),str): labels[var]=meta['label']
            except NotFound:
                pass
        return [self._hit(query,header,r,labels,params,len(rows)) for r in rows[:MAX_HITS]]

    def _hit(self, dataset, header, row, labels, params, total):
        cells=dict(zip(header,row))
        parts=[f'{labels.get(h,h)} ({h}): {v}' for h,v in cells.items()]
        geo_level,geo_value=header[-1],row[-1]  # Census appends the geography column last
        url_params=[(k,v) for k,v in params if k not in ('for',)]+[('for',f'{geo_level}:{geo_value}')]
        name=cells.get('NAME') or f'{geo_level} {geo_value}'
        return Hit(url=API.format(dataset=dataset)+'?'+urlencode(url_params),title=f'US Census {dataset} — {name}',
                   published_at='unknown',
                   snippet=clip(f'US Census Bureau, dataset {dataset}: '+'; '.join(parts)+
                                f'. Variables: https://api.census.gov/data/{dataset}/variables.html'),
                   independence_group='us_census',
                   metadata={'dataset':dataset,'row':cells,'labels':labels,'total_rows':total,'truncated':total>MAX_HITS})
