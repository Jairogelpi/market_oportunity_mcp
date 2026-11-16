"""EUR-Lex documents via the EU Publications Office Cellar SPARQL endpoint (open, no registration).
Endpoint: https://publications.europa.eu/webapi/rdf/sparql (the EUR-Lex SOAP web service requires registration).

The query is 1-6 words that must all appear in the document title (in the title language). The SPARQL is a
fixed template; words are validated to letters, digits and hyphens, so no agent text reaches the query as syntax.
Options: lang (EU 3-letter code, e.g. ENG, SPA; default from the study language), sector (CELEX sector digit:
3 legislation, 5 preparatory acts, 6 case law), since (YYYY-MM-DD). Title search is slow (~45 s, checked live)."""
import re
from ..http_client import Policy, ConnectorFailure
from .base import Connector, Hit, clip, iso_date

API = 'https://publications.europa.eu/webapi/rdf/sparql'
LIMIT = 25
LANG3 = {'en':'ENG','es':'SPA','fr':'FRA','de':'DEU','it':'ITA','pt':'POR','nl':'NLD','pl':'POL','sv':'SWE','da':'DAN',
         'fi':'FIN','el':'ELL','cs':'CES','ro':'RON','hu':'HUN','bg':'BUL','sk':'SLK','sl':'SLV','hr':'HRV','lt':'LIT',
         'lv':'LAV','et':'EST','ga':'GLE','mt':'MLT'}
LANG2 = {v:k for k,v in LANG3.items()}
WORD = re.compile(r'[^\W_][\w\-]{0,39}',re.UNICODE)

TEMPLATE = '''PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?celex ?title ?date WHERE {{
  ?work cdm:resource_legal_id_celex ?celex ; cdm:work_date_document ?date .
  ?expr cdm:expression_belongs_to_work ?work ; cdm:expression_title ?title ;
        cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/{lang}> .
  {filters}
}} ORDER BY DESC(?date) LIMIT {limit}'''

def words_of(query):
    words=query.split()
    if not 1<=len(words)<=6 or not all(WORD.fullmatch(w) for w in words):
        raise ValueError('EUR-Lex: usa de 1 a 6 palabras (letras, dígitos o guiones) que deban aparecer en el título')
    return [w.lower() for w in words]

class EurLex(Connector):
    evidence_kind = 'primary'
    OPTIONS = {'lang':r'[A-Z]{3}','sector':r'[0-9CE]','since':r'\d{4}-\d{2}-\d{2}'}

    QUERY_HELP = ('EUR-Lex necesita query con 1-6 palabras clave que deban aparecer en el título '
                  '(p. ej. "electronic invoicing" con lang ENG, o "factura electrónica" con lang SPA)')

    def __init__(self):
        self.policy=Policy('eurlex',frozenset({'publications.europa.eu'}),min_interval=2.0,timeout=90)

    def build_query(self, terms):
        return ''  # every word must be in the title: a market description with stopwords never matches

    def validate_query(self, query):
        words_of(query)

    def sparql(self, query, options):
        lang=options.get('lang') or LANG3.get(self.terms.get('language',''),'ENG')
        if lang not in LANG2: raise ConnectorFailure(f'Idioma EUR-Lex no válido: {lang}')
        filters=[f'FILTER(CONTAINS(LCASE(STR(?title)),"{w}"))' for w in words_of(query)]
        if 'sector' in options: filters.append(f'FILTER(STRSTARTS(STR(?celex),"{options["sector"]}"))')
        if 'since' in options: filters.append(f'FILTER(?date >= "{options["since"]}"^^<http://www.w3.org/2001/XMLSchema#date>)')
        return lang,TEMPLATE.format(lang=lang,filters='\n  '.join(filters),limit=LIMIT)

    def search(self, fetch, query, options):
        lang,sparql=self.sparql(query,options)
        data,_,_=fetch(API,{'query':sparql,'format':'application/sparql-results+json'},{'Accept':'application/sparql-results+json'})
        rows=((data or {}).get('results') or {}).get('bindings') if isinstance(data,dict) else None
        if not isinstance(rows,list): raise ConnectorFailure('Respuesta SPARQL de EUR-Lex sin results.bindings')
        seen,hits=set(),[]
        for b in rows:
            celex=(b.get('celex') or {}).get('value',''); title=' '.join((b.get('title') or {}).get('value','').split())
            if not re.fullmatch(r'[0-9CE][0-9A-Z()\-_.]{4,30}',celex) or not title or celex in seen: continue
            seen.add(celex); date=(b.get('date') or {}).get('value')
            hits.append(Hit(url=f'https://eur-lex.europa.eu/legal-content/{LANG2[lang].upper()}/TXT/?uri=CELEX:{celex}',
                            title=title[:300],published_at=iso_date(date),
                            snippet=clip(f'{title}. CELEX {celex}, fecha del documento {date}. Fuente: EUR-Lex (Oficina de Publicaciones de la UE).'),
                            independence_group='eurlex',
                            metadata={'celex':celex,'sector':celex[0],'date':date,'lang':lang,
                                      'caveat':'Interpretación jurídica requiere revisión'}))
        return hits
