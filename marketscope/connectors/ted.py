"""TED (Tenders Electronic Daily) notice search, API v3: POST /v3/notices/search with an expert query.
Spec: https://api.ted.europa.eu/api-v3.yaml (no authentication; up to 250 notices/page, 15k per query).

Plain-text queries become full-text search with explicit AND, `FT ~ (a AND b)`; expert queries (containing ~, =, IN or SORT BY)
are used as given. Options add clauses: country (ISO 3166 alpha-3 buyer country; default from the study
geography), cpv (one 8-digit CPV code), since (YYYYMMDD publication date), lang (ISO 639-2 title language).
Tendered spend is not awarded spend: notices document demand signals from the public sector only."""
import re
from ..http_client import Policy, ConnectorFailure
from .base import Connector, Hit, clip

API = 'https://api.ted.europa.eu/v3/notices/search'
LIMIT = 25
FIELDS = ['publication-number','notice-title','buyer-name','buyer-country','publication-date','notice-type',
          'classification-cpv','place-of-performance','description-lot','links']
ISO3 = dict(zip('AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE NO IS LI CH GB'.split(),
                'AUT BEL BGR HRV CYP CZE DNK EST FIN FRA DEU GRC HUN IRL ITA LVA LTU LUX MLT NLD POL PRT ROU SVK SVN ESP SWE NOR ISL LIE CHE GBR'.split()))
EXPERT = re.compile(r'~|=|\bIN\s*\(|\bSORT\s+BY\b',re.I)
SORT = re.compile(r'\s+SORT\s+BY\s+.*$',re.I)

def _pick(i18n, lang):
    """Value of an i18n field in `lang`, else English, else the first language present."""
    if not isinstance(i18n,dict) or not i18n: return None
    return i18n.get(lang) or i18n.get('eng') or next(iter(i18n.values()))

def _first(value):
    return value[0] if isinstance(value,list) and value else value if isinstance(value,str) else ''

def _iso(value):
    m=re.fullmatch(r'(\d{4}-\d{2}-\d{2})(Z|[+-]\d{2}:\d{2})?',str(value or ''))
    if not m: return 'unknown'
    tz=m.group(2) or '+00:00'
    return f'{m.group(1)}T00:00:00{"+00:00" if tz=="Z" else tz}'

class Ted(Connector):
    evidence_kind = 'primary'
    OPTIONS = {'country':r'[A-Z]{3}','cpv':r'\d{8}','since':r'\d{8}','lang':r'[a-z]{3}'}

    def __init__(self):
        self.policy=Policy('ted',frozenset({'api.ted.europa.eu'}),min_interval=1.0)

    def validate_query(self, query):
        if len(query)>1000: raise ValueError('Consulta TED demasiado larga (máx. 1000 caracteres)')

    def expert_query(self, query, options):
        sort=SORT.search(query)
        base=query[:sort.start()] if sort else query
        if not EXPERT.search(base):
            # Space-separated words have undocumented semantics in TED (neither AND nor OR, checked live):
            # join with an explicit AND so every word must appear.
            words=[w for w in re.sub(r'[()"~=<>]',' ',base).split() if w.upper() not in ('AND','OR','NOT')]
            base=f'FT ~ ({" AND ".join(words)})'
        clauses=[f'({base})']
        country=options.get('country') or ISO3.get(self.terms.get('geography',''))
        if country and 'buyer-country' not in base: clauses.append(f'buyer-country IN ({country})')
        if 'cpv' in options: clauses.append(f'classification-cpv IN ({options["cpv"]})')
        if 'since' in options: clauses.append(f'publication-date >= {options["since"]}')
        return ' AND '.join(clauses)+(sort.group(0) if sort else ' SORT BY publication-date DESC')

    def search(self, fetch, query, options):
        expert=self.expert_query(query,options)
        fetch.query=expert  # the audited event records the exact expert query sent
        data,_,_=fetch(API,None,None,json_body={'query':expert,'fields':FIELDS,'limit':LIMIT,'page':1,
                                               'scope':'ALL','paginationMode':'PAGE_NUMBER'})
        if not isinstance(data,dict) or not isinstance(data.get('notices'),list):
            raise ConnectorFailure('Respuesta de TED sin lista notices')
        if data.get('timedOut'): raise ConnectorFailure('TED: la búsqueda agotó el tiempo; acota la consulta')
        lang=options.get('lang','eng')
        context={'total_count':data.get('totalNoticeCount'),'expert_query':expert}
        return [h for h in (self._hit(n,lang,context) for n in data['notices']) if h]

    def _hit(self, n, lang, context):
        pub=n.get('publication-number')
        title=' '.join(str(_pick(n.get('notice-title'),lang) or '').split())
        if not isinstance(pub,str) or not re.fullmatch(r'\d{1,9}-\d{4}',pub) or not title: return None
        html=((n.get('links') or {}).get('html') or {}).get('ENG','')
        url=html if html.startswith('https://ted.europa.eu/') else f'https://ted.europa.eu/en/notice/-/detail/{pub}'
        desc=' '.join(str(d) for d in (_pick(n.get('description-lot'),lang) or [])[:2])
        buyer=' '.join(_first(_pick(n.get('buyer-name'),lang)).split())
        group=f'ted:buyer:{buyer.lower()[:80]}' if buyer else f'ted:notice:{pub}'
        return Hit(url=url,title=title,published_at=_iso(n.get('publication-date')),
                   snippet=clip(f'{title}. {desc}' if desc else title),independence_group=group,
                   metadata={'publication_number':pub,'notice_type':n.get('notice-type'),'buyer_name':buyer or None,
                             'buyer_country':n.get('buyer-country'),'cpv':n.get('classification-cpv'),
                             'place_of_performance':n.get('place-of-performance'),
                             'caveat':'Gasto licitado no equivale a gasto adjudicado',**context})
