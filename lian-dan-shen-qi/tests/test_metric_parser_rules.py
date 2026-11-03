import json
import unittest
from app.models.training_metrics import initial_stream,parse_page
from app.models.training_project import MetricParserRequest
from app.repositories.metric_repository import validate_stream

RULE={'pattern':r'step=(?P<step>\d+)\s+loss=(?P<loss>[-+0-9.eE]+)',
      'dataset':'custom','split':'train','metrics':['loss']}

def observation(size):
    return {'status':'running','log_size':size,'truncated':False,'stale':False}

class ParserRulesTests(unittest.TestCase):
    def test_unconfigured_plain_text_is_ignored(self):
        raw=b'step=1 loss=0.25\n'
        state,events=parse_page(initial_stream(),raw,observation(len(raw)))
        self.assertEqual(events,[]);self.assertEqual(state['ignored'],1)

    def test_configured_text_uses_original_byte_offset(self):
        raw='提示\nstep=1 loss=0.25\n'.encode()
        state,events=parse_page(initial_stream(),raw,observation(len(raw)),rules=[RULE])
        self.assertEqual(events[0]['source_offset'],len('提示\n'.encode()))
        self.assertEqual(events[0]['values'],{'loss':0.25})
        self.assertEqual(state['valid'],1);validate_stream(state)

    def test_byte_chunks_and_first_matching_rule_do_not_duplicate(self):
        raw=b'step=1 loss=0.25\n';state=initial_stream();events=[]
        for byte in raw:
            state,new=parse_page(state,bytes([byte]),observation(len(raw)),rules=[RULE,RULE])
            events.extend(new)
        self.assertEqual(len(events),1);self.assertEqual(state['offset'],len(raw))

    def test_native_json_has_priority_and_no_rule_invents_values(self):
        raw=(json.dumps({'type':'metric','step':1,'loss':0.5})+'\n').encode()
        state,events=parse_page(initial_stream(),raw,observation(len(raw)),rules=[RULE])
        self.assertEqual(events[0]['values']['loss'],0.5)
        self.assertEqual(state['valid'],1)
        state,events=parse_page(initial_stream(),b'step=2 loss=nan\n',observation(16),rules=[RULE])
        self.assertFalse(events)

    def test_invalid_rules_rejected_before_storage(self):
        for value in ({**RULE,'pattern':'('},{**RULE,'metrics':['missing']},{**RULE,'dataset':'bad dataset'}):
            with self.assertRaises(ValueError):MetricParserRequest(rules=[value])


    def test_mixed_case_groups_names_and_log_text_are_same_metric(self):
        rule = {**RULE, 'pattern':r'epoch: (?P<EPOCH>\d+) mae: (?P<MAE>[0-9.eE+-]+)',
                'dataset':'Tiny_COD', 'split':'Validation', 'metrics':['MaE']}
        rules = MetricParserRequest(rules=[rule]).model_dump()['rules']
        self.assertEqual(rules[0]['metrics'], ['mae'])
        self.assertEqual(rules[0]['pattern'], rule['pattern'])
        raw = b'EpOcH: 37 MAE: 1.7E-2\n'
        state, events = parse_page(initial_stream(),raw,observation(len(raw)),rules=rules)
        self.assertEqual(state['valid'],1)
        self.assertEqual(events[0]['epoch'],37)
        self.assertEqual(events[0]['values'],{'mae':0.017})
        self.assertEqual(events[0]['dataset'],'tiny_cod')
        self.assertEqual(events[0]['split'],'validation')
        validate_stream(state)

    def test_case_only_duplicate_groups_and_metrics_are_rejected(self):
        for changes in ({'pattern':r'(?P<step>\d+) (?P<mae>[0-9.]+) (?P<MAE>[0-9.]+)', 'metrics':['mae']},
                        {'metrics':['loss','LOSS']}):
            with self.assertRaises(ValueError):
                MetricParserRequest(rules=[{**RULE, **changes}])

    def test_case_insensitive_matching_preserves_regex_escape_meaning(self):
        rule={**RULE,'pattern':r'(?P<STEP>\d+)\s+(?P<LOSS>\d+\.\d+)\s+\D+'}
        rules=MetricParserRequest(rules=[rule]).model_dump()['rules']
        raw=b'1 0.5 END\n'
        _,events=parse_page(initial_stream(),raw,observation(len(raw)),rules=rules)
        self.assertEqual(events[0]['values'],{'loss':0.5})
