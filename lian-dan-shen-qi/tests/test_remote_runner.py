"""真实标准库监督进程执行微型脚本；Windows 分支不冒充 Linux flock/进程组验收。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import uuid4

from app.services.runner_transport import BOOTSTRAP
from app.models.runner import launch_digest

SOURCE = (Path(__file__).resolve().parents[1] / 'app/runner/remote_agent.py').read_text(encoding='utf-8')
SCRIPT = '''import argparse,json,time,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--config');a=p.parse_args()
d=json.loads(Path(a.config).read_text())
with Path('launch_count.txt').open('a') as f:f.write('launch\\n')
print('epoch=1 loss=0.25',flush=True)
print('x'*d.get('output',1),flush=True)
time.sleep(d.get('delay',0))
sys.exit(d.get('exit_code',0))
'''


class RemoteRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.work = Path(self.temp.name)
        (self.work/'train.py').write_text(SCRIPT, encoding='utf-8')
        self.launch = {'work_dir': '/' + self.work.as_posix() if sys.platform == 'win32' else self.work.as_posix(),
                       'run_id': uuid4().hex, 'spec': {'path': 'train.py', 'sha256': hashlib.sha256((self.work/'train.py').read_bytes()).hexdigest(),
                       'max_runtime_seconds': 10, 'max_log_bytes': 1024}, 'parameters': {}}
        self.job_id = uuid4().hex

    def request(self, action, **options):
        text = json.dumps(self.launch, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        payload = {'action': action, 'job_id': self.job_id, 'launch': self.launch, 'digest': launch_digest(text), '_source': SOURCE, **options}
        result = subprocess.run([sys.executable, '-B', '-c', BOOTSTRAP], input=json.dumps(payload).encode(), capture_output=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        decoded = json.loads(result.stdout)
        self.assertTrue(decoded.get('ok'), decoded)
        return decoded['observation']

    def wait_terminal(self):
        end = time.monotonic()+12
        while time.monotonic() < end:
            result = self.request('status')
            if result['status'] in {'succeeded', 'failed', 'cancelled'}:
                # 最终元数据已写入，给子进程正常退出、关闭句柄的短暂机会。
                time.sleep(.15)
                return result
            time.sleep(.05)
        self.fail('supervisor did not finish')

    def tearDown(self):
        try:
            self.request('stop'); self.wait_terminal()
        finally:
            self.temp.cleanup()

    def test_success_logs_and_repeated_start_execute_exactly_once(self):
        self.request('start'); self.assertEqual(self.wait_terminal()['status'], 'succeeded')
        self.request('start')
        self.assertEqual((self.work/'launch_count.txt').read_text().count('launch'), 1)
        result = self.request('logs', offset=0, limit=8)
        self.assertEqual(result['next_offset'], 8); self.assertTrue(result['has_more'])

    def test_completed_script_artifacts_are_published_without_second_training(self):
        script = self.work / 'train.py'
        script.write_text(SCRIPT.replace("sys.exit(d.get('exit_code',0))", "out=Path(a.config).parent/'artifacts';out.mkdir();(out/'last.pth').write_bytes(b'CPU fixture weights');sys.exit(0)"), encoding='utf-8')
        self.launch['spec']['sha256'] = hashlib.sha256(script.read_bytes()).hexdigest()
        self.launch['output_directory'] = 'results/PRNet'
        self.request('start')
        observed = self.wait_terminal()
        self.assertEqual(observed['outputs']['status'], 'published')
        output = self.work / 'results/PRNet' / self.launch['run_id'] / 'artifacts/last.pth'
        self.assertEqual(output.read_bytes(), b'CPU fixture weights')
        self.request('status')
        self.assertEqual((self.work / 'launch_count.txt').read_text().count('launch'), 1)

    def test_existing_terminal_job_can_publish_again_without_retraining(self):
        self.request('start'); self.wait_terminal()
        destination = self.work / 'training_outputs' / self.launch['run_id']
        import shutil
        shutil.rmtree(destination)
        self.assertEqual(self.request('status')['outputs']['status'], 'published')
        self.assertTrue((destination / 'output.log').is_file())
        self.assertEqual((self.work / 'launch_count.txt').read_text().count('launch'), 1)

    def test_concurrent_start_has_one_training_subprocess(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.request('start'), range(4)))
        self.wait_terminal()
        self.assertEqual((self.work/'launch_count.txt').read_text().count('launch'), 1)

    def test_nonzero_exit_is_failed(self):
        self.launch['parameters'] = {'exit_code': 3}
        self.request('start'); result=self.wait_terminal()
        self.assertEqual((result['status'],result['exit_code']), ('failed',3))

    def test_changed_script_never_executes(self):
        (self.work/'train.py').write_text('print("CHANGED")',encoding='utf-8')
        self.request('start'); result=self.wait_terminal()
        self.assertEqual(result['error_code'],'SCRIPT_CHANGED')
        self.assertFalse((self.work/'launch_count.txt').exists())

    def test_stop_before_start_prevents_future_launch(self):
        self.assertEqual(self.request('stop')['status'],'cancelled')
        self.assertEqual(self.request('start')['status'],'cancelled')
        self.assertFalse((self.work/'launch_count.txt').exists())

    def test_cancel_running_job_and_replay_does_not_restart(self):
        self.launch['parameters']={'delay':20}
        self.request('start')
        end=time.monotonic()+5
        while not (self.work/'launch_count.txt').exists() and time.monotonic()<end:
            time.sleep(.05)
        self.request('stop'); self.assertEqual(self.wait_terminal()['status'],'cancelled')
        self.request('start')
        self.assertEqual((self.work/'launch_count.txt').read_text().count('launch'),1)

    def test_runtime_limit_kills_job_and_log_file_is_bounded(self):
        self.launch['spec'].update(max_runtime_seconds=1,max_log_bytes=100)
        self.launch['parameters']={'delay':20,'output':100000}
        self.request('start'); result=self.wait_terminal()
        self.assertEqual(result['error_code'],'RUNTIME_LIMIT')
        self.assertLessEqual(result['log_bytes'],100)
        self.assertTrue(self.request('logs',offset=0,limit=100)['truncated'])

    def test_missing_observation_is_explicit_and_does_not_launch(self):
        self.assertEqual(self.request('status')['status'],'missing')
        self.assertFalse((self.work/'launch_count.txt').exists())

    def test_stale_heartbeat_is_uncertain_and_start_does_not_relaunch(self):
        self.request('start');self.wait_terminal()
        path=self.work/'.experiment-assistant'/self.job_id/'state.json'
        saved=json.loads(path.read_text())
        try:
            path.write_text(json.dumps({**saved,'status':'running','exit_code':None,'heartbeat':time.time()-60}))
            self.assertTrue(self.request('status')['stale'])
            self.assertEqual(self.request('start')['status'],'running')
            self.assertEqual((self.work/'launch_count.txt').read_text().count('launch'),1)
        finally:
            path.write_text(json.dumps(saved))

    def test_remote_job_cannot_be_rebound_to_changed_launch(self):
        self.request('start');self.wait_terminal()
        changed={**self.launch,'parameters':{'delay':1}}
        text=json.dumps(changed,ensure_ascii=True,sort_keys=True,separators=(',',':'))
        payload={'action':'start','job_id':self.job_id,'launch':changed,'digest':launch_digest(text),'_source':SOURCE}
        result=subprocess.run([sys.executable,'-B','-c',BOOTSTRAP],input=json.dumps(payload).encode(),capture_output=True,timeout=12)
        self.assertEqual(json.loads(result.stdout),{'ok':False})
        self.assertEqual((self.work/'launch_count.txt').read_text().count('launch'),1)
