"""Research Planner: (study, catalogued question) -> deterministic, versioned research plan."""
import json
import re
import uuid
from .core import (EvidenceError, load_questions, load_sources, active_run, append_event, remaining_queries, utc_now)
from .studies import get_study
from .connectors import REGISTRY

PLAN_VERSION_FORMAT = 1
CONNECTOR_SOURCES = frozenset(REGISTRY)
EU_MEMBERS = frozenset('AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE'.split())
REGION_BY_SCOPE = {'UE':'EU','España':'ES','Estados Unidos':'US'}
# Access that needs a contract, agreement, permissions or unverified terms: never attempted.
RESTRICTED_ACCESS = re.compile(r'comercial|acuerdo|a verificar|con permisos|autorizado',re.I)
RESTRICTED_AUTH = re.compile(r'contrato|cuenta',re.I)
# Evidence that cannot be gathered from the public web.
NON_WEB = re.compile(r'entrevista|encuesta|propi[oa]|intern[oa]|privad|experimento|cohorte|contrato|contabilidad|'
                     r'facturaci|\blogs?\b|load test|consentid|estudios? de campo|evaluaci[oó]n ciega|diario|\bCRM\b|'
                     r'atribuci|acuerdo|autorizad|benchmark',re.I)
MIN_GROUPS = re.compile(r'>=\s*(\d+)[^;]*?independ',re.I)
TIER_ORDER = {'P0':0,'P1':1,'P2':2}

def source_region(scope):
    return REGION_BY_SCOPE.get(scope.strip(),'GLOBAL')

# Testimony sources whose authors are a self-selected, mostly technical population.
TESTIMONY_POPULATION = {
    'github_issues':'desarrolladores y usuarios técnicos de proyectos de código abierto',
    'stackexchange':'población técnica (desarrolladores, administradores de sistemas)',
    'hackernews':'comunidad tecnológica, mayoritariamente anglófona',
    'reddit':'comunidades autoseleccionadas por tema',
    'producthunt':'early adopters de productos tecnológicos',
    'indiehackers':'fundadores independientes de productos digitales',
}
TECH_SEGMENT = re.compile(r'desarroll|developer|programad|programmer|ingenier|engineer|devops|sysadmin|'
                          r'administrador(es)? de sistemas|\bIT\b|\bTI\b|técnic|technical|software (team|house)',re.I)

# Company-filing sources: useful as competitor data for any market, never as local market size.
COMPANY_DATA_SOURCES = {'sec_edgar':'empresas cotizadas en EE. UU.: datos de competidores, no del mercado local'}

# Broader social networks: not technical, but still self-selected (always warned, whatever the segment).
SELF_SELECTED_SOCIAL = {'bluesky':'usuarios de Bluesky','mastodon':'usuarios de instancias de Mastodon'}

def population_warning(source_id, study):
    if source_id in SELF_SELECTED_SOCIAL:
        return (f'Muestra autoseleccionada de {SELF_SELECTED_SOCIAL[source_id]}: '
                'usar como señal cualitativa, nunca como testimonio representativo ni para inferir prevalencia')
    population=TESTIMONY_POPULATION.get(source_id)
    if not population or TECH_SEGMENT.search(f"{study['segment']} {study['market_definition']}"): return None
    return (f"La población de {source_id} ({population}) no coincide con el segmento '{study['segment']}': "
            'usar como señal cualitativa, nunca como testimonio representativo ni para inferir prevalencia')

def geo_coverage(study_geo, region):
    """'full' if the source covers the study region, 'partial' if it covers only part of it,
    'global_unfiltered' if it is global and cannot be restricted to the study region, None if disjoint."""
    if region=='GLOBAL': return 'full' if study_geo=='GLOBAL' else 'global_unfiltered'
    if region==study_geo or (region=='EU' and study_geo in EU_MEMBERS): return 'full'
    if study_geo=='GLOBAL' or (study_geo=='EU' and region in EU_MEMBERS): return 'partial'
    return None

def access_exclusion(src):
    if src['verification_status']=='rejected' or src['license_review']=='rejected':
        return 'fuente rechazada en el inventario'
    if RESTRICTED_ACCESS.search(src['access_method']) or RESTRICTED_AUTH.search(src['authentication']):
        return f"acceso '{src['access_method']}' / autenticación '{src['authentication']}' no autorizados ni verificados"
    return None

def _hint_tokens(text):
    return [t.strip() for t in re.split(r'[;,]',text) if t.strip()]

def _words(text):
    return set(re.findall(r'[a-z0-9]+',text.lower()))

def _mentions_source(token, sources):
    """Whole-word match on provider, or compact match on source_id ('World Bank' -> worldbank)."""
    words=_words(token); compact=re.sub(r'[^a-z0-9]','',token.lower())
    return any(_words(s['provider'])<=words or sid.replace('_','')==compact for sid,s in sources.items())

def build_plan(run, question, study, sources, connector_sources=CONNECTOR_SOURCES):
    """Pure function: same inputs => same plan."""
    dim=question['dimension_id']; geo=study['geography']
    steps,excluded=[],[]
    for sid,src in sorted(sources.items()):
        if dim not in src['dimension_ids'].split(): continue
        region=source_region(src['geographic_scope'])
        if geo=='unknown':
            coverage='global_unfiltered' if region=='GLOBAL' else None
        else:
            coverage=geo_coverage(geo,region)
        scope_note=None
        if sid in COMPANY_DATA_SOURCES and coverage!='full':
            coverage,scope_note='partial',COMPANY_DATA_SOURCES[sid]
        if coverage is None:
            excluded.append({'source_id':sid,'reason':f"cobertura '{src['geographic_scope']}' fuera de la geografía del study ({geo})"})
            continue
        if reason := access_exclusion(src):
            excluded.append({'source_id':sid,'reason':reason}); continue
        steps.append({'source_id':sid,'route':'connector' if sid in connector_sources else 'agent','coverage':coverage,
                      'priority_tier':src['priority_tier'],'risk_notes':src['risk_notes'],
                      'population_warning':population_warning(sid,study),'scope_note':scope_note,
                      'license_review':src['license_review']})
    steps.sort(key=lambda s:(s['route']!='connector',TIER_ORDER.get(s['priority_tier'],9),s['source_id']))
    _allocate_budget(steps,run['query_budget'])
    terms={'market':study['market_definition'],'focus':question['primary_indicator'],'language':study['language']}
    if study['segment']!='unknown': terms['segment']=study['segment']
    if geo not in ('unknown','GLOBAL'): terms['geography']=geo
    if study.get('keywords_en'): terms['keywords_en']=study['keywords_en']
    # step_id = source_id (one step per source): closures stay attached to the same source across replans.
    for s in steps:
        s.update(step_id=s['source_id'],query_terms=terms,status='pending' if s['budget'] else 'deferred')
    tokens=_hint_tokens(question['candidate_sources_unverified'])
    m=MIN_GROUPS.search(question['operational_definition'])
    return {
        'plan_format':PLAN_VERSION_FORMAT,'run_id':run['run_id'],'study_id':study['study_id'],'question_id':question['question_id'],
        'objective':f"{question['question']} — {study['market_definition']} ({geo}; segmento: {study['segment']}; idioma: {study['language']})",
        'population_defined':geo!='unknown' and study['segment']!='unknown',
        'evidence_required':question['evidence_required'],'abstention_condition':question['abstention_condition'],
        'independence_check':question['independence_check'],
        'min_independent_groups':int(m.group(1)) if m else 2,
        'steps':steps,'excluded_sources':excluded,
        'gaps_out_of_scope':[t for t in tokens if NON_WEB.search(t)],
        'source_hints':[t for t in tokens if not NON_WEB.search(t) and not _mentions_source(t,sources)],
        'bias_notes':question['principal_bias'],
        'population_warnings':[f"{s['source_id']}: {s['population_warning']}" for s in steps if s['population_warning']],
        'license_notes':[f"{s['source_id']}: licencia restricted, ver docs/license_audit.md antes de usarla en informes"
                         for s in steps if s['license_review']=='restricted'],
        'coverage_notes':('global_unfiltered: la fuente es global y no puede restringirse a la geografía del study; '
                          'sus resultados no son representativos de esa geografía'
                          if any(s['coverage']=='global_unfiltered' for s in steps) else None),
    }

def _allocate_budget(steps, budget):
    """One query per step in priority order, then round-robin the rest (connector steps first)."""
    for s in steps: s['budget']=0
    for s in steps:
        if budget<=0: break
        s['budget']+=1; budget-=1
    pool=[s for s in steps if s['route']=='connector'] or steps
    i=0
    while budget>0 and pool:
        pool[i%len(pool)]['budget']+=1; budget-=1; i+=1

def _store(db, run_id, version, plan):
    db.execute('INSERT INTO research_plans (plan_id,run_id,version,plan_json,created_at) VALUES (?,?,?,?,?)',
               (str(uuid.uuid4()),run_id,version,json.dumps(plan,ensure_ascii=False,sort_keys=True),utc_now()))

def _generate(db, run_id):
    run=active_run(db,run_id)
    if not run['study_id']:
        raise EvidenceError('El run no pertenece a un study; crea uno con create_study y abre el run con study_id')
    return build_plan(run,load_questions()[run['question_id']],get_study(db,run['study_id']),load_sources())

def get_plan(db, run_id):
    row=db.execute('SELECT version,plan_json FROM research_plans WHERE run_id=? ORDER BY version DESC LIMIT 1',(run_id,)).fetchone()
    if not row: raise EvidenceError('El run no tiene plan; ejecuta plan_research')
    return {'plan_version':row[0],**json.loads(row[1])}

def plan_research(db, run_id):
    """Create version 1, or return the latest plan if one exists (idempotent)."""
    try: return get_plan(db,run_id)
    except EvidenceError: pass
    _store(db,run_id,1,_generate(db,run_id)); db.commit()
    return get_plan(db,run_id)

def replan_research(db, run_id, reason):
    if not isinstance(reason,str) or not reason.strip(): raise EvidenceError('Replanificar requiere motivo')
    version=get_plan(db,run_id)['plan_version']+1
    _store(db,run_id,version,_generate(db,run_id))
    append_event(db,run_id,'replanned',reason=reason.strip())
    db.commit()
    return get_plan(db,run_id)

def find_step(db, run_id, step_id):
    for s in get_plan(db,run_id)['steps']:
        if s['step_id']==step_id: return s
    raise EvidenceError(f'Paso {step_id!r} inexistente en el plan vigente')

# connector_error closes a step for next_step; execute_step(retry=True) may reopen it explicitly.
TERMINAL_EVENTS = ('step_completed','blocked','connector_error')

def last_terminal_event(db, run_id, step_id):
    row=db.execute(f"SELECT event_type FROM research_events WHERE run_id=? AND step_id=? AND event_type IN ({','.join('?'*len(TERMINAL_EVENTS))}) ORDER BY event_id DESC LIMIT 1",
                   (run_id,step_id,*TERMINAL_EVENTS)).fetchone()
    return row[0] if row else None

def _closed_steps(db, run_id):
    return {r[0] for r in db.execute(
        f"SELECT DISTINCT step_id FROM research_events WHERE run_id=? AND step_id IS NOT NULL AND event_type IN ({','.join('?'*len(TERMINAL_EVENTS))})",
        (run_id,*TERMINAL_EVENTS))}

def next_step(db, run_id):
    active_run(db,run_id)
    plan=get_plan(db,run_id); closed=_closed_steps(db,run_id); left=remaining_queries(db,run_id)
    pending=[s for s in plan['steps'] if s['status']=='pending' and s['step_id'] not in closed]
    base={'run_id':run_id,'plan_version':plan['plan_version'],'remaining_queries':left,
          'pending_steps':len(pending),'deferred_steps':sum(s['status']=='deferred' for s in plan['steps'])}
    if left<=0: return {**base,'done':True,'reason':'presupuesto agotado'}
    if not pending: return {**base,'done':True,'reason':'no quedan pasos pendientes'}
    step=pending[0]
    empty=db.execute("SELECT COUNT(*) FROM research_events WHERE run_id=? AND step_id=? AND event_type='step_empty'",
                     (run_id,step['step_id'])).fetchone()[0]
    return {**base,'done':False,'step':step,
            **({'note':f'{empty} intento(s) sin resultados: cambia la consulta'} if empty else {})}

def complete_agent_step(db, run_id, step_id, notes):
    active_run(db,run_id)
    step=find_step(db,run_id,step_id)
    if step['route']!='agent': raise EvidenceError('Solo los pasos de ruta agent se cierran manualmente')
    if step_id in _closed_steps(db,run_id): raise EvidenceError('Paso ya cerrado')
    if not isinstance(notes,str) or not notes.strip(): raise EvidenceError('Cerrar un paso requiere notas (qué se buscó y qué se encontró)')
    append_event(db,run_id,'step_completed',reason=notes.strip(),step_id=step_id)
    db.commit()
    return next_step(db,run_id)
