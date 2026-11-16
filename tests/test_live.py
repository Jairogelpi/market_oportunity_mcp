"""Opt-in live smoke tests: one real request per connector. Run with MARKETSCOPE_LIVE_TESTS=1.
They never touch the project DB nor update catalog/sources.csv `live_test` (that stays a human review)."""
import os
import tempfile
import unittest
from pathlib import Path
from marketscope import executor, planner
from marketscope.core import init_db, open_db, start_run, run_report
from marketscope.studies import create_study

def _load_dotenv():
    """Same local secrets the MCP loads (start_mcp.py); existing env vars win."""
    path=Path(__file__).resolve().parents[1]/'.env'
    if not path.is_file(): return
    for line in path.read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if line and not line.startswith('#') and '=' in line:
            k,v=line.split('=',1); os.environ.setdefault(k.strip(),v.strip())

if os.environ.get('MARKETSCOPE_LIVE_TESTS')=='1': _load_dotenv()

@unittest.skipUnless(os.environ.get('MARKETSCOPE_LIVE_TESTS')=='1','tests en vivo desactivados (MARKETSCOPE_LIVE_TESTS=1)')
class TestLiveConnectors(unittest.TestCase):
    def run_step(self, question, step_id, query):
        with tempfile.TemporaryDirectory() as d:
            path=init_db(Path(d)/'live.sqlite')
            with open_db(path,write=True) as db:
                s=create_study(db,name='live',market_definition='CRM',geography='GLOBAL',segment='SMEs',language='en')
                run=start_run(db,question,3,s['study_id']); planner.plan_research(db,run)
                result=executor.execute_step(db,run,step_id,query)
                return result,executor.list_hits(db,run),run_report(db,run)['events']
    def test_openalex(self):
        result,hits,events=self.run_step('Q07.01','openalex','customer relationship management small business adoption')
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(events[0]['http_status'],200)
        self.assertGreater(len(hits),0)
        self.assertTrue(all(h['url'].startswith('https://openalex.org/W') for h in hits))

    def test_stackexchange(self):
        result,hits,events=self.run_step('Q01.01','stackexchange','crm duplicate contacts')
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(events[0]['http_status'],200)
        self.assertGreater(len(hits),0)
        self.assertTrue(all(h['url'].startswith('https://stackoverflow.com/') for h in hits))

    def test_github_issues(self):
        result,hits,events=self.run_step('Q01.01','github_issues','crm duplicate contacts')
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(events[0]['http_status'],200)
        self.assertGreater(len(hits),0)
        self.assertTrue(all(h['url'].startswith('https://github.com/') and '/issues/' in h['url'] for h in hits))

    def run_eu_step(self, step_id, query, options):
        with tempfile.TemporaryDirectory() as d:
            path=init_db(Path(d)/'live.sqlite')
            with open_db(path,write=True) as db:
                s=create_study(db,name='live-eu',market_definition='software',geography='ES',segment='pymes',language='es')
                run=start_run(db,'Q06.01',5,s['study_id']); planner.plan_research(db,run)
                return executor.execute_step(db,run,step_id,query,options=options),executor.list_hits(db,run)
    def test_eurostat(self):
        result,hits=self.run_eu_step('eurostat','isoc_eb_ai',{'filters':'indic_is=E_AI_TANY;size_emp=GE10;unit=PC_ENT;lastTimePeriod=1'})
        self.assertEqual((result['status'],result['hits']),('completed',1),result)
        self.assertIn('valor: ',hits[0]['snippet'])
    def test_ted(self):
        result,hits=self.run_eu_step('ted','software',None)
        self.assertEqual(result['status'],'completed',result)
        self.assertGreater(len(hits),0)
        self.assertTrue(all(h['url'].startswith('https://ted.europa.eu/') for h in hits))

    def run_global_step(self, step_id, query, options):
        with tempfile.TemporaryDirectory() as d:
            path=init_db(Path(d)/'live.sqlite')
            with open_db(path,write=True) as db:
                s=create_study(db,name='live-global',market_definition='software',geography='GLOBAL',segment='smb',language='en')
                run=start_run(db,'Q06.01',8,s['study_id']); planner.plan_research(db,run)
                return executor.execute_step(db,run,step_id,query,options=options),executor.list_hits(db,run)
    def test_sec_edgar(self):
        result,hits=self.run_global_step('sec_edgar','INTU',None)
        self.assertEqual(result['status'],'completed',result)
        self.assertGreater(len(hits),3)
        self.assertTrue(all(h['metadata']['form'] in ('10-K','10-K/A') for h in hits))
    def test_worldbank(self):
        # The API is erratic (0.5-55 s); a timeout here is a provider issue, reported as failed.
        result,hits=self.run_global_step('worldbank','IT.NET.USER.ZS',{'countries':'WLD;US;ES','mrv':'2'})
        self.assertEqual(result['status'],'completed',result)
        self.assertGreater(len(hits),0)

    def run_q(self, question, step_id, query, options=None, geography='GLOBAL'):
        with tempfile.TemporaryDirectory() as d:
            path=init_db(Path(d)/'live.sqlite')
            with open_db(path,write=True) as db:
                s=create_study(db,name='live-q',market_definition='invoicing software',geography=geography,segment='smb',language='en')
                run=start_run(db,question,8,s['study_id']); planner.plan_research(db,run)
                return executor.execute_step(db,run,step_id,query,options=options),executor.list_hits(db,run)
    def test_federal_register(self):
        result,hits=self.run_q('Q08.01','federal_register','"electronic invoicing"')
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(all(h['url'].startswith('https://www.federalregister.gov/documents/') for h in hits))
    def test_eurlex(self):
        # Title search over Cellar takes ~45-60 s.
        result,hits=self.run_q('Q08.01','eurlex','electronic invoicing',geography='EU')
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(all('CELEX:' in h['url'] for h in hits))
    @unittest.skipUnless(os.environ.get('MARKETSCOPE_WIKIMEDIA_CONTACT'),'sin MARKETSCOPE_WIKIMEDIA_CONTACT (consentimiento pendiente)')
    def test_wikimedia_pageviews(self):
        result,hits=self.run_q('Q10.01','wikimedia_pageviews','QuickBooks|Xero')
        self.assertEqual(result['status'],'completed',result)
        self.assertGreater(hits[0]['metadata']['total_views'],0)
    @unittest.skipUnless(os.environ.get('MARKETSCOPE_WIKIMEDIA_CONTACT'),'sin MARKETSCOPE_WIKIMEDIA_CONTACT (consentimiento pendiente)')
    def test_wikidata(self):
        result,hits=self.run_q('Q04.01','wikidata','QuickBooks')
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(any(h['title']=='QuickBooks' for h in hits))

    def test_internet_archive(self):
        result,hits=self.run_q('Q05.01','internet_archive','freshbooks.com/pricing',{'from':'2019','snapshots':'2'})
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(any(h['metadata']['price_count']>0 for h in hits))
    def test_oecd(self):
        result,hits=self.run_q('Q06.01','oecd','OECD.SDD.TPS,DSD_SDBSBSC_ISIC4@DF_SDBS_ISIC4,1.0/A.DEU.ENTR.J._T.ENT',{'start':'2021'})
        self.assertEqual(result['status'],'completed',result)
        self.assertIn('valor: ',hits[0]['snippet'])
    @unittest.skipUnless(os.environ.get('MARKETSCOPE_CENSUS_KEY'),'sin MARKETSCOPE_CENSUS_KEY (clave gratuita pendiente)')
    def test_us_census(self):
        result,hits=self.run_q('Q06.01','us_census','2022/cbp',{'get':'NAME,ESTAB','filters':'NAICS2017=5112'},geography='US')
        self.assertEqual(result['status'],'completed',result)
        self.assertIn('Number of establishments',hits[0]['snippet'])

    def test_hackernews(self):
        result,hits=self.run_q('Q01.01','hackernews','invoicing freelancers',{'tags':'comment'})
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(all(h['url'].startswith('https://news.ycombinator.com/item?id=') for h in hits))
    def test_mastodon(self):
        result,hits=self.run_q('Q01.01','mastodon','freelance')
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(all(h['url'].startswith('https://') for h in hits))
    @unittest.skipUnless(os.environ.get('MARKETSCOPE_BLUESKY_APP_PASSWORD'),'sin credenciales de Bluesky (contraseña de aplicación pendiente)')
    def test_bluesky(self):
        result,hits=self.run_q('Q01.01','bluesky','invoicing software')
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(all(h['url'].startswith('https://bsky.app/profile/did:') for h in hits))

    def test_trend_mode(self):
        for question,step,query,opts in (('Q07.01','hackernews','invoicing',{'mode':'trend','periods':'3'}),
                                         ('Q01.01','stackexchange','invoice',{'mode':'trend','periods':'2'}),
                                         ('Q07.01','openalex','electronic invoicing',{'mode':'trend','periods':'3'})):
            with self.subTest(step=step):
                result,hits=self.run_q(question,step,query,opts)
                self.assertEqual(result['status'],'completed',result)
                self.assertEqual(hits[0]['metadata']['mode'],'trend')
                self.assertTrue(all(p['total'] for p in hits[0]['metadata']['series']))
    def test_gdelt(self):
        result,hits=self.run_q('Q07.01','gdelt','"electronic invoicing"',{'granularity':'month','periods':'3'})
        if result['status']=='failed' and '429' in result.get('reason',''):
            self.skipTest('GDELT limita esta conexión (HTTP 429); no es un fallo del conector')
        self.assertIn(result['status'],('completed','empty'),result)

if __name__=='__main__': unittest.main()
