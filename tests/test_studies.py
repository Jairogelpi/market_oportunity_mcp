import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from marketscope.core import (init_db, open_db, ingest, validate, start_run, run_report, EvidenceError,
                              DatabaseNotInitialized, SCHEMA_VERSION)
from marketscope.studies import create_study, get_study, list_studies

EXAMPLE=Path(__file__).resolve().parents[1]/'examples/evidence.json'
STUDY=dict(name='Facturación autónomos',market_definition='Software de facturación para autónomos',
           geography='es',segment='autónomos',language='ES')

# Schema exactly as shipped in v0.2 (no study_id, no studies table, user_version 0).
V02_SCHEMA=(
    '''CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, source_url TEXT NOT NULL,
      status TEXT NOT NULL, method TEXT NOT NULL, evidence_kind TEXT NOT NULL,
      source_access_status TEXT NOT NULL, imported_at TEXT NOT NULL, payload TEXT NOT NULL)''',
    '''CREATE TABLE research_runs (run_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, query_budget INTEGER NOT NULL,
      created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active')''',
    '''CREATE TABLE research_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
      event_type TEXT NOT NULL, query TEXT, source_url TEXT, reason TEXT, occurred_at TEXT NOT NULL)''',
    '''CREATE TABLE intake_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL,
      event_type TEXT NOT NULL, at TEXT NOT NULL, details TEXT NOT NULL)''',
)

class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=init_db(Path(self.tmp.name)/'t.sqlite')
        self.e=json.loads(EXAMPLE.read_text(encoding='utf-8'))[0]
    def tearDown(self): self.tmp.cleanup()

class TestStudies(DbCase):
    def test_create_normalizes_and_roundtrips(self):
        with open_db(self.path,write=True) as db:
            s=create_study(db,**STUDY)
            self.assertEqual((s['geography'],s['language']),('ES','es'))
            self.assertEqual(get_study(db,s['study_id']),s)
            self.assertEqual(list_studies(db),[s])
    def test_special_geographies_and_unknown(self):
        with open_db(self.path,write=True) as db:
            for i,g in enumerate(('EU','global','unknown')):
                s=create_study(db,**{**STUDY,'name':f'n{i}','geography':g,'language':'unknown','segment':'unknown'})
                self.assertEqual(s['geography'],{'EU':'EU','global':'GLOBAL','unknown':'unknown'}[g])
    def test_invalid_fields(self):
        bad=[{'geography':'España'},{'geography':'E1'},{'language':'spa'},{'name':'  '},
             {'market_definition':''},{'segment':None},{'name':'x'*121}]
        with open_db(self.path,write=True) as db:
            for b in bad:
                with self.subTest(b=b),self.assertRaises(EvidenceError): create_study(db,**{**STUDY,**b})
            self.assertEqual(list_studies(db),[])
    def test_keywords_optional_and_updatable(self):
        from marketscope.studies import set_study_keywords
        with open_db(self.path,write=True) as db:
            s=create_study(db,**STUDY)
            self.assertEqual(s['keywords_en'],'')
            u=set_study_keywords(db,s['study_id'],'  invoicing   software ')
            self.assertEqual(u['keywords_en'],'invoicing software')
            self.assertEqual({k:v for k,v in u.items() if k!='keywords_en'},{k:v for k,v in s.items() if k!='keywords_en'})
            self.assertEqual(set_study_keywords(db,s['study_id'],'')['keywords_en'],'')
            with self.assertRaises(EvidenceError): set_study_keywords(db,s['study_id'],'x'*201)
            with self.assertRaises(EvidenceError): set_study_keywords(db,'missing','x')
    def test_duplicate_name(self):
        with open_db(self.path,write=True) as db:
            create_study(db,**STUDY)
            with self.assertRaises(EvidenceError): create_study(db,**STUDY)
    def test_unknown_study(self):
        with open_db(self.path) as db, self.assertRaises(EvidenceError): get_study(db,'nope')

class TestStudyLinks(DbCase):
    def test_run_belongs_to_study(self):
        with open_db(self.path,write=True) as db:
            sid=create_study(db,**STUDY)['study_id']
            self.assertEqual(run_report(db,start_run(db,'Q01.01',2,sid))['study_id'],sid)
            self.assertIsNone(run_report(db,start_run(db,'Q01.01',2))['study_id'])
            with self.assertRaises(EvidenceError): start_run(db,'Q01.01',2,'missing')
    def test_evidence_requires_existing_study(self):
        with open_db(self.path,write=True) as db, self.assertRaises(EvidenceError):
            ingest(db,{**self.e,'study_id':'missing'})
    def test_same_claim_in_two_studies_is_not_duplicate(self):
        with open_db(self.path,write=True) as db:
            a=create_study(db,**STUDY)['study_id']; b=create_study(db,**{**STUDY,'name':'otro'})['study_id']
            ida,sa,_=ingest(db,{**self.e,'study_id':a}); idb,sb,_=ingest(db,{**self.e,'study_id':b})
            self.assertEqual((sa,sb),('created','created')); self.assertNotEqual(ida,idb)
            self.assertEqual(db.execute('SELECT study_id FROM evidence WHERE evidence_id=?',(ida,)).fetchone()[0],a)
    def test_legacy_evidence_id_unchanged(self):
        key='|'.join([self.e['question_id'],'https://example.org/reviews/sample',self.e['claim'].strip().lower()])
        self.assertEqual(validate(self.e)['evidence_id'],hashlib.sha256(key.encode()).hexdigest()[:24])
        self.assertEqual(validate({**self.e,'study_id':''})['evidence_id'],validate(self.e)['evidence_id'])

class TestMigration(unittest.TestCase):
    def _v02_db(self,path):
        db=sqlite3.connect(path)
        for s in V02_SCHEMA: db.execute(s)
        db.execute("INSERT INTO research_runs VALUES ('r1','Q01.01',3,'2026-01-01T00:00:00+00:00','active')")
        db.execute("INSERT INTO evidence VALUES ('e1','Q01.01','https://example.org/','candidate','manual','opinion','unknown','t','{}')")
        db.commit(); db.close()
    def test_v02_db_is_rejected_then_migrated_idempotently(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'old.sqlite'; self._v02_db(path)
            with self.assertRaises(DatabaseNotInitialized):
                with open_db(path): pass
            init_db(path); init_db(path)
            with open_db(path) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
                self.assertEqual(db.execute("SELECT study_id FROM research_runs WHERE run_id='r1'").fetchone(),(None,))
                self.assertEqual(db.execute("SELECT study_id FROM evidence WHERE evidence_id='e1'").fetchone(),(None,))
                self.assertEqual(run_report(db,'r1')['study_id'],None)
    def test_outdated_version_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path=init_db(Path(d)/'v.sqlite')
            db=sqlite3.connect(path); db.execute('PRAGMA user_version=2'); db.commit(); db.close()
            with self.assertRaises(DatabaseNotInitialized):
                with open_db(path): pass

if __name__=='__main__': unittest.main()
