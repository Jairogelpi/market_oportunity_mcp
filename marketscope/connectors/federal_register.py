"""Federal Register (US) documents search. Docs: https://www.federalregister.gov/developers/documentation/api/v1

No key. Full-text `conditions[term]`, ordered by relevance (newest-first returns unrelated documents,
checked live). Options: type (RULE, PRORULE, NOTICE, PRESDOCU), since (YYYY-MM-DD), agency (slug).
Rules and proposed rules signal regulatory demand drivers; legal interpretation needs review."""
import re
from ..http_client import Policy, ConnectorFailure
from .base import Connector, Hit, clip, iso_date

API = 'https://www.federalregister.gov/api/v1/documents.json'
PER_PAGE = 20
FIELDS = ['title','abstract','document_number','html_url','publication_date','type','agencies','significant']

class FederalRegister(Connector):
    evidence_kind = 'primary'
    ENGLISH_FIRST = True
    OPTIONS = {'type':r'RULE|PRORULE|NOTICE|PRESDOCU','since':r'\d{4}-\d{2}-\d{2}','agency':r'[a-z0-9\-]{2,80}'}

    def __init__(self):
        self.policy=Policy('federal_register',frozenset({'www.federalregister.gov'}),min_interval=1.0)

    def validate_query(self, query):
        if len(query)>300: raise ValueError('Consulta demasiado larga (máx. 300 caracteres)')

    def search(self, fetch, query, options):
        params=[('conditions[term]',query),('per_page',PER_PAGE),('order','relevance')]+[('fields[]',f) for f in FIELDS]
        if 'type' in options: params.append(('conditions[type][]',options['type']))
        if 'since' in options: params.append(('conditions[publication_date][gte]',options['since']))
        if 'agency' in options: params.append(('conditions[agencies][]',options['agency']))
        data,_,_=fetch(API,params)
        if not isinstance(data,dict) or 'count' not in data:
            raise ConnectorFailure('Respuesta del Federal Register con formato inesperado')
        context={'total_count':data.get('count')}
        return [h for h in (self._hit(d,context) for d in data.get('results') or []) if h]

    def _hit(self, d, context):
        url,title=d.get('html_url'),' '.join((d.get('title') or '').split())
        if not isinstance(url,str) or not url.startswith('https://www.federalregister.gov/') or not title: return None
        agencies=[a for a in d.get('agencies') or [] if isinstance(a,dict)]
        slug=next((str(a.get('slug') or a.get('id')) for a in agencies if a.get('slug') or a.get('id')),None)
        if not slug and agencies: slug=re.sub(r'\W+','-',str(agencies[0].get('raw_name','')).lower()).strip('-') or None
        abstract=' '.join((d.get('abstract') or '').split())
        return Hit(url=url,title=title,published_at=iso_date(d.get('publication_date')),
                   snippet=clip(f'{title}. {abstract}' if abstract else title),
                   independence_group=f'federalregister:agency:{slug}' if slug else f"federalregister:doc:{d.get('document_number')}",
                   metadata={'document_number':d.get('document_number'),'type':d.get('type'),'significant':d.get('significant'),
                             'agencies':[a.get('name') or a.get('raw_name') for a in agencies],
                             'caveat':'Interpretación jurídica requiere revisión',**context})
