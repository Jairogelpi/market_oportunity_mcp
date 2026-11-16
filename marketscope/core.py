"""Research evidence intake: deterministic validation, provenance and SQLite persistence."""
import hashlib
import ipaddress
import csv
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

STATES = {'candidate','documented','observed','verified','rejected'}
KINDS = {'primary','secondary','opinion','estimate','agent_inference'}
METHODS = {'official_api','licensed_dataset','authorized_download','agent_research','manual','derived_metric'}

class EvidenceError(ValueError):
    pass

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def normalize_url(url):
    try:
        p = urlsplit(url.strip())
        if p.scheme not in ('https','http') or not p.hostname or p.username or p.password:
            raise ValueError()
        host=p.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith('.localhost') or host.endswith('.local'):
            raise ValueError()
        try:
            ip=ipaddress.ip_address(host.strip('[]'))
            if not ip.is_global: raise ValueError()
        except ValueError as ex:
            if host.replace('[','').replace(']','').count(':') >= 2 or all(c.isdigit() or c=='.' for c in host):
                raise ValueError() from ex
        if p.port is not None and p.port not in (80,443): raise ValueError()
        return urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path or '/',p.query,''))
    except (ValueError,AttributeError):
        raise EvidenceError('URL pública HTTP(S) inválida')

def validate(e):
    required = ('question_id','claim','source_url','source_title','source_published_at','retrieved_at','method','evidence_kind','evidence_excerpt','research_query','source_access_status')
    missing = [k for k in required if not isinstance(e.get(k),str) or not e[k].strip()]
    if missing:
        raise EvidenceError('Campos requeridos: '+', '.join(missing))
    if e['question_id'] not in load_questions():
        raise EvidenceError('question_id inválido')
    if e['method'] not in METHODS or e['evidence_kind'] not in KINDS:
        raise EvidenceError('method o evidence_kind inválido')
    if e.get('status','candidate') not in STATES:
        raise EvidenceError('status inválido')
    if e['source_access_status'] not in ('unknown','permitted','restricted','prohibited','needs_review'):
        raise EvidenceError('source_access_status inválido')
    for k in ('retrieved_at','source_published_at'):
        if e[k] != 'unknown':
            try:
                d = datetime.fromisoformat(e[k].replace('Z','+00:00'))
                if d.tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise EvidenceError(k+' requiere ISO 8601 con zona horaria o unknown')
    e = dict(e)
    e['source_url'] = normalize_url(e['source_url'])
    e.setdefault('status','candidate')
    if e['source_access_status'] != 'permitted' and e['status'] == 'verified':
        raise EvidenceError('No se puede verificar sin revisión positiva del acceso')
    if e['evidence_kind'] == 'agent_inference' and e['status'] == 'verified':
        raise EvidenceError('Una inferencia del agente no es evidencia primaria verificada')
    if e['status'] == 'verified' and (not e.get('reviewer') or str(e['reviewer']).strip().lower() in ('agent','claude','codex','llm','ai','research_agent')):
        raise EvidenceError('Verificación requiere revisor identificado')
    if e.get('study_id') in (None,''):
        e.pop('study_id',None)
    elif not isinstance(e['study_id'],str):
        raise EvidenceError('study_id inválido')
    # Legacy records (no study) keep their v0.2 identifier.
    key = '|'.join(([e['study_id']] if 'study_id' in e else [])+[e['question_id'],e['source_url'],e['claim'].strip().lower()])
    e['evidence_id'] = hashlib.sha256(key.encode()).hexdigest()[:24]
    return e

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT/'data'/'marketscope.sqlite'
TABLES = ('evidence','research_runs','research_events','intake_events','studies','research_plans','source_hits','conclusions',
          'bronze_objects','silver_observations','silver_documents','gold_metrics')
# PRAGMA user_version: 0 = v0.2 (sin versión), 3 = studies, 4 = planes y step_id en eventos,
# 5 = source_hits y metadatos HTTP en eventos, 6 = studies.keywords_en, 7 = conclusions, 8 = Bronze/Silver/Gold.
SCHEMA_VERSION = 8
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS evidence (
      evidence_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, source_url TEXT NOT NULL,
      status TEXT NOT NULL, method TEXT NOT NULL, evidence_kind TEXT NOT NULL,
      source_access_status TEXT NOT NULL, imported_at TEXT NOT NULL, payload TEXT NOT NULL,
      study_id TEXT)''',
    '''CREATE TABLE IF NOT EXISTS research_runs (
      run_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, query_budget INTEGER NOT NULL,
      created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', study_id TEXT)''',
    '''CREATE TABLE IF NOT EXISTS studies (
      study_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, market_definition TEXT NOT NULL,
      geography TEXT NOT NULL, segment TEXT NOT NULL, language TEXT NOT NULL, created_at TEXT NOT NULL,
      keywords_en TEXT NOT NULL DEFAULT '')''',
    '''CREATE TABLE IF NOT EXISTS research_events (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
      event_type TEXT NOT NULL, query TEXT, source_url TEXT, reason TEXT,
      occurred_at TEXT NOT NULL, step_id TEXT, http_status INTEGER, content_sha256 TEXT,
      FOREIGN KEY(run_id) REFERENCES research_runs(run_id))''',
    '''CREATE TABLE IF NOT EXISTS conclusions (
      conclusion_id TEXT PRIMARY KEY, study_id TEXT NOT NULL, question_id TEXT NOT NULL,
      verdict TEXT NOT NULL, allowed_verdict TEXT NOT NULL, text TEXT NOT NULL,
      cited_evidence_ids TEXT NOT NULL, uncertainty_json TEXT NOT NULL, metrics_json TEXT NOT NULL,
      metrics_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate', created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS bronze_objects (
      sha256 TEXT PRIMARY KEY, source_id TEXT NOT NULL, logged_url TEXT NOT NULL, retrieved_at TEXT NOT NULL,
      content_type TEXT, bytes INTEGER NOT NULL, path TEXT, stored INTEGER NOT NULL, reason TEXT,
      run_id TEXT, step_id TEXT)''',
    '''CREATE TABLE IF NOT EXISTS silver_observations (
      obs_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, dataset TEXT NOT NULL, measure TEXT NOT NULL,
      dims_json TEXT NOT NULL, geo TEXT, entity TEXT, period_label TEXT, period_start TEXT, period_end TEXT,
      unit TEXT, unit_mult INTEGER NOT NULL DEFAULT 0, value_raw REAL NOT NULL, value_scaled REAL NOT NULL,
      status_flag TEXT, hit_id TEXT, bronze_sha256 TEXT, extractor_version TEXT NOT NULL, extracted_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS silver_documents (
      doc_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, published_at TEXT,
      language TEXT, excerpt TEXT NOT NULL, author_group TEXT, kind TEXT NOT NULL, entity TEXT, metadata_json TEXT NOT NULL,
      hit_id TEXT, bronze_sha256 TEXT, deleted_at TEXT, extractor_version TEXT NOT NULL, extracted_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS gold_metrics (
      gold_id TEXT PRIMARY KEY, metric_id TEXT NOT NULL, study_id TEXT, params_json TEXT NOT NULL,
      value REAL NOT NULL, unit TEXT NOT NULL, formula_hash TEXT NOT NULL, input_ids TEXT NOT NULL,
      input_groups TEXT NOT NULL, detail_json TEXT NOT NULL, computed_at TEXT NOT NULL, engine_version TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS source_hits (
      hit_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL, source_id TEXT NOT NULL,
      url TEXT NOT NULL, title TEXT NOT NULL, published_at TEXT NOT NULL, snippet TEXT NOT NULL,
      query TEXT NOT NULL, content_sha256 TEXT NOT NULL, retrieved_at TEXT NOT NULL, metadata_json TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS research_plans (
      plan_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, version INTEGER NOT NULL,
      plan_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id,version))''',
    '''CREATE TABLE IF NOT EXISTS intake_events (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL,
      event_type TEXT NOT NULL, at TEXT NOT NULL, details TEXT NOT NULL)''',
)

class DatabaseNotInitialized(EvidenceError):
    pass

def resolve_db_path(path=None):
    """Absolute DB path: explicit arg > MARKETSCOPE_DB > default; relative paths anchor to the project root, never to the cwd."""
    p = Path(path or os.environ.get('MARKETSCOPE_DB') or DEFAULT_DB).expanduser()
    if not p.is_absolute(): p = PROJECT_ROOT/p
    return p.resolve()

def init_db(path):
    """The only operation that creates the directory, file and schema. Idempotent."""
    path = resolve_db_path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        for stmt in SCHEMA: db.execute(stmt)
        _migrate(db)
        db.commit()
    finally:
        db.close()
    return path

def _migrate(db):
    """Idempotent upgrade of v0.2 databases: legacy rows keep study_id NULL."""
    for table,column,kind in (('research_runs','study_id','TEXT'),('evidence','study_id','TEXT'),
                              ('research_events','step_id','TEXT'),('research_events','http_status','INTEGER'),
                              ('research_events','content_sha256','TEXT'),
                              ('studies','keywords_en',"TEXT NOT NULL DEFAULT ''")):
        if column not in {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
    db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

@contextmanager
def open_db(path, write=False):
    """Open an existing, initialized DB. Read-only by default; never creates files or tables."""
    path = resolve_db_path(path)
    if not path.is_file():
        raise DatabaseNotInitialized(f'Base de datos no inicializada: {path}. Ejecuta: python -m marketscope.cli init')
    db = sqlite3.connect(path.as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True)
    try:
        found = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if missing := [t for t in TABLES if t not in found]:
            raise DatabaseNotInitialized(f'Esquema incompleto en {path} (faltan: {", ".join(missing)}). Ejecuta: python -m marketscope.cli init')
        if (version := db.execute('PRAGMA user_version').fetchone()[0]) < SCHEMA_VERSION:
            raise DatabaseNotInitialized(f'Esquema desactualizado en {path} (versión {version} < {SCHEMA_VERSION}). Ejecuta: python -m marketscope.cli init')
        yield db
        if write: db.commit()
    except BaseException:
        if write: db.rollback()
        raise
    finally:
        db.close()

def connect(path):
    """Compatibility helper: initialize and return a writable connection. The caller must close it."""
    return sqlite3.connect(init_db(path))

def require_study(db, study_id):
    if not db.execute('SELECT 1 FROM studies WHERE study_id=?',(study_id,)).fetchone():
        raise EvidenceError('Study inexistente')

def ingest(db, record):
    e = validate(record)
    if 'study_id' in e: require_study(db,e['study_id'])
    existing = db.execute('SELECT payload FROM evidence WHERE evidence_id=?',(e['evidence_id'],)).fetchone()
    if existing:
        old = json.loads(existing[0]); return e['evidence_id'],'duplicate',old
    db.execute('''INSERT INTO evidence (evidence_id,question_id,source_url,status,method,evidence_kind,
                  source_access_status,imported_at,payload,study_id) VALUES (?,?,?,?,?,?,?,?,?,?)''',(
        e['evidence_id'],e['question_id'],e['source_url'],e['status'],e['method'],
        e['evidence_kind'],e['source_access_status'],utc_now(),json.dumps(e,ensure_ascii=False),e.get('study_id')))
    db.execute('INSERT INTO intake_events (evidence_id,event_type,at,details) VALUES (?,?,?,?)',
               (e['evidence_id'],'ingested',utc_now(),'Initial research evidence'))
    db.commit()
    return e['evidence_id'],'created',e

def export(db):
    return [json.loads(x[0]) for x in db.execute('SELECT payload FROM evidence ORDER BY evidence_id')]

def summary(db):
    rows = db.execute('SELECT status,COUNT(*) FROM evidence GROUP BY status').fetchall()
    return {'total':sum(n for _,n in rows),'by_status':dict(rows),
            'verified':sum(n for s,n in rows if s=='verified')}


CATALOG=PROJECT_ROOT/'catalog'

def load_questions():
    with (CATALOG/'questions.csv').open(encoding='utf-8-sig',newline='') as f:
        return {row['question_id']:row for row in csv.DictReader(f)}

def load_sources():
    with (CATALOG/'sources.csv').open(encoding='utf-8-sig',newline='') as f:
        return {row['source_id']:row for row in csv.DictReader(f)}

QUESTION_COLUMNS = ('question_id','dimension_id','decision_id','question','falsifiable_hypothesis','primary_metric_id',
    'operational_definition','population_and_eligibility','unit_of_analysis','evidence_required','abstention_condition','priority','status')
SOURCE_COLUMNS = ('source_id','provider','dataset','access_method','dimension_ids','official_or_candidate_documentation',
    'verification_status','license_review','live_test','production_ready','priority_tier')
SOURCE_ENUMS = {'verification_status':{'candidate','documented','tested','rejected'},
    'license_review':{'pending','approved','restricted','rejected'},
    'live_test':{'not_tested','passed','failed'},
    'production_ready':{'true','false'},
    'priority_tier':{'P0','P1','P2'}}

def _read_catalog(name):
    raw = (CATALOG/name).read_bytes()
    text = raw.decode('utf-8-sig')  # UnicodeDecodeError => catalog is not UTF-8
    return list(csv.DictReader(text.splitlines())), text

def check_catalog():
    """Structural and referential integrity of catalog/*.csv. Returns a list of errors (empty = OK)."""
    errors = []
    try:
        questions, qtext = _read_catalog('questions.csv')
        sources, stext = _read_catalog('sources.csv')
    except (OSError,UnicodeDecodeError) as ex:
        return [f'Catálogo ilegible: {ex}']
    for name,text in (('questions.csv',qtext),('sources.csv',stext)):
        if '\ufffd' in text or '\u00c3' in text: errors.append(f'{name}: posible mojibake o carácter de reemplazo')
    for name,rows,cols in (('questions.csv',questions,QUESTION_COLUMNS),('sources.csv',sources,SOURCE_COLUMNS)):
        if not rows: errors.append(f'{name}: vacío'); continue
        if missing := [c for c in cols if c not in rows[0]]:
            errors.append(f'{name}: faltan columnas {missing}'); continue
        for i,row in enumerate(rows,2):
            if None in row or any(v is None for v in row.values()): errors.append(f'{name}:{i}: número de columnas incorrecto')
            elif empty := [c for c in cols if not row[c].strip()]: errors.append(f'{name}:{i}: campos vacíos {empty}')
    if errors: return errors
    seen = set()
    for i,q in enumerate(questions,2):
        qid = q['question_id']
        if not re.fullmatch(r'Q\d{2}\.\d{2}',qid): errors.append(f'questions.csv:{i}: question_id mal formado {qid!r}')
        if qid in seen: errors.append(f'questions.csv:{i}: question_id duplicado {qid}')
        seen.add(qid)
        if not re.fullmatch(r'DIM\d{2}',q['dimension_id']) or q['dimension_id'][3:] != qid[1:3]:
            errors.append(f'questions.csv:{i}: dimension_id {q["dimension_id"]!r} no corresponde a {qid}')
        if not re.fullmatch(r'D\d{2}',q['decision_id']): errors.append(f'questions.csv:{i}: decision_id mal formado')
        if not re.fullmatch(r'M\d{2}_\d{2}',q['primary_metric_id']) or q['primary_metric_id'][1:3] != qid[1:3]:
            errors.append(f'questions.csv:{i}: primary_metric_id {q["primary_metric_id"]!r} no corresponde a {qid}')
        if q['priority'] not in ('P0','P1','P2'): errors.append(f'questions.csv:{i}: priority inválida')
    dimensions = {q['dimension_id'] for q in questions}
    seen = set()
    for i,src in enumerate(sources,2):
        sid = src['source_id']
        if not re.fullmatch(r'[a-z0-9_]+',sid): errors.append(f'sources.csv:{i}: source_id mal formado {sid!r}')
        if sid in seen: errors.append(f'sources.csv:{i}: source_id duplicado {sid}')
        seen.add(sid)
        for col,allowed in SOURCE_ENUMS.items():
            if src[col] not in allowed: errors.append(f'sources.csv:{i}: {col}={src[col]!r} fuera de {sorted(allowed)}')
        if unknown := sorted(set(src['dimension_ids'].split()) - dimensions):
            errors.append(f'sources.csv:{i}: dimensiones inexistentes {unknown}')
        try:
            if not normalize_url(src['official_or_candidate_documentation']).startswith('https://'): raise EvidenceError()
        except EvidenceError:
            errors.append(f'sources.csv:{i}: documentación no es URL HTTPS pública')
        if src['production_ready'] == 'true' and (src['license_review'] != 'approved' or src['live_test'] != 'passed'):
            errors.append(f'sources.csv:{i}: production_ready=true exige license_review=approved y live_test=passed')
    from .gold import check_metrics  # gold depends on core; resolve lazily
    return errors+check_metrics()

def start_run(db,question_id,query_budget=10,study_id=None):
    if question_id not in load_questions(): raise EvidenceError('Pregunta no catalogada')
    if type(query_budget) is not int or not 1 <= query_budget <= 100: raise EvidenceError('Presupuesto inválido (1..100)')
    study_id=study_id or None
    if study_id: require_study(db,study_id)
    import uuid
    run_id=str(uuid.uuid4())
    db.execute('INSERT INTO research_runs (run_id,question_id,query_budget,created_at,status,study_id) VALUES (?,?,?,?,?,?)',
               (run_id,question_id,query_budget,utc_now(),'active',study_id))
    db.commit()
    return run_id

# Events that consume one unit of the run's query budget (each connector HTTP request is one).
BUDGET_EVENTS = ('search','connector_call')

def active_run(db,run_id):
    row=db.execute('SELECT run_id,question_id,query_budget,status,study_id FROM research_runs WHERE run_id=?',(run_id,)).fetchone()
    if not row or row[3]!='active': raise EvidenceError('Research run inexistente o cerrado')
    return dict(zip(('run_id','question_id','query_budget','status','study_id'),row))

def remaining_queries(db,run_id):
    budget=db.execute('SELECT query_budget FROM research_runs WHERE run_id=?',(run_id,)).fetchone()[0]
    used=db.execute(f"SELECT COUNT(*) FROM research_events WHERE run_id=? AND event_type IN ({','.join('?'*len(BUDGET_EVENTS))})",
                    (run_id,*BUDGET_EVENTS)).fetchone()[0]
    return budget-used

def append_event(db,run_id,event_type,query='',source_url='',reason='',step_id=None,http_status=None,content_sha256=None):
    """Low-level audit append; callers validate event semantics."""
    db.execute('''INSERT INTO research_events(run_id,event_type,query,source_url,reason,occurred_at,step_id,http_status,content_sha256)
                  VALUES (?,?,?,?,?,?,?,?,?)''',
               (run_id,event_type,query,source_url,reason,utc_now(),step_id or None,http_status,content_sha256))

def log_research_event(db,run_id,event_type,query='',source_url='',reason='',step_id=''):
    if event_type not in ('search','discarded','source_considered'): raise EvidenceError('Evento inválido')
    active_run(db,run_id)
    if step_id:
        from .planner import find_step  # planner depends on core; resolve lazily
        find_step(db,run_id,step_id)
    if event_type=='search':
        if remaining_queries(db,run_id) <= 0: raise EvidenceError('Presupuesto de búsquedas agotado')
        if not query.strip(): raise EvidenceError('La búsqueda requiere query')
    if source_url: source_url=normalize_url(source_url)
    if event_type=='discarded' and (not source_url or not reason.strip()): raise EvidenceError('Descartes requieren URL y motivo')
    append_event(db,run_id,event_type,query,source_url,reason,step_id)
    db.commit()
    return {'run_id':run_id,'event_type':event_type,'remaining_queries':remaining_queries(db,run_id)}

def run_report(db,run_id):
    run=db.execute('SELECT run_id,question_id,query_budget,created_at,status,study_id FROM research_runs WHERE run_id=?',(run_id,)).fetchone()
    if not run: raise EvidenceError('Research run inexistente')
    cols=('event_type','query','source_url','reason','occurred_at','step_id','http_status','content_sha256')
    events=db.execute(f'SELECT {",".join(cols)} FROM research_events WHERE run_id=? ORDER BY event_id',(run_id,)).fetchall()
    return {'run_id':run[0],'question_id':run[1],'query_budget':run[2],'created_at':run[3],'status':run[4],'study_id':run[5],
            'events':[dict(zip(cols,e)) for e in events]}
