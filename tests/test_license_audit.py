"""The licence audit must cover every connected source, and no approval may be recorded without a human."""
import csv
import unittest
from marketscope.connectors import REGISTRY
from marketscope.core import CATALOG, load_sources, SOURCE_ENUMS

def audit_rows():
    with (CATALOG/'license_audit.csv').open(encoding='utf-8-sig',newline='') as f:
        return {r['source_id']:r for r in csv.DictReader(f)}

class TestLicenseAudit(unittest.TestCase):
    def test_every_connected_source_is_audited(self):
        self.assertEqual(set(REGISTRY)-set(audit_rows()),set())
    def test_audited_sources_exist_in_catalog(self):
        self.assertEqual(set(audit_rows())-set(load_sources()),set())
    def test_fields_are_complete_and_valid(self):
        for sid,r in audit_rows().items():
            with self.subTest(source=sid):
                self.assertTrue(all(v.strip() for v in r.values()),[k for k,v in r.items() if not v.strip()])
                self.assertIn(r['proposed_license_review'],SOURCE_ENUMS['license_review'])
                self.assertIn(r['evidence_of_terms'],{'verified_from_terms','secondary_source','unverified'})
                self.assertTrue(r['terms_urls'].startswith('https://'))
    def test_no_approval_without_human_decision(self):
        # The audit only proposes; the catalog changes only after a recorded human decision (AGENTS.md rule 4).
        for sid,r in audit_rows().items():
            with self.subTest(source=sid):
                review=load_sources()[sid]['license_review']
                if r['human_decision']=='pending':
                    self.assertEqual(review,'pending')
                else:
                    self.assertTrue(r['human_decision'].startswith('confirmado'),r['human_decision'])
                    self.assertEqual(review,r['proposed_license_review'])
    def test_unaudited_sources_stay_pending(self):
        audited=set(audit_rows())
        for sid,src in load_sources().items():
            if sid not in audited:
                with self.subTest(source=sid): self.assertEqual(src['license_review'],'pending')

if __name__=='__main__': unittest.main()
