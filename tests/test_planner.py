import json
import tempfile
import unittest
from pathlib import Path
from marketscope import planner
from marketscope.core import (init_db, open_db, start_run, log_research_event, run_report, load_questions, load_sources,
                              EvidenceError)
from marketscope.studies import create_study

ES=dict(name='es',market_definition='Software de facturación para autónomos',geography='ES',segment='autónomos',language='es')

class PlannerCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=init_db(Path(self.tmp.name)/'p.sqlite')
    def tearDown(self): self.tmp.cleanup()
    def run_for(self,db,question_id,budget=10,**study):
        s=create_study(db,**{**ES,**study})
        return start_run(db,question_id,budget,s['study_id'])
    def plan(self,question_id,budget=10,**study):
        with open_db(self.path,write=True) as db:
            return planner.plan_research(db,self.run_for(db,question_id,budget,**study))

class TestBuildPlan(PlannerCase):
    def test_sources_match_question_dimension(self):
        dim=load_questions()['Q01.01']['dimension_id']; sources=load_sources()
        p=self.plan('Q01.01')
        considered=[s['source_id'] for s in p['steps']]+[x['source_id'] for x in p['excluded_sources']]
        self.assertEqual(sorted(considered),sorted(k for k,v in sources.items() if dim in v['dimension_ids'].split()))
    def test_restricted_access_never_planned(self):
        p=self.plan('Q04.01')
        planned={s['source_id'] for s in p['steps']}
        for sid in ('g2','crunchbase','similarweb','reddit','producthunt'):
            self.assertNotIn(sid,planned)
        self.assertIn('crunchbase',{x['source_id'] for x in p['excluded_sources']})
        self.assertTrue(all(x['reason'] for x in p['excluded_sources']))
    def test_geography_filters(self):
        es={s['source_id']:s['coverage'] for s in self.plan('Q06.01',geography='ES')['steps']}
        self.assertEqual((es['ine'],es['eurostat']),('full','full'))
        self.assertEqual(es['sec_edgar'],'partial')  # competitor filings, kept outside the US with a scope note
        self.assertNotIn('usaspending',es)
        us={s['source_id'] for s in self.plan('Q06.01',name='us',geography='US')['steps']}
        self.assertTrue({'sec_edgar','usaspending'}<=us); self.assertFalse({'ine','eurostat'}&us)
        eu={s['source_id']:s['coverage'] for s in self.plan('Q06.01',name='eu',geography='EU')['steps']}
        self.assertEqual((eu['eurostat'],eu['ine']),('full','partial'))
        unknown=self.plan('Q06.01',name='unk',geography='unknown')
        unknown_steps={s['source_id']:s for s in unknown['steps']}
        self.assertFalse({'ine','eurostat','usaspending'}&set(unknown_steps))
        self.assertEqual(unknown_steps['sec_edgar']['coverage'],'partial')  # competitor data is geography-independent
        self.assertFalse(unknown['population_defined'])
    def test_global_sources_marked_unfiltered_outside_global_studies(self):
        es=self.plan('Q01.01')
        self.assertEqual({s['coverage'] for s in es['steps']},{'global_unfiltered'})
        self.assertIn('no son representativos',es['coverage_notes'])
        glob=self.plan('Q01.01',name='g',geography='GLOBAL')
        cov={s['source_id']:s['coverage'] for s in glob['steps']}
        self.assertEqual((cov['github_issues'],cov['cfpb']),('full','partial'))  # regional sources are partial
        self.assertNotIn('global_unfiltered',cov.values()); self.assertIsNone(glob['coverage_notes'])
        unk=self.plan('Q01.01',name='u',geography='unknown')
        self.assertEqual({s['coverage'] for s in unk['steps']},{'global_unfiltered'})
    def test_population_warning_for_non_technical_segment(self):
        p=self.plan('Q01.01')
        warned={s['source_id'] for s in p['steps'] if s['population_warning']}
        self.assertTrue({'github_issues','stackexchange','hackernews','bluesky','mastodon'}<=warned)
        self.assertEqual(len(p['population_warnings']),len(warned))
        self.assertTrue(all('nunca como testimonio representativo' in w for w in p['population_warnings']))
        dev=self.plan('Q01.01',name='d',segment='desarrolladores de software')
        # Technical sources match a developer segment; social networks stay self-selected whatever the segment.
        self.assertEqual({w.split(':')[0] for w in dev['population_warnings']},{'bluesky','mastodon'})
        research=self.plan('Q07.01',name='r')
        self.assertIsNone({s['source_id']:s for s in research['steps']}['openalex']['population_warning'])
    def test_license_review_and_notes_in_plan(self):
        p=self.plan('Q01.01')
        reviews={s['source_id']:s['license_review'] for s in p['steps']}
        self.assertEqual(reviews['github_issues'],load_sources()['github_issues']['license_review'])
        restricted={s for s,r in reviews.items() if r=='restricted'}
        self.assertEqual({n.split(':')[0] for n in p['license_notes']},restricted)
    def test_keywords_in_terms(self):
        p=self.plan('Q01.01',keywords_en='invoicing software freelancers')
        self.assertEqual(p['steps'][0]['query_terms']['keywords_en'],'invoicing software freelancers')
    def test_gaps_and_hints(self):
        self.assertEqual(self.plan('Q01.01')['gaps_out_of_scope'],['entrevistas'])
        hints=self.plan('Q04.01',name='b')['source_hints']
        self.assertIn('repositorios',hints)  # 'epo' inside the word must not match EPO
        self.assertNotIn('World Bank',self.plan('Q06.01',name='c')['source_hints'])
    def test_min_groups_from_definition(self):
        self.assertEqual(self.plan('Q01.01')['min_independent_groups'],2)
    def test_budget_allocation(self):
        p=self.plan('Q01.01',budget=2)
        n=len(p['steps']); self.assertGreater(n,2)
        self.assertEqual([s['budget'] for s in p['steps']],[1,1]+[0]*(n-2))
        self.assertEqual([s['status'] for s in p['steps']],['pending','pending']+['deferred']*(n-2))
        self.assertEqual(sum(s['budget'] for s in self.plan('Q01.01',budget=10,name='x')['steps']),10)
    def test_connector_steps_first_and_get_extra_budget(self):
        q,s=load_questions()['Q01.01'],{**ES,'study_id':'x'}
        steps=planner.build_plan({'run_id':'r','query_budget':9},q,s,load_sources(),frozenset({'stackexchange'}))['steps']
        self.assertEqual((steps[0]['source_id'],steps[0]['route']),('stackexchange','connector'))
        self.assertEqual({x['route'] for x in steps[1:]},{'agent'})
        # One query each, then the remainder goes to the only connector step.
        self.assertEqual([x['budget'] for x in steps],[9-len(steps)+1]+[1]*(len(steps)-1))
    def test_deterministic(self):
        q=load_questions()['Q01.01']; s={**ES,'study_id':'x'}; run={'run_id':'r','query_budget':5}
        a=planner.build_plan(run,q,s,load_sources()); b=planner.build_plan(run,q,s,load_sources())
        self.assertEqual(json.dumps(a,sort_keys=True),json.dumps(b,sort_keys=True))
    def test_query_terms_omit_unknowns(self):
        terms=self.plan('Q01.01',segment='unknown',geography='GLOBAL')['steps'][0]['query_terms']
        self.assertNotIn('segment',terms); self.assertNotIn('geography',terms)
        self.assertEqual(terms['market'],ES['market_definition'])

class TestPlanLifecycle(PlannerCase):
    def test_requires_study(self):
        with open_db(self.path,write=True) as db, self.assertRaises(EvidenceError):
            planner.plan_research(db,start_run(db,'Q01.01',3))
    def test_idempotent_and_replan_versions(self):
        with open_db(self.path,write=True) as db:
            run=self.run_for(db,'Q01.01',3)
            self.assertEqual(planner.plan_research(db,run)['plan_version'],1)
            self.assertEqual(planner.plan_research(db,run)['plan_version'],1)
            with self.assertRaises(EvidenceError): planner.replan_research(db,run,' ')
            self.assertEqual(planner.replan_research(db,run,'catálogo actualizado')['plan_version'],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM research_plans WHERE run_id=?',(run,)).fetchone()[0],2)
            self.assertEqual(run_report(db,run)['events'][-1]['event_type'],'replanned')
    def test_next_step_walk(self):
        with open_db(self.path,write=True) as db:
            # Q08.01 (ES): connector steps first, then agent steps (boe, ine).
            run=self.run_for(db,'Q08.01',30); plan=planner.plan_research(db,run)
            routes=[s['route'] for s in plan['steps']]
            self.assertEqual(routes,sorted(routes,key=lambda r:r!='connector'))
            first_connector=plan['steps'][0]['step_id']
            n=planner.next_step(db,run)
            self.assertEqual((n['step']['step_id'],n['remaining_queries'],n['pending_steps']),(first_connector,30,len(routes)))
            with self.assertRaises(EvidenceError): planner.complete_agent_step(db,run,first_connector,'conector, no manual')
            with self.assertRaises(EvidenceError): planner.complete_agent_step(db,run,'boe','')
            log_research_event(db,run,'search',query='factura electrónica obligatoria',step_id='boe')
            n=planner.complete_agent_step(db,run,'boe','2 normas relevantes')
            self.assertEqual((n['step']['step_id'],n['remaining_queries'],n['pending_steps']),(first_connector,29,len(routes)-1))
            with self.assertRaises(EvidenceError): planner.complete_agent_step(db,run,'boe','otra vez')
            for i in range(29): log_research_event(db,run,'search',query=f'q{i}',step_id='ine')
            n=planner.next_step(db,run)
            self.assertEqual((n['done'],n['reason'],n['remaining_queries']),(True,'presupuesto agotado',0))
    def test_closure_survives_replan_on_same_source(self):
        with open_db(self.path,write=True) as db:
            run=self.run_for(db,'Q08.01',30); n_steps=len(planner.plan_research(db,run)['steps'])
            planner.complete_agent_step(db,run,'boe','hecho')
            planner.replan_research(db,run,'cambio')
            n=planner.next_step(db,run)
            # Same steps in v2; boe stays closed from v1.
            self.assertEqual((n['plan_version'],n['pending_steps']),(2,n_steps-1))
            self.assertEqual(len(planner.get_plan(db,run)['steps']),n_steps)
            self.assertEqual(planner._closed_steps(db,run),{'boe'})
    def test_unknown_step_rejected(self):
        with open_db(self.path,write=True) as db:
            run=self.run_for(db,'Q01.01',2); planner.plan_research(db,run)
            with self.assertRaises(EvidenceError): log_research_event(db,run,'search',query='x',step_id='s99')
            with self.assertRaises(EvidenceError): planner.complete_agent_step(db,run,'s99','notas')

if __name__=='__main__': unittest.main()
