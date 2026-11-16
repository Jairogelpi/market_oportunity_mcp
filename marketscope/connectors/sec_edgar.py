"""SEC EDGAR XBRL company facts. Docs: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data

Fair access: at most 10 requests/second and a User-Agent declaring a contact (MARKETSCOPE_CONTACT, sent
only to SEC). The query is a ticker (resolved via company_tickers.json, one extra request) or a CIK.
Annual values come from the canonical calendar-year `frame` (CYyyyy) of 10-K/20-F/40-F filings, so each
year appears once even if restated in several filings. Listed issuers only: competitor data, not market size."""
import os
import re
from urllib.parse import urlencode
from ..http_client import Policy, ConnectorFailure, NotFound
from .base import Connector, Hit, clip, iso_date

TICKERS = 'https://www.sec.gov/files/company_tickers.json'
CONCEPT = 'https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{concept}.json'
# Revenue is reported under either tag depending on the filer and year.
DEFAULT_CONCEPTS = ('Revenues','RevenueFromContractWithCustomerExcludingAssessedTax')
ANNUAL_FORMS = ('10-K','10-K/A','20-F','40-F')
MAX_YEARS = 10

def _fmt(value):
    return str(value)  # raw digits: thousands separators would be ambiguous with decimals

class SecEdgar(Connector):
    evidence_kind = 'primary'
    OPTIONS = {'concept':r'[A-Za-z][A-Za-z0-9]{1,120}','taxonomy':r'us-gaap|ifrs-full|dei|srt'}
    QUERY_HELP = 'SEC EDGAR necesita query=<ticker o CIK> de una empresa cotizada (p. ej. INTU o 896878)'

    def __init__(self):
        self.contact=os.environ.get('MARKETSCOPE_CONTACT','').strip()
        self.policy=Policy('sec_edgar',frozenset({'www.sec.gov','data.sec.gov'}),min_interval=0.15,contact_env='MARKETSCOPE_CONTACT')

    def build_query(self, terms):
        return ''  # the agent names the company

    def validate_query(self, query):
        if not self.contact or '@' not in self.contact:
            raise ValueError('SEC exige un User-Agent con email de contacto: define MARKETSCOPE_CONTACT en .env')
        if not re.fullmatch(r'\d{1,10}|[A-Za-z][A-Za-z0-9.\-]{0,9}',query):
            raise ValueError('Consulta SEC no válida: usa un ticker (p. ej. INTU) o un CIK numérico')

    def _resolve(self, fetch, query):
        if query.isdigit(): return query.zfill(10),None
        data,_,_=fetch(TICKERS,None)
        for row in (data.values() if isinstance(data,dict) else []):
            if str(row.get('ticker','')).upper()==query.upper():
                return str(row['cik_str']).zfill(10),row['ticker']
        raise ConnectorFailure(f'Ticker {query!r} no encontrado en company_tickers.json de la SEC')

    def search(self, fetch, query, options):
        cik,ticker=self._resolve(fetch,query)
        taxonomy=options.get('taxonomy','us-gaap')
        concepts=(options['concept'],) if 'concept' in options else DEFAULT_CONCEPTS
        for concept in concepts:
            url=CONCEPT.format(cik=cik,taxonomy=taxonomy,concept=concept)
            try:
                data,_,_=fetch(url,None)
            except NotFound:
                continue  # concept not reported by this filer: try the next one
            return self._hits(url,data,ticker)
        raise ConnectorFailure(f'CIK {cik} no reporta {", ".join(concepts)} en {taxonomy}; prueba otra opción concept/taxonomy')

    def _hits(self, url, data, ticker):
        if not isinstance(data,dict) or not isinstance(data.get('units'),dict):
            raise ConnectorFailure('Respuesta de SEC sin units')
        entity,label,cik=data.get('entityName',''),data.get('label') or data.get('tag'),str(data.get('cik','')).zfill(10)
        hits=[]
        for unit,facts in data['units'].items():
            annual=[f for f in facts if f.get('form') in ANNUAL_FORMS and re.fullmatch(r'CY\d{4}',f.get('frame',''))]
            for f in sorted(annual,key=lambda f:f['frame'])[-MAX_YEARS:]:
                accn=f.get('accn','')
                filing=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-','')}/"
                snippet=(f"{entity} (CIK {cik}) — {label}: {_fmt(f.get('val'))} {unit}, periodo {f.get('start','?')} a {f.get('end','?')} "
                         f"({f['frame']}), presentado en {f.get('form')} el {f.get('filed','?')} (accn {accn}). Fuente: SEC EDGAR XBRL.")
                hits.append(Hit(url=url+'?'+urlencode({'frame':f['frame'],'unit':unit}),title=f'{entity} — {label} {f["frame"]}',
                                published_at=iso_date(f.get('filed')),snippet=clip(snippet),independence_group=f'sec:cik:{cik}',
                                metadata={'cik':cik,'ticker':ticker,'entity':entity,'concept':data.get('tag'),'unit':unit,
                                          'value':f.get('val'),'start':f.get('start'),'end':f.get('end'),'fy':f.get('fy'),
                                          'form':f.get('form'),'filed':f.get('filed'),'accn':accn,'frame':f['frame'],
                                          'filing_url':filing,'caveat':'Empresa cotizada: dato de competidor, no tamaño de mercado'}))
        return hits
