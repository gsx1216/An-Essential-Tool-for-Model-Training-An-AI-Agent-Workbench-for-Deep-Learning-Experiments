"""运行参数的确定性、不可变快照、结构/数值边界和 UUID 校验。"""

import json
import unittest

from app.models.experiment_run import canonical_parameters, normalize_run_uuid, run_input_digest, validate_run_status


class RunModelTests(unittest.TestCase):
    def test_ordered_canonical_text_preserves_values_and_distinguishes_types(self):
        value = {'z': ['中', True, None, .001], 'epochs': 2}
        text = canonical_parameters(value)
        self.assertEqual(json.loads(text), value)
        self.assertEqual(text, canonical_parameters(dict(reversed(list(value.items())))))
        self.assertNotEqual(canonical_parameters({'x': True}), canonical_parameters({'x': 1}))

    def test_integer_limits_supported_and_overflow_rejected(self):
        self.assertEqual(json.loads(canonical_parameters({'min': -2**63, 'max': 2**64 - 1}))['max'], 2**64 - 1)
        for value in (-2**63 - 1, 2**64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_parameters({'n': value})

    def test_nonfinite_float_and_non_json_values_rejected(self):
        for value in (float('nan'), float('inf'), -float('inf'), b'bytes', {1, 2}, (1, 2), object()):
            with self.subTest(type=type(value).__name__), self.assertRaises(ValueError):
                canonical_parameters({'value': value})

    def test_only_string_keys_and_object_root_supported(self):
        for value in ([], None, 'text', {1: 'a'}, {'': 'a'}, {'x' * 129: 1}):
            with self.subTest(type=type(value).__name__), self.assertRaises(ValueError):
                canonical_parameters(value)

    def test_utf8_encoded_size_includes_json_framing(self):
        self.assertEqual(len(canonical_parameters({'x': 'a' * 32760}).encode()), 32768)
        for value in ({'x': 'a' * 32761}, {'x': '中' * 11000}, {'x': '\ud800'}):
            with self.assertRaises(ValueError):
                canonical_parameters(value)

    def test_depth_and_node_limits_prevent_unbounded_structures(self):
        value = {}
        nested = value
        for _ in range(9):
            nested['x'] = {}
            nested = nested['x']
        for data in (value, {'x': list(range(1000))}):
            with self.assertRaises(ValueError):
                canonical_parameters(data)

    def test_cycles_rejected_but_reusing_same_subobject_is_supported(self):
        loop = {}
        loop['self'] = loop
        with self.assertRaises(ValueError):
            canonical_parameters(loop)
        shared = [1, 2]
        self.assertEqual(json.loads(canonical_parameters({'a': shared, 'b': shared})), {'a': [1, 2], 'b': [1, 2]})

    def test_nested_credentials_rejected_without_rejecting_token_hyperparameters(self):
        for key in ('password', 'API_KEY', 'wandb_api_key', 'access-token', 'Authorization'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                canonical_parameters({'optimizer': {key: 'private'}})
        self.assertTrue(canonical_parameters({'max_tokens': 100, 'tokenizer': 'model'}))

    def test_request_digest_covers_experiment_and_canonical_parameters(self):
        first = run_input_digest('a' * 32, canonical_parameters({'epochs': 2}))
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, run_input_digest('b' * 32, canonical_parameters({'epochs': 2})))
        self.assertNotEqual(first, run_input_digest('a' * 32, canonical_parameters({'epochs': 3})))

    def test_uuid_and_status_validation_are_strict_for_direct_callers(self):
        self.assertEqual(normalize_run_uuid('AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA'), 'a' * 32)
        for value in (None, 3, 'bad'):
            with self.assertRaises(ValueError):
                normalize_run_uuid(value)
        for value in (True, [], 'unknown', 'RUNNING'):
            with self.assertRaises(ValueError):
                validate_run_status(value)
        self.assertIsNone(validate_run_status(None))


if __name__ == '__main__':
    unittest.main()
