"""OpenAlex works search. Docs: https://help.openalex.org/api/authentication/

Keyless use is allowed for basic queries (10 req/s, small daily budget; each search costs credits).
Paging past the first page needs a key, so one request per step. Optional key from
MARKETSCOPE_OPENALEX_API_KEY, sent as a bearer header so it never reaches logged URLs."""
import os
from urllib.parse import urlencode
from ..http_client import Policy, ConnectorFailure
from .base import Connector, Hit, clip, iso_date, TREND_OPTIONS, period_ranges, trend_hit

API = 'https://api.openalex.org/works'
PER_PAGE = 25
SELECT = ('id,doi,display_name,publication_date,publication_year,type,language,cited_by_count,'
          'primary_location,authorships,abstract_inverted_index')
KEYLESS_DAILY_QUOTA = 100   # conservative: keyless daily budget covers roughly this many searches
KEYED_DAILY_QUOTA = 1000

def abstract_text(inverted):
    if not isinstance(inverted,dict): return ''
    positions=sorted((p,word) for word,ps in inverted.items() for p in ps)
    return ' '.join(word for _,word in positions)

def _short(openalex_url):
    return (openalex_url or '').rsplit('/',1)[-1]

class OpenAlex(Connector):
    evidence_kind = 'secondary'
    ENGLISH_FIRST = True
    # Trend mode groups by publication_year in a single request: yearly granularity only.
    OPTIONS = {**TREND_OPTIONS,'granularity':r'year'}

    def __init__(self):
        self.api_key=os.environ.get('MARKETSCOPE_OPENALEX_API_KEY','').strip()
        self.policy=Policy('openalex',frozenset({'api.openalex.org'}),min_interval=0.2,
                           daily_quota=KEYED_DAILY_QUOTA if self.api_key else KEYLESS_DAILY_QUOTA)

    def _by_year(self, fetch, params, headers):
        data,_,_=fetch(API,params,headers)
        groups=data.get('group_by') if isinstance(data,dict) else None
        if not isinstance(groups,list): raise ConnectorFailure('Respuesta de OpenAlex sin group_by')
        return {str(g.get('key')):g.get('count') for g in groups if isinstance(g.get('count'),int)}

    def trend(self, fetch, query, options, headers):
        """Works per complete publication year: 1 request (group_by), 2 when normalized by all works per year."""
        normalize=options.get('normalize','yes')=='yes'
        ranges=period_ranges('year',int(options.get('periods','5')))
        years=f'{ranges[0][0]}-{ranges[-1][0]}'
        params={'search':query,'group_by':'publication_year','filter':f'publication_year:{years}'}
        counts=self._by_year(fetch,params,headers)
        totals=self._by_year(fetch,{'group_by':'publication_year','filter':f'publication_year:{years}'},headers) if normalize else {}
        series=[(label,counts.get(label,0),totals.get(label) if normalize else None) for label,_,_ in ranges]
        return [trend_hit('openalex',query,'Trabajos académicos',f'año de publicación',series,API+'?'+urlencode(params),
                          normalize,ranges[-1][2].isoformat(),
                          {'caveat':'La investigación no equivale a demanda comercial; la indexación del último año puede estar incompleta'})]

    def search(self, fetch, query, options):
        headers={'Authorization':f'Bearer {self.api_key}'} if self.api_key else {}
        if options.get('mode')=='trend': return self.trend(fetch,query,options,headers)
        data,_,_=fetch(API,{'search':query,'per_page':PER_PAGE,'select':SELECT},headers)
        if not isinstance(data,dict) or not isinstance(data.get('results'),list):
            raise ConnectorFailure('Respuesta de OpenAlex sin lista results')
        total=(data.get('meta') or {}).get('count')
        return [h for h in (self._hit(w,total) for w in data['results']) if h]

    def _hit(self, w, total):
        title=' '.join((w.get('display_name') or '').split())
        if not title or not str(w.get('id','')).startswith('https://openalex.org/'): return None
        source=((w.get('primary_location') or {}).get('source') or {})
        authors=[a.get('author') or {} for a in (w.get('authorships') or [])]
        first=authors[0] if authors else {}
        group=(f"openalex:author:{_short(first.get('id'))}" if first.get('id') else
               f"openalex:venue:{_short(source.get('id'))}" if source.get('id') else f"openalex:work:{_short(w['id'])}")
        abstract=abstract_text(w.get('abstract_inverted_index'))
        return Hit(url=w['id'],title=title,published_at=iso_date(w.get('publication_date')),
                   snippet=clip(f'{title}. {abstract}' if abstract else title),independence_group=group,
                   metadata={'doi':w.get('doi'),'publication_year':w.get('publication_year'),'type':w.get('type'),
                             'language':w.get('language'),'cited_by_count':w.get('cited_by_count'),
                             'venue':source.get('display_name'),'first_author':first.get('display_name'),
                             'total_matches':total,
                             'has_abstract':bool(abstract)})
