"""Research Executor: runs one connector step, stores hits, and promotes hits to evidence with a literal-excerpt check."""
import hashlib
import json
from . import bronze, silver
from .core import EvidenceError, active_run, append_event, remaining_queries, ingest, utc_now
from .http_client import SafeHttp, BudgetExhausted, StepBlocked, ConnectorFailure
from .connectors import REGISTRY
from .planner import find_step, last_terminal_event

_http = None

def default_http():
    global _http
    if _http is None: _http=SafeHttp()
    return _http

def _step_calls(db, run_id, step_id):
    return db.execute("SELECT COUNT(*) FROM research_events WHERE run_id=? AND step_id=? AND event_type='connector_call'",
                      (run_id,step_id)).fetchone()[0]

class _Fetch:
    """Budgeted fetch bound to one run step; also lets the connector honour server backoff."""
    def __init__(self, http, db, run_id, step_id, policy, query):
        self.http,self.db,self.run_id,self.step_id,self.policy,self.query=http,db,run_id,step_id,policy,query
    def __call__(self, url, params, headers=None, json_body=None, expect='json'):
        return self.http.get_json(self.db,self.run_id,self.step_id,self.policy,url,params,headers,self.query,json_body,expect,
                                  on_body=self._to_bronze)
    def _to_bronze(self, body, sha, logged_url, content_type):
        bronze.store(self.db,self.policy.source_id,body,sha,logged_url,content_type,self.run_id,self.step_id)
    def defer(self, seconds):
        self.http.defer(self.policy.source_id,seconds)

def execute_step(db, run_id, step_id, query='', retry=False, http=None, options=None):
    active_run(db,run_id)
    step=find_step(db,run_id,step_id)
    if step['route']!='connector' or step['source_id'] not in REGISTRY:
        raise EvidenceError('El paso no es de ruta connector; investígalo con WebSearch/WebFetch y ciérralo con complete_agent_step')
    if step['status']=='deferred': raise EvidenceError('Paso sin presupuesto asignado (deferred); replanifica con más presupuesto')
    last=last_terminal_event(db,run_id,step_id)
    if last in ('step_completed','blocked'): raise EvidenceError(f'Paso ya cerrado ({last})')
    if last=='connector_error' and not retry: raise EvidenceError('El paso falló; vuelve a ejecutarlo con retry=true solo si el fallo era transitorio')
    if _step_calls(db,run_id,step_id)>=step['budget']: raise EvidenceError('Presupuesto del paso agotado')
    connector=REGISTRY[step['source_id']]()
    connector.terms=_current_terms(db,run_id,step)
    try:
        options=connector.check_options(options)
    except ValueError as ex:
        raise EvidenceError(str(ex))
    q=' '.join((query or '').split()) or connector.build_query(connector.terms)
    if not q: raise EvidenceError(connector.query_help())
    try:
        connector.validate_query(q)
    except ValueError as ex:
        raise EvidenceError(str(ex))
    fetch=_Fetch(http or default_http(),db,run_id,step_id,connector.policy,q)
    base={'run_id':run_id,'step_id':step_id,'query':q,'options':options}
    try:
        hits=connector.search(fetch,q,options)
    except BudgetExhausted as ex:
        return _close(db,run_id,step_id,'budget_exhausted',str(ex),{**base,'status':'budget_exhausted'})
    except StepBlocked as ex:
        return _close(db,run_id,step_id,'blocked',str(ex),{**base,'status':'blocked','reason':str(ex)})
    except ConnectorFailure as ex:
        return _close(db,run_id,step_id,'connector_error',str(ex),{**base,'status':'failed','reason':str(ex)})
    except (KeyError,TypeError,ValueError,AttributeError) as ex:
        msg=f'Respuesta con formato inesperado: {type(ex).__name__}'
        return _close(db,run_id,step_id,'connector_error',msg,{**base,'status':'failed','reason':msg})
    sha=db.execute("SELECT content_sha256 FROM research_events WHERE run_id=? AND step_id=? AND event_type='connector_call' ORDER BY event_id DESC LIMIT 1",
                   (run_id,step_id)).fetchone()[0]
    ids,new=[],0
    for h in hits:
        hit_id=hashlib.sha256(f'{run_id}|{step["source_id"]}|{h.url}'.encode()).hexdigest()[:24]
        cur=db.execute('INSERT OR IGNORE INTO source_hits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (hit_id,run_id,step_id,step['source_id'],h.url,h.title,h.published_at,h.snippet,q,sha,utc_now(),
                        json.dumps({**h.metadata,'independence_group':h.independence_group},ensure_ascii=False)))
        new+=cur.rowcount; ids.append(hit_id)
    if not hits:
        # Not terminal: the step stays open so the agent can try a better query (budget permitting).
        left=step['budget']-_step_calls(db,run_id,step_id)
        return _close(db,run_id,step_id,'step_empty','0 resultados',
                      {**base,'status':'empty','hits':0,'step_budget_left':left,
                       'hint':'Sin resultados: prueba otra consulta (query) u opciones' if left>0 else
                              'Sin resultados y sin presupuesto del paso: replanifica con más presupuesto o ciérralo'})
    layer=silver.ingest_hits(db,[get_hit(db,i) for i in dict.fromkeys(ids)])
    return _close(db,run_id,step_id,'step_completed',f'{len(hits)} hits ({new} nuevos)',
                  {**base,'status':'completed','hits':len(hits),'new_hits':new,'hit_ids':ids,'silver':layer})

def _current_terms(db, run_id, step):
    """Plan terms plus the study's current keywords_en (search vocabulary may be refined after planning)."""
    terms=dict(step['query_terms'])
    row=db.execute('SELECT s.keywords_en FROM research_runs r JOIN studies s ON s.study_id=r.study_id WHERE r.run_id=?',
                   (run_id,)).fetchone()
    if row and row[0]: terms['keywords_en']=row[0]
    return terms

def _close(db, run_id, step_id, event_type, reason, result):
    append_event(db,run_id,event_type,reason=reason,step_id=step_id)
    db.commit()
    return {**result,'remaining_queries':remaining_queries(db,run_id)}

HIT_FIELDS = ('hit_id','run_id','step_id','source_id','url','title','published_at','snippet','query','content_sha256',
              'retrieved_at','metadata')

def _hit_row(r):
    d=dict(zip(HIT_FIELDS,r)); d['metadata']=json.loads(d['metadata']); return d

def list_hits(db, run_id, limit=100):
    rows=db.execute(f'SELECT {",".join(HIT_FIELDS[:-1])},metadata_json FROM source_hits WHERE run_id=? ORDER BY rowid LIMIT ?',
                    (run_id,limit)).fetchall()
    return [_hit_row(r) for r in rows]

PREVIEW = 200
PAGE_MAX = 50
# Small, decision-relevant metadata shown in compact listings.
COMPACT_META = ('site','repo','score','answer_count','comments','reactions','cited_by_count','publication_year',
                'venue','value','notice_type','buyer_name','dataset')

def hits_page(db, run_id, offset=0, limit=20, step_id=''):
    """Compact, paginated view for the agent: preview text, key metadata and how hits spread across
    independence groups (many hits from one group are one testimony, not many)."""
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=PAGE_MAX:
        raise EvidenceError(f'offset >= 0 y limit entre 1 y {PAGE_MAX}')
    where,args=('run_id=? AND step_id=?',(run_id,step_id)) if step_id else ('run_id=?',(run_id,))
    rows=[_hit_row(r) for r in db.execute(
        f'SELECT {",".join(HIT_FIELDS[:-1])},metadata_json FROM source_hits WHERE {where} ORDER BY rowid',args)]
    groups={}
    for h in rows: groups[h['metadata']['independence_group']]=groups.get(h['metadata']['independence_group'],0)+1
    page=[{'hit_id':h['hit_id'],'step_id':h['step_id'],'url':h['url'],'title':h['title'][:160],
           'published_at':h['published_at'],'group':h['metadata']['independence_group'],
           'preview':h['snippet'][:PREVIEW]+('…' if len(h['snippet'])>PREVIEW else ''),
           **{k:h['metadata'][k] for k in COMPACT_META if h['metadata'].get(k) is not None}}
          for h in rows[offset:offset+limit]]
    return {'run_id':run_id,'total':len(rows),'offset':offset,'limit':limit,'independent_groups':len(groups),
            'largest_groups':sorted(groups.items(),key=lambda kv:-kv[1])[:5],'hits':page}

def get_hit(db, hit_id):
    row=db.execute(f'SELECT {",".join(HIT_FIELDS[:-1])},metadata_json FROM source_hits WHERE hit_id=?',(hit_id,)).fetchone()
    if not row: raise EvidenceError('Hit inexistente')
    return _hit_row(row)

# Agent-supplied fields allowed on top of the automatic provenance.
EXTRA_FIELDS = frozenset({'limitations','contradictions','geography','language','sample_frame','sampling_method'})

def _norm(text):
    return ' '.join(text.split()).casefold()

def submit_evidence_from_hit(db, hit_id, claim, excerpt, extra=None):
    row=db.execute(f'SELECT {",".join(HIT_FIELDS[:-1])},metadata_json FROM source_hits WHERE hit_id=?',(hit_id,)).fetchone()
    if not row: raise EvidenceError('Hit inexistente')
    hit=_hit_row(row)
    if not isinstance(excerpt,str) or not excerpt.strip() or _norm(excerpt) not in _norm(hit['snippet']):
        raise EvidenceError('El extracto debe aparecer literalmente en el fragmento guardado del hit')
    extra=extra or {}
    if not isinstance(extra,dict): raise EvidenceError('extra debe ser un objeto JSON')
    if bad := sorted(set(extra)-EXTRA_FIELDS): raise EvidenceError(f'Campos no permitidos en extra (la procedencia es automática): {bad}')
    run=db.execute('SELECT question_id,study_id FROM research_runs WHERE run_id=?',(hit['run_id'],)).fetchone()
    record={**extra,'question_id':run[0],'claim':claim,'source_url':hit['url'],'source_title':hit['title'],
            'source_published_at':hit['published_at'],'retrieved_at':hit['retrieved_at'],'method':'official_api',
            'evidence_kind':REGISTRY[hit['source_id']].evidence_kind,'evidence_excerpt':' '.join(excerpt.split()),
            'research_query':hit['query'],'source_access_status':'needs_review','status':'candidate',
            'source_independence_group':hit['metadata']['independence_group'],'source_id':hit['source_id'],
            'hit_id':hit_id,'content_sha256':hit['content_sha256'],'run_id':hit['run_id']}
    if run[1]: record['study_id']=run[1]
    evidence_id,state,_=ingest(db,record)
    return {'evidence_id':evidence_id,'result':state}
