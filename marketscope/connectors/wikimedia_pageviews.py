"""Wikipedia page views per article (Wikimedia Analytics REST API).
Docs: https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/

Wikimedia's robot policy requires a User-Agent with contact information (checked live: 403 without it);
the contact comes from MARKETSCOPE_WIKIMEDIA_CONTACT and is sent only to Wikimedia.
The query is 1-5 article titles separated by '|' (one request each), e.g. "QuickBooks|Xero|FreshBooks".
Options: project (default from the study language, e.g. es.wikipedia), granularity (monthly|daily),
start/end (YYYYMMDD; default the last 24 full months). Human traffic only (agent=user).
Page views measure attention, not demand or buyers."""
import re
from datetime import date, timedelta
from urllib.parse import quote
from ..http_client import Policy, ConnectorFailure, NotFound
from .base import Connector, Hit, clip, require_contact, WIKIMEDIA_CONTACT

API = 'https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/all-access/user/{article}/{granularity}/{start}/{end}'
MAX_ARTICLES = 5

def default_window(today=None):
    """Last 24 full months. The API end date is inclusive, so ending on the 1st of the current month
    would add a one-day "month" (checked live: 128 views vs ~13,000 in a full month)."""
    today=today or date.today()
    end=date(today.year,today.month,1)-timedelta(days=1)  # last day of the previous month
    start=date(end.year-2,end.month,1)+timedelta(days=31)
    start=date(start.year,start.month,1)
    return start.strftime('%Y%m%d'),end.strftime('%Y%m%d')

def _period(ts, granularity):
    return f'{ts[:4]}-{ts[4:6]}' if granularity=='monthly' else f'{ts[:4]}-{ts[4:6]}-{ts[6:8]}'

class WikimediaPageviews(Connector):
    evidence_kind = 'secondary'
    OPTIONS = {'project':r'[a-z\-]{2,12}\.wikipedia','granularity':r'monthly|daily','start':r'\d{8}','end':r'\d{8}'}
    QUERY_HELP = 'Wikimedia necesita query con 1-5 títulos de artículo separados por | (p. ej. "QuickBooks|Xero")'

    def __init__(self):
        self.policy=Policy('wikimedia_pageviews',frozenset({'wikimedia.org'}),min_interval=0.2,contact_env=WIKIMEDIA_CONTACT)

    def build_query(self, terms):
        return ''  # article titles must be named exactly

    def validate_query(self, query):
        require_contact(WIKIMEDIA_CONTACT,'La política de robots de Wikimedia exige un contacto en el User-Agent')
        titles=[t.strip() for t in query.split('|')]
        if not 1<=len(titles)<=MAX_ARTICLES or any(not t or len(t)>200 or re.search(r'[#<>\[\]{}/]',t) for t in titles):
            raise ValueError(f'Usa de 1 a {MAX_ARTICLES} títulos de artículo separados por |, sin / # < > [ ] {{ }}')

    def search(self, fetch, query, options):
        project=options.get('project') or f"{self.terms.get('language') if self.terms.get('language') not in (None,'unknown') else 'en'}.wikipedia"
        granularity=options.get('granularity','monthly')
        start,end=default_window()
        start,end=options.get('start',start),options.get('end',end)
        hits,missing=[],[]
        for title in (t.strip() for t in query.split('|')):
            article=quote(title.replace(' ','_'),safe='')
            url=API.format(project=project,article=article,granularity=granularity,start=start,end=end)
            try:
                data,_,_=fetch(url,None)
            except NotFound:
                missing.append(title); continue
            items=data.get('items') if isinstance(data,dict) else None
            if not isinstance(items,list): raise ConnectorFailure('Respuesta de Wikimedia sin items')
            series=[(_period(str(i.get('timestamp','')),granularity),i.get('views')) for i in items if isinstance(i.get('views'),int)]
            if not series: missing.append(title); continue
            total=sum(v for _,v in series)
            snippet=(f'Páginas vistas (usuarios humanos) del artículo "{title}" en {project}, {granularity}: '+
                     '; '.join(f'{p}: {v}' for p,v in series)+f'. Total {total}. Fuente: Wikimedia Analytics API.')
            hits.append(Hit(url=url,title=f'{title} — páginas vistas en {project}',published_at=f'{end[:4]}-{end[4:6]}-{end[6:]}T00:00:00+00:00',
                            snippet=clip(snippet),independence_group='wikimedia:pageviews',
                            metadata={'article':title,'project':project,'granularity':granularity,'start':start,'end':end,
                                      'points':len(series),'total_views':total,'first':series[0],'last':series[-1],
                                      'series':[{'period':p,'views':v} for p,v in series],
                                      'caveat':'Interés relativo, no demanda ni compradores'}))
        if missing and not hits:
            raise ConnectorFailure(f'Ningún artículo encontrado en {project}: {", ".join(missing)}; revisa el título exacto o el proyecto')
        for h in hits: h.metadata['missing_articles']=missing
        return hits
