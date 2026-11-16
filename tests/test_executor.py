"""Executor + hardened HTTP + OpenAlex connector, offline (httpx.MockTransport, fake DNS)."""
import itertools
import json
from datetime import datetime, timezone
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import httpx
from marketscope import executor, planner, http_client
from marketscope.connectors import openalex
from marketscope.core import init_db, open_db, start_run, log_research_event, run_report, export, EvidenceError
from marketscope.http_client import SafeHttp, Policy, ConnectorFailure
from marketscope.studies import create_study

_names=itertools.count()  # unique study names when a test runs several steps
PUBLIC_DNS=lambda host,port,proto=0: [(2,1,6,'',('104.20.1.1',port))]
PRIVATE_DNS=lambda host,port,proto=0: [(2,1,6,'',('10.0.0.5',port))]

# Synthetic OpenAlex-shaped payload (not real data).
WORKS={'meta':{'count':2},'results':[
    {'id':'https://openalex.org/W1','doi':'https://doi.org/10.1/x','display_name':'Adopción de CRM en pymes',
     'publication_date':'2024-05-01','publication_year':2024,'type':'article','language':'es','cited_by_count':3,
     'primary_location':{'source':{'id':'https://openalex.org/S9','display_name':'Revista X'}},
     'authorships':[{'author':{'id':'https://openalex.org/A7','display_name':'Autora Uno'}}],
     'abstract_inverted_index':{'Las':[0],'pymes':[1],'abandonan':[2],'el':[3],'CRM':[4]}},
    {'id':'https://openalex.org/W2','display_name':'Sin autores','publication_date':None,'authorships':[],
     'primary_location':None,'abstract_inverted_index':None},
    {'id':'not-openalex','display_name':'Ignorado'},
]}

def responder(*responses):
    """MockTransport returning the given responses in order; records requests."""
    seen=[]; queue=list(responses)
    def handle(request):
        seen.append(request)
        r=queue.pop(0) if len(queue)>1 else queue[0]
        return r(request) if callable(r) else r
    return httpx.MockTransport(handle),seen

def ok_json(payload=WORKS):
    return httpx.Response(200,json=payload)

class ExecCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=init_db(Path(self.tmp.name)/'e.sqlite')
        self.sleeps=[]
        self.env=mock.patch.dict(os.environ,{},clear=False); self.env.start()
        os.environ.pop('MARKETSCOPE_OPENALEX_API_KEY',None)
    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()
    def http(self,*responses,resolver=PUBLIC_DNS):
        transport,seen=responder(*responses)
        return SafeHttp(transport=transport,resolver=resolver,sleep=self.sleeps.append),seen
    def run_with_plan(self,db,question='Q07.01',budget=30):
        s=create_study(db,name=f's{budget}{question}{next(_names)}',market_definition='CRM',geography='ES',segment='pymes',
                       language='es',keywords_en='CRM pymes')
        run=start_run(db,question,budget,s['study_id']); planner.plan_research(db,run)
        return run
    def events(self,db,run):
        return [e['event_type'] for e in run_report(db,run)['events']]

class TestExecuteStep(ExecCase):
    def test_success_stores_hits_and_audits(self):
        http,seen=self.http(ok_json())
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db)
            r=executor.execute_step(db,run,'openalex',http=http)
            self.assertEqual((r['status'],r['hits'],r['new_hits'],r['query'],r['remaining_queries']),('completed',2,2,'CRM pymes',29))
            self.assertEqual(self.events(db,run),['connector_call','step_completed'])
            call=run_report(db,run)['events'][0]
            self.assertEqual(call['http_status'],200); self.assertEqual(len(call['content_sha256']),64)
            self.assertTrue(call['source_url'].startswith('https://api.openalex.org/works?'))
            hits=executor.list_hits(db,run)
            self.assertEqual(hits[0]['snippet'],'Adopción de CRM en pymes. Las pymes abandonan el CRM')
            self.assertEqual(hits[0]['published_at'],'2024-05-01T00:00:00+00:00')
            self.assertEqual(hits[0]['metadata']['independence_group'],'openalex:author:A7')
            self.assertEqual((hits[1]['published_at'],hits[1]['metadata']['independence_group']),('unknown','openalex:work:W2'))
            with self.assertRaises(EvidenceError): executor.execute_step(db,run,'openalex',http=http)
            self.assertNotEqual(planner.next_step(db,run)['step']['step_id'],'openalex')  # closed step skipped
        req=seen[0]
        self.assertEqual((req.url.host,req.url.params['search']),('api.openalex.org','CRM pymes'))
        self.assertIn('MarketScopeResearch',req.headers['user-agent'])
        self.assertNotIn('authorization',req.headers)
    def test_query_override(self):
        http,seen=self.http(ok_json())
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db)
            self.assertEqual(executor.execute_step(db,run,'openalex',query='  CRM   adoption SMEs ',http=http)['query'],'CRM adoption SMEs')
        self.assertEqual(seen[0].url.params['search'],'CRM adoption SMEs')
    def test_api_key_goes_in_header_and_is_never_stored(self):
        os.environ['MARKETSCOPE_OPENALEX_API_KEY']='secret-key-123'
        http,seen=self.http(ok_json())
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db); executor.execute_step(db,run,'openalex',http=http)
            dump='\n'.join(str(r) for t in ('research_events','source_hits','research_plans') for r in db.execute(f'SELECT * FROM {t}'))
        self.assertEqual(seen[0].headers['authorization'],'Bearer secret-key-123')
        self.assertNotIn('secret-key-123',str(seen[0].url)); self.assertNotIn('secret-key-123',dump)
    def test_agent_route_rejected(self):
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db)
            with self.assertRaises(EvidenceError): executor.execute_step(db,run,'arxiv',http=self.http(ok_json())[0])
    def test_redirect_fails_then_explicit_retry(self):
        http,seen=self.http(httpx.Response(302,headers={'location':'https://elsewhere.example/'}),ok_json())
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db)
            r=executor.execute_step(db,run,'openalex',http=http)
            self.assertEqual(r['status'],'failed'); self.assertIn('Redirección',r['reason'])
            self.assertEqual(len(seen),1)  # redirect not followed
            with self.assertRaises(EvidenceError): executor.execute_step(db,run,'openalex',http=http)
            self.assertEqual(executor.execute_step(db,run,'openalex',retry=True,http=http)['status'],'completed')
            self.assertEqual(self.events(db,run),['connector_call','connector_error','connector_call','step_completed'])
    def test_403_blocks_permanently(self):
        http,_=self.http(httpx.Response(403,text='forbidden'))
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db)
            self.assertEqual(executor.execute_step(db,run,'openalex',http=http)['status'],'blocked')
            for retry in (False,True):
                with self.assertRaises(EvidenceError): executor.execute_step(db,run,'openalex',retry=retry,http=http)
    def test_429_is_retryable_after_wait(self):
        http,seen=self.http(httpx.Response(429,headers={'retry-after':'120'}),ok_json())
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db)
            r=executor.execute_step(db,run,'openalex',http=http)
            self.assertEqual(r['status'],'failed'); self.assertIn('120',r['reason'])
            self.assertGreater(http._not_before['openalex'],http.clock()+100)
            # Retry before Retry-After elapses: fails fast, no request, no budget spent.
            self.assertEqual(executor.execute_step(db,run,'openalex',retry=True,http=http)['status'],'failed')
            self.assertEqual(len(seen),1)
            http._not_before.clear()
            self.assertEqual(executor.execute_step(db,run,'openalex',retry=True,http=http)['status'],'completed')
    def test_captcha_html_blocks_and_plain_html_fails(self):
        for body,expected in (('<html>Please verify you are human (captcha)</html>','blocked'),('<html>hola</html>','failed')):
            with self.subTest(expected=expected):
                http,_=self.http(httpx.Response(200,text=body,headers={'content-type':'text/html'}))
                with open_db(self.path,write=True) as db:
                    self.assertEqual(executor.execute_step(db,self.run_with_plan(db,budget=3 if expected=='blocked' else 4),'openalex',http=http)['status'],expected)
    def test_oversized_response_discarded(self):
        http,_=self.http(httpx.Response(200,content=b'{"results":[' + b' '*200 + b']}',headers={'content-type':'application/json'}))
        with mock.patch.object(http_client,'MAX_BYTES',100), open_db(self.path,write=True) as db:
            r=executor.execute_step(db,self.run_with_plan(db),'openalex',http=http)
        self.assertEqual(r['status'],'failed'); self.assertIn('supera',r['reason'])
    def test_malformed_payload_fails(self):
        http,_=self.http(ok_json({'unexpected':True}))
        with open_db(self.path,write=True) as db:
            self.assertEqual(executor.execute_step(db,self.run_with_plan(db),'openalex',http=http)['status'],'failed')
    def test_network_error_is_audited(self):
        def boom(request): raise httpx.ConnectError('down',request=request)
        http,_=self.http(boom)
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db)
            self.assertEqual(executor.execute_step(db,run,'openalex',http=http)['status'],'failed')
            self.assertIsNone(run_report(db,run)['events'][0]['http_status'])
    def test_run_budget_exhausted_makes_no_request(self):
        http,seen=self.http(ok_json())
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db,budget=2)  # hackernews and openalex get one query each
            log_research_event(db,run,'search',query='otra búsqueda')
            log_research_event(db,run,'search',query='y otra')
            self.assertEqual(executor.execute_step(db,run,'openalex',http=http)['status'],'budget_exhausted')
            self.assertEqual(self.events(db,run),['search','search','budget_exhausted'])
        self.assertEqual(seen,[])
    def test_daily_quota_blocks(self):
        http,seen=self.http(ok_json())
        with mock.patch.object(openalex,'KEYLESS_DAILY_QUOTA',0), open_db(self.path,write=True) as db:
            r=executor.execute_step(db,self.run_with_plan(db),'openalex',http=http)
        self.assertEqual((r['status'],seen),('blocked',[])); self.assertIn('Cuota diaria',r['reason'])

class TestSafeHttp(ExecCase):
    POLICY=Policy('openalex',frozenset({'api.openalex.org'}),min_interval=0.2)
    def test_host_scheme_port_and_private_ip_rejected_before_request(self):
        cases=[('https://evil.example.com/x',PUBLIC_DNS),('http://api.openalex.org/works',PUBLIC_DNS),
               ('https://api.openalex.org:8443/works',PUBLIC_DNS),('https://api.openalex.org/works',PRIVATE_DNS)]
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db)
            for url,resolver in cases:
                with self.subTest(url=url):
                    http,seen=self.http(ok_json(),resolver=resolver)
                    with self.assertRaises(ConnectorFailure): http.get_json(db,run,'openalex',self.POLICY,url)
                    self.assertEqual(seen,[])
            self.assertEqual(self.events(db,run),[])  # nothing sent, nothing spent
    def test_rate_limit_waits_between_requests(self):
        http,_=self.http(ok_json())
        http.clock=lambda: 1000.0
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db)
            http.get_json(db,run,'openalex',self.POLICY,'https://api.openalex.org/works')
            http.get_json(db,run,'openalex',self.POLICY,'https://api.openalex.org/works')
        self.assertEqual(len(self.sleeps),1); self.assertAlmostEqual(self.sleeps[0],0.2)
    def test_long_backoff_fails_fast(self):
        http,seen=self.http(ok_json()); http.defer('openalex',600)
        with open_db(self.path,write=True) as db, self.assertRaises(ConnectorFailure):
            http.get_json(db,self.run_with_plan(db),'openalex',self.POLICY,'https://api.openalex.org/works')
        self.assertEqual(seen,[])
    def test_redacted_params_not_logged(self):
        policy=Policy('openalex',frozenset({'api.openalex.org'}),0,redact_params=frozenset({'api_key'}))
        http,seen=self.http(ok_json())
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db)
            http.get_json(db,run,'openalex',policy,'https://api.openalex.org/works',{'api_key':'zzz','search':'a'})
            logged=run_report(db,run)['events'][0]['source_url']
        self.assertEqual(seen[0].url.params['api_key'],'zzz'); self.assertNotIn('zzz',logged); self.assertIn('search=a',logged)

class TestEvidenceFromHit(ExecCase):
    def setUp(self):
        super().setUp()
        self.db_ctx=open_db(self.path,write=True); self.db=self.db_ctx.__enter__()
        self.run=self.run_with_plan(self.db)
        executor.execute_step(self.db,self.run,'openalex',http=self.http(ok_json())[0])
        self.hit=executor.list_hits(self.db,self.run)[0]
    def tearDown(self):
        self.db_ctx.__exit__(None,None,None); super().tearDown()
    def test_promotes_with_automatic_provenance(self):
        r=executor.submit_evidence_from_hit(self.db,self.hit['hit_id'],'Un estudio describe abandono de CRM en pymes',
                                            'las  PYMES abandonan el CRM',{'limitations':'un solo estudio'})
        self.assertEqual(r['result'],'created')
        e=export(self.db)[0]
        self.assertEqual((e['method'],e['evidence_kind'],e['source_access_status'],e['status']),
                         ('official_api','secondary','needs_review','candidate'))
        self.assertEqual((e['source_url'],e['source_independence_group'],e['hit_id']),
                         ('https://openalex.org/W1','openalex:author:A7',self.hit['hit_id']))
        self.assertEqual(e['study_id'],run_report(self.db,self.run)['study_id'])
        self.assertEqual(e['research_query'],'CRM pymes'); self.assertEqual(e['limitations'],'un solo estudio')
        again=executor.submit_evidence_from_hit(self.db,self.hit['hit_id'],'Un estudio describe abandono de CRM en pymes','abandonan el CRM')
        self.assertEqual(again['result'],'duplicate')
    def test_non_literal_excerpt_rejected(self):
        for excerpt in ('el 80% de las pymes abandonan el CRM','',None):
            with self.subTest(excerpt=excerpt), self.assertRaises(EvidenceError):
                executor.submit_evidence_from_hit(self.db,self.hit['hit_id'],'claim',excerpt)
    def test_provenance_cannot_be_overridden(self):
        for extra in ({'source_url':'https://otra.org/'},{'status':'verified'},{'source_access_status':'permitted'}):
            with self.subTest(extra=extra), self.assertRaises(EvidenceError):
                executor.submit_evidence_from_hit(self.db,self.hit['hit_id'],'claim','pymes',extra)
        self.assertEqual(export(self.db),[])
    def test_unknown_hit(self):
        with self.assertRaises(EvidenceError): executor.submit_evidence_from_hit(self.db,'nope','c','e')

# Synthetic Stack Exchange-shaped payload (not real data).
QUESTIONS={'items':[
    {'question_id':11,'link':'https://stackoverflow.com/questions/11/crm-sync','title':'CRM sync &amp; duplicates',
     'body':'<p>Our <b>CRM</b> duplicates contacts &lt;daily&gt;.</p>','creation_date':1700000000,'score':4,
     'answer_count':1,'is_answered':True,'view_count':90,'tags':['crm'],
     'owner':{'user_id':77,'display_name':'Persona Real'}},
    {'question_id':12,'link':'https://stackoverflow.com/questions/12/x','title':'Sin owner','owner':{'user_type':'does_not_exist'}},
    {'question_id':13,'link':'http://insecure.example/q','title':'descartado'},
],'has_more':True,'quota_max':300,'quota_remaining':299}

class TestStackExchange(ExecCase):
    def run_se(self,payload=QUESTIONS,options=None,status=200,budget=10):
        http,seen=self.http(httpx.Response(status,json=payload))
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db,'Q01.01',budget)
            r=executor.execute_step(db,run,'stackexchange',http=http,options=options)
            return r,executor.list_hits(db,run),seen,http,run_report(db,run)['events']
    def test_planned_as_connector(self):
        with open_db(self.path,write=True) as db:
            steps={s['step_id']:s['route'] for s in planner.plan_research(db,self.run_with_plan(db,'Q01.01'))['steps']}
        self.assertEqual({steps[k] for k in ('stackexchange','github_issues','hackernews')},{'connector'})
    def test_parses_questions_without_personal_names(self):
        r,hits,seen,_,_=self.run_se()
        self.assertEqual((r['status'],r['hits']),('completed',2))
        self.assertEqual(hits[0]['title'],'CRM sync & duplicates')
        self.assertEqual(hits[0]['snippet'],'CRM sync & duplicates. Our CRM duplicates contacts <daily>.')
        self.assertEqual(hits[0]['published_at'],'2023-11-14T22:13:20+00:00')
        self.assertEqual(hits[0]['metadata']['independence_group'],'stackexchange:stackoverflow:user:77')
        self.assertEqual(hits[1]['metadata']['independence_group'],'stackexchange:stackoverflow:question:12')
        self.assertNotIn('Persona Real',json.dumps(hits,ensure_ascii=False))
        p=seen[0].url.params
        self.assertEqual((p['site'],p['filter'],p['q']),('stackoverflow','withbody','CRM pymes'))
    def test_options_site_and_validation(self):
        _,_,seen,_,_=self.run_se(options={'site':'softwarerecs','tagged':'crm;invoicing'})
        self.assertEqual((seen[0].url.params['site'],seen[0].url.params['tagged']),('softwarerecs','crm;invoicing'))
        for bad in ({'site':'evil.com/x?'},{'pagesize':'100'},{'site':5}):
            with self.subTest(bad=bad), self.assertRaises(EvidenceError): self.run_se(options=bad)
        with open_db(self.path,write=True) as db, self.assertRaises(EvidenceError):
            executor.execute_step(db,self.run_with_plan(db,'Q07.01',5),'openalex',http=self.http(ok_json())[0],options={'site':'x'})
    def test_backoff_defers_next_request(self):
        _,_,_,http,_=self.run_se({**QUESTIONS,'backoff':10})
        self.assertGreater(http._not_before['stackexchange'],http.clock()+5)
    def test_throttle_violation_blocks(self):
        r,_,_,_,events=self.run_se({'error_id':502,'error_name':'throttle_violation','error_message':'too many requests'},status=400)
        self.assertEqual(r['status'],'blocked'); self.assertIn('throttle_violation',r['reason'])
        self.assertEqual([e['event_type'] for e in events],['connector_call','blocked'])
    def test_other_400_fails_with_detail(self):
        r,*_=self.run_se({'error_id':400,'error_name':'bad_parameter','error_message':'site is required'},status=400)
        self.assertEqual(r['status'],'failed'); self.assertIn('bad_parameter',r['reason'])
    def test_quota_exhausted_blocks(self):
        r,*_=self.run_se({'items':[],'quota_remaining':0})
        self.assertEqual(r['status'],'blocked')
    def test_key_redacted_from_logs(self):
        os.environ['MARKETSCOPE_STACKEXCHANGE_KEY']='se-key-9'
        try:
            _,_,seen,_,events=self.run_se()
        finally:
            os.environ.pop('MARKETSCOPE_STACKEXCHANGE_KEY')
        self.assertEqual(seen[0].url.params['key'],'se-key-9')
        self.assertNotIn('se-key-9',events[0]['source_url'])

# Synthetic GitHub-shaped payload (not real data).
ISSUES={'total_count':3,'incomplete_results':False,'items':[
    {'html_url':'https://github.com/acme/crm/issues/5','repository_url':'https://api.github.com/repos/acme/crm',
     'number':5,'title':'Sync  loses invoices','body':'<!-- template -->Invoices vanish after **sync**.',
     'created_at':'2025-01-02T03:04:05Z','state':'open','comments':2,'reactions':{'total_count':7},
     'labels':[{'name':'bug'}],'author_association':'NONE','user':{'id':42,'login':'persona-real'}},
    {'html_url':'https://github.com/acme/crm/pull/6','title':'PR','pull_request':{},'user':{'id':1}},
]}

class TestGitHub(ExecCase):
    def setUp(self):
        super().setUp(); os.environ.pop('MARKETSCOPE_GITHUB_TOKEN',None)
    def run_gh(self,response=None,options=None,query=''):
        http,seen=self.http(response or httpx.Response(200,json=ISSUES))
        with open_db(self.path,write=True) as db:
            run=self.run_with_plan(db,'Q01.01')
            r=executor.execute_step(db,run,'github_issues',query=query,http=http,options=options)
            return r,executor.list_hits(db,run),seen,http,run_report(db,run)['events'],db.execute('SELECT * FROM source_hits').fetchall()
    def test_parses_issues_skips_prs_and_logins(self):
        r,hits,seen,_,_,rows=self.run_gh()
        self.assertEqual((r['status'],r['hits']),('completed',1))
        h=hits[0]
        self.assertEqual((h['title'],h['snippet']),('Sync loses invoices','Sync loses invoices. Invoices vanish after **sync**.'))
        self.assertEqual(h['published_at'],'2025-01-02T03:04:05+00:00')
        self.assertEqual((h['metadata']['independence_group'],h['metadata']['repo'],h['metadata']['reactions']),
                         ('github:user:42','acme/crm',7))
        self.assertNotIn('persona-real',str(rows))
        self.assertEqual(seen[0].url.params['q'],'CRM pymes is:issue')
        self.assertEqual(seen[0].headers['accept'],'application/vnd.github+json')
        self.assertNotIn('authorization',seen[0].headers)
    def test_options_become_qualifiers(self):
        _,_,seen,*_=self.run_gh(options={'repo':'acme/crm','state':'open','sort':'reactions'},query='sync invoices is:issue')
        p=seen[0].url.params
        self.assertEqual((p['q'],p['sort'],p['order']),('sync invoices is:issue repo:acme/crm state:open','reactions','desc'))
        with self.assertRaises(EvidenceError): self.run_gh(options={'repo':'not a repo'})
    def test_token_in_header_never_stored(self):
        os.environ['MARKETSCOPE_GITHUB_TOKEN']='github_pat_test_123'
        try:
            _,_,seen,_,events,rows=self.run_gh()
        finally:
            os.environ.pop('MARKETSCOPE_GITHUB_TOKEN')
        self.assertEqual(seen[0].headers['authorization'],'Bearer github_pat_test_123')
        self.assertNotIn('github_pat_test_123',str(events)+str(rows))
    def test_rate_limit_403_is_retryable(self):
        limited=httpx.Response(403,json={'message':'API rate limit exceeded for user ID 1.'},headers={'retry-after':'60'})
        r,_,_,http,_,_=self.run_gh(limited)
        self.assertEqual(r['status'],'failed'); self.assertIn('Límite temporal',r['reason'])
        self.assertGreater(http._not_before['github_issues'],http.clock()+50)
    def test_plain_403_blocks(self):
        r,*_=self.run_gh(httpx.Response(403,json={'message':'Resource not accessible by personal access token'}))
        self.assertEqual(r['status'],'blocked')
    def test_exhausted_remaining_defers_until_reset(self):
        import time as _t
        resp=httpx.Response(200,json=ISSUES,headers={'x-ratelimit-remaining':'0','x-ratelimit-reset':str(int(_t.time())+600)})
        _,_,_,http,_,_=self.run_gh(resp)
        self.assertGreater(http._not_before['github_issues'],http.clock()+500)
    def test_invalid_query_fails_with_detail(self):
        r,*_=self.run_gh(httpx.Response(422,json={'message':'Validation Failed'}))
        self.assertEqual(r['status'],'failed'); self.assertIn('Validation Failed',r['reason'])

# Synthetic JSON-stat 2.0 payload (not real data): 2 sizes x 2 years, one missing cell, one flagged.
JSONSTAT={'version':'2.0','class':'dataset','label':'Demo dataset','updated':'2026-06-15T11:00:00+0200',
    'id':['size_emp','geo','time'],'size':[2,1,2],
    'dimension':{'size_emp':{'label':'Size','category':{'index':{'GE10':0,'10_49':1},'label':{'GE10':'10+','10_49':'10-49'}}},
                 'geo':{'label':'Geo','category':{'index':{'ES':0},'label':{'ES':'Spain'}}},
                 'time':{'label':'Time','category':{'index':{'2024':0,'2025':1},'label':{'2024':'2024','2025':'2025'}}}},
    'value':{'0':12.5,'1':20.0,'3':7.25},'status':{'3':'p'}}

class TestEurostat(ExecCase):
    def run_es(self,payload=JSONSTAT,query='demo_ds',options=None,geography='ES',status=200):
        http,seen=self.http(httpx.Response(status,json=payload))
        with open_db(self.path,write=True) as db:
            s=create_study(db,name=f'eu{next(_names)}',market_definition='CRM',geography=geography,segment='pymes',language='es')
            run=start_run(db,'Q06.01',10,s['study_id']); planner.plan_research(db,run)
            r=executor.execute_step(db,run,'eurostat',query=query,http=http,options=options)
            return r,executor.list_hits(db,run),seen
    def test_observations_become_reproducible_hits(self):
        r,hits,seen=self.run_es()
        self.assertEqual((r['status'],r['hits']),('completed',3))
        by_obs={tuple(h['metadata']['observation'].values()):h for h in hits}
        h=by_obs[('GE10','ES','2025')]
        self.assertEqual(h['metadata']['value'],20.0)
        self.assertIn('valor: 20. Fuente: Eurostat',h['snippet'])  # 20.0 rendered as 20
        self.assertTrue(h['url'].endswith('demo_ds?lang=EN&size_emp=GE10&geo=ES&time=2025'))
        flagged=by_obs[('10_49','ES','2025')]
        self.assertIn('valor: 7.25 [estado: p]',flagged['snippet'])
        self.assertEqual((h['published_at'],h['metadata']['independence_group']),('2026-06-15T11:00:00+02:00','eurostat'))
        self.assertEqual(seen[0].url.params.get_list('geo'),['ES'])
    def test_filters_and_geo_aliases(self):
        _,_,seen=self.run_es(options={'filters':'size_emp=GE10,10_49;lastTimePeriod=2'},geography='GR')
        p=seen[0].url.params
        self.assertEqual((p.get_list('size_emp'),p['lastTimePeriod'],p['geo']),(['GE10','10_49'],'2','EL'))
        _,_,seen=self.run_es(geography='EU')
        self.assertEqual(seen[0].url.params['geo'],'EU27_2020')
    def test_explicit_geo_wins_and_global_has_no_geo(self):
        _,_,seen=self.run_es(options={'filters':'geo=FR'})
        self.assertEqual(seen[0].url.params.get_list('geo'),['FR'])
    def test_query_required_and_validated(self):
        with self.assertRaisesRegex(EvidenceError,'código de dataset'): self.run_es(query='')
        with self.assertRaisesRegex(EvidenceError,'no válido'): self.run_es(query='bad code!')
        with self.assertRaisesRegex(EvidenceError,'Valor no válido'): self.run_es(options={'filters':'geo=ES&x=1'})
    def test_not_found_fails_with_eurostat_label(self):
        r,_,_=self.run_es({'error':[{'status':404,'id':100,'label':'ERR_NOT_FOUND_4: NOPE is not available'}]},status=404)
        self.assertEqual(r['status'],'failed'); self.assertIn('ERR_NOT_FOUND_4',r['reason'])
    def test_empty_selection_leaves_step_open(self):
        r,hits,_=self.run_es({**JSONSTAT,'value':{}})
        self.assertEqual((r['status'],r['hits'],hits),('empty',0,[]))
    def test_truncation_flagged(self):
        from marketscope.connectors import eurostat
        with mock.patch.object(eurostat,'MAX_HITS',2):
            _,hits,_=self.run_es()
        self.assertEqual(len(hits),2); self.assertTrue(hits[0]['metadata']['truncated'])
        self.assertEqual(hits[0]['metadata']['total_observations'],3)

# Synthetic TED-shaped payload (not real data).
NOTICES={'totalNoticeCount':2,'timedOut':False,'notices':[
    {'publication-number':'123-2026','publication-date':'2026-11-13+01:00','notice-type':'cn-standard',
     'notice-title':{'eng':'Spain – Software – Invoicing tool','spa':'España – Software – Herramienta de facturación'},
     'buyer-name':{'spa':['Ayuntamiento  de Ejemplo']},'buyer-country':['ESP'],'classification-cpv':['48000000'],
     'description-lot':{'spa':['Suministro de software de facturación.','Lote 2.']},
     'links':{'html':{'ENG':'https://ted.europa.eu/en/notice/-/detail/123-2026'}}},
    {'publication-number':'bad','notice-title':{'eng':'x'}},
]}

class TestTed(ExecCase):
    def run_ted(self,payload=NOTICES,query='',options=None,geography='ES',status=200):
        http,seen=self.http(httpx.Response(status,json=payload))
        with open_db(self.path,write=True) as db:
            s=create_study(db,name=f'ted{next(_names)}',market_definition='software facturación',geography=geography,segment='pymes',language='es')
            run=start_run(db,'Q06.01',10,s['study_id']); planner.plan_research(db,run)
            r=executor.execute_step(db,run,'ted',query=query,http=http,options=options)
            return r,executor.list_hits(db,run),seen,run_report(db,run)['events']
    def body(self,seen): return json.loads(seen[0].content)
    def test_plain_query_becomes_full_text_with_study_country(self):
        r,hits,seen,events=self.run_ted()
        self.assertEqual(seen[0].method,'POST')
        expected='(FT ~ (software AND facturación AND pymes)) AND buyer-country IN (ESP) SORT BY publication-date DESC'
        self.assertEqual(self.body(seen)['query'],expected)
        self.assertEqual(events[0]['query'],expected)  # exact expert query audited
        self.assertEqual((r['status'],r['hits']),('completed',1))
        h=hits[0]
        self.assertEqual((h['url'],h['title']),('https://ted.europa.eu/en/notice/-/detail/123-2026','Spain – Software – Invoicing tool'))
        self.assertEqual(h['published_at'],'2026-11-13T00:00:00+01:00')
        self.assertEqual(h['metadata']['independence_group'],'ted:buyer:ayuntamiento de ejemplo')
        self.assertIn('Gasto licitado',h['metadata']['caveat'])
    def test_options_and_language(self):
        _,hits,seen,_=self.run_ted(options={'cpv':'48000000','since':'20250101','lang':'spa','country':'FRA'})
        q=self.body(seen)['query']
        self.assertIn('buyer-country IN (FRA)',q); self.assertIn('classification-cpv IN (48000000)',q)
        self.assertIn('publication-date >= 20250101',q)
        self.assertTrue(hits[0]['snippet'].startswith('España – Software – Herramienta de facturación. Suministro'))
    def test_expert_query_kept_and_sort_preserved(self):
        _,_,seen,_=self.run_ted(query='notice-title ~ crm SORT BY publication-number ASC',geography='EU')
        self.assertEqual(self.body(seen)['query'],'(notice-title ~ crm) SORT BY publication-number ASC')
    def test_syntax_error_and_timeout_fail(self):
        r,*_=self.run_ted({'message':'Syntax error in expert query','error':{'type':'QUERY_SYNTAX_ERROR'}},status=400)
        self.assertEqual(r['status'],'failed'); self.assertIn('QUERY_SYNTAX_ERROR',r['reason'])
    def test_timed_out_fails(self):
        r,*_=self.run_ted({**NOTICES,'timedOut':True},geography='FR')
        self.assertEqual(r['status'],'failed')
    def test_not_planned_outside_eu(self):
        with open_db(self.path,write=True) as db:
            s=create_study(db,name='us',market_definition='x',geography='US',segment='y',language='en')
            steps={x['step_id'] for x in planner.plan_research(db,start_run(db,'Q06.01',10,s['study_id']))['steps']}
        self.assertFalse({'ted','eurostat'}&steps)

class TestQueryQuality(ExecCase):
    """Regressions from the live review: useless template queries, empty steps closed, oversized listings."""
    def study_run(self,db,question='Q01.01',budget=20,**study):
        s=create_study(db,**{'name':f'q{next(_names)}','market_definition':'Software de facturación para autónomos',
                             'geography':'ES','segment':'autónomos','language':'es',**study})
        run=start_run(db,question,budget,s['study_id']); planner.plan_research(db,run)
        return s,run
    def test_english_first_source_refuses_spanish_template_without_spending(self):
        http,seen=self.http(httpx.Response(200,json=QUESTIONS))
        with open_db(self.path,write=True) as db:
            _,run=self.study_run(db)
            with self.assertRaisesRegex(EvidenceError,'inglés'): executor.execute_step(db,run,'stackexchange',http=http)
            self.assertEqual((seen,self.events(db,run)),([],[]))
            self.assertEqual(executor.execute_step(db,run,'stackexchange',query='invoice freelancer',http=http)['status'],'completed')
    def test_keywords_set_after_planning_are_used(self):
        from marketscope.studies import set_study_keywords
        http,seen=self.http(httpx.Response(200,json=QUESTIONS))
        with open_db(self.path,write=True) as db:
            s,run=self.study_run(db)
            set_study_keywords(db,s['study_id'],'invoicing software freelancers freelancers')
            executor.execute_step(db,run,'stackexchange',http=http)
        self.assertEqual(seen[0].url.params['q'],'invoicing software freelancers')
    def test_template_dedupes_words_and_english_study_needs_no_keywords(self):
        http,seen=self.http(httpx.Response(200,json=QUESTIONS))
        with open_db(self.path,write=True) as db:
            _,run=self.study_run(db,market_definition='Invoicing software for freelancers',segment='freelancers',language='en')
            executor.execute_step(db,run,'stackexchange',http=http)
        self.assertEqual(seen[0].url.params['q'],'Invoicing software for freelancers')
    def test_empty_result_keeps_step_open_for_a_better_query(self):
        http,seen=self.http(httpx.Response(200,json={'items':[],'quota_remaining':200}),httpx.Response(200,json=QUESTIONS))
        with open_db(self.path,write=True) as db:
            _,run=self.study_run(db,keywords_en='nothing matches this')
            r=executor.execute_step(db,run,'stackexchange',http=http)
            self.assertEqual((r['status'],r['step_budget_left']),('empty',r['step_budget_left']))
            self.assertGreater(r['step_budget_left'],0)
            n=planner.next_step(db,run)
            self.assertEqual(n['step']['step_id'],'github_issues')  # still pending steps in order
            self.assertIn('stackexchange',{s['step_id'] for s in planner.get_plan(db,run)['steps']}-planner._closed_steps(db,run))
            self.assertEqual(executor.execute_step(db,run,'stackexchange',query='invoice freelancer',http=http)['status'],'completed')
    def test_empty_with_no_step_budget_says_so(self):
        http,_=self.http(httpx.Response(200,json={'items':[],'quota_remaining':200}))
        with open_db(self.path,write=True) as db:
            _,run=self.study_run(db,budget=3,keywords_en='x')
            step=[s for s in planner.get_plan(db,run)['steps'] if s['step_id']=='stackexchange'][0]
            for _ in range(step['budget']): r=executor.execute_step(db,run,'stackexchange',http=http)
            self.assertEqual(r['step_budget_left'],0); self.assertIn('replanifica',r['hint'])
            with self.assertRaisesRegex(EvidenceError,'Presupuesto del paso'): executor.execute_step(db,run,'stackexchange',http=http)
    def test_compact_paginated_listing(self):
        many={'items':[{**QUESTIONS['items'][0],'question_id':i,'link':f'https://stackoverflow.com/questions/{i}/x',
                        'body':'<p>'+'larga '*200+'</p>'} for i in range(30)],'quota_remaining':100}
        http,_=self.http(httpx.Response(200,json=many))
        with open_db(self.path,write=True) as db:
            _,run=self.study_run(db,keywords_en='crm')
            executor.execute_step(db,run,'stackexchange',http=http)
            page=executor.hits_page(db,run,offset=0,limit=10)
            self.assertEqual((page['total'],len(page['hits']),page['independent_groups']),(30,10,1))
            self.assertEqual(page['largest_groups'],[('stackexchange:stackoverflow:user:77',30)])
            h=page['hits'][0]
            self.assertLessEqual(len(h['preview']),201); self.assertNotIn('snippet',h)
            self.assertEqual((h['score'],h['site']),(4,'stackoverflow'))
            self.assertLess(len(json.dumps(page,ensure_ascii=False)),8000)
            self.assertEqual(len(executor.hits_page(db,run,offset=25,limit=10)['hits']),5)
            self.assertEqual(executor.hits_page(db,run,step_id='github_issues')['total'],0)
            self.assertGreater(len(executor.get_hit(db,h['hit_id'])['snippet']),1000)
            for bad in ((-1,10),(0,0),(0,51)):
                with self.subTest(bad=bad), self.assertRaises(EvidenceError): executor.hits_page(db,run,*bad)
            with self.assertRaises(EvidenceError): executor.get_hit(db,'nope')

# Synthetic SEC-shaped payloads (not real data).
SEC_TICKERS={'0':{'cik_str':42,'ticker':'ACME','title':'ACME CORP'}}
SEC_CONCEPT={'cik':42,'taxonomy':'us-gaap','tag':'Revenues','label':'Revenues','entityName':'ACME CORP','units':{'USD':[
    {'start':'2023-01-01','end':'2023-12-31','val':1000,'accn':'0000000042-24-000001','fy':2023,'fp':'FY','form':'10-K','filed':'2024-02-01','frame':'CY2023'},
    {'start':'2023-01-01','end':'2023-12-31','val':1000,'accn':'0000000042-25-000001','fy':2024,'fp':'FY','form':'10-K','filed':'2025-02-01'},
    {'start':'2024-01-01','end':'2024-12-31','val':1500,'accn':'0000000042-25-000001','fy':2024,'fp':'FY','form':'10-K','filed':'2025-02-01','frame':'CY2024'},
    {'start':'2024-07-01','end':'2024-09-30','val':400,'accn':'0000000042-24-000009','fy':2024,'fp':'Q3','form':'10-Q','filed':'2024-11-01','frame':'CY2024Q3'},
]}}

class TestSecEdgar(ExecCase):
    def setUp(self):
        super().setUp(); os.environ['MARKETSCOPE_CONTACT']='research@example.org'
    def run_sec(self,*responses,query='ACME',options=None):
        http,seen=self.http(*responses)
        with open_db(self.path,write=True) as db:
            s=create_study(db,name=f'sec{next(_names)}',market_definition='invoicing',geography='GLOBAL',segment='smb',language='en')
            run=start_run(db,'Q06.01',12,s['study_id']); planner.plan_research(db,run)
            r=executor.execute_step(db,run,'sec_edgar',query=query,http=http,options=options)
            return r,executor.list_hits(db,run),seen,run_report(db,run)['events']
    def test_ticker_resolved_and_one_value_per_year(self):
        r,hits,seen,events=self.run_sec(httpx.Response(200,json=SEC_TICKERS),httpx.Response(200,json=SEC_CONCEPT))
        self.assertEqual((r['status'],r['hits']),('completed',2))  # restated 2023 and 10-Q dropped
        self.assertEqual([h['metadata']['frame'] for h in hits],['CY2023','CY2024'])
        h=hits[1]
        self.assertIn('Revenues: 1500 USD, periodo 2024-01-01 a 2024-12-31 (CY2024)',h['snippet'])
        self.assertEqual((h['published_at'],h['metadata']['independence_group']),('2025-02-01T00:00:00+00:00','sec:cik:0000000042'))
        self.assertEqual(h['metadata']['filing_url'],'https://www.sec.gov/Archives/edgar/data/42/000000004225000001/')
        self.assertEqual([str(s.url).split('?')[0] for s in seen],
                         ['https://www.sec.gov/files/company_tickers.json',
                          'https://data.sec.gov/api/xbrl/companyconcept/CIK0000000042/us-gaap/Revenues.json'])
        self.assertTrue(seen[0].headers['user-agent'].startswith('MarketScopeResearch/0.3 (research@example.org;'))
        self.assertEqual(len([e for e in events if e['event_type']=='connector_call']),2)
    def test_cik_skips_lookup_and_404_falls_back_to_next_concept(self):
        alt={**SEC_CONCEPT,'tag':'RevenueFromContractWithCustomerExcludingAssessedTax'}
        r,hits,seen,_=self.run_sec(httpx.Response(404,text='<Error/>',headers={'content-type':'application/xml'}),
                                   httpx.Response(200,json=alt),query='42')
        self.assertEqual((r['status'],r['hits']),('completed',2))
        self.assertTrue(str(seen[1].url).endswith('/RevenueFromContractWithCustomerExcludingAssessedTax.json'))
    def test_unknown_ticker_and_unreported_concept_fail(self):
        r,*_=self.run_sec(httpx.Response(200,json=SEC_TICKERS),query='NOPE')
        self.assertEqual(r['status'],'failed'); self.assertIn('no encontrado',r['reason'])
        r,*_=self.run_sec(httpx.Response(404,text='x'),query='42',options={'concept':'Foo'})
        self.assertEqual(r['status'],'failed'); self.assertIn('no reporta Foo',r['reason'])
    def test_contact_required_and_only_sent_to_sec(self):
        os.environ.pop('MARKETSCOPE_CONTACT')
        with self.assertRaisesRegex(EvidenceError,'MARKETSCOPE_CONTACT'): self.run_sec(httpx.Response(200,json=SEC_TICKERS))
        os.environ['MARKETSCOPE_CONTACT']='research@example.org'
        http,seen=self.http(ok_json())
        with open_db(self.path,write=True) as db:
            executor.execute_step(db,self.run_with_plan(db),'openalex',http=http)
        self.assertNotIn('research@example.org',seen[0].headers['user-agent'])
    def test_query_validation(self):
        for q in ('ACME;DROP','https://x','12345678901'):
            with self.subTest(q=q), self.assertRaises(EvidenceError): self.run_sec(httpx.Response(200,json=SEC_TICKERS),query=q)
    def test_planned_as_competitor_data_outside_us(self):
        with open_db(self.path,write=True) as db:
            s=create_study(db,name='sec-es',market_definition='x',geography='ES',segment='y',language='es')
            step={x['step_id']:x for x in planner.plan_research(db,start_run(db,'Q05.01',5,s['study_id']))['steps']}['sec_edgar']
        self.assertEqual(step['coverage'],'partial'); self.assertIn('competidores',step['scope_note'])

# Synthetic World Bank-shaped payloads (not real data).
WB=[{'page':1,'pages':1,'per_page':100,'total':3,'sourceid':'2','lastupdated':'2026-11-11'},[
    {'indicator':{'id':'IT.NET.USER.ZS','value':'Internet users (%)'},'country':{'id':'1W','value':'World'},'countryiso3code':'WLD','date':'2024','value':67.4,'obs_status':''},
    {'indicator':{'id':'IT.NET.USER.ZS','value':'Internet users (%)'},'country':{'id':'ES','value':'Spain'},'countryiso3code':'ESP','date':'2024','value':95.76,'obs_status':''},
    {'indicator':{'id':'IT.NET.USER.ZS','value':'Internet users (%)'},'country':{'id':'ES','value':'Spain'},'countryiso3code':'ESP','date':'2023','value':None,'obs_status':''},
]]

class TestWorldBank(ExecCase):
    def run_wb(self,payload=WB,query='IT.NET.USER.ZS',options=None,geography='GLOBAL'):
        http,seen=self.http(httpx.Response(200,json=payload))
        with open_db(self.path,write=True) as db:
            s=create_study(db,name=f'wb{next(_names)}',market_definition='x',geography=geography,segment='y',language='en')
            run=start_run(db,'Q06.01',12,s['study_id']); planner.plan_research(db,run)
            r=executor.execute_step(db,run,'worldbank',query=query,http=http,options=options)
            return r,executor.list_hits(db,run),seen
    def test_observations_with_values_become_hits(self):
        r,hits,seen=self.run_wb()
        self.assertEqual((r['status'],r['hits']),('completed',2))  # null value dropped
        h=hits[1]
        self.assertIn('Internet users (%) — Spain (ESP), 2024: valor 95.76',h['snippet'])
        self.assertEqual((h['published_at'],h['metadata']['independence_group']),('2026-11-11T00:00:00+00:00','worldbank'))
        self.assertTrue(h['url'].startswith('https://api.worldbank.org/v2/country/ESP/indicator/IT.NET.USER.ZS?date=2024'))
        self.assertIn('/country/WLD/indicator/',str(seen[0].url))
    def test_geography_and_options(self):
        _,_,seen=self.run_wb(geography='EU')
        self.assertIn('/country/EUU/',str(seen[0].url))
        _,_,seen=self.run_wb(options={'countries':'ES;US','mrv':'3'})
        self.assertIn('/country/ES;US/',str(seen[0].url)); self.assertEqual(seen[0].url.params['mrv'],'3')
        _,_,seen=self.run_wb(geography='FR')
        self.assertIn('/country/FR/',str(seen[0].url))
    def test_http_200_error_message_fails(self):
        r,_,_=self.run_wb([{'message':[{'id':'120','key':'Invalid value','value':'The provided parameter value is not valid'}]}])
        self.assertEqual(r['status'],'failed'); self.assertIn('Invalid value',r['reason'])
    def test_slow_api_gets_longer_timeout(self):
        from marketscope.connectors.worldbank import WorldBank
        self.assertGreaterEqual(WorldBank().policy.timeout,90)
    def test_query_required(self):
        with self.assertRaisesRegex(EvidenceError,'indicador'): self.run_wb(query='')

class GlobalCase(ExecCase):
    def run_src(self,question,step_id,*responses,query='',options=None,language='en',geography='GLOBAL'):
        http,seen=self.http(*responses)
        with open_db(self.path,write=True) as db:
            s=create_study(db,name=f'g{next(_names)}',market_definition='invoicing software',geography=geography,
                           segment='small businesses',language=language)
            run=start_run(db,question,12,s['study_id']); planner.plan_research(db,run)
            r=executor.execute_step(db,run,step_id,query=query,http=http,options=options)
            return r,executor.list_hits(db,run),seen,run_report(db,run)['events']

# Synthetic Federal Register payload (not real data).
FR={'count':2,'results':[
    {'title':'Electronic  Invoicing Rule','abstract':'Agencies must accept e-invoices.','document_number':'2025-1','type':'Rule',
     'html_url':'https://www.federalregister.gov/documents/2025/01/02/2025-1/x','publication_date':'2025-01-02','significant':True,
     'agencies':[{'name':'Treasury Department','slug':'treasury-department'}]},
    {'title':'No URL','html_url':'https://evil.example/x'},
]}

class TestFederalRegister(GlobalCase):
    def test_relevance_order_options_and_parsing(self):
        r,hits,seen,_=self.run_src('Q08.01','federal_register',httpx.Response(200,json=FR),query='"electronic invoicing"',
                                   options={'type':'RULE','since':'2020-01-01','agency':'treasury-department'})
        p=seen[0].url.params
        self.assertEqual((p['conditions[term]'],p['order'],p['conditions[type][]'],p['conditions[publication_date][gte]']),
                         ('"electronic invoicing"','relevance','RULE','2020-01-01'))
        self.assertIn('abstract',p.get_list('fields[]'))
        self.assertEqual((r['status'],r['hits']),('completed',1))
        h=hits[0]
        self.assertEqual((h['title'],h['snippet']),('Electronic Invoicing Rule','Electronic Invoicing Rule. Agencies must accept e-invoices.'))
        self.assertEqual((h['published_at'],h['metadata']['independence_group']),('2025-01-02T00:00:00+00:00','federalregister:agency:treasury-department'))
    def test_english_first(self):
        with self.assertRaisesRegex(EvidenceError,'inglés'):
            self.run_src('Q08.01','federal_register',httpx.Response(200,json=FR),language='es')

# Synthetic SPARQL results (not real data).
CELLAR={'head':{'vars':['celex','title','date']},'results':{'bindings':[
    {'celex':{'value':'32014L0055'},'title':{'value':'Directive 2014/55/EU on  electronic invoicing'},'date':{'value':'2014-04-16'}},
    {'celex':{'value':'32014L0055'},'title':{'value':'duplicate'},'date':{'value':'2014-04-16'}},
    {'celex':{'value':'bad celex!'},'title':{'value':'x'},'date':{'value':'2014-01-01'}},
]}}

class TestEurLex(GlobalCase):
    def test_template_filters_and_hits(self):
        r,hits,seen,_=self.run_src('Q08.01','eurlex',httpx.Response(200,json=CELLAR),query='Electronic invoicing',
                                   options={'sector':'3','since':'2010-01-01'},language='es',geography='EU')
        sparql=seen[0].url.params['query']
        self.assertIn('language/SPA>',sparql)  # title language from the study
        self.assertIn('CONTAINS(LCASE(STR(?title)),"electronic")',sparql); self.assertIn('"invoicing"',sparql)
        self.assertIn('STRSTARTS(STR(?celex),"3")',sparql); self.assertIn('"2010-01-01"^^',sparql)
        self.assertEqual((r['status'],r['hits']),('completed',1))
        h=hits[0]
        self.assertEqual(h['url'],'https://eur-lex.europa.eu/legal-content/ES/TXT/?uri=CELEX:32014L0055')
        self.assertIn('CELEX 32014L0055, fecha del documento 2014-04-16',h['snippet'])
        self.assertEqual((h['metadata']['sector'],h['metadata']['independence_group']),('3','eurlex'))
    def test_query_cannot_inject_sparql(self):
        for q in ('x") } DELETE {','a b c d e f g','"quoted"','',):
            with self.subTest(q=q), self.assertRaises(EvidenceError):
                self.run_src('Q08.01','eurlex',httpx.Response(200,json=CELLAR),query=q,geography='EU')
    def test_lang_option(self):
        _,hits,seen,_=self.run_src('Q08.01','eurlex',httpx.Response(200,json=CELLAR),query='invoicing',options={'lang':'FRA'},geography='EU')
        self.assertIn('language/FRA>',seen[0].url.params['query']); self.assertIn('/legal-content/FR/',hits[0]['url'])

PAGEVIEWS={'items':[{'project':'en.wikipedia','article':'QuickBooks','granularity':'monthly','timestamp':'2025010100','views':100},
                    {'project':'en.wikipedia','article':'QuickBooks','granularity':'monthly','timestamp':'2025020100','views':150}]}

class TestWikimedia(GlobalCase):
    def setUp(self):
        super().setUp(); os.environ['MARKETSCOPE_WIKIMEDIA_CONTACT']='User:Example'
    def tearDown(self):
        os.environ.pop('MARKETSCOPE_WIKIMEDIA_CONTACT',None); super().tearDown()
    def test_series_per_article_and_missing_articles(self):
        missing=httpx.Response(404,json={'detail':'not found'})
        r,hits,seen,events=self.run_src('Q10.01','wikimedia_pageviews',httpx.Response(200,json=PAGEVIEWS),missing,
                                        query='QuickBooks|No such page',options={'start':'20250101','end':'20250301'})
        self.assertEqual((r['status'],r['hits']),('completed',1))
        self.assertEqual(str(seen[0].url),'https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/QuickBooks/monthly/20250101/20250301')
        self.assertIn('/user/No_such_page/',str(seen[1].url))
        h=hits[0]
        self.assertIn('2025-01: 100; 2025-02: 150. Total 250',h['snippet'])
        self.assertEqual((h['metadata']['missing_articles'],h['metadata']['independence_group']),(['No such page'],'wikimedia:pageviews'))
        self.assertEqual(seen[0].headers['user-agent'],'MarketScopeResearch/0.3 (User:Example; research prototype; respects documented API limits)')
        self.assertEqual(len([e for e in events if e['event_type']=='connector_call']),2)
    def test_project_from_study_language(self):
        _,_,seen,_=self.run_src('Q10.01','wikimedia_pageviews',httpx.Response(200,json=PAGEVIEWS),query='Factura',language='es')
        self.assertIn('/es.wikipedia/',str(seen[0].url))
    def test_requires_consented_contact_and_valid_titles(self):
        os.environ.pop('MARKETSCOPE_WIKIMEDIA_CONTACT')
        os.environ['MARKETSCOPE_CONTACT']='sec-only@example.org'  # SEC contact must never be reused for Wikimedia
        try:
            with self.assertRaisesRegex(EvidenceError,'MARKETSCOPE_WIKIMEDIA_CONTACT'):
                self.run_src('Q10.01','wikimedia_pageviews',httpx.Response(200,json=PAGEVIEWS),query='QuickBooks')
        finally:
            os.environ.pop('MARKETSCOPE_CONTACT')
        os.environ['MARKETSCOPE_WIKIMEDIA_CONTACT']='User:Example'
        for q in ('a|b|c|d|e|f','../x','a||b'):
            with self.subTest(q=q), self.assertRaises(EvidenceError):
                self.run_src('Q10.01','wikimedia_pageviews',httpx.Response(200,json=PAGEVIEWS),query=q)
    def test_all_missing_fails(self):
        r,*_=self.run_src('Q10.01','wikimedia_pageviews',httpx.Response(404,json={}),query='Nope')
        self.assertEqual(r['status'],'failed'); self.assertIn('Ningún artículo',r['reason'])
    def test_default_window_is_last_24_full_months(self):
        from datetime import date
        from marketscope.connectors.wikimedia_pageviews import default_window
        # Nov 2024 .. Oct 2026 = 24 full months; the partial current month is excluded.
        self.assertEqual(default_window(date(2026,11,16)),('20241101','20261031'))
        self.assertEqual(default_window(date(2026,1,1)),('20240101','20251231'))
        self.assertEqual(default_window(date(2026,3,31)),('20240301','20260228'))

WIKIDATA={'results':{'bindings':[
    {'item':{'value':'http://www.wikidata.org/entity/Q123'},'l':{'value':'QuickBooks'},'d':{'value':'accounting software'},
     'types':{'value':'accounting software'},'devs':{'value':'Intuit'},'countries':{'value':'United States'},
     'since':{'value':'1983-01-01T00:00:00Z'},'site':{'value':'https://quickbooks.intuit.com/'},'mod':{'value':'2026-10-01T12:00:00Z'}},
    {'item':{'value':'https://evil.example/Q1'}},
]}}

class TestWikidata(GlobalCase):
    def setUp(self):
        super().setUp(); os.environ['MARKETSCOPE_WIKIMEDIA_CONTACT']='User:Example'
    def tearDown(self):
        os.environ.pop('MARKETSCOPE_WIKIMEDIA_CONTACT',None); super().tearDown()
    def test_entity_search_and_hit(self):
        r,hits,seen,_=self.run_src('Q04.01','wikidata',httpx.Response(200,json=WIKIDATA),query='QuickBooks')
        sparql=seen[0].url.params['query']
        self.assertIn('mwapi:search "QuickBooks"',sparql); self.assertIn('mwapi:language "en"',sparql)
        self.assertEqual((r['status'],r['hits']),('completed',1))
        h=hits[0]
        self.assertEqual((h['url'],h['title']),('https://www.wikidata.org/wiki/Q123','QuickBooks'))
        self.assertIn('Desarrollador/fabricante: Intuit. País: United States. Inicio/lanzamiento: 1983-01-01',h['snippet'])
        self.assertEqual((h['published_at'],h['metadata']['independence_group']),('2026-10-01T12:00:00+00:00','wikidata'))
    def test_class_mode(self):
        _,_,seen,_=self.run_src('Q04.01','wikidata',httpx.Response(200,json=WIKIDATA),query='cloud',options={'class':'Q7397'})
        sparql=seen[0].url.params['query']
        self.assertIn('wdt:P31 wd:Q7397',sparql); self.assertIn('CONTAINS(LCASE(STR(?label)),"cloud")',sparql)
        self.assertNotIn('EntitySearch',sparql)
    def test_query_cannot_inject_sparql(self):
        for q in ('x" } ; DROP','a{b}','<script>'):
            with self.subTest(q=q), self.assertRaises(EvidenceError):
                self.run_src('Q04.01','wikidata',httpx.Response(200,json=WIKIDATA),query=q)
        with self.assertRaises(EvidenceError):
            self.run_src('Q04.01','wikidata',httpx.Response(200,json=WIKIDATA),query='x',options={'class':'Q1 }'})

# Synthetic Wayback payloads (not real data).
CDX_ROWS=[['timestamp','original','digest'],['20190112000000','https://www.acme.com/pricing','D1'],
          ['20220101000000','https://www.acme.com/pricing','D2'],['20230101000000','https://www.acme.com/pricing','D3'],
          ['20260101000000','https://www.acme.com/pricing','D4']]
PAGE=('<html><head><script>var p="$999 fake";</script><style>.x{}</style></head><body><h1>Pricing</h1>'
      '<p>Lite plan $15 /mo billed monthly.</p><p>Plus plan $25 per month. Log in</p></body></html>')

def html_response(text=PAGE):
    return httpx.Response(200,text=text,headers={'content-type':'text/html; charset=utf-8'})

class TestInternetArchive(GlobalCase):
    def test_yearly_captures_and_price_excerpts(self):
        r,hits,seen,events=self.run_src('Q05.01','internet_archive',httpx.Response(200,json=CDX_ROWS),html_response(),
                                        query='acme.com/pricing',options={'from':'2019'})
        p=seen[0].url.params
        self.assertEqual((p['url'],p['collapse'],p['from'],p.get_list('filter')),
                         ('acme.com/pricing','timestamp:4','2019',['statuscode:200','mimetype:text/html']))
        self.assertEqual([str(s.url) for s in seen[1:]],  # first, middle, last of 4 captures
                         ['https://web.archive.org/web/20190112000000id_/https://www.acme.com/pricing',
                          'https://web.archive.org/web/20230101000000id_/https://www.acme.com/pricing',
                          'https://web.archive.org/web/20260101000000id_/https://www.acme.com/pricing'])
        self.assertEqual((r['status'],r['hits']),('completed',3))
        h=hits[0]
        self.assertEqual(h['url'],'https://web.archive.org/web/20190112000000/https://www.acme.com/pricing')
        self.assertEqual((h['published_at'],h['metadata']['independence_group']),('2019-01-12T00:00:00+00:00','web:acme.com'))
        self.assertEqual(h['metadata']['prices'],['$15 /mo','$25 per month'])
        self.assertNotIn('999',h['snippet'])  # script content removed
        self.assertIn('Lite plan $15 /mo billed monthly',h['snippet'])
        self.assertEqual(r['status'],'completed')  # "Log in" on an archived page is not a challenge
        self.assertEqual(len([e for e in events if e['event_type']=='connector_call']),4)
    def test_failed_capture_is_reported_not_fatal(self):
        r,hits,_,_=self.run_src('Q05.01','internet_archive',httpx.Response(200,json=CDX_ROWS[:3]),
                                httpx.Response(302,headers={'location':'https://web.archive.org/web/2/x'}),html_response(),
                                query='acme.com/pricing')
        self.assertEqual((r['status'],r['hits']),('completed',1))
        self.assertIn('Redirección',hits[0]['metadata']['failed_captures'][0])
    def test_no_captures_is_empty(self):
        r,*_=self.run_src('Q05.01','internet_archive',httpx.Response(200,json=[]),query='acme.com/pricing')
        self.assertEqual(r['status'],'empty')
    def test_page_without_prices(self):
        _,hits,_,_=self.run_src('Q05.01','internet_archive',httpx.Response(200,json=CDX_ROWS[:2]),html_response('<p>Contact sales</p>'),
                                query='acme.com/pricing')
        self.assertIn('Sin precios detectados',hits[0]['snippet']); self.assertEqual(hits[0]['metadata']['price_count'],0)
    def test_query_validation(self):
        for q in ('https://acme.com/pricing','localhost/x','10.0.0.1/admin','acme','acme.com/<x>'):
            with self.subTest(q=q), self.assertRaises(EvidenceError):
                self.run_src('Q05.01','internet_archive',httpx.Response(200,json=CDX_ROWS),query=q)
    def test_split_figures_are_joined(self):
        from marketscope.connectors.internet_archive import price_excerpt
        excerpt,prices,_=price_excerpt('<p>Lite <span>$</span> <b>8</b> <sup>.40</sup> USD/mo</p>')
        self.assertIn('Lite $8.40 USD/mo',excerpt); self.assertEqual(prices,['$8.40 USD/mo'])
    def test_pick_spreads_captures(self):
        from marketscope.connectors.internet_archive import pick
        self.assertEqual(pick(list(range(10)),3),[0,4,9]); self.assertEqual(pick([1,2],3),[1,2]); self.assertEqual(pick(list(range(5)),1),[4])

# Synthetic SDMX-JSON payload (not real data): 2 sizes x 1 year, UNIT_MULT 6 on one observation.
SDMX={'data':{'dataSets':[{'observations':{'0:0:0':[1200,0,0],'0:1:0':[3.5,0,1]}}],
 'structure':{'name':'Demo SBS','dimensions':{'observation':[
    {'id':'REF_AREA','name':'Reference area','values':[{'id':'DEU','name':'Germany'}]},
    {'id':'SIZE_CLASS','name':'Size class','values':[{'id':'_T','name':'Total'},{'id':'S10T249','name':'10 to 249'}]},
    {'id':'TIME_PERIOD','name':'Time period','values':[{'id':'2023','name':'2023'}]}]},
  'attributes':{'observation':[{'id':'OBS_STATUS','values':[{'id':'A'}]},{'id':'UNIT_MULT','values':[{'id':'0'},{'id':'6'}]}]}}}}
FLOW='OECD.SDD.TPS,DSD_SDBSBSC_ISIC4@DF_SDBS_ISIC4,1.0'

class TestOecd(GlobalCase):
    def test_observations_with_explicit_unit_multiplier(self):
        r,hits,seen,_=self.run_src('Q06.01','oecd',httpx.Response(200,json=SDMX),query=FLOW+'/A.DEU.ENTR.J..ENT',options={'start':'2021'})
        self.assertEqual(str(seen[0].url).split('?')[0],f'https://sdmx.oecd.org/public/rest/data/{FLOW}/A.DEU.ENTR.J..ENT')
        self.assertEqual((seen[0].url.params['startPeriod'],seen[0].url.params['dimensionAtObservation']),('2021','AllDimensions'))
        self.assertEqual((r['status'],r['hits']),('completed',2))
        self.assertIn('Size class: Total (_T); Time period: 2023 (2023); valor: 1200.',hits[0]['snippet'])
        self.assertIn('valor: 3.5 × 10^6',hits[1]['snippet'])
        self.assertEqual(hits[1]['url'],f'https://sdmx.oecd.org/public/rest/data/{FLOW}/DEU.S10T249?startPeriod=2023&endPeriod=2023')
        self.assertEqual((hits[1]['metadata']['unit_mult'],hits[1]['metadata']['independence_group']),('6','oecd'))
    def test_no_results_404_is_empty(self):
        r,*_=self.run_src('Q06.01','oecd',httpx.Response(404,text='NoResultsFound',headers={'content-type':'text/plain'}),query=FLOW+'/A.XXX')
        self.assertEqual(r['status'],'empty')
    def test_query_validation_and_no_last_option(self):
        for q in ('not a flow/A.DEU',FLOW,FLOW+'/....',FLOW+'/A.DEU;DROP'):
            with self.subTest(q=q), self.assertRaises(EvidenceError):
                self.run_src('Q06.01','oecd',httpx.Response(200,json=SDMX),query=q)
        with self.assertRaises(EvidenceError):  # lastNObservations makes the API answer 413
            self.run_src('Q06.01','oecd',httpx.Response(200,json=SDMX),query=FLOW+'/A.DEU',options={'last':'1'})

CENSUS=[['NAME','ESTAB','NAICS2017','us'],['United States','42000','5112','1']]

class TestUsCensus(GlobalCase):
    def setUp(self):
        super().setUp(); os.environ['MARKETSCOPE_CENSUS_KEY']='census-key-1'
    def tearDown(self):
        os.environ.pop('MARKETSCOPE_CENSUS_KEY',None); super().tearDown()
    def run_census(self,*responses,options=None,query='2022/cbp'):
        return self.run_src('Q06.01','us_census',*responses,query=query,
                            options=options or {'get':'NAME,ESTAB','filters':'NAICS2017=5112'},geography='US')
    def test_rows_with_labels_and_key_redacted(self):
        r,hits,seen,events=self.run_census(httpx.Response(200,json=CENSUS),httpx.Response(200,json={'name':'ESTAB','label':'Number of establishments'}))
        p=seen[0].url.params
        self.assertEqual((p['get'],p['for'],p['NAICS2017'],p['key']),('NAME,ESTAB','us:*','5112','census-key-1'))
        self.assertEqual(str(seen[1].url),'https://api.census.gov/data/2022/cbp/variables/ESTAB.json')
        self.assertEqual((r['status'],r['hits']),('completed',1))
        h=hits[0]
        self.assertIn('Number of establishments (ESTAB): 42000',h['snippet'])
        self.assertNotIn('census-key-1',str(events)+str(hits))
        self.assertTrue(h['url'].endswith('for=us%3A1')); self.assertNotIn('key=',h['url'])
    def test_invalid_key_redirect_explained(self):
        r,*_=self.run_census(httpx.Response(302,headers={'location':'https://api.census.gov/data/invalid_key.html'}))
        self.assertEqual(r['status'],'failed'); self.assertIn('activación',r['reason'])
    def test_requires_key_and_get_before_any_request(self):
        os.environ.pop('MARKETSCOPE_CENSUS_KEY')
        with self.assertRaisesRegex(EvidenceError,'key_signup'): self.run_census(httpx.Response(200,json=CENSUS))
        os.environ['MARKETSCOPE_CENSUS_KEY']='k'
        with self.assertRaisesRegex(EvidenceError,"requiere options \\['get'\\]"):
            self.run_src('Q06.01','us_census',httpx.Response(200,json=CENSUS),query='2022/cbp',options={'for':'us:*'},geography='US')
        for q in ('2022','../x','2022/cbp?x=1'):
            with self.subTest(q=q), self.assertRaises(EvidenceError): self.run_census(httpx.Response(200,json=CENSUS),query=q)

# Synthetic HN Algolia payload (not real data).
HN={'nbHits':2,'hits':[
    {'objectID':'101','_tags':['comment','author_alice'],'author':'alice','comment_text':'Invoicing tools are <i>too</i> complex for freelancers.',
     'story_title':'Ask HN: Billing pain','created_at':'2026-01-02T03:04:05Z','story_id':100},
    {'objectID':'102','_tags':['story'],'author':'bob','title':'Show HN: Tiny invoicing app','url':'https://example.org','points':12,
     'num_comments':3,'created_at':'2026-02-01T00:00:00Z'},
    {'objectID':'x','title':'bad id'},
]}

class TestHackerNews(GlobalCase):
    def test_comments_and_stories_without_author_names(self):
        r,hits,seen,_=self.run_src('Q01.01','hackernews',httpx.Response(200,json=HN),query='invoicing freelancers',options={'since':'2026-01-01'})
        p=seen[0].url.params
        self.assertEqual((p['query'],p['tags'],p['numericFilters']),('invoicing freelancers','(story,comment)','created_at_i>1767225600'))
        self.assertEqual((r['status'],r['hits']),('completed',2))
        c,s=hits
        self.assertEqual(c['url'],'https://news.ycombinator.com/item?id=101')
        self.assertEqual(c['snippet'],'Comentario en «Ask HN: Billing pain». Invoicing tools are too complex for freelancers.')
        self.assertEqual((s['title'],s['metadata']['points']),('Show HN: Tiny invoicing app',12))
        self.assertTrue(c['metadata']['independence_group'].startswith('hackernews:author:'))
        self.assertNotIn('alice',json.dumps(hits)); self.assertNotIn('bob',json.dumps(hits))
    def test_sort_by_date_endpoint(self):
        _,_,seen,_=self.run_src('Q01.01','hackernews',httpx.Response(200,json=HN),query='invoicing',options={'sort':'date','tags':'comment'})
        self.assertTrue(str(seen[0].url).startswith('https://hn.algolia.com/api/v1/search_by_date?'))

# Synthetic Mastodon payload (not real data).
TOOTS=[{'id':'1','url':'https://mastodon.social/@someone/1','visibility':'public','reblog':None,'created_at':'2026-11-16T10:00:00.000Z',
        'content':'<p>Chasing unpaid <b>invoices</b> again</p>','language':'en','account':{'id':'77','username':'someone'},
        'replies_count':1,'reblogs_count':0,'favourites_count':2,'tags':[{'name':'freelance'}]},
       {'id':'2','url':'https://mastodon.social/@x/2','visibility':'unlisted','reblog':None,'content':'<p>private-ish</p>','account':{'id':'8'}},
       {'id':'3','url':'https://mastodon.social/@y/3','visibility':'public','reblog':{'id':'9'},'content':'<p>boost</p>','account':{'id':'9'}}]

class TestMastodon(GlobalCase):
    def test_public_original_posts_per_hashtag(self):
        r,hits,seen,_=self.run_src('Q01.01','mastodon',httpx.Response(200,json=TOOTS),httpx.Response(404,json={}),
                                   query='Freelance|nohashtag')
        self.assertEqual([str(s.url) for s in seen],['https://mastodon.social/api/v1/timelines/tag/freelance?limit=40',
                                                    'https://mastodon.social/api/v1/timelines/tag/nohashtag?limit=40'])
        self.assertEqual((r['status'],r['hits']),('completed',1))  # unlisted and boosts dropped
        h=hits[0]
        self.assertEqual((h['snippet'],h['metadata']['independence_group']),('Chasing unpaid invoices again','mastodon:mastodon.social:account:77'))
        self.assertEqual(h['metadata']['missing_tags'],['nohashtag'])
        self.assertNotIn('"someone"',json.dumps(h['metadata']))
    def test_instance_allowlist_and_validation(self):
        _,_,seen,_=self.run_src('Q01.01','mastodon',httpx.Response(200,json=TOOTS),query='freelance',options={'instance':'fosstodon.org'})
        self.assertEqual(seen[0].url.host,'fosstodon.org')
        with self.assertRaises(EvidenceError):
            self.run_src('Q01.01','mastodon',httpx.Response(200,json=TOOTS),query='freelance',options={'instance':'evil.example'})
        for q in ('#freelance','a|b|c|d','free lance',''):
            with self.subTest(q=q), self.assertRaises(EvidenceError):
                self.run_src('Q01.01','mastodon',httpx.Response(200,json=TOOTS),query=q)

BSKY_POSTS={'hitsTotal':1,'posts':[
    {'uri':'at://did:plc:abc123/app.bsky.feed.post/3kxyz','record':{'text':'Invoicing  apps are overpriced','createdAt':'2026-10-01T09:00:00.000Z','langs':['en']},
     'author':{'handle':'person.bsky.social'},'likeCount':5,'replyCount':1,'repostCount':0},
    {'uri':'bad','record':{'text':'x'}}]}

class TestBluesky(GlobalCase):
    def setUp(self):
        super().setUp()
        os.environ.update(MARKETSCOPE_BLUESKY_HANDLE='me.bsky.social',MARKETSCOPE_BLUESKY_APP_PASSWORD='app-pass-123')
    def tearDown(self):
        for k in ('MARKETSCOPE_BLUESKY_HANDLE','MARKETSCOPE_BLUESKY_APP_PASSWORD'): os.environ.pop(k,None)
        super().tearDown()
    def test_session_then_search_with_secrets_never_stored(self):
        r,hits,seen,events=self.run_src('Q01.01','bluesky',httpx.Response(200,json={'accessJwt':'jwt-secret'}),
                                        httpx.Response(200,json=BSKY_POSTS),query='invoicing apps',options={'lang':'en','sort':'latest'})
        self.assertEqual((seen[0].method,str(seen[0].url)),('POST','https://bsky.social/xrpc/com.atproto.server.createSession'))
        self.assertEqual(json.loads(seen[0].content),{'identifier':'me.bsky.social','password':'app-pass-123'})
        self.assertEqual((seen[1].headers['authorization'],seen[1].url.params['q'],seen[1].url.params['sort']),('Bearer jwt-secret','invoicing apps','latest'))
        self.assertEqual((r['status'],r['hits']),('completed',1))
        h=hits[0]
        self.assertEqual((h['url'],h['snippet']),('https://bsky.app/profile/did:plc:abc123/post/3kxyz','Invoicing apps are overpriced'))
        self.assertEqual(h['metadata']['independence_group'],'bluesky:did:plc:abc123')
        dump=str(events)+json.dumps(hits)
        for secret in ('app-pass-123','jwt-secret','person.bsky.social'): self.assertNotIn(secret,dump)
        self.assertEqual(len([e for e in events if e['event_type']=='connector_call']),2)
    def test_wrong_password_blocks(self):
        r,*_=self.run_src('Q01.01','bluesky',httpx.Response(401,json={'error':'AuthenticationRequired'}),query='x')
        self.assertEqual(r['status'],'blocked')
    def test_requires_credentials_before_any_request(self):
        os.environ.pop('MARKETSCOPE_BLUESKY_APP_PASSWORD')
        with self.assertRaisesRegex(EvidenceError,'contraseña de aplicación'):
            self.run_src('Q01.01','bluesky',httpx.Response(200,json={}),query='x')

class TestPeriods(unittest.TestCase):
    def test_complete_periods_only(self):
        from datetime import date
        from marketscope.connectors.base import period_ranges
        today=date(2026,11,16)
        self.assertEqual([l for l,_,_ in period_ranges('year',3,today)],['2023','2024','2025'])
        self.assertEqual([l for l,_,_ in period_ranges('quarter',3,today)],['2026-Q1','2026-Q2','2026-Q3'])
        self.assertEqual([l for l,_,_ in period_ranges('month',3,today)],['2026-08','2026-09','2026-10'])
        self.assertEqual([l for l,_,_ in period_ranges('month',2,date(2026,1,5))],['2025-11','2025-12'])
        self.assertEqual([l for l,_,_ in period_ranges('quarter',1,date(2026,2,1))],['2025-Q4'])
        _,s,e=period_ranges('quarter',1,today)[0]
        self.assertEqual((s.isoformat(),e.isoformat()),('2026-07-01T00:00:00+00:00','2026-10-01T00:00:00+00:00'))

class TestTrendFormatting(unittest.TestCase):
    def test_small_shares_keep_three_significant_digits(self):
        from marketscope.connectors.base import trend_hit
        h=trend_hit('gdelt','x','Artículos','quarter',[('2025-Q4',111,15176028),('2026-Q2',146,13217939),('2026-Q3',0,10)],
                    'https://api.gdeltproject.org/x',True,'2026-10-01T00:00:00+00:00')
        self.assertIn('111 de 15176028 (0.000731 %)',h.snippet)
        self.assertIn('146 de 13217939 (0.00110 %)',h.snippet)
        self.assertIn('0 de 10 (0 %)',h.snippet)
        self.assertIn('por trimestre',h.snippet)

class TrendCase(GlobalCase):
    def counting(self,counts):
        """Responder returning successive counts in the shape each API uses."""
        it=iter(counts)
        return lambda request: httpx.Response(200,json=next(it))

class TestTrendMode(TrendCase):
    def test_hackernews_normalized_series(self):
        with mock.patch('marketscope.connectors.base.datetime') as dt:
            dt.now.return_value=datetime(2026,11,16,tzinfo=timezone.utc); dt.side_effect=lambda *a,**k: datetime(*a,**k)
            r,hits,seen,_=self.run_src('Q07.01','hackernews',self.counting([{'nbHits':n} for n in (10,1000,30,1500)]),
                                       query='invoicing',options={'mode':'trend','granularity':'year','periods':'2'})
        self.assertEqual((r['status'],r['hits'],len(seen)),('completed',1,4))
        self.assertEqual([s.url.params['query'] for s in seen],['invoicing','','invoicing',''])
        self.assertEqual(seen[0].url.params['numericFilters'],'created_at_i>=1704067200,created_at_i<1735689600')
        h=hits[0]
        self.assertIn('por año: 2024: 10 de 1000 (1.000 %); 2025: 30 de 1500 (2.000 %). Proporción sobre el total',h['snippet'])
        self.assertEqual((h['metadata']['independence_group'],h['published_at']),('hackernews:trend','2026-01-01T00:00:00+00:00'))
    def test_raw_counts_are_labelled(self):
        _,hits,seen,_=self.run_src('Q07.01','hackernews',self.counting([{'nbHits':5},{'nbHits':7}]),
                                   query='invoicing',options={'mode':'trend','periods':'2','normalize':'no'})
        self.assertEqual(len(seen),2); self.assertIn('Recuento bruto',hits[0]['snippet'])
    def test_stackexchange_uses_questions_total(self):
        r,hits,seen,_=self.run_src('Q01.01','stackexchange',self.counting([{'total':20},{'total':1000}]),
                                   query='invoice',options={'mode':'trend','periods':'1','site':'softwarerecs'})
        self.assertEqual([str(s.url).split('?')[0] for s in seen],['https://api.stackexchange.com/2.3/search/advanced',
                                                                 'https://api.stackexchange.com/2.3/questions'])
        self.assertEqual((seen[0].url.params['filter'],seen[0].url.params['site']),('total','softwarerecs'))
        self.assertIn('20 de 1000 (2.000 %)',hits[0]['snippet'])
    def test_openalex_group_by_year_and_monthly_rejected(self):
        groups=lambda d: {'group_by':[{'key':k,'count':v} for k,v in d.items()]}
        r,hits,seen,_=self.run_src('Q07.01','openalex',self.counting([groups({'2025':40}),groups({'2024':900,'2025':1000})]),
                                   query='electronic invoicing',options={'mode':'trend','periods':'2'})
        self.assertEqual(seen[0].url.params['group_by'],'publication_year'); self.assertNotIn('search',seen[1].url.params)
        self.assertIn('0 de 900',hits[0]['snippet']); self.assertIn('40 de 1000 (4.000 %)',hits[0]['snippet'])
        with self.assertRaises(EvidenceError):
            self.run_src('Q07.01','openalex',self.counting([]),query='x',options={'mode':'trend','granularity':'month'})

GDELT_TL={'timeline':[{'series':'Article Count','data':[
    {'date':'20260801T000000Z','value':3,'norm':1000},{'date':'20260815T000000Z','value':2,'norm':1000},
    {'date':'20261001T000000Z','value':7,'norm':2000},{'date':'bad','value':1}]}]}

class TestGdelt(TrendCase):
    def run_gdelt(self,payload=GDELT_TL,options=None,status=200,query='"electronic invoicing"'):
        with mock.patch('marketscope.connectors.base.datetime') as dt:
            dt.now.return_value=datetime(2026,11,16,tzinfo=timezone.utc); dt.side_effect=lambda *a,**k: datetime(*a,**k)
            return self.run_src('Q07.01','gdelt',httpx.Response(status,json=payload),query=query,
                                options=options or {'granularity':'month','periods':'3'})
    def test_one_request_summed_into_months_with_missing_period(self):
        r,hits,seen,_=self.run_gdelt()
        p=seen[0].url.params
        self.assertEqual((p['mode'],p['startdatetime'],p['enddatetime']),('TimelineVolRaw','20260801000000','20261101000000'))
        self.assertEqual((r['status'],r['hits'],len(seen)),('completed',1,1))
        h=hits[0]
        self.assertIn('2026-08: 5 de 2000 (0.250 %); 2026-10: 7 de 2000 (0.350 %)',h['snippet'])
        self.assertEqual(h['metadata']['missing_periods'],['2026-09'])  # missing, never zero
    def test_lang_country_and_no_data(self):
        _,_,seen,_=self.run_gdelt(options={'granularity':'month','periods':'1','lang':'spanish','country':'SP'})
        self.assertEqual(seen[0].url.params['query'],'"electronic invoicing" sourcelang:spanish sourcecountry:SP')
        r,*_=self.run_gdelt({'timeline':[{'data':[]}]})
        self.assertEqual(r['status'],'empty')
    def test_rate_limit_is_retryable_and_query_validated(self):
        r,*_=self.run_gdelt(status=429)
        self.assertEqual(r['status'],'failed')
        self.assertGreaterEqual(__import__('marketscope.connectors.gdelt',fromlist=['Gdelt']).Gdelt().policy.min_interval,5)
        with self.assertRaises(EvidenceError): self.run_gdelt(query='x; DROP <script>')

class TestUserAgent(unittest.TestCase):
    def test_contact_only_for_policies_that_declare_it(self):
        with mock.patch.dict(os.environ,{'MARKETSCOPE_CONTACT':'a@b.org','MARKETSCOPE_WIKIMEDIA_CONTACT':'User:X'}):
            self.assertEqual(SafeHttp.user_agent(Policy('s',frozenset(),0,contact_env='MARKETSCOPE_CONTACT')),
                             'MarketScopeResearch/0.3 (a@b.org; research prototype; respects documented API limits)')
            self.assertEqual(SafeHttp.user_agent(Policy('o',frozenset(),0)),
                             'MarketScopeResearch/0.3 (research prototype; respects documented API limits)')

class TestOpenAlexParsing(unittest.TestCase):
    def test_abstract_reconstruction(self):
        self.assertEqual(openalex.abstract_text({'b':[1,3],'a':[0],'c':[2]}),'a b c b')
        self.assertEqual(openalex.abstract_text(None),'')

if __name__=='__main__': unittest.main()
