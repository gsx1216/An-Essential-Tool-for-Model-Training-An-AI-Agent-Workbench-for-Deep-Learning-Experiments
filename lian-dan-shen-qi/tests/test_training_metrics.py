"""JSONL 字节流边界、格式/预算、序列与缺失数据的事实报告。"""

import base64
from copy import deepcopy
import json
import unittest

from app.models.training_metrics import anchor_for, build_report, initial_stream, metric_line, parse_page
from app.repositories.metric_repository import validate_stream


def obs(size, status='succeeded', **options):
    return {'log_size': size, 'status': status, 'stale': False, 'truncated': False, **options}


def line(**options):
    return json.dumps({'type': 'metric', 'step': 1, 'loss': .5, **options}, ensure_ascii=False).encode()


class MetricParserTests(unittest.TestCase):
    def test_valid_flat_event_and_optional_group_and_epoch(self):
        result=metric_line(line(split='validation',epoch=2,accuracy=.8))
        self.assertEqual(result['split'],'validation');self.assertEqual(result['values'],{'loss':.5,'accuracy':.8})

    def test_ordinary_logs_are_ignored(self):
        for raw in (b'loss=0.25', b'{"type":"info","message":"ok"}', b''):
            self.assertIsNone(metric_line(raw))

    def test_invalid_values_do_not_become_zero(self):
        for options in ({'step':True},{'step':-1},{'step':2**53},{'epoch':'2'},{'loss':None},{'loss':True},
                        {'loss':'0.5'},{'loss':float('nan')},{'loss':float('inf')},{'loss':1e101},{'split':'other'},{'Loss':1}):
            with self.subTest(options=options),self.assertRaises(ValueError):
                metric_line(line(**options))

    def test_json_metric_identifiers_ignore_case_and_merge_series(self):
        raw=b'{"TYPE":"MeTrIc","STEP":1,"DATASET":"Tiny_COD","SPLIT":"Validation","MAE":0.2}\n' + b'{"type":"metric","step":2,"dataset":"tiny_cod","split":"validation","mae":0.1}\n'
        state,events=parse_page(initial_stream(),raw,obs(len(raw)))
        self.assertEqual(state['series'],['tiny_cod/validation/mae'])
        self.assertEqual([event['values']['mae'] for event in events],[0.2,0.1])
        for raw in (b'{"type":"metric","step":1,"mae":1,"MAE":2}',
                    b'{"type":"metric","step":1,"Step":2,"mae":1}'):
            with self.assertRaises(ValueError): metric_line(raw)

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(ValueError):
            metric_line(b'{"type":"metric","step":1,"loss":1,"loss":2}')

    def test_scalar_metrics_and_invalid_json_classification(self):
        state,events=parse_page(initial_stream(), b'ordinary\n{"type":"metric","step":1,"loss":null}\n{broken}\n',obs(69,'running'))
        self.assertEqual(state['invalid'],2);self.assertEqual(state['ignored'],1);self.assertFalse(events)

    def test_every_single_byte_boundary_preserves_chinese_and_event_position(self):
        raw='普通日志\n'.encode()+line()+b'\n'
        state=initial_stream();events=[]
        for byte in raw:
            state,new=parse_page(state,bytes([byte]),obs(len(raw),'running'));events+=new;validate_stream(state)
        self.assertEqual(len(events),1);self.assertEqual(events[0]['source_offset'],len('普通日志\n'.encode()))
        self.assertEqual(state['pending'],'');self.assertEqual(state['valid'],1)

    def test_partial_line_waits_until_next_page_and_does_not_mutate_input(self):
        raw=line();before=initial_stream();saved=deepcopy(before)
        state,events=parse_page(before,raw[:15],obs(len(raw),'running'))
        self.assertEqual(before,saved);self.assertFalse(events)
        state,events=parse_page(state,raw[15:]+b'\n',obs(len(raw)+1,'running'))
        self.assertEqual(len(events),1);self.assertEqual(events[0]['source_offset'],0)

    def test_terminal_valid_tail_without_newline_is_processed_once(self):
        raw=line();state,events=parse_page(initial_stream(),raw,obs(len(raw),'running'))
        self.assertFalse(events)
        state,events=parse_page(state,b'',obs(len(raw)))
        self.assertEqual(len(events),1);self.assertTrue(state['complete'])
        state,events=parse_page(state,b'',obs(len(raw)));self.assertFalse(events)

    def test_truncated_tail_is_not_guessed_as_complete(self):
        raw=line();state,events=parse_page(initial_stream(),raw,obs(len(raw),truncated=True))
        self.assertFalse(events);self.assertFalse(state['complete']);self.assertTrue(state['pending'])

    def test_oversized_line_discard_carries_across_pages_then_recovers(self):
        state,events=parse_page(initial_stream(),b'x'*9000,obs(10000,'running'))
        self.assertTrue(state['discarding']);self.assertEqual(state['oversized'],1)
        data=b'xx\n'+line()+b'\n'
        state,events=parse_page(state,data,obs(9000+len(data)))
        self.assertEqual(len(events),1);self.assertEqual(events[0]['source_offset'],9003)
        self.assertEqual(state['oversized'],1);self.assertTrue(state['complete'])

    def test_step_regression_is_kept_and_grouped_independently(self):
        raw=line(step=10)+b'\n'+line(step=1,split='validation')+b'\n'+line(step=2)+b'\n'
        state,events=parse_page(initial_stream(),raw,obs(len(raw)))
        self.assertEqual(len(events),3);self.assertEqual(state['regressions'],1)

    def test_series_budget_rejects_extra_series_without_losing_previous(self):
        state=initial_stream();state['series']=['train/m'+str(i) for i in range(64)]
        raw=line()+b'\n';state,events=parse_page(state,raw,obs(len(raw)))
        self.assertEqual(state['invalid'],1);self.assertFalse(events)

    def test_invalid_utf8_json_is_counted_and_next_valid_line_survives(self):
        raw=b'{"x":"\xff"}\n'+line()+b'\n'
        state,events=parse_page(initial_stream(),raw,obs(len(raw)))
        self.assertEqual(state['invalid'],1);self.assertEqual(len(events),1)

    def test_stale_terminal_never_claims_complete(self):
        raw=line()+b'\n';state,_=parse_page(initial_stream(),raw,obs(len(raw),stale=True))
        self.assertFalse(state['complete'])

    def test_stream_validation_detects_bad_buffer_and_false_completeness(self):
        for changes in ({'pending':'!bad'},{'offset':-1},{'complete':True},{'valid':True},{'tail':base64.b64encode(b'a').decode()}):
            with self.assertRaises(ValueError):
                validate_stream({**initial_stream(),**changes})

    def test_anchor_tracks_only_last_128_raw_bytes(self):
        state,_=parse_page(initial_stream(),b'a'*200,obs(200,'running'))
        self.assertEqual(len(base64.b64decode(state['tail'])),128)
        self.assertEqual(len(anchor_for(state)),64)


class ReportTests(unittest.TestCase):
    def run_record(self):
        return dict(run_id='a'*32,experiment_id='b'*32,experiment_title='test',experiment_kind='training',status='failed',
                    exit_code=3,error_code=None,started_at=None,finished_at=None,parameters_json='{}')

    def test_no_metrics_report_does_not_fabricate_values(self):
        report=build_report(self.run_record(),None,[])
        self.assertEqual(report['series'],[]);self.assertIn('NO_METRICS',report['warnings']);self.assertIn('NOT_SYNCED',report['warnings'])
        self.assertEqual(report['exit_code'],3)

    def test_numeric_delta_is_not_an_improvement_claim(self):
        report=build_report(self.run_record(),initial_stream(),[{'dataset':'unspecified','split':'train','metric_name':'loss','first_value':.8,'last_value':.2,'min_value':.2,'max_value':.8}])
        self.assertAlmostEqual(report['series'][0]['delta'],-.6)
        self.assertIn('不据此认定',report['interpretation'])

    def test_quality_and_partial_source_are_explicit(self):
        stream={**initial_stream(),'invalid':2,'truncated':True,'regressions':1}
        report=build_report(self.run_record(),stream,[])
        self.assertTrue({'INVALID_LINES','LOG_TRUNCATED','STEP_REGRESSION','SOURCE_INCOMPLETE'}<=set(report['warnings']))
