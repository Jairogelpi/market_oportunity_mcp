import argparse
import json
import sys
from pathlib import Path
from .core import open_db, init_db, resolve_db_path, ingest, export, summary, check_catalog, EvidenceError
from . import studies, planner, evaluator, silver, gold, maintenance

def main():
    p=argparse.ArgumentParser(description='MarketScope evidence intake')
    p.add_argument('command',choices=['init','check-catalog','ingest','export','summary','create-study','list-studies','plan',
                                      'evaluate','conclusions','build-silver','recheck-deleted','metrics'])
    p.add_argument('--run',help='plan: run_id')
    p.add_argument('--study',help='evaluate / conclusions: study_id')
    p.add_argument('--question',help='evaluate: question_id')
    p.add_argument('--file',help='JSON array or JSONL for ingest; output JSON for export')
    p.add_argument('--db',default=None,help='Default: MARKETSCOPE_DB or <proyecto>/data/marketscope.sqlite')
    for f in ('name','market','geography','segment','language','keywords-en'):
        p.add_argument('--'+f,help='create-study')
    a=p.parse_args()
    dbpath=resolve_db_path(a.db)
    if a.command=='check-catalog':
        errors=check_catalog()
        print(json.dumps({'ok':not errors,'errors':errors},ensure_ascii=False,indent=2))
        sys.exit(1 if errors else 0)
    if a.command=='init':
        print(json.dumps({'db':str(init_db(dbpath))}))
        return
    try:
        if a.command=='ingest':
            if not a.file: p.error('--file obligatorio para ingest')
            raw=Path(a.file).read_text(encoding='utf-8-sig')
            records=json.loads(raw) if raw.lstrip().startswith('[') else [json.loads(x) for x in raw.splitlines() if x.strip()]
            counts={'created':0,'duplicate':0,'rejected':0}; errors=[]
            init_db(dbpath)
            with open_db(dbpath,write=True) as db:
                for idx,e in enumerate(records,1):
                    try:
                        _,state,_=ingest(db,e);counts[state]+=1
                    except (EvidenceError,KeyError,TypeError) as ex:
                        counts['rejected']+=1;errors.append({'record':idx,'error':str(ex)})
            print(json.dumps({'counts':counts,'errors':errors},ensure_ascii=False,indent=2))
        elif a.command=='create-study':
            with open_db(dbpath,write=True) as db:
                s=studies.create_study(db,a.name,a.market,a.geography,a.segment,a.language,a.keywords_en)
            print(json.dumps(s,ensure_ascii=False,indent=2))
        elif a.command=='plan':
            if not a.run: p.error('--run obligatorio para plan')
            with open_db(dbpath,write=True) as db: print(json.dumps(planner.plan_research(db,a.run),ensure_ascii=False,indent=2))
        elif a.command=='evaluate':
            if not (a.study and a.question): p.error('--study y --question obligatorios para evaluate')
            with open_db(dbpath) as db: print(json.dumps(evaluator.evaluate_question(db,a.study,a.question),ensure_ascii=False,indent=2))
        elif a.command=='conclusions':
            if not a.study: p.error('--study obligatorio para conclusions')
            with open_db(dbpath) as db: print(json.dumps(evaluator.list_conclusions(db,a.study),ensure_ascii=False,indent=2))
        elif a.command=='build-silver':
            with open_db(dbpath,write=True) as db: print(json.dumps(silver.build_all(db),indent=2))
        elif a.command=='recheck-deleted':
            with open_db(dbpath,write=True) as db: print(json.dumps(maintenance.recheck_deleted(db),ensure_ascii=False,indent=2))
        elif a.command=='metrics':
            print(json.dumps(list(gold.load_metrics().values()),ensure_ascii=False,indent=2))
        elif a.command=='list-studies':
            with open_db(dbpath) as db: print(json.dumps(studies.list_studies(db),ensure_ascii=False,indent=2))
        elif a.command=='export':
            with open_db(dbpath) as db: result=json.dumps(export(db),ensure_ascii=False,indent=2)
            if a.file: Path(a.file).write_text(result,encoding='utf-8')
            else: print(result)
        else:
            with open_db(dbpath) as db: print(json.dumps(summary(db),indent=2))
    except EvidenceError as ex:
        sys.exit(str(ex))

if __name__=='__main__': main()
