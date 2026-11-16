import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from marketscope import core
from marketscope.core import (init_db, open_db, resolve_db_path, check_catalog, summary,
                              DatabaseNotInitialized, PROJECT_ROOT)

class TestDbPath(unittest.TestCase):
    def test_relative_env_anchors_to_project_root(self):
        with mock.patch.dict(os.environ,{'MARKETSCOPE_DB':'data/x.sqlite'}):
            self.assertEqual(resolve_db_path(),(PROJECT_ROOT/'data/x.sqlite').resolve())
    def test_default_is_absolute(self):
        with mock.patch.dict(os.environ,{},clear=True):
            self.assertTrue(resolve_db_path().is_absolute())
            self.assertEqual(resolve_db_path(),core.DEFAULT_DB)

class TestReadWriteSeparation(unittest.TestCase):
    def test_read_on_missing_db_creates_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'sub'/'db.sqlite'
            with self.assertRaises(DatabaseNotInitialized):
                with open_db(path): pass
            self.assertFalse(path.parent.exists())
    def test_read_on_unschematized_db_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db.sqlite'; sqlite3.connect(path).close()
            with self.assertRaises(DatabaseNotInitialized):
                with open_db(path): pass
    def test_readonly_rejects_writes(self):
        with tempfile.TemporaryDirectory() as d:
            path=init_db(Path(d)/'db.sqlite')
            with open_db(path) as db:
                self.assertEqual(summary(db)['total'],0)
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("INSERT INTO research_runs (run_id,question_id,query_budget,created_at,status) VALUES ('r','Q01.01',1,'t','active')")
    def test_init_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(init_db(Path(d)/'db.sqlite'),init_db(Path(d)/'db.sqlite'))

class TestCatalogChecks(unittest.TestCase):
    def test_shipped_catalog_is_valid(self):
        self.assertEqual(check_catalog(),[])
    def _broken(self,name,old,new):
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(core.CATALOG,Path(d),dirs_exist_ok=True)
            f=Path(d)/name; text=f.read_text(encoding='utf-8-sig')
            self.assertIn(old,text); f.write_text(text.replace(old,new,1),encoding='utf-8')
            with mock.patch.object(core,'CATALOG',Path(d)): return check_catalog()
    def test_detects_duplicate_question(self):
        self.assertTrue(any('duplicado' in e for e in self._broken('questions.csv','Q01.02,','Q01.01,')))
    def test_detects_unknown_dimension(self):
        self.assertTrue(any('dimensiones inexistentes' in e for e in self._broken('sources.csv','DIM01 DIM03','DIM99 DIM03')))
    def test_detects_bad_enum(self):
        self.assertTrue(any('license_review' in e for e in self._broken('sources.csv',',pending,',',granted,')))
    def test_detects_mojibake(self):
        self.assertTrue(any('mojibake' in e for e in self._broken('questions.csv','Qué','QuÃ©')))

def _client_available():
    try:
        import mcp.client.stdio  # noqa: F401
        return True
    except ImportError:
        return False

@unittest.skipUnless(_client_available(),'mcp no instalado en este intérprete')
class TestMcpStartup(unittest.TestCase):
    """Launch the server exactly as .mcp.json declares it and talk MCP over stdio."""
    EXPECTED={'server_info','submit_evidence','evidence_summary','list_evidence','catalog_questions',
              'catalog_sources','begin_research','record_research_event','get_research_run',
              'create_study','get_study','list_studies','plan_research','replan_research','next_step','complete_agent_step',
              'execute_step','list_hits','submit_evidence_from_hit','get_hit','set_study_keywords',
              'evaluate_question','submit_conclusion','list_conclusions','build_silver','query_observations','list_metrics',
              'compute_metric','promote_metric','get_attribution','recheck_deleted'}
    def test_real_startup_from_mcp_json(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        cfg=json.loads((PROJECT_ROOT/'.mcp.json').read_text(encoding='utf-8'))['mcpServers']['marketscope']
        if not Path(cfg['command']).is_file(): self.skipTest(f"intérprete de .mcp.json no existe: {cfg['command']}")
        with tempfile.TemporaryDirectory() as d:
            dbpath=Path(d)/'startup.sqlite'
            # Foreign cwd proves the server does not depend on the client's working directory.
            params=StdioServerParameters(command=cfg['command'],args=cfg['args'],cwd=d,
                                         env={**os.environ,**cfg.get('env',{}),'MARKETSCOPE_DB':str(dbpath)})
            async def run():
                async with stdio_client(params) as (r,w), ClientSession(r,w) as s:
                    await s.initialize()
                    names={t.name for t in (await s.list_tools()).tools}
                    call=lambda n,**a: s.call_tool(n,a)
                    info=json.loads((await call('server_info')).content[0].text)
                    total=json.loads((await call('evidence_summary')).content[0].text)['total']
                    study=json.loads((await call('create_study',name='Startup test',market_definition='CRM para pymes',
                                                  geography='es',segment='pymes',language='ES')).content[0].text)
                    run_id=json.loads((await call('begin_research',question_id='Q01.01',query_budget=1,
                                                  study_id=study['study_id'])).content[0].text)['run_id']
                    plan=json.loads((await call('plan_research',run_id=run_id)).content[0].text)
                    step=json.loads((await call('next_step',run_id=run_id)).content[0].text)
                    report=json.loads((await call('get_research_run',run_id=run_id)).content[0].text)
                    return names,info,total,report,study,plan,step
            names,info,total,report,study,plan,step=asyncio.run(asyncio.wait_for(run(),60))
            self.assertEqual((plan['plan_version'],plan['question_id']),(1,'Q01.01'))
            self.assertEqual((step['step']['step_id'],step['step']['route']),('github_issues','connector'))
            self.assertEqual((study['geography'],study['language']),('ES','es'))
            self.assertEqual(report['study_id'],study['study_id'])
            self.assertTrue(self.EXPECTED<=names,self.EXPECTED-names)
            self.assertEqual(Path(info['python']).resolve(),Path(cfg['command']).resolve())
            self.assertEqual(Path(info['db']),dbpath.resolve())
            self.assertEqual((info['questions'],info['sources']),(78,48))
            self.assertEqual(total,0)
            self.assertEqual(report['question_id'],'Q01.01')

if __name__=='__main__': unittest.main()
