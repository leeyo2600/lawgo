import json
import unittest
from unittest.mock import Mock, patch
from upper_law_suggestions import suggest_upper_laws


class SuggestionsTests(unittest.TestCase):
    def test_no_evidence_no_call(self):
        n=Mock();api=Mock()
        _,_,fills=suggest_upper_laws({"상위법령":{}},[],n,api)
        self.assertFalse(fills);n.session.post.assert_not_called();api.search_current_law.assert_not_called()

    def test_only_quoted_verified_missing_slot_is_filled(self):
        record={"상위법령":{"법":None,"시행령":"기존 시행령 제1조"}}
        text="이 조례는 건축법 제42조에 따른 사항을 정한다."
        details=[{"구분":"자치법규","최종상태":"OK","현행매칭명":"시험 조례","추출내용":text}]
        n=Mock();n.calls=0;n.model="test";n.timeout=10
        n.session.post.return_value.json.return_value={"choices":[{"message":{"content":json.dumps({"candidates":[{"slot":"법","reference":"건축법 제42조","source":"시험 조례","quote":text},{"slot":"시행규칙","reference":"건축법 시행규칙 제1조","source":"시험 조례","quote":"없는 인용"}]})}}]}
        api=Mock();api.search_current_law.return_value=({"법령ID":"1","법령명한글":"건축법"},"CURRENT_EXACT")
        with patch("upper_law_suggestions.core.extract_law_subunit",return_value={"status":"OK","조문내용":"조문 본문"}):
            _,notes,fills=suggest_upper_laws(record,details,n,api)
        self.assertEqual(len(fills),1);self.assertEqual(fills[0]["slot"],"법")
        self.assertIsNone(record["상위법령"]["법"])
        self.assertIn(text,notes);self.assertEqual(api.search_current_law.call_count,1)
