"""标准库 Linux CPU 验收；调用者注入固定辅助源码，所有写入在临时目录。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
from uuid import uuid4


def verify(helper_source, runner_source):
    helper=types.ModuleType('revision_helper_fixture');exec(helper_source,helper.__dict__)
    passed=[]
    with tempfile.TemporaryDirectory(prefix='revision_cpu_') as directory:
        root=Path(directory);change=uuid4().hex
        project=root/'code_changes'/change/'project';project.mkdir(parents=True)
        (root/'adapters').mkdir();(project/'adapters').mkdir()
        (root/'helper.py').write_bytes(b'value = 1\n');(project/'helper.py').write_bytes(b'value = 2\n')
        original=b'print("ORIGINAL MUST NOT RUN")\n'
        (root/'adapters/entry.py').write_bytes(original)
        script=b'''import argparse,os,json
from pathlib import Path
from helper import value
assert value==2
p=argparse.ArgumentParser();p.add_argument('--config');a=p.parse_args()
assert Path(os.environ['EXPERIMENT_INPUT_ROOT']).is_dir()
with (Path(os.environ['EXPERIMENT_INPUT_ROOT'])/'count.txt').open('a') as f:f.write('one\\n')
out=Path(os.environ['EXPERIMENT_ARTIFACT_DIR']);out.mkdir()
(out/'weights.txt').write_text('CPU candidate version 2')
print('{"type":"metric","step":1,"dataset":"cod","split":"test","mae":0.2}',flush=True)
'''
        (project/'adapters/entry.py').write_bytes(script)
        payload={'work_dir':str(root),'change_id':change,'spec':{'path':'adapters/entry.py','sha256':helper.digest(original)},
            'changed_files':[{'path':'adapters/entry.py','after_sha256':helper.digest(script)}],'mode':'cwd'}
        inspection=helper.inspect(payload)
        assert (root/'adapters/entry.py').read_bytes()==original
        passed.append('read-only inspection permits modified generic entry without executing it')

        link=project/'linked.py';link.symlink_to(root/'adapters/entry.py')
        try:helper.inspect(payload)
        except helper.RevisionError:pass
        else:raise AssertionError('symlink accepted')
        link.unlink();passed.append('candidate symlink rejected')

        fifo=project/'pipe.py';os.mkfifo(str(fifo))
        try:helper.inspect(payload)
        except helper.RevisionError:pass
        else:raise AssertionError('FIFO accepted')
        fifo.unlink();passed.append('special-file rejection does not block reading')

        (project/'.env').write_bytes(b'do not copy')
        try:helper.inspect(payload)
        except helper.RevisionError:pass
        else:raise AssertionError('hidden input accepted')
        (project/'.env').unlink();passed.append('hidden/private inputs not silently included in source inventory')

        run_id,job_id=uuid4().hex,uuid4().hex
        launch={'work_dir':str(root),'run_id':run_id,'spec':{**payload['spec'],'max_runtime_seconds':10,'max_log_bytes':16384},
            'parameters':{},'revision':{**inspection,'change_id':change},'revision_helper_source':helper_source}
        def call(action,**options):
            value={'action':action,'job_id':job_id,'launch':launch,
                'digest':helper.digest(helper.canonical(launch).encode()),'_source':runner_source,**options}
            command='import json,sys;request=json.load(sys.stdin);source=request.pop("_source");exec(source)'
            result=subprocess.run([sys.executable,'-B','-c',command],input=json.dumps(value).encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
            assert result.returncode==0 and not result.stderr,result.stderr.decode(errors='replace')
            response=json.loads(result.stdout);assert response['ok'],response
            return response['observation']
        call('start')
        deadline=time.monotonic()+15
        while True:
            observed=call('status')
            if observed['status'] in {'succeeded','failed','cancelled'}:break
            if time.monotonic()>deadline:raise AssertionError('CPU runner timeout')
            time.sleep(.05)
        assert observed['status']=='succeeded',observed
        assert (root/'training_outputs'/run_id/'artifacts/weights.txt').read_text()=='CPU candidate version 2'
        assert '"mae":0.2' in call('logs',offset=0,limit=4096)['content']
        passed.append('real Linux supervisor executes candidate and publishes CPU artifacts plus metrics')
        (project/'adapters/entry.py').write_bytes(b'raise RuntimeError("after start")\n')
        call('start');assert (root/'count.txt').read_text().count('one')==1
        assert (root/'adapters/entry.py').read_bytes()==original
        frozen=root/'.experiment-assistant'/job_id/'revision_source'
        assert (frozen/'adapters/entry.py').read_bytes()==script
        passed.append('repeated start reuses job and immutable source after candidate manual changes')

        job_id,run_id=uuid4().hex,uuid4().hex;launch['run_id']=run_id
        call('start');deadline=time.monotonic()+15
        while True:
            observed=call('status')
            if observed['status'] in {'succeeded','failed','cancelled'}:break
            if time.monotonic()>deadline:raise AssertionError('changed candidate timeout')
            time.sleep(.05)
        assert observed['error_code']=='REVISION_SOURCE_CHANGED'
        assert (root/'count.txt').read_text().count('one')==1
        passed.append('post-plan source mismatch fails explicitly before launching training')

    return passed
