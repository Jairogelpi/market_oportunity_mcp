import json
import tempfile
import unittest
from pathlib import Path
from marketscope.core import validate, EvidenceError, connect, ingest, summary

EXAMPLE=Path(__file__).resolve().parents[1]/'examples/evidence.json'
class TestIntake(unittest.TestCase):
    def setUp(self): self.e=json.loads(EXAMPLE.read_text())[0]
    def test_valid(self): self.assertEqual(validate(self.e)['status'],'candidate')
    def test_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=connect(Path(tmp)/'test.db')
            self.assertEqual(ingest(db,self.e)[1],'created')
            self.assertEqual(ingest(db,self.e)[1],'duplicate')
            self.assertEqual(summary(db)['total'],1)
            db.close()
    def test_bad_url(self):
        self.e['source_url']='file:///etc/passwd'
        with self.assertRaises(EvidenceError): validate(self.e)
    def test_agent_cannot_verify(self):
        self.e['status']='verified';self.e['reviewer']='agent'
        with self.assertRaises(EvidenceError): validate(self.e)
    def test_missing_excerpt(self):
        self.e.pop('evidence_excerpt')
        with self.assertRaises(EvidenceError): validate(self.e)
    def test_bad_timestamp(self):
        self.e['retrieved_at']='yesterday'
        with self.assertRaises(EvidenceError): validate(self.e)
if __name__=='__main__': unittest.main()
