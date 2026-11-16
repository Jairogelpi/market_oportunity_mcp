"""Connector contract: build a query from plan terms, run budgeted requests through `fetch`, return hits."""
import html
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

SNIPPET_MAX = 1500  # licences pending: keep short excerpts and metadata, never full responses

@dataclass(frozen=True)
class Hit:
    url: str
    title: str
    published_at: str          # ISO 8601 with timezone, or 'unknown'
    snippet: str               # literal text an evidence excerpt must come from
    independence_group: str
    metadata: dict = field(default_factory=dict)

def iso_date(value):
    """'YYYY-MM-DD' -> ISO 8601 at UTC midnight; anything else -> 'unknown'."""
    return f'{value}T00:00:00+00:00' if isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) else 'unknown'

TREND_OPTIONS = {'mode':r'trend','granularity':r'year|quarter|month','periods':r'[1-9]|1[0-2]','normalize':r'yes|no'}
MAX_PERIODS = 12

def period_ranges(granularity, periods, today=None):
    """Last `periods` COMPLETE periods, oldest first, as (label, start, end_exclusive) UTC datetimes.
    The current period is excluded: a partial period would look like a drop (see Wikimedia fix)."""
    today=today or datetime.now(timezone.utc).date()
    if granularity=='year':
        return [(str(y),datetime(y,1,1,tzinfo=timezone.utc),datetime(y+1,1,1,tzinfo=timezone.utc))
                for y in range(today.year-periods,today.year)]
    step=3 if granularity=='quarter' else 1
    month=((today.month-1)//step)*step+1  # first month of the current (partial) period
    out=[]
    y,m=today.year,month
    for _ in range(periods):
        m-=step
        if m<1: m+=12; y-=1
        ey,em=(y+(m+step-1)//12,(m+step-1)%12+1)
        label=f'{y}-Q{(m-1)//3+1}' if granularity=='quarter' else f'{y}-{m:02d}'
        out.append((label,datetime(y,m,1,tzinfo=timezone.utc),datetime(ey,em,1,tzinfo=timezone.utc)))
    return out[::-1]

def trend_hit(source, query, unit, granularity, series, url, normalize, published_at, extra=None):
    """One hit with the literal per-period series. series = [(label, count, total_or_None)].
    Raw counts grow with the platform itself, so the share of all activity is given when normalized."""
    def pct(c,t):
        """Three significant digits, never rounded to an identical-looking 0.001 % (checked on GDELT data)."""
        p=c/t*100
        if p==0: return '0'
        decimals=max(3,2-math.floor(math.log10(p)))
        return f'{p:.{decimals}f}'
    def fmt(c,t): return f'{c} de {t} ({pct(c,t)} %)' if normalize and t else str(c)
    body='; '.join(f'{label}: {fmt(c,t)}' for label,c,t in series)
    note=('Proporción sobre el total de la plataforma en cada periodo.' if normalize else
          'Recuento bruto: crece también con la actividad de la plataforma; no es una tendencia normalizada.')
    per={'year':'año','quarter':'trimestre','month':'mes'}.get(granularity,granularity)
    snippet=f'{unit} que mencionan «{query}» en {source}, por {per}: {body}. {note}'
    return Hit(url=url,title=f'Tendencia de «{query}» en {source} ({granularity})',
               published_at=published_at,snippet=clip(snippet),independence_group=f'{source}:trend',
               metadata={'mode':'trend','granularity':granularity,'normalized':normalize,
                         'series':[{'period':l,'count':c,'total':t} for l,c,t in series],**(extra or {})})

def iso_datetime(value):
    """Any ISO 8601 timestamp with zone (e.g. '2026-06-15T11:00:00+0200') -> normalized ISO; else 'unknown'."""
    try:
        d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except ValueError:
        return 'unknown'
    return d.isoformat() if d.tzinfo else 'unknown'

def clip(text):
    text=' '.join((text or '').split())
    return text if len(text)<=SNIPPET_MAX else text[:SNIPPET_MAX].rsplit(' ',1)[0]

WIKIMEDIA_CONTACT = 'MARKETSCOPE_WIKIMEDIA_CONTACT'

def require_contact(env, why):
    """Some APIs require a declared contact in the User-Agent; refuse before any request when it is missing."""
    if not os.environ.get(env,'').strip():
        raise ValueError(f'{why}: define {env} en .env (email, web o usuario wiki). No se ha enviado ninguna petición.')

def dedupe_words(text):
    """Collapse repeated words (case-insensitive), keeping first occurrence order."""
    seen,out=set(),[]
    for w in (text or '').split():
        if w.casefold() not in seen: seen.add(w.casefold()); out.append(w)
    return ' '.join(out)

def html_text(fragment):
    """Visible text of an HTML fragment (titles/bodies returned by APIs)."""
    return ' '.join(html.unescape(re.sub(r'<[^>]+>',' ',fragment or '')).split())

class Connector:
    """Subclasses set `policy`, `evidence_kind`, `OPTIONS` and implement build_query/search."""
    policy = None
    evidence_kind = None
    OPTIONS = {}  # option name -> validation regex for agent-supplied options
    QUERY_HELP = 'Consulta vacía'
    ENGLISH_FIRST = False  # mostly-English corpus: a non-English template query is useless
    terms = {}  # plan query_terms of the step being executed (set by the executor)

    def build_query(self, terms):
        """Template query from the study. English-first sources use keywords_en, and give up
        (empty => ask the agent) rather than send a query in another language."""
        if self.ENGLISH_FIRST:
            if terms.get('keywords_en'): return dedupe_words(terms['keywords_en'])
            if terms.get('language') not in ('en','unknown'): return ''
        return dedupe_words(' '.join(t for t in (terms.get('market'),terms.get('segment')) if t))

    def query_help(self):
        if self.ENGLISH_FIRST and self.terms.get('language') not in ('en','unknown'):
            return (f"{self.policy.source_id} es mayoritariamente en inglés y el study está en '{self.terms.get('language')}': "
                    'pasa query en inglés o define keywords_en en el study (set_study_keywords). No se ha gastado presupuesto.')
        return self.QUERY_HELP

    def validate_query(self, query):
        """Raise ValueError before any request if the query cannot be valid for this API."""

    REQUIRED_OPTIONS = ()

    def check_options(self, options):
        options=options or {}
        if not isinstance(options,dict): raise ValueError('options debe ser un objeto JSON')
        if missing := [k for k in self.REQUIRED_OPTIONS if k not in options]:
            raise ValueError(f'{self.policy.source_id} requiere options {missing}. {self.QUERY_HELP}')
        for k,v in options.items():
            if k not in self.OPTIONS: raise ValueError(f'Opción no admitida por {self.policy.source_id}: {k}')
            if not isinstance(v,str) or not re.fullmatch(self.OPTIONS[k],v): raise ValueError(f'Valor no válido para {k}')
        return options

    def search(self, fetch, query, options):
        """`fetch(url, params, headers=None)` -> (data, sha256, headers); each call costs one query unit.
        `fetch.defer(seconds)` honours a server-requested backoff for this source."""
        raise NotImplementedError
