import itertools
import json
import tempfile
import unittest
from pathlib import Path
from marketscope import evaluator, planner
from marketscope.core import init_db, open_db, ingest, start_run, EvidenceError
from marketscope.evaluator import unsupported_numbers
from marketscope.studies import create_study

_n = itertools.count()
STUDY = dict(market_definition='Software de facturación para autónomos', geography='ES', segment='autónomos', language='es')

class EvalCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db_ctx=open_db(init_db(Path(self.tmp.name)/'ev.sqlite'),write=True); self.db=self.db_ctx.__enter__()
    def tearDown(self):
        self.db_ctx.__exit__(None,None,None); self.tmp.cleanup()
    def study(self, question='Q01.01', **kw):
        s=create_study(self.db,name=f's{next(_n)}',**{**STUDY,**kw})
        planner.plan_research(self.db,start_run(self.db,question,10,s['study_id']))  # plan gives gaps and population warnings
        return s['study_id']
    def evidence(self, study_id, question='Q01.01', group='g1', kind='opinion', source_id='stackexchange', hit=True,
                 excerpt='Users report that invoicing tools are too complex', **extra):
        i=next(_n)
        rec={'question_id':question,'study_id':study_id,'claim':f'claim {i}','source_url':f'https://example.org/{i}',
             'source_title':'t','source_published_at':'2026-01-01T00:00:00+00:00','retrieved_at':'2026-11-16T00:00:00+00:00',
             'method':'official_api' if hit else 'agent_research','evidence_kind':kind,'evidence_excerpt':excerpt,
             'research_query':'q','source_access_status':'needs_review','source_independence_group':group,
             'source_id':source_id,**({'hit_id':f'h{i}'} if hit else {}),**extra}
        return ingest(self.db,rec)[0]
    def verdict(self, study_id, question='Q01.01'):
        return evaluator.evaluate_question(self.db,study_id,question)

class TestLadder(EvalCase):
    def test_no_evidence_is_insufficient(self):
        ev=self.verdict(self.study())
        self.assertEqual(ev['allowed_verdict'],'INSUFFICIENT_EVIDENCE')
        self.assertIn('0 grupos',ev['reasons'][0])
    def test_population_not_defined(self):
        s=self.study(segment='unknown')
        self.evidence(s,group='a'); self.evidence(s,group='b')
        ev=self.verdict(s)
        self.assertEqual(ev['allowed_verdict'],'INSUFFICIENT_EVIDENCE'); self.assertIn('Población no definida',ev['reasons'][0])
    def test_one_group_is_not_independent_corroboration(self):
        s=self.study()
        for _ in range(5): self.evidence(s,group='stackexchange:stackoverflow:user:1')  # 5 posts, one author
        ev=self.verdict(s)
        self.assertEqual((ev['allowed_verdict'],ev['metrics']['n_evidence'],ev['metrics']['independent_groups']),('INSUFFICIENT_EVIDENCE',5,1))
    def test_opinions_from_warned_sources_are_weak(self):
        s=self.study()
        self.evidence(s,group='a'); self.evidence(s,group='b',source_id='github_issues')
        ev=self.verdict(s)
        self.assertEqual(ev['allowed_verdict'],'WEAK_SUPPORT')
        self.assertTrue(any('primary_or_verified_secondary' in r for r in ev['reasons']))
        self.assertTrue(any('aviso de población' in r for r in ev['reasons']))
    def test_primary_with_checked_excerpt_is_moderate(self):
        s=self.study()
        self.evidence(s,group='a'); self.evidence(s,group='ted:buyer:x',kind='primary',source_id='ted')
        ev=self.verdict(s)
        self.assertEqual(ev['allowed_verdict'],'MODERATE_SUPPORT'); self.assertTrue(ev['metrics']['evidence_requirement_met'])
    def test_candidate_secondary_does_not_meet_requirement(self):
        s=self.study()
        self.evidence(s,group='a',kind='secondary',source_id='openalex'); self.evidence(s,group='b',kind='secondary',source_id='openalex')
        self.assertEqual(self.verdict(s)['allowed_verdict'],'WEAK_SUPPORT')
    def test_agent_research_without_checked_excerpt_is_weak(self):
        s=self.study()
        self.evidence(s,group='a',kind='primary',hit=False,source_id=None); self.evidence(s,group='b',hit=False,source_id=None)
        ev=self.verdict(s)
        self.assertEqual(ev['allowed_verdict'],'WEAK_SUPPORT')
        self.assertTrue(any('extracto comprobado' in r for r in ev['reasons']))
    def test_contradiction_defers(self):
        s=self.study()
        self.evidence(s,group='a'); self.evidence(s,group='b',kind='primary',source_id='ted',contradictions=['Otra fuente dice lo contrario'])
        self.assertEqual(self.verdict(s)['allowed_verdict'],'DEFER')
    def test_quantitative_question_with_only_opinions(self):
        s=self.study('Q06.01')
        self.evidence(s,'Q06.01',group='a'); self.evidence(s,'Q06.01',group='b')
        ev=self.verdict(s,'Q06.01')
        self.assertEqual(ev['allowed_verdict'],'INSUFFICIENT_EVIDENCE'); self.assertIn('solo por opiniones',ev['reasons'][0])
    def test_rejected_evidence_ignored(self):
        s=self.study()
        a=self.evidence(s,group='a'); self.evidence(s,group='b')
        self.db.execute("UPDATE evidence SET status='rejected' WHERE evidence_id=?",(a,)); self.db.commit()
        self.assertEqual(self.verdict(s)['metrics']['independent_groups'],1)
    def test_supported_only_with_human_verification(self):
        s=self.study()
        for g in ('ted:buyer:a','ted:buyer:b'): self.evidence(s,group=g,kind='primary',source_id='ted')
        self.assertEqual(self.verdict(s)['allowed_verdict'],'MODERATE_SUPPORT')
        # Simulate the independent human review (the only way to reach verified).
        for (eid,payload) in self.db.execute('SELECT evidence_id,payload FROM evidence').fetchall():
            p=json.loads(payload); p['status']='verified'
            self.db.execute("UPDATE evidence SET status='verified',payload=? WHERE evidence_id=?",(json.dumps(p),eid))
        self.db.commit()
        self.assertEqual(self.verdict(s)['allowed_verdict'],'SUPPORTED')
    def test_gaps_and_blocked_sources_reported(self):
        m=self.verdict(self.study())['metrics']
        self.assertEqual(m['gaps'],['entrevistas'])
        self.assertEqual(m['min_independent_groups'],2)

UNCERTAINTY={'coverage_limits':'Solo fuentes públicas en inglés','biases':'Autoselección de usuarios técnicos',
             'missing_evidence':['entrevistas con autónomos'],'contradictions':'Ninguna declarada'}

class TestSubmitConclusion(EvalCase):
    def moderate(self):
        s=self.study()
        a=self.evidence(s,group='a',excerpt='Invoicing takes 5 hours per week for 1,200 freelancers')
        b=self.evidence(s,group='ted:buyer:x',kind='primary',source_id='ted',excerpt='Suministro de software de facturación')
        return s,[a,b]
    def submit(self, s, **kw):
        rec={'verdict':'MODERATE_SUPPORT','text':'Hay señales independientes de que facturar consume tiempo a los autónomos.',
             'cited_evidence_ids':kw.pop('cited'),'uncertainty':UNCERTAINTY,**kw}
        return evaluator.submit_conclusion(self.db,s,'Q01.01',rec)
    def test_valid_conclusion_stored_as_candidate(self):
        s,ids=self.moderate()
        r=self.submit(s,cited=ids,text='Un testimonio cifra en 5 horas semanales el tiempo de facturación (1.200 autónomos), en 2026.')
        self.assertEqual((r['status'],r['allowed_verdict']),('candidate','MODERATE_SUPPORT'))
        c=evaluator.list_conclusions(self.db,s)
        self.assertEqual((len(c),c[0]['stale']),(1,False))
    def test_verdict_above_allowed_rejected(self):
        s,ids=self.moderate()
        with self.assertRaisesRegex(EvidenceError,'supera el permitido'): self.submit(s,cited=ids,verdict='SUPPORTED')
    def test_more_conservative_verdict_allowed(self):
        s,ids=self.moderate()
        with self.assertRaisesRegex(EvidenceError,'1 grupo'):  # support verdicts still need enough groups
            self.submit(s,cited=ids[:1],verdict='WEAK_SUPPORT')
        self.assertEqual(self.submit(s,cited=ids,verdict='WEAK_SUPPORT')['verdict'],'WEAK_SUPPORT')
    def test_citations_must_belong_to_study_and_question(self):
        s,ids=self.moderate(); other=self.study()
        foreign=self.evidence(other,group='z')
        with self.assertRaisesRegex(EvidenceError,'inexistentes'): self.submit(s,cited=ids+[foreign])
        with self.assertRaisesRegex(EvidenceError,'inexistentes'): self.submit(s,cited=ids+['nope'])
    def test_uncertainty_and_gaps_required(self):
        s,ids=self.moderate()
        with self.assertRaisesRegex(EvidenceError,'uncertainty requiere'): self.submit(s,cited=ids,uncertainty={'biases':'x'})
        with self.assertRaisesRegex(EvidenceError,'vacíos'): self.submit(s,cited=ids,uncertainty={**UNCERTAINTY,'biases':' '})
        with self.assertRaisesRegex(EvidenceError,'entrevistas'):
            self.submit(s,cited=ids,uncertainty={**UNCERTAINTY,'missing_evidence':['encuestas']})
    def test_fabricated_numbers_rejected(self):
        s,ids=self.moderate()
        for text in ('El 40 % de los autónomos pierde 5 horas facturando.','Facturar cuesta 10 horas semanales a los autónomos.'):
            with self.subTest(text=text), self.assertRaisesRegex(EvidenceError,'Cifras sin respaldo'):
                self.submit(s,cited=ids,text=text)
    def test_abstention_needs_no_citations_but_must_explain(self):
        s=self.study()
        r=evaluator.submit_conclusion(self.db,s,'Q01.01',{'verdict':'INSUFFICIENT_EVIDENCE',
            'text':'No hay testimonios independientes suficientes; faltan entrevistas.','cited_evidence_ids':[],'uncertainty':UNCERTAINTY})
        self.assertEqual(r['verdict'],'INSUFFICIENT_EVIDENCE')
        with self.assertRaisesRegex(EvidenceError,'supera'):
            evaluator.submit_conclusion(self.db,s,'Q01.01',{'verdict':'WEAK_SUPPORT','text':'x'*30,'cited_evidence_ids':[],'uncertainty':UNCERTAINTY})
    def test_new_evidence_makes_conclusion_stale(self):
        s,ids=self.moderate()
        self.submit(s,cited=ids)
        self.evidence(s,group='c')
        self.assertTrue(evaluator.list_conclusions(self.db,s)[0]['stale'])
    def test_invalid_records(self):
        s,ids=self.moderate()
        for bad in ({'verdict':'GREAT'},{'text':'corto'},{'cited':'notalist'}):
            with self.subTest(bad=bad), self.assertRaises(EvidenceError):
                kw={'cited':ids,**bad}; self.submit(s,**kw)

class TestNumbers(unittest.TestCase):
    def test_exemptions_and_separators(self):
        ex=['17438 establecimientos; valor: 20.27; 1,200 usuarios']
        self.assertEqual(unsupported_numbers('17.438 establecimientos y 17,438 en total',ex),[])
        self.assertEqual(unsupported_numbers('valor 20.27 en 2025 (Q06.01)',ex),[])
        self.assertEqual(unsupported_numbers('1.200 usuarios',ex),[])
        self.assertEqual(unsupported_numbers('crece un 15 % hasta 20.3',ex),['15','20.3'])
        self.assertEqual(unsupported_numbers('1.5 veces',['15 usuarios']),['1.5'])  # no digit-only matching
        self.assertEqual(unsupported_numbers('3 fuentes',[],['3']),[])

if __name__=='__main__': unittest.main()
