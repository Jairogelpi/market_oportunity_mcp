"""Silver layer: normalized, deduplicated observations (numbers) and documents (texts) derived from hits.

Extractors are deterministic and versioned: the same hit and EXTRACTOR_VERSION always yield the same rows,
with stable ids (hash of the defining fields), so re-running never duplicates."""
import hashlib
import json
import re
from datetime import date
from urllib.parse import urlsplit
from .core import utc_now

EXTRACTOR_VERSION = 'silver-1'

# ISO 3166-1 alpha-3 -> alpha-2 (OECD, EU/EFTA and other large economies) and statistical aliases.
ISO3 = dict(zip(
    'AUS AUT BEL CAN CHL COL CRI CZE DNK EST FIN FRA DEU GRC HUN ISL IRL ISR ITA JPN KOR LVA LTU LUX MEX NLD NZL NOR POL PRT '
    'SVK SVN ESP SWE CHE TUR GBR USA BGR HRV CYP MLT ROU LIE ARG BRA CHN IND IDN RUS SAU ZAF SGP HKG TWN ARE EGY NGA KEN MAR '
    'PHL VNM THA MYS PAK UKR PER'.split(),
    'AU AT BE CA CL CO CR CZ DK EE FI FR DE GR HU IS IE IL IT JP KR LV LT LU MX NL NZ NO PL PT '
    'SK SI ES SE CH TR GB US BG HR CY MT RO LI AR BR CN IN ID RU SA ZA SG HK TW AE EG NG KE MA '
    'PH VN TH MY PK UA PE'.split()))
ALIASES = {'EL':'GR','UK':'GB','EU27_2020':'EU27','EU27':'EU27','EUU':'EU27','EU':'EU27','WLD':'WORLD','1W':'WORLD','WORLD':'WORLD'}

def normalize_geo(code):
    """Country -> ISO alpha-2; known aggregates -> EU27 / WORLD; anything else kept upper-case as-is."""
    if not code: return None
    c=str(code).strip().upper()
    return ALIASES.get(c) or ISO3.get(c) or c

def _add_months(d, n):
    y,m=divmod(d.month-1+n,12)
    return date(d.year+y,m+1,1)

def period_bounds(label):
    """'2024' | '2024-Q2' | '2024Q2' | '2024-03' | '2024M03' | '2024-03-31' -> (start, end_exclusive) ISO dates."""
    s=str(label or '').strip()
    if m:=re.fullmatch(r'(\d{4})',s):
        y=int(m[1]); return date(y,1,1).isoformat(),date(y+1,1,1).isoformat()
    if m:=re.fullmatch(r'(\d{4})-?Q([1-4])',s):
        start=date(int(m[1]),3*int(m[2])-2,1); return start.isoformat(),_add_months(start,3).isoformat()
    if m:=re.fullmatch(r'(\d{4})-?M?(\d{2})',s):
        start=date(int(m[1]),int(m[2]),1); return start.isoformat(),_add_months(start,1).isoformat()
    if m:=re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})',s):
        d=date(int(m[1]),int(m[2]),int(m[3])); return d.isoformat(),date.fromordinal(d.toordinal()+1).isoformat()
    return None,None

def _id(*parts):
    return hashlib.sha256(json.dumps(parts,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]

def _num(v):
    try:
        return float(v)
    except (TypeError,ValueError):
        return None

def _obs(hit, dataset, measure, dims, value, geo=None, entity=None, period=None, start=None, end=None,
         unit=None, unit_mult=0, status=None):
    v=_num(value)
    if v is None: return None
    if period and not start: start,end=period_bounds(period)
    mult=int(unit_mult or 0)
    return {'obs_id':_id(hit['source_id'],dataset,measure,dims,geo,entity,period,start,end,unit),
            'source_id':hit['source_id'],'dataset':dataset,'measure':measure,'dims_json':json.dumps(dims,sort_keys=True,ensure_ascii=False),
            'geo':normalize_geo(geo),'entity':entity,'period_label':period,'period_start':start,'period_end':end,
            'unit':unit,'unit_mult':mult,'value_raw':v,'value_scaled':v*10**mult,'status_flag':status,
            'hit_id':hit['hit_id'],'bronze_sha256':hit.get('content_sha256')}

def _split(dims, keep_out):
    return ';'.join(f'{k}={v}' for k,v in sorted(dims.items()) if k not in keep_out)

def _eurostat(hit, md):
    dims=dict(md.get('observation') or {})
    return [_obs(hit,md['dataset'],_split(dims,('geo','time','freq','unit')) or md['dataset'],dims,md.get('value'),
                 geo=dims.get('geo'),period=dims.get('time'),unit=dims.get('unit'),status=md.get('status'))]

def _oecd(hit, md):
    dims=dict(md.get('observation') or {})
    return [_obs(hit,md['dataflow'],_split(dims,('REF_AREA','TIME_PERIOD','FREQ','UNIT_MEASURE')),dims,md.get('value'),
                 geo=dims.get('REF_AREA'),period=dims.get('TIME_PERIOD'),unit=dims.get('UNIT_MEASURE'),
                 unit_mult=md.get('unit_mult') or 0,status=md.get('obs_status'))]

def _worldbank(hit, md):
    dims={'indicator':md['indicator'],'country':md['country']}
    return [_obs(hit,'WDI',md['indicator'],dims,md.get('value'),geo=md['country'],period=md.get('date'),status=md.get('obs_status'))]

def _census(hit, md):
    row=md.get('row') or {}; labels=md.get('labels') or {}
    geo_level=list(row)[-1] if row else None
    vintage=(re.match(r'\d{4}',md['dataset']) or [None])[0]
    dims={k:v for k,v in row.items() if k not in labels and k!='NAME'}
    geo='US' if geo_level=='us' else f"US-{geo_level}-{row.get(geo_level)}" if geo_level else None
    return [_obs(hit,md['dataset'],var,dims,row.get(var),geo=geo,period=vintage,unit=labels[var]) for var in labels]

def _sec(hit, md):
    dims={'concept':md.get('concept'),'form':md.get('form'),'frame':md.get('frame'),'accn':md.get('accn')}
    return [_obs(hit,'xbrl_companyconcept',md.get('concept'),dims,md.get('value'),entity=f"CIK{md.get('cik')}",
                 period=md.get('frame'),start=md.get('start'),
                 end=date.fromordinal(date.fromisoformat(md['end']).toordinal()+1).isoformat() if md.get('end') else None,
                 unit=md.get('unit'))]

def _wikimedia(hit, md):
    dims={'article':md['article'],'project':md['project'],'granularity':md['granularity']}
    return [_obs(hit,'pageviews',f"pageviews:{md['project']}",dims,p['views'],entity=md['article'],period=p['period'],unit='views')
            for p in md.get('series') or []]

def _trend(hit, md):
    out=[]
    dims={'query':hit.get('query'),'granularity':md.get('granularity')}
    for p in md.get('series') or []:
        out.append(_obs(hit,f"{hit['source_id']}:trend",'mentions',dims,p['count'],period=p['period'],unit='count'))
        if p.get('total'):
            out.append(_obs(hit,f"{hit['source_id']}:trend",'platform_total',dims,p['total'],period=p['period'],unit='count'))
            out.append(_obs(hit,f"{hit['source_id']}:trend",'share',dims,p['count']/p['total'],period=p['period'],unit='ratio'))
    return out

OBSERVATION_EXTRACTORS = {'eurostat':_eurostat,'oecd':_oecd,'worldbank':_worldbank,'us_census':_census,
                          'sec_edgar':_sec,'wikimedia_pageviews':_wikimedia,'gdelt':_trend}
DOCUMENT_KINDS = {'github_issues':'issue','stackexchange':'question','hackernews':'post','mastodon':'post','bluesky':'post',
                  'ted':'notice','eurlex':'legal_act','federal_register':'rule','openalex':'paper','wikidata':'entity_record',
                  'internet_archive':'pricing_page'}

def _entity(hit, md):
    if hit['source_id']=='internet_archive': return md.get('host')
    if hit['source_id']=='wikidata':
        # Website domain when known, so a product's Wikidata record and its archived pricing page are one entity.
        return host_of(md['website']) if md.get('website') else md.get('qid')
    if hit['source_id']=='ted': return md.get('buyer_name')
    return None

def observations_from_hit(hit):
    md=hit['metadata']
    if md.get('mode')=='trend': fn=_trend
    else: fn=OBSERVATION_EXTRACTORS.get(hit['source_id'])
    return [o for o in (fn(hit,md) if fn else []) if o]

def document_from_hit(hit):
    md=hit['metadata']
    if hit['source_id'] not in DOCUMENT_KINDS or md.get('mode')=='trend': return None
    langs=md.get('langs')
    return {'doc_id':_id(hit['source_id'],hit['url']),'source_id':hit['source_id'],'url':hit['url'],'title':hit['title'],
            'published_at':hit['published_at'],'language':md.get('language') or (langs[0] if isinstance(langs,list) and langs else None),
            'excerpt':hit['snippet'],'author_group':md.get('independence_group'),'kind':DOCUMENT_KINDS[hit['source_id']],
            'entity':_entity(hit,md),'metadata_json':json.dumps(md,ensure_ascii=False,sort_keys=True),
            'hit_id':hit['hit_id'],'bronze_sha256':hit.get('content_sha256')}

OBS_COLS = ('obs_id','source_id','dataset','measure','dims_json','geo','entity','period_label','period_start','period_end',
            'unit','unit_mult','value_raw','value_scaled','status_flag','hit_id','bronze_sha256')
DOC_COLS = ('doc_id','source_id','url','title','published_at','language','excerpt','author_group','kind','entity',
            'metadata_json','hit_id','bronze_sha256')

def ingest_hits(db, hits):
    """Insert Silver rows for the given hits (dicts as returned by executor.list_hits). Returns counts of new rows."""
    now=utc_now(); new_obs=new_docs=0
    for hit in hits:
        for o in observations_from_hit(hit):
            cur=db.execute(f'INSERT OR IGNORE INTO silver_observations ({",".join(OBS_COLS)},extractor_version,extracted_at) '
                           f'VALUES ({",".join("?"*(len(OBS_COLS)+2))})',[o[c] for c in OBS_COLS]+[EXTRACTOR_VERSION,now])
            new_obs+=cur.rowcount
        if d:=document_from_hit(hit):
            cur=db.execute(f'INSERT OR IGNORE INTO silver_documents ({",".join(DOC_COLS)},extractor_version,extracted_at) '
                           f'VALUES ({",".join("?"*(len(DOC_COLS)+2))})',[d[c] for c in DOC_COLS]+[EXTRACTOR_VERSION,now])
            new_docs+=cur.rowcount
    return {'observations':new_obs,'documents':new_docs}

def build_all(db):
    """Initial load / rebuild: extract Silver from every stored hit (idempotent)."""
    from .executor import HIT_FIELDS, _hit_row
    rows=db.execute(f'SELECT {",".join(HIT_FIELDS[:-1])},metadata_json FROM source_hits ORDER BY rowid').fetchall()
    counts=ingest_hits(db,[_hit_row(r) for r in rows]); db.commit()
    return {'hits':len(rows),**counts}

def host_of(url):
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')

def query_observations(db, source_id='', measure='', geo='', dataset='', limit=50):
    """Compact view of Silver observations for the agent."""
    where,args=[],[]
    for col,val in (('source_id',source_id),('measure',measure),('geo',normalize_geo(geo) if geo else ''),('dataset',dataset)):
        if val: where.append(f'{col}=?'); args.append(val)
    sql=('SELECT obs_id,source_id,dataset,measure,geo,entity,period_label,unit,unit_mult,value_raw,value_scaled,status_flag '
         'FROM silver_observations'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY source_id,measure,geo,period_start LIMIT ?')
    cols=('obs_id','source_id','dataset','measure','geo','entity','period','unit','unit_mult','value_raw','value_scaled','status')
    return [dict(zip(cols,r)) for r in db.execute(sql,args+[min(max(int(limit),1),500)])]
