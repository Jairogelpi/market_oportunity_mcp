"""Gold metrics: deterministic formulas with lineage, refusals on heterogeneous inputs, derived evidence and attribution."""
import json
import os
import unittest
from unittest import mock
import httpx
from marketscope import gold, evaluator, executor, attribution
from marketscope.core import open_db, EvidenceError, check_catalog
from test_executor import (GlobalCase, TrendCase, CDX_ROWS, html_response, WIKIDATA, QUESTIONS)

FLOW='OECD.SDD.TPS,DSD_SDBSBSC_ISIC4@DF_SDBS_ISIC4,1.0'

def sdmx(areas, values, mult='0', mults=None):
    """Synthetic SDMX-JSON: one observation per area for 2023, enterprises. `mults` gives a UNIT_MULT per area."""
    mults=mults or [mult]*len(areas)
    levels=sorted(set(mults))
    obs={f'{i}:0:0':[v,0,levels.index(mults[i])] for i,v in enumerate(values)}
    return {'data':{'dataSets':[{'observations':obs}],'structure':{'name':'Demo SBS','dimensions':{'observation':[
        {'id':'REF_AREA','name':'Area','values':[{'id':a,'name':a} for a in areas]},
        {'id':'MEASURE','name':'Measure','values':[{'id':'ENTR','name':'Enterprises'}]},
        {'id':'TIME_PERIOD','name':'Time','values':[{'id':'2023','name':'2023'}]}]},
        'attributes':{'observation':[{'id':'OBS_STATUS','values':[{'id':'A'}]},{'id':'UNIT_MULT','values':[{'id':l} for l in levels]}]}}}}

def jsonstat_pct(values, unit='PC_ENT'):
    return {'version':'2.0','label':'AI use','updated':'2026-06-15T11:00:00+0200','id':['unit','geo','time'],'size':[1,1,len(values)],
            'dimension':{'unit':{'label':'Unit','category':{'index':{unit:0},'label':{unit:unit}}},
                         'geo':{'label':'Geo','category':{'index':{'ES':0},'label':{'ES':'Spain'}}},
                         'time':{'label':'Time','category':{'index':{str(2023+i):i for i in range(len(values))},
                                                            'label':{str(2023+i):str(2023+i) for i in range(len(values))}}}},
            'value':{str(i):v for i,v in enumerate(values)}}

class GoldCase(GlobalCase):
    def study_id(self):
        with open_db(self.path) as db: return db.execute('SELECT study_id FROM research_runs ORDER BY created_at DESC').fetchone()[0]
    def oecd(self, areas, values, mult='0', mults=None):
        self.run_src('Q06.01','oecd',httpx.Response(200,json=sdmx(areas,values,mult,mults)),query=FLOW+'/A..ENTR')
        return self.study_id()
    def compute(self, study, metric_id, **params):
        with open_db(self.path,write=True) as db: return gold.compute_metric(db,study,metric_id,params)

UNIVERSE=dict(source='oecd',dataset=FLOW,measure='MEASURE=ENTR',period='2023')

class TestFormulas(GoldCase):
    def test_sum_with_lineage(self):
        s=self.oecd(['DEU','FRA'],[1200,800])
        g=self.compute(s,'G06_01_UNIVERSE',geos=['DE','FR'],**UNIVERSE)
        self.assertEqual((g['value'],g['unit'],len(g['input_ids'])),(2000.0,'organizaciones',2))  # metric unit when the source has none
        self.assertEqual(g['input_groups'],['oecd']); self.assertTrue(g['current_definition']); self.assertFalse(g['reviewed'])
        self.assertEqual(g['detail']['geos'],['DE','FR'])
    def test_sum_refuses_overlap_missing_and_ambiguity(self):
        s=self.oecd(['EU27_2020','DEU'],[25000000,1200])
        with self.assertRaisesRegex(EvidenceError,'solapadas'): self.compute(s,'G06_01_UNIVERSE',geos=['EU27','DE'],**UNIVERSE)
        with self.assertRaisesRegex(EvidenceError,'Faltan observaciones'): self.compute(s,'G06_01_UNIVERSE',geos=['DE','IT'],**UNIVERSE)
        with self.assertRaisesRegex(EvidenceError,'Fuente'): self.compute(s,'G06_01_UNIVERSE',geos=['DE'],**{**UNIVERSE,'source':'gdelt'})
        with self.assertRaisesRegex(EvidenceError,'parámetros'): self.compute(s,'G06_01_UNIVERSE',source='oecd')
    def test_unit_multiplier_mismatch_refused(self):
        s=self.oecd(['DEU','FRA'],[1.2,800],mults=['6','0'])
        with self.assertRaisesRegex(EvidenceError,'multiplicadores'): self.compute(s,'G06_01_UNIVERSE',geos=['DE','FR'],**UNIVERSE)
    def test_sum_uses_scaled_values(self):
        s=self.oecd(['DEU'],[1.2],mult='6')
        self.assertEqual(self.compute(s,'G06_01_UNIVERSE',geos=['DE'],**UNIVERSE)['value'],1200000.0)
    def test_latest_percentage(self):
        self.run_src('Q06.01','eurostat',httpx.Response(200,json=jsonstat_pct([12.5,20.27])),query='isoc_eb_ai',geography='ES')
        g=self.compute(self.study_id(),'G06_01_ADOPTION',source='eurostat',dataset='isoc_eb_ai',measure='isoc_eb_ai',geo='ES')
        self.assertEqual((g['value'],g['unit'],g['detail']['period']),(20.27,'PC_ENT','2024'))
    def test_latest_rejects_non_percentage_units(self):
        self.run_src('Q06.01','eurostat',httpx.Response(200,json=jsonstat_pct([5],unit='NR')),query='isoc_eb_ai',geography='ES')
        with self.assertRaisesRegex(EvidenceError,'No hay observaciones'):
            self.compute(self.study_id(),'G06_01_ADOPTION',source='eurostat',dataset='isoc_eb_ai',measure='isoc_eb_ai',geo='ES')
    def test_count_distinct_unifies_wikidata_website_and_archive_host(self):
        os.environ['MARKETSCOPE_WIKIMEDIA_CONTACT']='User:X'
        try:
            self.run_src('Q04.01','internet_archive',httpx.Response(200,json=CDX_ROWS[:2]),html_response(),query='acme.com/pricing')
            s=self.study_id()
            payload={'results':{'bindings':[dict(WIKIDATA['results']['bindings'][0],site={'value':'https://www.acme.com/'})]}}
            with open_db(self.path,write=True) as db:
                run=db.execute('SELECT run_id FROM research_runs WHERE study_id=?',(s,)).fetchone()[0]
                executor.execute_step(db,run,'wikidata',query='Acme',http=self.http(httpx.Response(200,json=payload))[0])
        finally:
            os.environ.pop('MARKETSCOPE_WIKIMEDIA_CONTACT')
        g=self.compute(s,'G04_01_ALTERNATIVES_FOUND')
        self.assertEqual((g['value'],g['detail']['entities']),(1.0,['acme.com']))

class TestGrowth(TrendCase):
    def test_growth_of_normalized_share(self):
        self.run_src('Q07.01','hackernews',self.counting([{'nbHits':n} for n in (10,1000,30,1500)]),
                     query='invoicing',options={'mode':'trend','granularity':'year','periods':'2'})
        with open_db(self.path,write=True) as db:
            s=db.execute('SELECT study_id FROM research_runs').fetchone()[0]
            g=gold.compute_metric(db,s,'G07_01_INTEREST_GROWTH',{'source':'hackernews','dataset':'hackernews:trend','measure':'share'})
        self.assertAlmostEqual(g['value'],1.0)  # 0.01 -> 0.02
        self.assertEqual(g['detail']['periods'],2); self.assertAlmostEqual(g['detail']['cagr'],1.0)

class TestDerivedEvidence(GoldCase):
    def universe_evidence(self):
        s=self.oecd(['DEU','FRA'],[1200,800])
        g=self.compute(s,'G06_01_UNIVERSE',geos=['DE','FR'],**UNIVERSE)
        with open_db(self.path,write=True) as db:
            r=gold.promote_metric(db,g['gold_id'],'Universo de empresas en DE y FR')
        return s,g,r
    def test_promotion_has_literal_figure_and_input_groups(self):
        s,g,r=self.universe_evidence()
        self.assertIn('G06_01_UNIVERSE = 2000',r['excerpt']); self.assertIn('definición NO revisada',r['excerpt'])
        with open_db(self.path) as db:
            e=json.loads(db.execute('SELECT payload FROM evidence WHERE evidence_id=?',(r['evidence_id'],)).fetchone()[0])
            m=evaluator.evaluate_question(db,s,'Q06.01')['metrics']
        self.assertEqual((e['method'],e['evidence_kind'],e['derived_from_groups'],e['metric_reviewed']),('derived_metric','estimate',['oecd'],False))
        self.assertEqual(m['groups'],['oecd'])  # no new group for the derived figure
        self.assertEqual((m['strong_evidence'],m['unreviewed_derived']),(0,1))
    def test_unreviewed_metric_caps_conclusion_at_weak(self):
        s,g,r=self.universe_evidence()
        with open_db(self.path,write=True) as db:
            # A second independent group so a support verdict is otherwise possible.
            db.execute("UPDATE evidence SET payload=json_set(payload,'$.derived_from_groups',json('[\"oecd\",\"eurostat\"]')) WHERE evidence_id=?",(r['evidence_id'],))
            db.commit()
            u={'coverage_limits':'x','biases':'y','missing_evidence':['encuestas'],'contradictions':'-'}
            rec=lambda v:{'verdict':v,'text':'El universo suma 2000 empresas en DE y FR en 2023.','cited_evidence_ids':[r['evidence_id']],'uncertainty':u}
            with self.assertRaisesRegex(EvidenceError,'supera|no ha revisado'): evaluator.submit_conclusion(db,s,'Q06.01',rec('MODERATE_SUPPORT'))
            allowed=evaluator.evaluate_question(db,s,'Q06.01')['allowed_verdict']
            self.assertIn(allowed,('WEAK_SUPPORT','INSUFFICIENT_EVIDENCE'))
    def test_changed_definition_blocks_promotion(self):
        s=self.oecd(['DEU'],[1200])
        g=self.compute(s,'G06_01_UNIVERSE',geos=['DE'],**UNIVERSE)
        changed={**gold.load_metrics()}; changed['G06_01_UNIVERSE']={**changed['G06_01_UNIVERSE'],'caveats':'otra'}
        with mock.patch.object(gold,'load_metrics',return_value=changed), open_db(self.path,write=True) as db:
            self.assertFalse(gold.get_gold(db,g['gold_id'])['current_definition'])
            with self.assertRaisesRegex(EvidenceError,'cambió'): gold.promote_metric(db,g['gold_id'],'x')

class TestAttribution(GoldCase):
    def test_block_includes_derived_input_sources_and_notes(self):
        s=self.oecd(['DEU'],[1200])
        g=self.compute(s,'G06_01_UNIVERSE',geos=['DE'],**UNIVERSE)
        with open_db(self.path,write=True) as db:
            d=gold.promote_metric(db,g['gold_id'],'Universo DE')
            se={'source_id':'stackexchange','method':'official_api','source_url':'https://stackoverflow.com/q/1'}
            blk=attribution.block([json.loads(db.execute('SELECT payload FROM evidence WHERE evidence_id=?',(d['evidence_id'],)).fetchone()[0]),se,
                                   {'source_url':'https://example.org/page','method':'agent_research'}])
        by={b['source_id']:b for b in blk}
        self.assertEqual(by['oecd']['derived_metrics'],['G06_01_UNIVERSE']); self.assertEqual(by['oecd']['license_review'],'approved')
        self.assertTrue(by['oecd']['urls'] and all(u.startswith('https://sdmx.oecd.org/') for u in by['oecd']['urls']))  # input observations
        self.assertIn('fecha de acceso',by['oecd']['required'])
        self.assertIn('autor',by['stackexchange']['note']); self.assertEqual(by['stackexchange']['urls'],['https://stackoverflow.com/q/1'])
        self.assertEqual(by['web:example.org']['license_review'],'pending')
    def test_list_conclusions_carries_attribution(self):
        s=self.oecd(['DEU'],[1200])
        with open_db(self.path,write=True) as db:
            evaluator.submit_conclusion(db,s,'Q06.01',{'verdict':'INSUFFICIENT_EVIDENCE','text':'Faltan datos de compradores con necesidad.',
                'cited_evidence_ids':[],'uncertainty':{'coverage_limits':'x','biases':'y','missing_evidence':['encuestas'],'contradictions':'-'}})
            self.assertEqual(evaluator.list_conclusions(db,s)[0]['attribution'],[])

class TestMetricsCatalog(unittest.TestCase):
    def test_shipped_metrics_valid_and_honest(self):
        self.assertEqual(check_catalog(),[])
        for mid,m in gold.load_metrics().items():
            with self.subTest(metric=mid):
                self.assertTrue(mid.startswith('G')); self.assertIn(m['relation'],('component','proxy'))
                self.assertFalse(m['reviewed'])  # nothing self-approved
    def test_invalid_definition_detected(self):
        bad={'GX':{'metric_id':'GX','catalog_metric':'M99_99','relation':'same','question_ids':['Q99.99'],'name':'x',
                   'formula':{'type':'ratio'},'inputs':{'table':'x'},'params':{},'unit':'u','caveats':'c','reviewed':'no'}}
        with mock.patch.object(gold,'load_metrics',return_value=bad):
            errs=' '.join(gold.check_metrics())
        for frag in ('fórmula desconocida','relation','catalog_metric','preguntas','booleano','inputs.table'):
            self.assertIn(frag,errs)

if __name__=='__main__': unittest.main()
