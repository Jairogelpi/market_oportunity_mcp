"""Bronze layer: raw, immutable API responses, content-addressed by SHA-256.

Only sources whose licence review is `approved` keep the full body on disk; for the others the index records
that nothing was stored and why (licence decisions in catalog/sources.csv and docs/license_audit.md)."""
import gzip
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from .core import EvidenceError, load_sources, utc_now

def bronze_root(db):
    """data/bronze next to the database file, so tests and alternate databases keep their own Bronze."""
    files=[r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main' and r[2]]
    if not files: raise EvidenceError('Base de datos sin fichero: Bronze no disponible')
    return Path(files[0]).parent/'bronze'

def store(db, source_id, body, sha256, logged_url, content_type, run_id=None, step_id=None):
    """Record one raw response; write it to disk only for approved sources. Idempotent per content hash."""
    if db.execute('SELECT 1 FROM bronze_objects WHERE sha256=?',(sha256,)).fetchone(): return False
    if hashlib.sha256(body).hexdigest()!=sha256: raise EvidenceError('Hash de la respuesta no coincide')
    licence=load_sources().get(source_id,{}).get('license_review')
    path=reason=None
    if licence=='approved':
        day=datetime.now(timezone.utc).date().isoformat()
        rel=Path(source_id)/day/f'{sha256}.json.gz'
        full=bronze_root(db)/rel
        if not full.exists():
            full.parent.mkdir(parents=True,exist_ok=True)
            tmp=full.with_suffix('.tmp')
            with gzip.open(tmp,'wb') as f: f.write(body)
            tmp.replace(full)  # atomic: a partial file never carries the final name
        path=rel.as_posix()
    else:
        reason=f'licencia {licence or "desconocida"}: no se guarda la respuesta completa'
    db.execute('''INSERT INTO bronze_objects (sha256,source_id,logged_url,retrieved_at,content_type,bytes,path,stored,reason,run_id,step_id)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
               (sha256,source_id,logged_url,utc_now(),content_type,len(body),path,1 if path else 0,reason,run_id,step_id))
    return True

def read(db, sha256):
    """Raw body of a stored object, verified against its hash."""
    row=db.execute('SELECT path,stored,reason FROM bronze_objects WHERE sha256=?',(sha256,)).fetchone()
    if not row: raise EvidenceError('Objeto Bronze inexistente')
    if not row[1]: raise EvidenceError(f'Objeto Bronze no almacenado: {row[2]}')
    with gzip.open(bronze_root(db)/row[0],'rb') as f: body=f.read()
    if hashlib.sha256(body).hexdigest()!=sha256: raise EvidenceError('Integridad Bronze rota: el contenido no coincide con su hash')
    return body
