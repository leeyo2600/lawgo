import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from matcher import HighRecallLawApi


class CandidateLimitTests(unittest.TestCase):
    def test_fuzzy_law_and_ordinance_validate_only_three(self):
        with tempfile.TemporaryDirectory() as folder:
            api = HighRecallLawApi("test", Path(folder))
            laws = [{"법령ID":str(i),"법령명한글":f"건축관리법{i}"} for i in range(8)]
            api._law_candidates = Mock(side_effect=lambda title,nw: laws if nw=="3" else [])
            api._validate_law = Mock(return_value=True)
            _, info = api.search_current_law_fuzzy("건축관리법", {"article":1})
            self.assertEqual(info["candidate_pool"], 8)
            self.assertEqual(api._validate_law.call_count, 3)
            ordinances = [{"자치법규명":f"고성군 건축관리 조례{i}","자치법규종류":"조례"} for i in range(8)]
            api._validate_ordin = Mock(return_value=True)
            _, info = api._rank_ordin("고성군 건축관리 조례", "강원 고성군", "조례", ordinances, {"article":1})
            self.assertEqual(info["candidate_pool"],8)
            self.assertEqual(api._validate_ordin.call_count,3)
            api.session.close()

    def test_exact_local_search_skips_broad_queries_but_still_validates_clause(self):
        with tempfile.TemporaryDirectory() as folder:
            api=HighRecallLawApi('test',Path(folder))
            api.current_ordinance_catalog=Mock(return_value=[])
            row={'자치법규명':'고성군 시험 조례','자치법규ID':'1','지자체기관명':'강원특별자치도 고성군','자치법규종류':'조례','시행일자':'20260901'}
            api._paged_ordin=Mock(return_value=[row])
            api._validate_ordin=Mock(return_value=True)
            meta,info=api.search_current_ordinance_fuzzy('고성군 시험 조례','강원 고성군','조례',{'article':1})
            self.assertEqual(meta['자치법규ID'],'1')
            self.assertEqual(info['method'],'CURRENT_EXACT_LOCAL_SEARCH')
            self.assertEqual(api._paged_ordin.call_count,1)
            api._validate_ordin.assert_called_once()
            api.session.close()
