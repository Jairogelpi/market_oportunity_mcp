"""Internet Archive Wayback Machine: archived competitor pages (pricing history).
CDX API: https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server

The query is a page URL without scheme (e.g. "freshbooks.com/pricing"). One CDX request lists one capture
per year (status 200, HTML); then up to `snapshots` captures (default 3: first, middle, last) are fetched
raw (`id_`), scripts/styles removed, and the text around price patterns kept as a short excerpt.
A published list price is the competitor's own claim, not the price customers actually pay."""
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from ..core import normalize_url, EvidenceError
from ..http_client import Policy, ConnectorFailure, BudgetExhausted, StepBlocked
from .base import Connector, Hit, clip, html_text

CDX = 'https://web.archive.org/cdx/search/cdx'
RAW = 'https://web.archive.org/web/{ts}id_/{url}'
VIEW = 'https://web.archive.org/web/{ts}/{url}'
PRICE = re.compile(r'(?:[$€£¥]|\b(?:USD|EUR|GBP|US\$)\s?)\s?\d[\d.,]*(?:\s?(?:USD|EUR|GBP)\b)?'
                   r'(?:\s?(?:/|per)\s?(?:mo|month|mes|yr|year|año|user|usuario)\w*)?',re.I)
NOISE = re.compile(r'<(script|style|noscript|svg|template)\b.*?</\1\s*>',re.I|re.S)
CONTEXT = 70
MAX_PRICES = 20

def pick(rows, n):
    """First, last and evenly spaced captures in between."""
    if len(rows)<=n: return rows
    idx={round(i*(len(rows)-1)/(n-1)) for i in range(n)} if n>1 else {len(rows)-1}
    return [rows[i] for i in sorted(idx)]

def join_split_numbers(text):
    """Pricing markup splits figures across tags: '$ 8 .40' -> '$8.40' (checked live on an archived pricing page),
    so a conclusion quoting 8.40 matches the excerpt."""
    text=re.sub(r'(\d)\s+([.,]\d)',r'\1\2',text)
    return re.sub(r'([$€£¥])\s+(\d)',r'\1\2',text)

def price_excerpt(html):
    text=join_split_numbers(html_text(NOISE.sub(' ',html)))
    spans,prices=[],[]
    for m in PRICE.finditer(text):
        prices.append(m.group(0).strip())
        s,e=max(0,m.start()-CONTEXT),min(len(text),m.end()+CONTEXT)
        if spans and s<=spans[-1][1]: spans[-1]=(spans[-1][0],e)
        else: spans.append((s,e))
        if len(prices)>=MAX_PRICES: break
    return ' … '.join(text[s:e].strip() for s,e in spans),prices,text

class InternetArchive(Connector):
    evidence_kind = 'primary'
    OPTIONS = {'from':r'(19|20)\d{2}','to':r'(19|20)\d{2}','snapshots':r'[1-6]'}
    QUERY_HELP = 'Internet Archive necesita query=<URL de la página sin esquema> (p. ej. "freshbooks.com/pricing")'

    def __init__(self):
        self.policy=Policy('internet_archive',frozenset({'web.archive.org'}),min_interval=2.0,timeout=60)

    def build_query(self, terms):
        return ''  # the agent names the competitor page

    def validate_query(self, query):
        if '://' in query or len(query)>300 or not re.fullmatch(r'[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(/[\w\-./%~+]*)?',query):
            raise ValueError('URL no válida: usa dominio y ruta sin esquema, p. ej. "freshbooks.com/pricing"')
        try:
            normalize_url('https://'+query)  # rejects local or private hosts
        except EvidenceError as ex:
            raise ValueError(str(ex))

    def search(self, fetch, query, options):
        params=[('url',query),('output','json'),('filter','statuscode:200'),('filter','mimetype:text/html'),
                ('collapse','timestamp:4'),('fl','timestamp,original,digest'),('limit','60')]
        for k in ('from','to'):
            if k in options: params.append((k,options[k]))
        data,_,_=fetch(CDX,params)
        if not isinstance(data,list): raise ConnectorFailure('Respuesta CDX con formato inesperado')
        rows=[r for r in data[1:] if isinstance(r,list) and len(r)>=3 and re.fullmatch(r'\d{14}',str(r[0]))]
        hits,failed=[],[]
        for ts,original,digest in (r[:3] for r in pick(rows,int(options.get('snapshots','3')))):
            try:
                html,sha,_=fetch(RAW.format(ts=ts,url=original),None,None,expect='text')
            except (BudgetExhausted,StepBlocked):
                raise
            except ConnectorFailure as ex:
                failed.append(f'{ts}: {ex}'); continue
            hits.append(self._hit(ts,original,digest,html))
        if failed and not hits: raise ConnectorFailure('Ninguna captura recuperable: '+'; '.join(failed))
        for h in hits: h.metadata.update(captures_available=len(rows),failed_captures=failed)
        return hits

    def _hit(self, ts, original, digest, html):
        when=datetime.strptime(ts,'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        excerpt,prices,text=price_excerpt(html)
        body=excerpt if prices else f'Sin precios detectados. Inicio del texto: {text[:300]}'
        host=(urlsplit(original).hostname or '').lower().removeprefix('www.')
        return Hit(url=VIEW.format(ts=ts,url=original),title=f'{original} — captura {when.date()}',published_at=when.isoformat(),
                   snippet=clip(f'Página {original} archivada el {when.date()}: {body}'),independence_group=f'web:{host}',
                   metadata={'original':original,'timestamp':ts,'digest':digest,'host':host,'prices':prices,
                             'price_count':len(prices),
                             'caveat':'Precio de lista publicado por el propio competidor; no es el precio efectivo pagado y puede variar por región, moneda o promoción'})
