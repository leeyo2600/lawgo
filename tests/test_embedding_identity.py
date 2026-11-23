import unittest
from unittest.mock import patch
import embedding_normalizer as module

class IdentityFastPathTests(unittest.TestCase):
    def test_identical_input_does_not_load_or_encode_model(self):
        with patch.object(module, '_model', None):
            audits = module.embedding_audits([{'raw':'건축법 제42조'}])
            self.assertIsNone(module._model)
        self.assertEqual(audits[0]['score'], 1)
        self.assertFalse(audits[0]['flag'])
        self.assertEqual(audits[0]['computation'], 'IDENTICAL_TEXT')

    def test_changed_input_still_encodes_and_flags(self):
        import numpy as np
        from unittest.mock import Mock
        model=Mock()
        model.encode.return_value=np.array([[1.,0.],[0.8,0.6]])
        from collections import OrderedDict
        with patch.object(module,'_model',model),patch.object(module,'_vectors',OrderedDict()):
            audits=module.embedding_audits([{'raw':'건축법 제42조'},{'raw':'건축법 제42조 2항'}])
        self.assertEqual(audits[0]['score'],1)
        self.assertTrue(audits[1]['flag'])
        self.assertEqual(len(model.encode.call_args.args[0]),2)
