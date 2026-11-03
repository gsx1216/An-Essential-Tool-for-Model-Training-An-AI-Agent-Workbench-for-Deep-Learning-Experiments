"""在 Linux 随机临时目录验证固定编辑器；不触碰真实项目或调用模型/GPU。"""
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4


def verify(editor, policy):
    passed = []
    with TemporaryDirectory(prefix='codex_edit_checks_') as temp:
        root = Path(temp)
        raw = b'VALUE = 1\n'
        (root/'train.py').write_bytes(raw)
        (root/'models').mkdir(); (root/'models/net.py').write_text('class Net:\n    pass\n')
        (root/'weights').mkdir(); (root/'weights/old.pth').write_bytes(b'WEIGHTS_UNCHANGED')
        (root/'.env').write_text('password=DO_NOT_COPY')
        (root/'config.json').write_text('{"password":"DO_NOT_COPY"}')
        def request(*, edits=None, checks=None):
            plan = {'edits': edits or [{'path':'train.py','old_text':'VALUE = 1','new_text':'VALUE = 2','reason':'fixture'}],
                'explanation':'fixture', 'files':[{'path':'train.py','before_sha256':hashlib.sha256((root/'train.py').read_bytes()).hexdigest(),
                    'suggestion_sha256':hashlib.sha256(raw).hexdigest(),'source_changed_since_suggestion':False}],
                'project_checks': checks or [], 'version_scope':'current_project_files'}
            return {'change_id':uuid4().hex,'plan':plan,'plan_digest':editor.digest(editor.canonical(plan).encode()),
                'work_dir':str(root),'excluded_directory':'training_outputs'}

        first = request()
        result = editor.handle(first, policy)
        assert result['ok'], result
        data = result['data']; project = Path(data['workspace_directory'])
        assert project.relative_to(root).parts == ('code_changes', first['change_id'], 'project')
        assert (root/'train.py').read_bytes() == raw and (project/'train.py').read_bytes() == b'VALUE = 2\n'
        assert (project.parent/'before/train.py').read_bytes() == raw
        assert (root/'weights/old.pth').read_bytes() == b'WEIGHTS_UNCHANGED'
        assert not (project/'weights').exists() and not (project/'.env').exists() and not (project/'config.json').exists()
        assert data['check_status']=='incomplete' and any(v['status']=='not_configured' for v in data['checks'])
        passed.append('independent visible copy/diff and before file exist; original source, weights and credentials unchanged')
        assert editor.handle(first, policy) == result
        (project/'train.py').write_text('MANUAL = 9\n')
        assert editor.handle(first, policy) == result and (project/'train.py').read_text()=='MANUAL = 9\n'
        passed.append('same modification ID reuses completed result without reapplying over a user-edited candidate')

        stale = request(); stale['plan']['files'][0]['before_sha256']='0'*64
        stale['plan_digest']=editor.digest(editor.canonical(stale['plan']).encode())
        assert editor.handle(stale, policy)['code']=='SOURCE_CHANGED'
        assert (root/'train.py').read_bytes()==raw
        passed.append('current file hash mismatch stops before edits; source remains untouched')

        bad = request(edits=[{'path':'../escape.py','old_text':'VALUE = 1','new_text':'VALUE = 2','reason':'fixture'}])
        assert editor.handle(bad, policy)['ok'] is False
        (root/'linked.py').symlink_to(root/'train.py')
        bad=request(); bad['plan']['files'][0]['path']='linked.py'; bad['plan']['edits'][0]['path']='linked.py'
        bad['plan_digest']=editor.digest(editor.canonical(bad['plan']).encode())
        assert editor.handle(bad, policy)['ok'] is False
        passed.append('traversal and source symlink are rejected without writes outside the candidate')

        (root/'train.py').write_bytes(b'VALUE = 1\nVALUE = 1\n')
        assert editor.handle(request(),policy)['code']=='REPLACEMENT_CONFLICT'
        (root/'train.py').write_bytes(raw)
        passed.append('ambiguous old text is a conflict rather than a multi-location replacement')

        invalid = request(edits=[{'path':'train.py','old_text':'VALUE = 1','new_text':'VALUE = (','reason':'fixture'}])
        invalid = editor.handle(invalid,policy)
        assert invalid['ok'] and invalid['data']['check_status']=='failed'
        assert (root/'train.py').read_bytes()==raw
        passed.append('syntax failure is retained as failed checks; not presented as a runnable model')

        (root/'tests').mkdir(); (root/'tests/test_value.py').write_text('import unittest\nfrom train import VALUE\nclass TestValue(unittest.TestCase):\n    def test_value(self): self.assertEqual(VALUE, 2)\n')
        checked = request(checks=[{'name':'cpu_fixture','module':'unittest','arguments':['discover','-s','tests'],'timeout_seconds':10}])
        checked = editor.handle(checked,policy)
        assert checked['ok'] and checked['data']['check_status']=='passed', checked
        assert next(v for v in checked['data']['checks'] if v['kind']=='project')['status']=='passed'
        passed.append('registered CPU unittest runs in the independent copy and reports actual exit status')

        (root/'tests/test_value.py').write_text('import unittest\nclass TestValue(unittest.TestCase):\n    def test_value(self): self.fail("intentional failure")\n')
        failed = editor.handle(request(checks=[{'name':'cpu_fixture','module':'unittest','arguments':['discover','-s','tests'],'timeout_seconds':10}]),policy)
        assert failed['ok'] and failed['data']['check_status']=='failed'
        passed.append('registered test failure is explicit and does not change original project code')

        interrupted = request(); folder=root/'code_changes'/interrupted['change_id']; folder.mkdir()
        (folder/'request.json').write_text(editor.canonical({'plan_digest':interrupted['plan_digest']}))
        assert editor.handle(interrupted,policy)['code']=='WORKSPACE_INCOMPLETE'
        wrong = request(); folder=root/'code_changes'/wrong['change_id']; folder.mkdir(); (folder/'user.txt').write_text('preserve')
        assert editor.handle(wrong,policy)['code']=='WORKSPACE_CONFLICT' and (folder/'user.txt').read_text()=='preserve'
        passed.append('partial or pre-existing foreign workspace is not silently overwritten')

        (root/'train.py').write_bytes(b'\xef\xbb\xbfVALUE = 1\r\n')
        crlf = editor.handle(request(),policy)
        assert crlf['ok'] and (Path(crlf['data']['workspace_directory'])/'train.py').read_bytes()==b'\xef\xbb\xbfVALUE = 2\r\n'
        passed.append('UTF-8 BOM and CRLF are preserved during exact replacement')

        (root/'train.py').write_bytes(b'VALUE = 1')
        nonewline = editor.handle(request(),policy)
        assert nonewline['ok'] and '\\ No newline at end of file' in nonewline['data']['files'][0]['diff']
        passed.append('diff explicitly marks files without a final newline')

        (root/'train.py').write_bytes(raw)
        special=request(); folder=root/'code_changes'/special['change_id']; folder.mkdir(); os.mkfifo(str(folder/'lock'))
        assert editor.handle(special,policy)['ok'] is False
        passed.append('special workspace lock file is rejected without blocking')

        (root/'raw_samples').mkdir()
        for number in range(35): (root/'raw_samples'/('sample_'+str(number)+'.txt')).write_text('sample')
        previous=editor.MAX_ENTRIES;editor.MAX_ENTRIES=30
        try:
            excluded=request();excluded['plan']['excluded_directories']=['raw_samples']
            excluded['plan_digest']=editor.digest(editor.canonical(excluded['plan']).encode())
            excluded=editor.handle(excluded,policy)
            assert excluded['ok'] and not (Path(excluded['data']['workspace_directory'])/'raw_samples').exists()
            assert editor.handle(request(),policy)['code']=='SNAPSHOT_TOO_LARGE'
        finally:editor.MAX_ENTRIES=previous
        passed.append('registered raw dataset exclusion avoids traversal; unexcluded oversized tree fails with an explicit bound')
    return passed
