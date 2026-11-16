"""Bronze (raw, immutable, licence-aware) and Silver (normalized, deterministic) layers."""
import gzip
import hashlib
import json
import os
import unittest
import httpx
from marketscope import bronze, silver, executor
from marketscope.core import open_db, EvidenceError
from test_executor import (GlobalCase, JSONSTAT, SDMX, FLOW, WB, CENSUS, SEC_TICKERS, SEC_CONCEPT, QUESTIONS, ok_json,
                           TrendCase, PAGEVIEWS)

class TestNormalization(unittest.TestCase):
    def test_geo(self):
        cases={'EL':'GR','UK':'GB','ESP':'ES','USA':'US','EU27_2020':'EU27','EUU':'EU27','WLD':'WORLD','fr':'FR','XYZ':'XYZ'}
        for raw,want in cases.items():
            with self.subTest(raw=raw): self.assertEqual(silver.normalize_geo(raw),want)
        self.assertIsNone(silver.normalize_geo(''))
    def test_periods(self):
        self.assertEqual(silver.period_bounds('2024'),('2024-01-01','2025-01-01'))
        self.assertEqual(silver.period_bounds('2024-Q4'),('2024-10-01','2025-01-01'))
        self.assertEqual(silver.period_bounds('2024Q2'),('2024-04-01','2024-07-01'))
        self.assertEqual(silver.period_bounds('2024-12'),('2024-12-01','2025-01-01'))
        self.assertEqual(silver.period_bounds('2024M02'),('2024-02-01','2024-03-01'))
        self.assertEqual(silver.period_bounds('2024-02-29'),('2024-02-29','2024-03-01'))
        self.assertEqual(silver.period_bounds('CY2024'),(None,None))

class LayerCase(GlobalCase):
    def rows(self, table, where=''):
        with open_db(self.path) as db:
            cur=db.execute(f'SELECT * FROM {table} {where}')
            cols=[c[0] for c in cur.description]
            return [dict(zip(cols,r)) for r in cur.fetchall()]

class TestBronze(LayerCase):
    def test_approved_source_stores_raw_body(self):
        self.run_src('Q06.01','eurostat',httpx.Response(200,json=JSONSTAT),query='demo_ds',geography='ES')
        (b,)=self.rows('bronze_objects')
        self.assertEqual((b['source_id'],b['stored'],b['reason']),('eurostat',1,None))
        with open_db(self.path) as db:
            body=bronze.read(db,b['sha256'])
            self.assertEqual(json.loads(body),JSONSTAT)
            self.assertTrue((bronze.bronze_root(db)/b['path']).name.endswith('.json.gz'))
        self.assertEqual(b['sha256'],hashlib.sha256(body).hexdigest())
    def test_restricted_source_keeps_no_body(self):
        self.run_src('Q01.01','stackexchange',httpx.Response(200,json=QUESTIONS),query='invoice')
        (b,)=self.rows('bronze_objects')
        self.assertEqual((b['stored'],b['path']),(0,None)); self.assertIn('restricted',b['reason'])
        with open_db(self.path) as db, self.assertRaisesRegex(EvidenceError,'no almacenado'):
            bronze.read(db,b['sha256'])
    def test_tampering_detected_and_content_addressed(self):
        self.run_src('Q06.01','eurostat',httpx.Response(200,json=JSONSTAT),query='demo_ds',geography='ES')
        self.run_src('Q06.01','eurostat',httpx.Response(200,json=JSONSTAT),query='demo_ds',geography='ES')
        rows=self.rows('bronze_objects'); self.assertEqual(len(rows),1)  # same content stored once
        with open_db(self.path) as db:
            full=bronze.bronze_root(db)/rows[0]['path']
            with gzip.open(full,'wb') as f: f.write(b'{"tampered":true}')
            with self.assertRaisesRegex(EvidenceError,'Integridad'): bronze.read(db,rows[0]['sha256'])
    def test_failed_responses_not_stored(self):
        self.run_src('Q06.01','eurostat',httpx.Response(500,json={}),query='demo_ds',geography='ES')
        self.assertEqual(self.rows('bronze_objects'),[])

class TestSilverObservations(LayerCase):
    def obs(self,**kw):
        where=' AND '.join(f"{k}='{v}'" for k,v in kw.items())
        return self.rows('silver_observations','WHERE '+where if where else '')
    def test_eurostat(self):
        r,*_=self.run_src('Q06.01','eurostat',httpx.Response(200,json=JSONSTAT),query='demo_ds',geography='ES')
        self.assertEqual(r['silver'],{'observations':3,'documents':0})
        o=[x for x in self.obs(source_id='eurostat') if x['period_label']=='2025' and x['measure']=='size_emp=GE10'][0]
        self.assertEqual((o['geo'],o['value_raw'],o['period_start'],o['period_end'],o['dataset']),('ES',20.0,'2025-01-01','2026-01-01','demo_ds'))
        self.assertEqual(json.loads(o['dims_json'])['size_emp'],'GE10')
    def test_oecd_unit_multiplier_applied(self):
        self.run_src('Q06.01','oecd',httpx.Response(200,json=SDMX),query=FLOW+'/A.DEU.ENTR.J..ENT')
        o=self.obs(source_id='oecd',measure='SIZE_CLASS=S10T249')[0]
        self.assertEqual((o['geo'],o['unit_mult'],o['value_raw'],o['value_scaled']),('DE',6,3.5,3.5e6))
    def test_worldbank_iso3_to_iso2(self):
        self.run_src('Q06.01','worldbank',httpx.Response(200,json=WB),query='IT.NET.USER.ZS')
        self.assertEqual(sorted(o['geo'] for o in self.obs(source_id='worldbank')),['ES','WORLD'])
    def test_census_one_row_per_labelled_variable(self):
        os.environ['MARKETSCOPE_CENSUS_KEY']='k'
        try:
            self.run_src('Q06.01','us_census',httpx.Response(200,json=CENSUS),
                         httpx.Response(200,json={'name':'ESTAB','label':'Number of establishments'}),
                         query='2022/cbp',options={'get':'NAME,ESTAB','filters':'NAICS2017=5112'},geography='US')
        finally:
            os.environ.pop('MARKETSCOPE_CENSUS_KEY')
        (o,)=self.obs(source_id='us_census')
        self.assertEqual((o['measure'],o['value_raw'],o['geo'],o['period_label'],o['unit']),('ESTAB',42000.0,'US','2022','Number of establishments'))
        self.assertEqual(json.loads(o['dims_json'])['NAICS2017'],'5112')
    def test_sec_period_end_exclusive_and_entity(self):
        os.environ['MARKETSCOPE_CONTACT']='a@b.org'
        try:
            self.run_src('Q06.01','sec_edgar',httpx.Response(200,json=SEC_TICKERS),httpx.Response(200,json=SEC_CONCEPT),query='ACME')
        finally:
            os.environ.pop('MARKETSCOPE_CONTACT')
        o=[x for x in self.obs(source_id='sec_edgar') if x['period_label']=='CY2024'][0]
        self.assertEqual((o['entity'],o['period_start'],o['period_end'],o['value_raw'],o['unit']),
                         ('CIK0000000042','2024-01-01','2025-01-01',1500.0,'USD'))
    def test_documents_for_text_sources(self):
        r,*_=self.run_src('Q01.01','stackexchange',httpx.Response(200,json=QUESTIONS),query='invoice')
        self.assertEqual(r['silver'],{'observations':0,'documents':2})
        d=[x for x in self.rows('silver_documents') if x['url'].endswith('/11/crm-sync')][0]
        self.assertEqual((d['kind'],d['author_group'],d['deleted_at']),('question','stackexchange:stackoverflow:user:77',None))
    def test_build_all_is_idempotent(self):
        self.run_src('Q06.01','eurostat',httpx.Response(200,json=JSONSTAT),query='demo_ds',geography='ES')
        with open_db(self.path,write=True) as db:
            self.assertEqual(silver.build_all(db),{'hits':3,'observations':0,'documents':0})
        self.assertEqual(len(self.obs(source_id='eurostat')),3)

class TestSilverTrends(TrendCase):
    def test_trend_series_become_count_total_and_share(self):
        self.run_src('Q07.01','hackernews',self.counting([{'nbHits':n} for n in (10,1000)]),
                     query='invoicing',options={'mode':'trend','periods':'1'})
        with open_db(self.path) as db:
            rows={r[0]:r[1] for r in db.execute("SELECT measure,value_raw FROM silver_observations WHERE source_id='hackernews'")}
        self.assertEqual(rows,{'mentions':10.0,'platform_total':1000.0,'share':0.01})
    def test_wikimedia_monthly_points(self):
        os.environ['MARKETSCOPE_WIKIMEDIA_CONTACT']='User:X'
        try:
            self.run_src('Q10.01','wikimedia_pageviews',httpx.Response(200,json=PAGEVIEWS),query='QuickBooks',
                         options={'start':'20250101','end':'20250228'})
        finally:
            os.environ.pop('MARKETSCOPE_WIKIMEDIA_CONTACT')
        with open_db(self.path) as db:
            rows=db.execute("SELECT period_label,value_raw,entity,period_start FROM silver_observations WHERE source_id='wikimedia_pageviews' ORDER BY period_start").fetchall()
        self.assertEqual(rows,[('2025-01',100.0,'QuickBooks','2025-01-01'),('2025-02',150.0,'QuickBooks','2025-02-01')])

from test_executor import TOOTS, BSKY_POSTS
from marketscope import maintenance, evaluator

class TestDeletionRecheck(LayerCase):
    def evidence_from(self, step_id, *responses, query):
        r,hits,_,_=self.run_src('Q01.01',step_id,*responses,query=query)
        with open_db(self.path,write=True) as db:
            return [executor.submit_evidence_from_hit(db,h['hit_id'],f'claim {i}',h['snippet'][:20])['evidence_id']
                    for i,h in enumerate(hits)]
    def status(self, eid):
        return self.rows('evidence',f"WHERE evidence_id='{eid}'")[0]['status']
    def test_mastodon_deleted_post_is_withdrawn(self):
        (eid,)=self.evidence_from('mastodon',httpx.Response(200,json=TOOTS),query='freelance')
        http,seen=self.http(httpx.Response(404,json={'error':'Record not found'}))
        with open_db(self.path,write=True) as db:
            s=maintenance.recheck_deleted(db,http)
        self.assertEqual((s['checked'],s['deleted']),(1,1))
        self.assertEqual(str(seen[0].url),'https://mastodon.social/api/v1/statuses/1')
        self.assertEqual(self.status(eid),'rejected')
        ev=self.rows('intake_events',"WHERE event_type='deleted_at_source'"); self.assertEqual(len(ev),1)
        self.assertIsNotNone(self.rows('silver_documents')[0]['deleted_at'])
        payload=json.loads(self.rows('evidence',f"WHERE evidence_id='{eid}'")[0]['payload'])
        self.assertEqual(payload['rejection_reason'],'deleted_at_source')
    def test_live_and_unknown_posts_are_kept(self):
        (eid,)=self.evidence_from('mastodon',httpx.Response(200,json=TOOTS),query='freelance')
        for resp in (httpx.Response(200,json={'id':'1'}),httpx.Response(503,json={})):
            with self.subTest(status=resp.status_code):
                http,_=self.http(resp)
                with open_db(self.path,write=True) as db: s=maintenance.recheck_deleted(db,http)
                self.assertEqual(s['deleted'],0); self.assertEqual(self.status(eid),'candidate')
        self.assertEqual(s['unknown'],1)
    def test_bluesky_missing_post_withdrawn_and_needs_session(self):
        os.environ.update(MARKETSCOPE_BLUESKY_HANDLE='me.bsky.social',MARKETSCOPE_BLUESKY_APP_PASSWORD='pw')
        try:
            (eid,)=self.evidence_from('bluesky',httpx.Response(200,json={'accessJwt':'jwt'}),httpx.Response(200,json=BSKY_POSTS),query='x')
            http,seen=self.http(httpx.Response(200,json={'accessJwt':'jwt2'}),httpx.Response(200,json={'posts':[]}))
            with open_db(self.path,write=True) as db: s=maintenance.recheck_deleted(db,http)
            self.assertEqual(s['deleted'],1); self.assertEqual(self.status(eid),'rejected')
            self.assertEqual(seen[1].url.params['uris'],'at://did:plc:abc123/app.bsky.feed.post/3kxyz')
            self.assertEqual(seen[1].headers['authorization'],'Bearer jwt2')
        finally:
            for k in ('MARKETSCOPE_BLUESKY_HANDLE','MARKETSCOPE_BLUESKY_APP_PASSWORD'): os.environ.pop(k,None)
    def test_bluesky_without_credentials_is_skipped(self):
        os.environ.update(MARKETSCOPE_BLUESKY_HANDLE='me.bsky.social',MARKETSCOPE_BLUESKY_APP_PASSWORD='pw')
        try:
            self.evidence_from('bluesky',httpx.Response(200,json={'accessJwt':'jwt'}),httpx.Response(200,json=BSKY_POSTS),query='x')
        finally:
            for k in ('MARKETSCOPE_BLUESKY_HANDLE','MARKETSCOPE_BLUESKY_APP_PASSWORD'): os.environ.pop(k,None)
        http,seen=self.http(httpx.Response(200,json={}))
        with open_db(self.path,write=True) as db: s=maintenance.recheck_deleted(db,http)
        self.assertEqual((seen,s['deleted']),([],0)); self.assertIn('sin credenciales',s['skipped'][0])
    def test_withdrawal_makes_conclusion_stale(self):
        (eid,)=self.evidence_from('mastodon',httpx.Response(200,json=TOOTS),query='freelance')
        with open_db(self.path,write=True) as db:
            study=db.execute('SELECT study_id FROM research_runs').fetchone()[0]
            evaluator.submit_conclusion(db,study,'Q01.01',{'verdict':'INSUFFICIENT_EVIDENCE',
                'text':'Una sola publicación autoseleccionada; faltan entrevistas.','cited_evidence_ids':[eid],
                'uncertainty':{'coverage_limits':'x','biases':'y','missing_evidence':['entrevistas'],'contradictions':'-'}})
            maintenance.recheck_deleted(db,self.http(httpx.Response(404,json={}))[0])
            self.assertTrue(evaluator.list_conclusions(db,study)[0]['stale'])

if __name__=='__main__': unittest.main()
