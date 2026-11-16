"""World Bank Indicators API v2. Docs: https://datahelpdesk.worldbank.org/knowledgebase/articles/889392-about-the-indicators-api-documentation

The query is an indicator code (e.g. IT.NET.USER.ZS). Countries default to the study geography
(GLOBAL -> WLD, EU -> EUU); option `countries` = "ES;US;WLD", `date` = "2015:2024" or "2023", `mrv` = N most recent.
The API is slow and erratic (0.5-55 s per request, checked live) and reports errors as HTTP 200 with a `message` array.
World Bank compiles national statistics: secondary evidence, one statistical lineage."""
import re
from urllib.parse import urlencode
from ..http_client import Policy, ConnectorFailure
from .base import Connector, Hit, clip, iso_date

API = 'https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}'
PER_PAGE = 100
MAX_HITS = 60
GEO = {'GLOBAL':'WLD','EU':'EUU'}

class WorldBank(Connector):
    evidence_kind = 'secondary'
    OPTIONS = {'countries':r'[A-Za-z0-9]{2,3}(;[A-Za-z0-9]{2,3}){0,30}','date':r'\d{4}(:\d{4})?','mrv':r'[1-9]\d?'}
    QUERY_HELP = 'World Bank necesita query=<código de indicador> (p. ej. IT.NET.USER.ZS), localizado en data.worldbank.org'

    def __init__(self):
        # Measured live: 0.5 s to 55 s for the same kind of query (cache-dependent).
        self.policy=Policy('worldbank',frozenset({'api.worldbank.org'}),min_interval=1.0,timeout=90)

    def build_query(self, terms):
        return ''

    def validate_query(self, query):
        if not re.fullmatch(r'[A-Za-z0-9_.]{2,60}',query): raise ValueError('Código de indicador del Banco Mundial no válido')

    def search(self, fetch, query, options):
        countries=options.get('countries') or GEO.get(self.terms.get('geography','GLOBAL'),self.terms.get('geography','WLD'))
        params={'format':'json','per_page':PER_PAGE}
        for k in ('date','mrv'):
            if k in options: params[k]=options[k]
        data,_,_=fetch(API.format(countries=countries,indicator=query),params)
        if isinstance(data,list) and data and isinstance(data[0],dict) and 'message' in data[0]:
            msg='; '.join(f"{m.get('key')}: {m.get('value')}" for m in data[0]['message'] if isinstance(m,dict))
            raise ConnectorFailure(f'World Bank rechazó la consulta ({msg}); revisa el indicador o los países')
        if not (isinstance(data,list) and len(data)==2 and isinstance(data[0],dict)):
            raise ConnectorFailure('Respuesta del Banco Mundial con formato inesperado')
        meta,rows=data[0],[r for r in (data[1] or []) if isinstance(r,dict) and r.get('value') is not None]
        return [self._hit(r,meta,len(rows)) for r in rows[:MAX_HITS]]

    def _hit(self, r, meta, total):
        ind,country,date=r['indicator'],r['country'],r.get('date')
        iso3=r.get('countryiso3code') or country.get('id')
        snippet=(f"{ind['value']} — {country['value']} ({iso3}), {date}: valor {r['value']}"+
                 (f" [estado: {r['obs_status']}]" if r.get('obs_status') else '')+
                 f". Fuente: Banco Mundial, indicador {ind['id']}, actualizado {meta.get('lastupdated','?')}.")
        url=API.format(countries=iso3,indicator=ind['id'])+'?'+urlencode({'date':date,'format':'json'})
        return Hit(url=url,title=f"{ind['value']} — {country['value']} {date}",published_at=iso_date(meta.get('lastupdated')),
                   snippet=clip(snippet),independence_group='worldbank',
                   metadata={'indicator':ind['id'],'country':iso3,'date':date,'value':r['value'],
                             'obs_status':r.get('obs_status') or None,'total_observations':total,'truncated':total>MAX_HITS})
