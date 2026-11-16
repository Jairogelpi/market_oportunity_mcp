import tempfile,unittest,json
from pathlib import Path
from marketscope.core import *
class ResearchTests(unittest.TestCase):
 def test_catalog(self):
  self.assertEqual(len(load_questions()),78)
  self.assertEqual(len(load_sources()),48)
 def test_unknown_question(self):
  e=json.loads((Path(__file__).parents[1]/'examples/evidence.json').read_text())[0]
  e['question_id']='Q99.99'
  with self.assertRaises(EvidenceError): validate(e)
 def test_run_budget_and_discard(self):
  with tempfile.TemporaryDirectory() as d:
   path=init_db(Path(d)/'test.sqlite')
   with open_db(path,write=True) as db:
    run=start_run(db,'Q01.01',1)
    self.assertEqual(log_research_event(db,run,'search',query='crm pains')['remaining_queries'],0)
    with self.assertRaises(EvidenceError): log_research_event(db,run,'search',query='another')
    log_research_event(db,run,'discarded',source_url='https://example.org',reason='duplicate')
    self.assertEqual(len(run_report(db,run)['events']),2)
 def test_private_addresses(self):
  for url in ['http://172.16.0.1/','http://[::1]/','http://0.0.0.0/','http://192.168.0.1/','http://10.0.0.1/']:
   with self.subTest(url=url),self.assertRaises(EvidenceError): normalize_url(url)
 def test_agent_review(self):
  e=json.loads((Path(__file__).parents[1]/'examples/evidence.json').read_text())[0]
  e.update(status='verified',reviewer='agent',source_access_status='permitted')
  with self.assertRaises(EvidenceError): validate(e)
if __name__=='__main__':unittest.main()
