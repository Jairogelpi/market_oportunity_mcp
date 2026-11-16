"""Gold layer: catalog metrics computed deterministically from Silver, with full lineage.

Definitions live in catalog/metrics.json. The engine refuses (never guesses) when inputs are not homogeneous.
A metric value enters a conclusion only as derived evidence whose excerpt carries the figure literally."""
import hashlib
import json
import math
import uuid
from . import core
from .core import EvidenceError, ingest, load_questions, utc_now
from .studies import get_study

ENGINE_VERSION = 'gold-1'
FORMULAS = ('latest','sum','growth','count_distinct')  # add a type only together with its implementation
RELATIONS = ('component','proxy')
EU27 = frozenset('AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE'.split())

def load_metrics():
    with (core.CATALOG/'metrics.json').open(encoding='utf-8') as f:  # read at call time (tests patch it)
        return {m['metric_id']:m for m in json.load(f)}

def check_metrics():
    """Structural checks for catalog/metrics.json (used by check_catalog)."""
    errors=[]
    try:
        metrics=load_metrics()
    except (OSError,ValueError,KeyError,TypeError) as ex:
        return [f'metrics.json ilegible: {ex}']
    questions=load_questions(); catalog_metrics={q['primary_metric_id'] for q in questions.values()}
    for mid,m in metrics.items():
        where=f'metrics.json:{mid}'
        for key in ('catalog_metric','relation','question_ids','name','formula','inputs','params','unit','caveats','reviewed'):
            if key not in m: errors.append(f'{where}: falta {key}')
        if errors: continue
        if m['formula'].get('type') not in FORMULAS: errors.append(f'{where}: fórmula desconocida')
        if m['relation'] not in RELATIONS: errors.append(f'{where}: relation debe ser {RELATIONS}')
        if m['catalog_metric'] not in catalog_metrics: errors.append(f'{where}: catalog_metric inexistente')
        if bad:=[q for q in m['question_ids'] if q not in questions]: errors.append(f'{where}: preguntas inexistentes {bad}')
        if not isinstance(m['reviewed'],bool): errors.append(f'{where}: reviewed debe ser booleano')
        if m['inputs'].get('table') not in ('observations','documents'): errors.append(f'{where}: inputs.table inválido')
        if not mid.startswith('G'): errors.append(f'{where}: los ids propios empiezan por G (no suplantan a la métrica del catálogo)')
    return errors

def formula_hash(metric):
    return hashlib.sha256(json.dumps(metric,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def _study_runs_clause(alias):
    return f'{alias}.hit_id IN (SELECT h.hit_id FROM source_hits h JOIN research_runs r ON r.run_id=h.run_id WHERE r.study_id=?)'

def _observations(db, study_id, metric, params):
    src=params.get('source')
    if src not in metric['inputs']['sources']:
        raise EvidenceError(f"Fuente {src!r} no admitida por {metric['metric_id']}: {metric['inputs']['sources']}")
    sql=('SELECT o.obs_id,o.source_id,o.dataset,o.measure,o.dims_json,o.geo,o.entity,o.period_label,o.period_start,'
         'o.unit,o.unit_mult,o.value_scaled,o.hit_id FROM silver_observations o WHERE o.source_id=? AND o.dataset=? AND o.measure=? AND '
         +_study_runs_clause('o'))
    rows=db.execute(sql,(src,params['dataset'],params['measure'],study_id)).fetchall()
    cols=('obs_id','source_id','dataset','measure','dims','geo','entity','period','period_start','unit','unit_mult','value','hit_id')
    obs=[dict(zip(cols,r)) for r in rows]
    for o in obs: o['dims']=json.loads(o['dims'])
    want=params.get('dims') or {}
    obs=[o for o in obs if all(str(o['dims'].get(k))==str(v) for k,v in want.items())]
    if params.get('entity'): obs=[o for o in obs if o['entity']==params['entity']]
    if prefixes:=metric['inputs'].get('unit_prefix'):
        obs=[o for o in obs if any(str(o['unit'] or '').upper().startswith(p) for p in prefixes)]
    if not obs: raise EvidenceError('No hay observaciones Silver del study que cumplan la selección')
    if len({(o['unit'],o['unit_mult']) for o in obs})>1:
        raise EvidenceError(f"Unidades o multiplicadores distintos en la selección: {sorted({(o['unit'],o['unit_mult']) for o in obs})}")
    return obs

def _one_per(obs, key, what):
    seen={}
    for o in obs:
        if o[key] in seen:
            raise EvidenceError(f'Serie ambigua: varias observaciones para {what} {o[key]!r}; filtra más dimensiones (dims)')
        seen[o[key]]=o
    return seen

def _sum(obs, params):
    geos=[g.upper() for g in params['geos']]
    if 'WORLD' in geos and len(geos)>1: raise EvidenceError('Geografías solapadas: WORLD incluye a las demás')
    if 'EU27' in geos and EU27 & set(geos): raise EvidenceError('Geografías solapadas: EU27 incluye a sus Estados miembros')
    sel=[o for o in obs if o['period']==params['period'] and o['geo'] in geos]
    by_geo=_one_per(sel,'geo','la geografía')
    if missing:=[g for g in geos if g not in by_geo]: raise EvidenceError(f'Faltan observaciones para {missing} en {params["period"]}')
    return sum(o['value'] for o in sel),sel,{'geos':geos,'period':params['period']}

def _latest(obs, params):
    sel=[o for o in obs if o['geo']==params['geo'].upper()]
    if not sel: raise EvidenceError(f"Sin observaciones para {params['geo']}")
    last=max(o['period_start'] or '' for o in sel)
    (o,)=_one_per([x for x in sel if (x['period_start'] or '')==last],'geo','la geografía').values()
    return o['value'],[o],{'geo':o['geo'],'period':o['period']}

def _growth(obs, params):
    by_period=_one_per(obs,'period_start','el periodo')
    if len(by_period)<2: raise EvidenceError('El crecimiento necesita al menos dos periodos')
    first,last=by_period[min(by_period)],by_period[max(by_period)]
    if first['value']<=0: raise EvidenceError('Valor inicial nulo o negativo: el crecimiento relativo no está definido')
    change=last['value']/first['value']-1
    detail={'first':{'period':first['period'],'value':first['value']},'last':{'period':last['period'],'value':last['value']},
            'periods':len(by_period)}
    years=int(last['period'][:4])-int(first['period'][:4]) if str(first['period'])[:4].isdigit() and str(last['period'])[:4].isdigit() else 0
    if years>0 and len(str(first['period']))==4: detail['cagr']=(last['value']/first['value'])**(1/years)-1
    return change,sorted(by_period.values(),key=lambda o:o['period_start']),detail

def _count_distinct(db, study_id, metric, params):
    sources=params.get('sources') or metric['inputs']['sources']
    if bad:=[s for s in sources if s not in metric['inputs']['sources']]: raise EvidenceError(f'Fuentes no admitidas: {bad}')
    kinds=metric['inputs'].get('kinds') or []
    rows=db.execute(f'''SELECT d.doc_id,d.source_id,d.entity,d.hit_id FROM silver_documents d
                        WHERE d.deleted_at IS NULL AND d.entity IS NOT NULL AND d.source_id IN ({",".join("?"*len(sources))})
                        AND d.kind IN ({",".join("?"*len(kinds))}) AND {_study_runs_clause("d")}''',(*sources,*kinds,study_id)).fetchall()
    if not rows: raise EvidenceError('No hay documentos Silver del study que cumplan la selección')
    entities=sorted({r[2] for r in rows})
    return len(entities),[{'obs_id':r[0],'hit_id':r[3]} for r in rows],{'entities':entities}

def _groups(db, items):
    ids=[i['hit_id'] for i in items if i.get('hit_id')]
    if not ids: return []
    rows=db.execute(f'SELECT metadata_json,source_id FROM source_hits WHERE hit_id IN ({",".join("?"*len(ids))})',ids).fetchall()
    return sorted({json.loads(m).get('independence_group') or s for m,s in rows})

def compute_metric(db, study_id, metric_id, params):
    get_study(db,study_id)
    metrics=load_metrics()
    if metric_id not in metrics: raise EvidenceError(f'Métrica inexistente: {metric_id}')
    m=metrics[metric_id]; params=dict(params or {})
    if missing:=[p for p in m['params']['required'] if p not in params]: raise EvidenceError(f'Faltan parámetros {missing}')
    kind=m['formula']['type']
    if kind=='count_distinct':
        value,inputs,detail=_count_distinct(db,study_id,m,params)
        unit=m['unit']
    else:
        obs=_observations(db,study_id,m,params)
        if kind=='sum': value,inputs,detail=_sum(obs,params)
        elif kind=='latest': value,inputs,detail=_latest(obs,params)
        else: value,inputs,detail=_growth(obs,params)
        unit=(obs[0]['unit'] or m['unit']) if kind!='growth' else m['unit']
    if not math.isfinite(value): raise EvidenceError('Resultado no finito')
    groups=_groups(db,inputs)
    sources=sorted({i.get('source_id') for i in inputs if i.get('source_id')}) or sorted(params.get('sources') or m['inputs']['sources'])
    detail.update(sources=sources,n_inputs=len(inputs),formula=kind)
    row=(str(uuid.uuid4()),metric_id,study_id,json.dumps(params,sort_keys=True,ensure_ascii=False),value,unit,formula_hash(m),
         json.dumps([i['obs_id'] for i in inputs]),json.dumps(groups),json.dumps(detail,ensure_ascii=False),utc_now(),ENGINE_VERSION)
    db.execute('''INSERT INTO gold_metrics (gold_id,metric_id,study_id,params_json,value,unit,formula_hash,input_ids,input_groups,
                  detail_json,computed_at,engine_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',row)
    db.commit()
    return get_gold(db,row[0])

GOLD_COLS = ('gold_id','metric_id','study_id','params_json','value','unit','formula_hash','input_ids','input_groups',
             'detail_json','computed_at','engine_version')

def get_gold(db, gold_id):
    r=db.execute(f'SELECT {",".join(GOLD_COLS)} FROM gold_metrics WHERE gold_id=?',(gold_id,)).fetchone()
    if not r: raise EvidenceError('Valor Gold inexistente')
    g=dict(zip(GOLD_COLS,r))
    for k in ('params_json','input_ids','input_groups','detail_json'): g[k.removesuffix('_json')]=json.loads(g.pop(k))
    m=load_metrics().get(g['metric_id'])
    g['current_definition']=bool(m) and formula_hash(m)==g['formula_hash']
    g['reviewed']=bool(m and m['reviewed'])
    return g

def fmt_number(v):
    if float(v).is_integer(): return str(int(v))
    return f'{v:.6f}'.rstrip('0').rstrip('.')

def excerpt(g, metric):
    d=g['detail']; value=fmt_number(g['value'])
    if d['formula']=='growth':
        pct=fmt_number(round(g['value']*100,2))
        body=(f"variación relativa {value} ({pct} %) entre {d['first']['period']} ({fmt_number(d['first']['value'])}) "
              f"y {d['last']['period']} ({fmt_number(d['last']['value'])})"+
              (f", tasa anual compuesta {fmt_number(round(d['cagr'],6))}" if 'cagr' in d else ''))
    elif d['formula']=='count_distinct':
        body=f"{value} {g['unit']}: {', '.join(d['entities'][:20])}"
    elif d['formula']=='sum':
        body=f"{value} {g['unit']} ({'+'.join(d['geos'])}, {d['period']})"
    else:
        body=f"{value} {g['unit']} ({d['geo']}, {d['period']})"
    state='revisada' if g['reviewed'] else 'NO revisada'
    return (f"{g['metric_id']} = {body}. Fórmula {d['formula']} sobre {d['n_inputs']} entradas de {', '.join(d['sources'])}; "
            f"{metric['relation']} de {metric['catalog_metric']}; definición {state}.")

def promote_metric(db, gold_id, claim, question_id=''):
    g=get_gold(db,gold_id)
    if not g['current_definition']: raise EvidenceError('La definición de la métrica cambió: recalcula antes de promoverla')
    m=load_metrics()[g['metric_id']]
    qid=question_id or m['question_ids'][0]
    if qid not in m['question_ids']: raise EvidenceError(f"{g['metric_id']} no responde a {qid}")
    ids=g['input_ids']; marks=','.join('?'*len(ids))
    urls=[r[0] for r in db.execute(f'''SELECT DISTINCT h.url FROM source_hits h WHERE h.hit_id IN (
        SELECT hit_id FROM silver_observations WHERE obs_id IN ({marks}) UNION SELECT hit_id FROM silver_documents WHERE doc_id IN ({marks}))
        ORDER BY h.url''',ids+ids)]
    first=(urls[0],)
    limitations=m['caveats']+('' if g['reviewed'] else ' Definición de métrica no revisada por una persona.')
    record={'question_id':qid,'study_id':g['study_id'],'claim':claim,'source_url':first[0],'source_title':m['name'],
            'source_published_at':'unknown','retrieved_at':g['computed_at'],'method':'derived_metric','evidence_kind':'estimate',
            'evidence_excerpt':excerpt(g,m),'research_query':f"{g['metric_id']} {json.dumps(g['params'],sort_keys=True,ensure_ascii=False)}",
            'source_access_status':'needs_review','status':'candidate','source_independence_group':f"derived:{g['metric_id']}",
            'derived_from_groups':g['input_groups'],'derived_from_sources':g['detail']['sources'],'derived_from_urls':urls,'gold_id':gold_id,
            'metric_reviewed':g['reviewed'],'formula_hash':g['formula_hash'],'limitations':limitations}
    evidence_id,state,_=ingest(db,record)
    return {'evidence_id':evidence_id,'result':state,'excerpt':record['evidence_excerpt']}
