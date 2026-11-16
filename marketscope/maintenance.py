"""Maintenance: withdraw evidence whose source post was deleted by its author (Bluesky developer guidelines require
honouring deletions; same courtesy for Mastodon). A deletion only ever withdraws evidence, it never verifies anything."""
import json
import re
from urllib.parse import urlsplit
from .core import utc_now
from .connectors.bluesky import Bluesky, SESSION
from .connectors.mastodon import Mastodon, INSTANCES
from .http_client import ConnectorFailure, StepBlocked
from .executor import default_http

GET_POSTS = 'https://bsky.social/xrpc/app.bsky.feed.getPosts'
BATCH = 25

def _candidates(db):
    rows=db.execute("SELECT evidence_id,payload FROM evidence WHERE status!='rejected'").fetchall()
    out=[]
    for eid,payload in rows:
        e=json.loads(payload)
        if e.get('source_id') in ('mastodon','bluesky'): out.append((eid,e))
    return out

def _withdraw(db, evidence_id, e, reason):
    now=utc_now()
    e.update(status='rejected',rejection_reason=reason,rejected_at=now)
    db.execute("UPDATE evidence SET status='rejected',payload=? WHERE evidence_id=?",(json.dumps(e,ensure_ascii=False),evidence_id))
    db.execute('INSERT INTO intake_events (evidence_id,event_type,at,details) VALUES (?,?,?,?)',
               (evidence_id,'deleted_at_source',now,reason))
    db.execute('UPDATE silver_documents SET deleted_at=? WHERE url=? AND deleted_at IS NULL',(now,e['source_url']))

def _mastodon_status(http, policy, url):
    """True = exists, False = deleted, None = could not tell."""
    parts=urlsplit(url); status_id=parts.path.rstrip('/').rsplit('/',1)[-1]
    if parts.hostname not in INSTANCES or not status_id.isdigit(): return None
    code,_=http.probe(policy,f'https://{parts.hostname}/api/v1/statuses/{status_id}')
    return True if code==200 else False if code in (404,410) else None

def _bluesky_uris(evidence):
    out={}
    for eid,e in evidence:
        m=re.fullmatch(r'https://bsky\.app/profile/(did:[^/]+)/post/([A-Za-z0-9]+)',e['source_url'])
        if m: out[eid]=f'at://{m[1]}/app.bsky.feed.post/{m[2]}'
    return out

def recheck_deleted(db, http=None):
    """Re-check every non-rejected Bluesky/Mastodon evidence; withdraw the ones deleted at source."""
    http=http or default_http()
    evidence=_candidates(db)
    summary={'checked':0,'deleted':0,'unknown':0,'skipped':[]}
    masto=Mastodon()
    for eid,e in [x for x in evidence if x[1]['source_id']=='mastodon']:
        try:
            alive=_mastodon_status(http,masto.policy,e['source_url'])
        except (ConnectorFailure,StepBlocked):
            alive=None
        summary['checked']+=1
        if alive is False: _withdraw(db,eid,e,'deleted_at_source'); summary['deleted']+=1
        elif alive is None: summary['unknown']+=1
    sky=[x for x in evidence if x[1]['source_id']=='bluesky']
    if sky:
        conn=Bluesky()
        if not (conn.handle and conn.password):
            summary['skipped'].append('bluesky: sin credenciales (MARKETSCOPE_BLUESKY_HANDLE / _APP_PASSWORD)')
        else:
            try:
                code,session=http.probe(conn.policy,SESSION,json_body={'identifier':conn.handle,'password':conn.password})
                token=(session or {}).get('accessJwt') if code==200 else None
            except (ConnectorFailure,StepBlocked):
                token=None
            uris=_bluesky_uris(sky); by_eid=dict(sky)
            if not token:
                summary['skipped'].append('bluesky: no se pudo abrir sesión'); summary['unknown']+=len(uris)
            else:
                items=list(uris.items())
                for i in range(0,len(items),BATCH):
                    chunk=items[i:i+BATCH]
                    try:
                        code,data=http.probe(conn.policy,GET_POSTS,[('uris',u) for _,u in chunk],{'Authorization':f'Bearer {token}'})
                    except (ConnectorFailure,StepBlocked):
                        code,data=None,None
                    summary['checked']+=len(chunk)
                    if code!=200 or not isinstance((data or {}).get('posts'),list):
                        summary['unknown']+=len(chunk); continue
                    present={p.get('uri') for p in data['posts']}
                    for eid,uri in chunk:
                        if uri not in present: _withdraw(db,eid,by_eid[eid],'deleted_at_source'); summary['deleted']+=1
    db.commit()
    return summary
