"""Study: the market context (what, where, for whom) that research runs and evidence belong to."""
import re
import sqlite3
import uuid
from .core import EvidenceError, utc_now

FIELDS = ('study_id','name','market_definition','geography','segment','language','created_at','keywords_en')
LIMITS = {'name':120,'market_definition':500,'segment':200,'keywords_en':200}

def _text(value, field):
    if not isinstance(value,str) or not value.strip():
        raise EvidenceError(f'{field} requerido')
    value=' '.join(value.split())
    if len(value) > LIMITS[field]:
        raise EvidenceError(f'{field} supera {LIMITS[field]} caracteres')
    return value

def _keywords(value):
    """Optional English search keywords for mostly-English sources (GitHub, Stack Exchange, OpenAlex)."""
    if value is None or (isinstance(value,str) and not value.strip()): return ''
    return _text(value,'keywords_en')

def normalize_geography(value):
    """ISO 3166-1 alfa-2, EU, GLOBAL o unknown."""
    v=(value or '').strip()
    if v.lower()=='unknown': return 'unknown'
    v=v.upper()
    if v in ('EU','GLOBAL') or re.fullmatch(r'[A-Z]{2}',v): return v
    raise EvidenceError('geography requiere código ISO de 2 letras, EU, GLOBAL o unknown')

def normalize_language(value):
    """ISO 639-1 o unknown."""
    v=(value or '').strip().lower()
    if v=='unknown' or re.fullmatch(r'[a-z]{2}',v): return v
    raise EvidenceError('language requiere código ISO 639-1 de 2 letras o unknown')

def create_study(db, name, market_definition, geography, segment, language, keywords_en=''):
    row=(str(uuid.uuid4()),_text(name,'name'),_text(market_definition,'market_definition'),
         normalize_geography(geography),_text(segment,'segment'),normalize_language(language),utc_now(),_keywords(keywords_en))
    try:
        db.execute(f'INSERT INTO studies ({",".join(FIELDS)}) VALUES (?,?,?,?,?,?,?,?)',row)
    except sqlite3.IntegrityError:
        raise EvidenceError('Ya existe un study con ese nombre')
    db.commit()
    return dict(zip(FIELDS,row))

def set_study_keywords(db, study_id, keywords_en):
    """The only mutable study field: search vocabulary, not the market definition."""
    get_study(db,study_id)
    db.execute('UPDATE studies SET keywords_en=? WHERE study_id=?',(_keywords(keywords_en),study_id))
    db.commit()
    return get_study(db,study_id)

def get_study(db, study_id):
    row=db.execute(f'SELECT {",".join(FIELDS)} FROM studies WHERE study_id=?',(study_id,)).fetchone()
    if not row: raise EvidenceError('Study inexistente')
    return dict(zip(FIELDS,row))

def list_studies(db):
    return [dict(zip(FIELDS,r)) for r in db.execute(f'SELECT {",".join(FIELDS)} FROM studies ORDER BY created_at')]
