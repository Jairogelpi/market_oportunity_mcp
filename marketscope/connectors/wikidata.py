"""Wikidata entities via the Wikidata Query Service (SPARQL). Docs: https://www.wikidata.org/wiki/Wikidata:SPARQL_query_service

Same robot policy as Wikimedia (contact in the User-Agent, from MARKETSCOPE_WIKIMEDIA_CONTACT; 403 without it, checked live).
The query is a product/company name searched over labels and aliases (EntitySearch). Option `class` (a QID such as
an "accounting software" class) lists direct instances of that class instead, optionally filtered by the words.
Fixed SPARQL template: the text is validated to letters, digits, spaces, hyphens, dots and '+'.
Collaborative knowledge base: coverage is incomplete; absence does not mean non-existence."""
import re
from ..http_client import Policy, ConnectorFailure
from .base import Connector, Hit, clip, iso_datetime, require_contact, WIKIMEDIA_CONTACT

API = 'https://query.wikidata.org/sparql'
LIMIT = 30
TEXT = re.compile(r'[^\W_][\w .+\-]{0,79}',re.UNICODE)

_FIELDS = '''
  OPTIONAL {{ ?item wdt:P31 ?type . ?type rdfs:label ?typeLabel . FILTER(LANG(?typeLabel)="{lang}") }}
  OPTIONAL {{ ?item wdt:P178|wdt:P176 ?dev . ?dev rdfs:label ?devLabel . FILTER(LANG(?devLabel)="{lang}") }}
  OPTIONAL {{ ?item wdt:P17|wdt:P495 ?country . ?country rdfs:label ?countryLabel . FILTER(LANG(?countryLabel)="{lang}") }}
  OPTIONAL {{ ?item wdt:P571|wdt:P577 ?inception }}
  OPTIONAL {{ ?item wdt:P856 ?website }}
  OPTIONAL {{ ?item rdfs:label ?label . FILTER(LANG(?label)="{lang}") }}
  OPTIONAL {{ ?item schema:description ?description . FILTER(LANG(?description)="{lang}") }}
  OPTIONAL {{ ?item schema:dateModified ?modified }}'''
_SELECT = '''SELECT ?item (SAMPLE(?label) AS ?l) (SAMPLE(?description) AS ?d) (GROUP_CONCAT(DISTINCT ?typeLabel; separator=", ") AS ?types)
  (GROUP_CONCAT(DISTINCT ?devLabel; separator=", ") AS ?devs) (GROUP_CONCAT(DISTINCT ?countryLabel; separator=", ") AS ?countries)
  (MIN(?inception) AS ?since) (SAMPLE(?website) AS ?site) (MAX(?modified) AS ?mod) WHERE {{'''
SEARCH = _SELECT+'''
  SERVICE wikibase:mwapi {{ bd:serviceParam wikibase:endpoint "www.wikidata.org"; wikibase:api "EntitySearch";
    mwapi:search "{text}"; mwapi:language "{lang}". ?item wikibase:apiOutputItem mwapi:item. }}'''+_FIELDS+'''
}} GROUP BY ?item LIMIT {limit}'''
CLASS = _SELECT+'''
  ?item wdt:P31 wd:{cls} .'''+_FIELDS+'''
  {text_filter}
}} GROUP BY ?item LIMIT {limit}'''

class Wikidata(Connector):
    evidence_kind = 'secondary'
    OPTIONS = {'class':r'Q[1-9]\d{0,10}','lang':r'[a-z]{2}'}
    QUERY_HELP = 'Wikidata necesita query con el nombre de un producto o empresa, o la opción class (QID) con palabras opcionales'

    def __init__(self):
        self.policy=Policy('wikidata',frozenset({'query.wikidata.org'}),min_interval=1.0,timeout=60,contact_env=WIKIMEDIA_CONTACT)

    def build_query(self, terms):
        return ''

    def validate_query(self, query):
        require_contact(WIKIMEDIA_CONTACT,'La política de robots de Wikimedia (Wikidata) exige un contacto en el User-Agent')
        if not TEXT.fullmatch(query): raise ValueError('Wikidata: usa letras, dígitos, espacios, guiones, puntos o + (máx. 80)')

    def sparql(self, query, options):
        lang=options.get('lang') or (self.terms.get('language') if self.terms.get('language') not in (None,'unknown') else 'en')
        if 'class' in options:
            words=[w.lower() for w in query.split() if w!='*']
            text_filter=' '.join(f'FILTER(CONTAINS(LCASE(STR(?label)),"{w}"))' for w in words)
            return CLASS.format(cls=options['class'],lang=lang,text_filter=text_filter,limit=LIMIT)
        return SEARCH.format(text=query,lang=lang,limit=LIMIT)

    def search(self, fetch, query, options):
        data,_,_=fetch(API,{'query':self.sparql(query,options),'format':'json'},{'Accept':'application/sparql-results+json'})
        rows=((data or {}).get('results') or {}).get('bindings') if isinstance(data,dict) else None
        if not isinstance(rows,list): raise ConnectorFailure('Respuesta SPARQL de Wikidata sin results.bindings')
        return [h for h in (self._hit(b) for b in rows) if h]

    def _hit(self, b):
        v=lambda k:' '.join(str((b.get(k) or {}).get('value','')).split())
        item=v('item')
        if not re.fullmatch(r'http://www\.wikidata\.org/entity/Q\d+',item): return None
        qid=item.rsplit('/',1)[-1]; label=v('l') or qid
        since=v('since')[:10]
        facts=[f'Tipo: {v("types")}' if v('types') else '',f'Desarrollador/fabricante: {v("devs")}' if v('devs') else '',
               f'País: {v("countries")}' if v('countries') else '',f'Inicio/lanzamiento: {since}' if since else '',
               f'Web: {v("site")}' if v('site') else '']
        snippet=f'{label} ({qid})'+(f': {v("d")}' if v('d') else '')+'. '+'. '.join(f for f in facts if f)+'. Fuente: Wikidata.'
        return Hit(url=f'https://www.wikidata.org/wiki/{qid}',title=label,published_at=iso_datetime(v('mod')),
                   snippet=clip(snippet),independence_group='wikidata',
                   metadata={'qid':qid,'types':v('types') or None,'developers':v('devs') or None,'countries':v('countries') or None,
                             'inception':since or None,'website':v('site') or None,
                             'caveat':'Base colaborativa: cobertura incompleta; ausencia no implica inexistencia'})
