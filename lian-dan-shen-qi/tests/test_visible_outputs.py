"""可见产物发布：真实文件复制、幂等、配置、协议及失败保护，不运行 GPU。"""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import config
from app.models.runner import RunnerSettings, validate_observation
from app.errors import RunnerProtocolError
from app.runner.remote_agent import publish_outputs
from tests.test_runner_core import observation


class VisibleOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.job = self.work / '.experiment-assistant' / ('a' * 32)
        self.job.mkdir(parents=True)
        self.artifacts = self.job / 'artifacts'
        self.artifacts.mkdir()
        (self.artifacts / 'last.pth').write_bytes(b'fixture weights, never loaded')
        (self.artifacts / 'metrics.jsonl').write_text('{"mae":0.03}\n')
        (self.job / 'output.log').write_text('training completed\n')
        work_dir = '/' + self.work.as_posix() if self.work.drive else str(self.work)
        self.launch = {'run_id': 'b' * 32, 'work_dir': work_dir,
                       'script_id': 'train', 'parameters': {'epochs': 1}}
        self.manifest = {'launch': self.launch, 'digest': 'c' * 64}
        self.state = {'status': 'succeeded', 'exit_code': 0}
        self.destination = self.work / 'training_outputs' / ('b' * 32)

    def publish(self):
        return publish_outputs(self.work, self.job, self.manifest, self.state)

    def test_old_snapshot_publishes_real_files_to_visible_default_and_preserves_original(self):
        result = self.publish()
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['file_count'], 3)
        output = self.destination / 'artifacts/last.pth'
        self.assertFalse(output.is_symlink())
        self.assertEqual(output.read_bytes(), (self.artifacts / 'last.pth').read_bytes())
        self.assertTrue((self.artifacts / 'last.pth').is_file())
        metadata = json.loads((self.destination / 'run.json').read_text())
        row = next(item for item in metadata['files'] if item['path'] == 'artifacts/last.pth')
        self.assertEqual(row['sha256'], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(metadata['run_id'], self.launch['run_id'])
        self.assertNotIn('password', metadata)
        self.assertEqual(validate_observation(observation('succeeded', outputs=result))['outputs'], result)

    def test_custom_nested_directory_is_used(self):
        self.launch['output_directory'] = 'results/PRNet'
        self.assertEqual(self.publish()['status'], 'published')
        self.assertTrue((self.work / 'results/PRNet' / ('b' * 32) / 'artifacts/last.pth').is_file())

    def test_repeat_publication_keeps_existing_files_and_mtime(self):
        self.publish()
        output = self.destination / 'artifacts/last.pth'
        before = output.stat().st_mtime_ns
        self.assertEqual(self.publish()['status'], 'published')
        self.assertEqual(output.stat().st_mtime_ns, before)

    def test_running_job_is_not_published(self):
        self.state.update(status='running', exit_code=None)
        self.assertEqual(self.publish()['status'], 'not_ready')
        self.assertFalse(self.destination.exists())

    def test_failed_job_keeps_failure_in_output_metadata(self):
        self.state.update(status='failed', exit_code=3)
        self.assertEqual(self.publish()['status'], 'published')
        metadata = json.loads((self.destination / 'run.json').read_text())
        self.assertEqual((metadata['status'], metadata['exit_code']), ('failed', 3))

    def test_other_run_directory_is_not_overwritten(self):
        self.destination.mkdir(parents=True)
        (self.destination / 'run.json').write_text('{"run_id":"foreign"}')
        (self.destination / 'keep.txt').write_text('keep')
        self.assertEqual(self.publish()['status'], 'unavailable')
        self.assertEqual((self.destination / 'keep.txt').read_text(), 'keep')
        self.assertTrue((self.artifacts / 'last.pth').is_file())

    def test_removed_published_file_is_reported_without_silent_overwrite(self):
        self.publish()
        (self.destination / 'artifacts/last.pth').unlink()
        result = self.publish()
        self.assertEqual(result['error_code'], 'OUTPUT_PUBLICATION_FAILED')
        self.assertTrue((self.artifacts / 'last.pth').is_file())

    def test_copy_failure_preserves_source_and_cleans_only_own_temp(self):
        with patch('app.runner.remote_agent.shutil.copy2', side_effect=OSError('fixture')):
            self.assertEqual(self.publish()['status'], 'unavailable')
        self.assertTrue((self.artifacts / 'last.pth').is_file())
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.iterdir()), [])
        diagnostic=json.loads((self.job/'publication_error.json').read_text())
        self.assertEqual(diagnostic['phase'],'copy_and_verify')
        self.assertTrue(diagnostic['trace'])
        self.assertNotIn('fixture',json.dumps(diagnostic))

    def test_source_symlink_is_rejected(self):
        # Windows 无须创建真正 symlink，也可验证拒绝路径；真实 Linux 链路另行检查。
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda p: p == self.artifacts / 'last.pth' or original(p)):
            self.assertEqual(self.publish()['status'], 'unavailable')
        self.assertFalse(self.destination.exists())

    def test_output_root_symlink_is_rejected(self):
        root = self.destination.parent
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda p: p == root or original(p)):
            self.assertEqual(self.publish()['status'], 'unavailable')

    def test_large_artifact_fails_before_copy(self):
        original = Path.stat
        def stat(path, *args, **kwargs):
            actual = original(path, *args, **kwargs)
            if path == self.artifacts / 'last.pth':
                values = list(actual); values[6] = 513 * 1024 * 1024
                return type(actual)(values)
            return actual
        with patch.object(Path, 'stat', stat):
            self.assertEqual(self.publish()['status'], 'unavailable')
        self.assertTrue((self.artifacts / 'last.pth').is_file())


class OutputContractTests(unittest.TestCase):
    def test_directory_config_rejects_hidden_absolute_traversal_and_shell_paths(self):
        for value in ('', '/tmp/results', '../results', '.hidden', 'results/.hidden', 'a//b', 'a\\b', 'a;cmd'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RunnerSettings(output_directory=value)

    def test_config_environment_loads_visible_root_without_touching_registry(self):
        with patch.object(config, 'load_dotenv'), patch.dict('os.environ', {'RUNNER_OUTPUT_DIRECTORY': 'results/PRNet'}, clear=True):
            settings = config.get_runner_settings()
        self.assertEqual(settings.output_directory, 'results/PRNet')

    def test_observation_rejects_unknown_output_status_or_raw_errors(self):
        baseline = {'status': 'published', 'directory': '/project/training_outputs/' + 'a' * 32,
                    'error_code': None, 'file_count': 1}
        for change in ({'status': 'fake'}, {'file_count': True}, {'error_code': 'PRIVATE'}, {'directory': 'relative'}):
            with self.subTest(change=change), self.assertRaises(RunnerProtocolError):
                validate_observation(observation('succeeded', outputs={**baseline, **change}))
