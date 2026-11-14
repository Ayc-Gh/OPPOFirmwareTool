import re
import unittest
from oplus_tool.models import load_profiles
from oplus_tool.tracker import REGIONS

class ModelTests(unittest.TestCase):
    def test_catalog_identity_and_region_integrity(self):
        profiles=load_profiles();keys=[(p.brand,p.model) for p in profiles]
        self.assertGreater(len(profiles),100);self.assertEqual(len(keys),len(set(keys)))
        for p in profiles:
            self.assertRegex(p.model,r'^[A-Za-z0-9.\-]+$');self.assertIn(p.region,['']+REGIONS)
            if p.region:self.assertRegex(p.model,r'^P[A-Z]{2,3}\d{2,3}$')
        phy=next(p for p in profiles if p.model=='PHY110')
        self.assertIn('Find X7 Ultra',phy.name);self.assertIn('OP565FL1',phy.codenames);self.assertEqual(phy.region,'cn')
    def test_search_supports_names_codes_and_brand_aliases(self):
        profiles=load_profiles();phy=next(p for p in profiles if p.model=='PHY110')
        self.assertTrue(phy.matches('find PHY110'));self.assertTrue(phy.matches('op565fl1'));self.assertFalse(phy.matches('find PKB110'))
        self.assertTrue(phy.matches('FindX7Ultra'))
        one=next(p for p in profiles if p.model=='PJZ110');self.assertTrue(one.matches('一加 13'))
        realme=next(p for p in profiles if p.brand=='realme');self.assertTrue(realme.matches('真我'))
