"""GDELT DOC 2.0 API: worldwide news coverage over time (65 languages). Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/

One request (mode=TimelineVolRaw) returns, per time step, the number of matching articles and the total
articles monitored; they are summed into complete periods. GDELT asks for at most one request every
5 seconds (checked live: it answers 429 with that message). Options: granularity (month|quarter|year),
periods, lang (GDELT sourcelang name, e.g. spanish), country (GDELT sourcecountry code, e.g. SP),
normalize (yes|no). Periods outside GDELT's coverage are reported as missing, never as zero."""
import re
from datetime import datetime, timezone
from urllib.parse import urlencode
from ..http_client import Policy, ConnectorFailure
from .base import Connector, period_ranges, trend_hit, TREND_OPTIONS

API = 'https://api.gdeltproject.org/api/v2/doc/doc'

def _parse_date(value):
    try:
        return datetime.strptime(str(value),'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None

class Gdelt(Connector):
    evidence_kind = 'secondary'
    OPTIONS = {**{k:v for k,v in TREND_OPTIONS.items() if k!='mode'},'granularity':r'month|quarter|year',
               'lang':r'[a-z]{3,20}','country':r'[A-Z]{2}'}

    def __init__(self):
        self.policy=Policy('gdelt',frozenset({'api.gdeltproject.org'}),min_interval=6.0,timeout=60)

    def validate_query(self, query):
        if len(query)>200 or not re.fullmatch(r'[\w\s"()\-.:]+',query,re.UNICODE):
            raise ValueError('Consulta GDELT no válida: palabras, frases entre comillas, paréntesis u OR (máx. 200)')

    def search(self, fetch, query, options):
        granularity=options.get('granularity','month'); normalize=options.get('normalize','yes')=='yes'
        ranges=period_ranges(granularity,int(options.get('periods','12')))
        q=query+(f" sourcelang:{options['lang']}" if 'lang' in options else '')+(f" sourcecountry:{options['country']}" if 'country' in options else '')
        params={'query':q,'mode':'TimelineVolRaw','format':'json','timelinesmooth':'0',
                'startdatetime':ranges[0][1].strftime('%Y%m%d%H%M%S'),'enddatetime':ranges[-1][2].strftime('%Y%m%d%H%M%S')}
        data,_,_=fetch(API,params)
        try:
            points=data['timeline'][0]['data']
        except (KeyError,IndexError,TypeError):
            raise ConnectorFailure('Respuesta de GDELT sin timeline')
        sums={label:[0,0,0] for label,_,_ in ranges}  # count, norm, points
        for p in points:
            when=_parse_date(p.get('date'))
            if not when or not isinstance(p.get('value'),(int,float)): continue
            for label,s,e in ranges:
                if s<=when<e:
                    sums[label][0]+=p['value']; sums[label][1]+=p.get('norm') or 0; sums[label][2]+=1
        covered=[(label,int(c),int(n) if normalize and n else None) for label,(c,n,k) in sums.items() if k]
        missing=[label for label,(_,_,k) in sums.items() if not k]
        if not covered: return []
        hit=trend_hit('gdelt',q,'Artículos de prensa',granularity,covered,API+'?'+urlencode(params),normalize,
                      ranges[-1][2].isoformat(),{'missing_periods':missing,
                      'caveat':'Atención mediática, no demanda; la cobertura de medios de GDELT varía por país e idioma'})
        if missing: hit.metadata['note']='Periodos sin datos de GDELT (no equivalen a cero): '+', '.join(missing)
        return [hit]
