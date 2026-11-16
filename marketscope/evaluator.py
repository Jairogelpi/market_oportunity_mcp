"""Evidence Evaluator: deterministic metrics and the strongest verdict the evidence allows.
The agent writes the conclusion; submit_conclusion validates it against these rules. Nothing here can
mark anything verified: SUPPORTED needs evidence verified by an independent human review."""
import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit
from . import attribution
from .core import EvidenceError, load_questions, utc_now
from .planner import MIN_GROUPS
from .studies import get_study

# Verdict strength. Abstentions share rank 0: a conclusion may always be more conservative than allowed.
RANK = {'INSUFFICIENT_EVIDENCE':0,'DEFER':0,'WEAK_SUPPORT':1,'MODERATE_SUPPORT':2,'SUPPORTED':3}
ABSTENTIONS = ('INSUFFICIENT_EVIDENCE','DEFER')
# Questions about how many / how much: opinions or anecdotes can never support them (AGENTS.md rule 10).
QUANTITATIVE_DIMENSIONS = frozenset({'DIM02','DIM05','DIM06','DIM11'})
STRONG_KINDS = ('primary','secondary','estimate')
UNCERTAINTY_FIELDS = ('coverage_limits','biases','missing_evidence','contradictions')

def _groups(e):
    """Independence groups an evidence contributes. A derived metric contributes the groups of its inputs,
    never a new one: summing three OECD observations is still one OECD lineage."""
    if e.get('method')=='derived_metric' and e.get('derived_from_groups'): return list(e['derived_from_groups'])
    return [e.get('source_independence_group') or f"host:{(urlsplit(e['source_url']).hostname or '').lower()}"]

def _unreviewed_derived(e):
    return e.get('method')=='derived_metric' and not e.get('metric_reviewed')

def _evidence(db, study_id, question_id):
    rows=db.execute("SELECT payload FROM evidence WHERE study_id=? AND question_id=? AND status!='rejected' ORDER BY evidence_id",
                    (study_id,question_id)).fetchall()
    return [json.loads(r[0]) for r in rows]

def _plans(db, study_id, question_id):
    """Latest plan of every run of this study and question."""
    rows=db.execute('''SELECT p.plan_json FROM research_plans p JOIN research_runs r ON r.run_id=p.run_id
                       WHERE r.study_id=? AND r.question_id=? AND p.version=(SELECT MAX(version) FROM research_plans q WHERE q.run_id=p.run_id)''',
                    (study_id,question_id)).fetchall()
    return [json.loads(r[0]) for r in rows]

def _blocked_steps(db, study_id, question_id):
    rows=db.execute('''SELECT DISTINCT e.step_id FROM research_events e JOIN research_runs r ON r.run_id=e.run_id
                       WHERE r.study_id=? AND r.question_id=? AND e.event_type='blocked' AND e.step_id IS NOT NULL''',
                    (study_id,question_id)).fetchall()
    return sorted(r[0] for r in rows)

def min_groups(question):
    m=MIN_GROUPS.search(question['operational_definition'])
    return int(m.group(1)) if m else 2

def metrics(db, study_id, question_id):
    questions=load_questions()
    if question_id not in questions: raise EvidenceError('Pregunta no catalogada')
    study=get_study(db,study_id); question=questions[question_id]
    ev=_evidence(db,study_id,question_id); plans=_plans(db,study_id,question_id)
    warned={w.split(':',1)[0] for p in plans for w in p.get('population_warnings') or []}
    by=lambda key:{k:sum(1 for e in ev if e.get(key)==k) for k in sorted({e.get(key) for e in ev})}
    groups=sorted({g for e in ev for g in _groups(e)})
    # A figure from a metric definition nobody has reviewed is not strong evidence yet.
    strong=[e for e in ev if e['evidence_kind'] in STRONG_KINDS and not _unreviewed_derived(e)]
    contradictions=[c for e in ev for c in (e.get('contradictions') or []) if c]
    dates=sorted(e['source_published_at'] for e in ev if e.get('source_published_at') not in (None,'unknown'))
    return {
        'study_id':study_id,'question_id':question_id,'dimension_id':question['dimension_id'],
        'quantitative':question['dimension_id'] in QUANTITATIVE_DIMENSIONS,
        'min_independent_groups':min_groups(question),
        'n_evidence':len(ev),'by_method':by('method'),'by_kind':by('evidence_kind'),
        'independent_groups':len(groups),'groups':groups,
        'groups_with_checked_excerpt':len({g for e in ev if e.get('hit_id') or (e.get('method')=='derived_metric' and e.get('metric_reviewed'))
                                           for g in _groups(e)}),
        'unreviewed_derived':sum(1 for e in ev if _unreviewed_derived(e)),
        'strong_evidence':len(strong),
        # evidence_required = primary_or_verified_secondary: a candidate secondary source does not meet it.
        'evidence_requirement_met':any((e['evidence_kind']=='primary' and e.get('hit_id')) or
                                       (e['evidence_kind'] in ('primary','secondary') and e.get('status')=='verified') for e in ev),
        'verified_groups':len({g for e in ev if e.get('status')=='verified' for g in _groups(e)}),
        'declared_contradictions':len(contradictions),
        'population_defined':study['geography']!='unknown' and study['segment']!='unknown',
        'only_population_warned_sources':bool(ev) and all(e.get('source_id') in warned for e in ev),
        'gaps':sorted({g for p in plans for g in p.get('gaps_out_of_scope') or []}),
        'blocked_sources':_blocked_steps(db,study_id,question_id),
        'age_range':[dates[0],dates[-1]] if dates else None,
        'evidence_ids':sorted(e['evidence_id'] for e in ev),
    }

def verdict(m):
    """First rule that applies wins. Returns (verdict, reasons)."""
    reasons=[]
    if not m['population_defined']:
        reasons.append('Población no definida: el study necesita geografía y segmento')
    if m['independent_groups']<m['min_independent_groups']:
        reasons.append(f"{m['independent_groups']} grupos de fuentes independientes; se necesitan {m['min_independent_groups']}")
    if m['quantitative'] and m['strong_evidence']==0 and m['n_evidence']:
        reasons.append('Pregunta cuantitativa respaldada solo por opiniones: no permiten inferir prevalencia ni magnitud')
    if reasons: return 'INSUFFICIENT_EVIDENCE',reasons
    if m['declared_contradictions']:
        return 'DEFER',[f"{m['declared_contradictions']} contradicción(es) declarada(s) sin resolver"]
    if m['verified_groups']>=m['min_independent_groups'] and m['evidence_requirement_met']:
        return 'SUPPORTED',['Evidencia verificada por revisión independiente en grupos suficientes']
    weak=[]
    if not m['evidence_requirement_met']:
        weak.append('Falta evidencia primaria con extracto comprobado o secundaria verificada (primary_or_verified_secondary)')
    if m['groups_with_checked_excerpt']==0:
        weak.append('Ningún grupo con extracto comprobado contra la fuente')
    if m['only_population_warned_sources']:
        weak.append('Todo el apoyo procede de fuentes con aviso de población (técnica o autoseleccionada)')
    if weak: return 'WEAK_SUPPORT',weak
    return 'MODERATE_SUPPORT',['Grupos independientes suficientes, extracto comprobado y requisito de evidencia cumplido']

def _hash(m):
    return hashlib.sha256(json.dumps(m,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def evaluate_question(db, study_id, question_id):
    m=metrics(db,study_id,question_id)
    allowed,reasons=verdict(m)
    return {'allowed_verdict':allowed,'reasons':reasons,'metrics':m,'metrics_hash':_hash(m),
            'note':'SUPPORTED solo es alcanzable con evidencias verificadas por revisión humana independiente'}

NUMBER = re.compile(r'\d+(?:[.,]\d+)*')
EXEMPT = re.compile(r'\bQ\d{2}\.\d{2}\b|\b[0-9a-f]{24}\b|\b(?:19|20)\d{2}\b')

THOUSANDS = re.compile(r'\d{1,3}(?:[.,]\d{3})+')

def _plain(token):
    """'17,438' / '17.438' -> '17438' only for thousands-separator shapes; '1.5' stays '1.5'."""
    return re.sub(r'[.,]','',token) if THOUSANDS.fullmatch(token) else token

def unsupported_numbers(text, excerpts, allowed=()):
    """Numbers in the conclusion that do not appear in any cited excerpt (years, question and evidence ids exempt).
    Derived figures (growth rates, sums) are rejected on purpose: the conclusion may not fabricate quantities."""
    known={t for x in excerpts for t in NUMBER.findall(x)}|set(allowed)
    known|={_plain(t) for t in known}
    return sorted({t for t in NUMBER.findall(EXEMPT.sub(' ',text)) if t not in known and _plain(t) not in known})

def _nonempty(value):
    return bool(value.strip()) if isinstance(value,str) else bool(value) if isinstance(value,list) else False

def submit_conclusion(db, study_id, question_id, record):
    if not isinstance(record,dict): raise EvidenceError('La conclusión debe ser un objeto JSON')
    ev=evaluate_question(db,study_id,question_id)
    m,allowed=ev['metrics'],ev['allowed_verdict']
    v=record.get('verdict')
    if v not in RANK: raise EvidenceError(f'verdict debe ser uno de {list(RANK)}')
    if RANK[v]>RANK[allowed]:
        raise EvidenceError(f'Veredicto {v} supera el permitido ({allowed}): '+'; '.join(ev['reasons']))
    text=record.get('text')
    if not isinstance(text,str) or len(text.strip())<20: raise EvidenceError('text requerido (mín. 20 caracteres)')
    cited=record.get('cited_evidence_ids')
    if not isinstance(cited,list) or not all(isinstance(c,str) for c in cited): raise EvidenceError('cited_evidence_ids debe ser una lista')
    by_id={e['evidence_id']:e for e in _evidence(db,study_id,question_id)}
    if unknown := sorted(set(cited)-set(by_id)):
        raise EvidenceError(f'Evidencias citadas inexistentes, rechazadas o de otro study/pregunta: {unknown}')
    if v not in ABSTENTIONS:
        if not cited: raise EvidenceError('Un veredicto de apoyo debe citar evidencias')
        groups={g for c in cited for g in _groups(by_id[c])}
        if len(groups)<m['min_independent_groups']:
            raise EvidenceError(f"Las citas cubren {len(groups)} grupo(s) independiente(s); se necesitan {m['min_independent_groups']}")
    if RANK[v]>RANK['WEAK_SUPPORT'] and any(_unreviewed_derived(by_id[c]) for c in cited):
        raise EvidenceError('Cita métricas cuya definición no ha revisado una persona: el veredicto no puede pasar de WEAK_SUPPORT')
    u=record.get('uncertainty')
    if not isinstance(u,dict) or (missing:=[f for f in UNCERTAINTY_FIELDS if f not in u]):
        raise EvidenceError(f'uncertainty requiere {list(UNCERTAINTY_FIELDS)}')
    if empty := [f for f in ('coverage_limits','biases','missing_evidence') if not _nonempty(u[f])]:
        raise EvidenceError(f'uncertainty con campos vacíos: {empty}')
    stated=' '.join(u['missing_evidence']) if isinstance(u['missing_evidence'],list) else str(u['missing_evidence'])
    if absent := [g for g in m['gaps'] if g.casefold() not in stated.casefold()]:
        raise EvidenceError(f'missing_evidence debe mencionar las carencias del plan: {absent}')
    excerpts=[by_id[c]['evidence_excerpt'] for c in cited]
    counts=(len(cited),m['independent_groups'],m['min_independent_groups'],m['n_evidence'])
    if bad := unsupported_numbers(text,excerpts,[str(n) for n in counts]):
        raise EvidenceError(f'Cifras sin respaldo literal en los extractos citados: {bad}. No se pueden derivar ni estimar cantidades')
    row=(str(uuid.uuid4()),study_id,question_id,v,allowed,text.strip(),json.dumps(sorted(set(cited))),
         json.dumps(u,ensure_ascii=False),json.dumps(m,ensure_ascii=False),ev['metrics_hash'],'candidate',utc_now())
    db.execute('''INSERT INTO conclusions (conclusion_id,study_id,question_id,verdict,allowed_verdict,text,cited_evidence_ids,
                  uncertainty_json,metrics_json,metrics_hash,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',row)
    db.commit()
    return {'conclusion_id':row[0],'verdict':v,'allowed_verdict':allowed,'status':'candidate'}

def list_conclusions(db, study_id):
    get_study(db,study_id)
    out=[]
    for r in db.execute('''SELECT conclusion_id,question_id,verdict,allowed_verdict,text,cited_evidence_ids,uncertainty_json,
                           metrics_hash,status,created_at FROM conclusions WHERE study_id=? ORDER BY created_at''',(study_id,)):
        current=_hash(metrics(db,study_id,r[1]))
        out.append({'conclusion_id':r[0],'question_id':r[1],'verdict':r[2],'allowed_verdict':r[3],'text':r[4],
                    'cited_evidence_ids':json.loads(r[5]),'uncertainty':json.loads(r[6]),'status':r[8],'created_at':r[9],
                    'stale':current!=r[7],'attribution':attribution.attribution(db,r[0])})
    return out
