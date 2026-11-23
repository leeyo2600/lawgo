import unittest
from pre_llm_audit import audit_reference
import pipeline_core as pc


class PreAuditTests(unittest.TestCase):
    def test_flag_and_no_blank_items(self):
        a=audit_reference("도로법 제31,64조")
        self.assertTrue(a["flag"])
        self.assertLess(a["score"],1)
        self.assertIn("제64조",a["comparison"])
        self.assertFalse(audit_reference("도로법 제31조")["flag"])
        self.assertIsNone(audit_reference("  ")["score"])
        self.assertEqual(pc.ref_specs({"상위법령":{"법":"  "}}),[])

    def test_audit_created_before_llm(self):
        items=pc.make_norm_items([{"category":"law","slot":"법","raw":"도로법 제31,64조"}])
        self.assertTrue(items[0]["pre_llm_audit"]["flag"])
