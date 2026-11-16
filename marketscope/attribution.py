"""Attribution block: the citations each licence requires for the sources behind a conclusion
(including the input sources of derived metrics). Rules come from catalog/license_audit.csv."""
import csv
import json
from urllib.parse import urlsplit
from . import core
from .core import EvidenceError

NOTES = {
    'stackexchange':'CC BY-SA: añade el nombre del autor de cada post antes de publicar (el sistema no guarda nombres).',
    'github_issues':'Solo investigación: la publicación derivada debe ser de acceso abierto.',
    'hackernews':'No usar en informes hasta aclarar la autorización de la API (docs/license_audit.md).',
    'internet_archive':'Solo uso no comercial; cita breve de una página de terceros.',
}

def audit_rules():
    with (core.CATALOG/'license_audit.csv').open(encoding='utf-8-sig',newline='') as f:
        return {r['source_id']:r for r in csv.DictReader(f)}

def _sources_of(e):
    if e.get('method')=='derived_metric': return e.get('derived_from_sources') or []
    if e.get('source_id'): return [e['source_id']]
    return [f"web:{(urlsplit(e['source_url']).hostname or '').lower()}"]

def block(evidences):
    rules=audit_rules(); out={}
    for e in evidences:
        for sid in _sources_of(e):
            entry=out.setdefault(sid,{'source_id':sid,'urls':[],'derived_metrics':[]})
            if e.get('method')=='derived_metric':
                entry['derived_metrics'].append(e.get('source_independence_group','').removeprefix('derived:'))
                urls=e.get('derived_from_urls') or []
            else:
                urls=[e['source_url']]
            entry['urls']+=[u for u in urls if u not in entry['urls']]
    for sid,entry in out.items():
        rule=rules.get(sid)
        if rule:
            entry.update(licence=rule['licence'],license_review=rule['proposed_license_review'],required=rule['attribution'],
                         obligations=rule['system_obligations'])
        else:
            entry.update(licence='desconocida',license_review='pending',required='cita la URL exacta, el título y la fecha de consulta',
                         obligations='fuente sin auditoría de licencia: revisar antes de publicar')
        if sid in NOTES: entry['note']=NOTES[sid]
        entry['derived_metrics']=sorted(set(entry['derived_metrics']))
    return sorted(out.values(),key=lambda x:x['source_id'])

def attribution(db, conclusion_id):
    row=db.execute('SELECT cited_evidence_ids FROM conclusions WHERE conclusion_id=?',(conclusion_id,)).fetchone()
    if not row: raise EvidenceError('Conclusión inexistente')
    ids=json.loads(row[0])
    if not ids: return []
    payloads=db.execute(f'SELECT payload FROM evidence WHERE evidence_id IN ({",".join("?"*len(ids))})',ids).fetchall()
    return block([json.loads(p[0]) for p in payloads])
