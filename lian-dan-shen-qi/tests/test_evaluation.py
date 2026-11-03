"""评测声明、数据集隔离、旧游标兼容及报告质量行为。"""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from app import database
from app.models.evaluation import canonical_evaluation, evaluation_digest, verified_evaluation, annotate_series
from app.models.training_metrics import initial_stream, metric_line, parse_page
from app.repositories.metric_repository import validate_stream


def example():
    return json.loads((Path(__file__).resolve().parents[1] / 'examples/evaluation_examples.json').read_text(encoding='utf-8'))['camouflaged_object_detection']['evaluation']


class EvaluationTests(unittest.TestCase):
    def test_examples_validate_and_canonicalize_stably(self):
        examples = json.loads((Path(__file__).resolve().parents[1] / 'examples/evaluation_examples.json').read_text())
        for value in examples.values():
            text = canonical_evaluation(value['evaluation'])
            self.assertEqual(canonical_evaluation(json.loads(text)), text)

    def test_identifiers_ignore_case_without_changing_display_text(self):
        value=example()
        expected=canonical_evaluation(value)
        value['protocol_id']=value['protocol_id'].upper()
        for dataset in value['datasets']: dataset['id']=dataset['id'].upper()
        for metric in value['metrics']: metric['name']=metric['name'].upper()
        value['primary_metric']=value['primary_metric'].upper()
        self.assertEqual(canonical_evaluation(value),expected)
        for field in ('datasets','metrics'):
            duplicate=example()
            key='id' if field=='datasets' else 'name'
            duplicate[field].append({**duplicate[field][0],key:duplicate[field][0][key].upper()})
            with self.assertRaises(ValueError): canonical_evaluation(duplicate)
        value['datasets'][0]['id']='UNSPECIFIED'
        with self.assertRaises(ValueError): canonical_evaluation(value)

    def test_no_profile_is_legacy_none(self):
        self.assertIsNone(canonical_evaluation(None))
        self.assertIsNone(verified_evaluation({}))

    def test_duplicate_datasets_rejected(self):
        value = example(); value['datasets'].append(value['datasets'][0])
        with self.assertRaises(ValueError): canonical_evaluation(value)

    def test_duplicate_metrics_rejected(self):
        value = example(); value['metrics'].append(value['metrics'][0])
        with self.assertRaises(ValueError): canonical_evaluation(value)

    def test_unknown_primary_metric_rejected(self):
        value = example(); value['primary_metric'] = 'missing'
        with self.assertRaises(ValueError): canonical_evaluation(value)

    def test_inverted_bounds_rejected(self):
        value = example(); value['metrics'][0]['min_value'] = 2
        with self.assertRaises(ValueError): canonical_evaluation(value)

    def test_non_numeric_and_nonfinite_bounds_rejected(self):
        for bound in (True, '1', float('inf'), float('nan')):
            value = example(); value['metrics'][0]['max_value'] = bound
            with self.subTest(bound=bound), self.assertRaises(ValueError): canonical_evaluation(value)

    def test_reserved_names_and_extra_fields_rejected(self):
        for name in ('dataset', 'split', 'step', 'epoch', 'type'):
            value = example(); value['metrics'][0]['name'] = name
            with self.assertRaises(ValueError): canonical_evaluation(value)
        value = example(); value['command'] = 'anything'
        with self.assertRaises(ValueError): canonical_evaluation(value)

    def test_reserved_dataset_rejected(self):
        value = example(); value['datasets'][0]['id'] = 'unspecified'
        with self.assertRaises(ValueError): canonical_evaluation(value)

    def test_digest_and_canonical_text_checked(self):
        text = canonical_evaluation(example())
        row = {'evaluation_json': text, 'evaluation_digest': evaluation_digest(text)}
        self.assertEqual(verified_evaluation(row), text)
        with self.assertRaises(ValueError): verified_evaluation({**row, 'evaluation_digest': 'wrong'})
        with self.assertRaises(ValueError): verified_evaluation({**row, 'evaluation_json': None})

    def test_structural_and_byte_budgets(self):
        value = example(); value['datasets'] *= 9
        with self.assertRaises(ValueError): canonical_evaluation(value)
        value = example()
        value['metrics'] = [{**value['metrics'][0], 'name': 'm'+str(i), 'definition': '长'*1000} for i in range(64)]
        value['primary_metric'] = None
        with self.assertRaises(ValueError): canonical_evaluation(value)

    def test_dataset_is_metadata_and_not_a_metric(self):
        event = metric_line(b'{"type":"metric","step":0,"dataset":"cod10k","mae":0.1}')
        self.assertEqual(event['dataset'], 'cod10k'); self.assertEqual(event['values'], {'mae':.1})
        event = metric_line(b'{"type":"metric","step":0,"mae":0.1}')
        self.assertEqual(event['dataset'], 'unspecified')

    def test_bad_dataset_does_not_block_following_valid_event(self):
        for dataset in ('', '../other', 2, None, 'a'*65):
            with self.assertRaises(ValueError): metric_line(json.dumps({'type':'metric','step':0,'dataset':dataset,'mae':.1}).encode())

    def test_steps_and_series_are_independent_across_datasets(self):
        raw = b'\n'.join(json.dumps({'type':'metric','step':step,'dataset':dataset,'mae':.1}).encode()
                         for dataset,step in [('cod10k',10),('camo',1),('cod10k',11)]) + b'\n'
        state, events = parse_page(initial_stream(), raw, {'log_size':len(raw),'status':'succeeded','truncated':False,'stale':False})
        self.assertEqual(state['regressions'], 0)
        self.assertEqual(state['series'], ['camo/train/mae','cod10k/train/mae'])
        self.assertEqual(len(events),3); validate_stream(state)

    def test_legacy_stream_conversion_preserves_cursor_buffers_and_counts(self):
        state = initial_stream(); state['series'] = ['train/mae']; state['last_steps'] = {'train':3}
        before = deepcopy(state)
        result, events = parse_page(state, b'', {'log_size':0,'status':'running','truncated':False,'stale':False})
        self.assertEqual(state, before); self.assertEqual(result['offset'],0)
        self.assertEqual(result['series'], ['unspecified/train/mae']); self.assertFalse(events)
        self.assertEqual(result['last_steps'], {'unspecified/train':3}); validate_stream(result)

    def test_series_budget_applies_across_all_datasets(self):
        state=initial_stream(); state['series']=['d'+str(i)+'/test/mae' for i in range(64)]
        raw=b'{"type":"metric","step":0,"dataset":"extra","mae":0.2}\n'
        result, events=parse_page(state,raw,{'log_size':len(raw),'status':'succeeded','truncated':False,'stale':False})
        self.assertFalse(events); self.assertEqual(result['invalid'],1)

    def test_report_best_value_obeys_declared_direction(self):
        rows=[{'dataset':'cod10k','metric_name':'mae','min_value':.1,'max_value':.3},
              {'dataset':'camo','metric_name':'sm','min_value':.4,'max_value':.8}]
        annotate_series(example(),rows)
        self.assertEqual(rows[0]['observed_best_value'],.1); self.assertEqual(rows[1]['observed_best_value'],.8)
        self.assertTrue(rows[1]['is_primary_metric'])

    def test_range_and_unknown_definition_keep_raw_values_without_ranking(self):
        rows=[{'dataset':'cod10k','metric_name':'mae','min_value':.1,'max_value':3},
              {'dataset':'other','metric_name':'new_metric','min_value':5,'max_value':7}]
        annotate_series(example(),rows)
        self.assertEqual(rows[0]['max_value'],3); self.assertIsNone(rows[0]['observed_best_value'])
        self.assertIn('VALUE_OUT_OF_RANGE',rows[0]['quality_flags'])
        self.assertEqual(set(rows[1]['quality_flags']), {'DATASET_UNDECLARED','METRIC_UNDECLARED'})

    def test_none_direction_never_selects_best(self):
        value=example(); value['metrics'][0]['direction']='none'
        rows=[{'dataset':'cod10k','metric_name':'sm','min_value':.2,'max_value':.6}]
        annotate_series(value,rows);self.assertIsNone(rows[0]['observed_best_value'])

    def test_report_references_definitions_without_repeating_large_text(self):
        value=example();value['metrics'][0]['definition']='长'*1000
        rows=[{'dataset':'cod10k','metric_name':'sm','min_value':.2,'max_value':.6} for _ in range(64)]
        annotate_series(value,rows)
        self.assertLess(len(json.dumps(rows).encode()),32768)
        self.assertEqual(rows[0]['metric_definition_name'],'sm')

    def test_migration_adds_only_missing_column_and_index(self):
        connection=MagicMock(); cursor=connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value=None;cursor.fetchall.return_value=[]
        database._ensure_evaluation(connection)
        sql='\n'.join(c.args[0] for c in cursor.execute.call_args_list)
        self.assertIn('ADD COLUMN dataset',sql);self.assertIn('ADD INDEX',sql)
        self.assertNotRegex(sql, r'(?m)^UPDATE\s'); self.assertNotRegex(sql, r'(?m)^DELETE\s')

    def test_repeat_migration_validates_existing_shape(self):
        connection=MagicMock();cursor=connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value=('varchar(64)','ascii','ascii_bin','NO','unspecified')
        cursor.fetchall.return_value=[(1,'run_id',1),(2,'dataset',1),(3,'split',1),(4,'source_offset',1)]
        database._ensure_evaluation(connection)
        sql='\n'.join(c.args[0] for c in cursor.execute.call_args_list)
        self.assertNotIn('ALTER TABLE',sql)
        cursor.fetchone.return_value=('varchar(2)','ascii','ascii_bin','NO','unspecified')
        with self.assertRaises(ValueError): database._ensure_evaluation(connection)

    def test_full_install_includes_evaluation_migration(self):
        connection=MagicMock();connection.__enter__.return_value=connection
        connection.cursor.return_value.__enter__.return_value.nextset.return_value=False
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(database,'get_connection',return_value=connection))
            names=('_ensure_user_password_column','_ensure_session_user_fk','_ensure_chat_turns','_ensure_login_sessions',
                   '_ensure_model_credentials','_ensure_context_summaries','_ensure_experiments','_ensure_experiment_runs',
                   '_ensure_server_connections','_ensure_run_executions','_ensure_metrics','_ensure_evaluation','_ensure_chat_tools','_ensure_research','_ensure_training_tools','_ensure_run_monitors','_ensure_event_bus','_ensure_analysis','_ensure_code_changes', '_ensure_revision_runs', '_ensure_papers', '_ensure_skills')
            mocks={n:stack.enter_context(patch.object(database,n)) for n in names}
            database.initialize_database('unused')
            mocks['_ensure_evaluation'].assert_called_once_with(connection)
