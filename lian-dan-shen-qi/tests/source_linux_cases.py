"""Linux/Python 3.8+ 标准库验收；reader 为只读辅助模块，目录仅在 /tmp 临时创建。"""

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory


def verify(reader):
    groups = []
    with TemporaryDirectory(prefix='source_reader_check_') as temporary:
        root = Path(temporary) / 'project'
        root.mkdir()
        source = b"import os\nraise RuntimeError('must never execute this source')\nvalue = 3\n"
        (root / 'train.py').write_bytes(source)
        (root / 'model.py').write_text('class Model:\n    pass\n', encoding='utf-8')
        (root / '.env').write_text('password=not-for-output', encoding='utf-8')
        (root / 'weights.pth').write_bytes(b'not source')
        (root / 'results').mkdir()
        (root / 'results/output.py').write_text('private_output = 1', encoding='utf-8')
        (root / 'data').mkdir()
        (root / 'data/loaders.py').write_text('def load():\n    return []\n', encoding='utf-8')
        (root / 'data/labels.txt').write_text('private dataset rows', encoding='utf-8')

        def query(action='read', path='train.py', **options):
            return reader.handle({'action': action, 'work_dir': str(root), 'path': path,
                                  'excluded_directory': 'results', **options})

        def denied(path, code='PATH_NOT_ALLOWED', **options):
            result = query(path=path, **options)
            assert result == {'ok': False, 'code': code}, result

        result = query(start_line=2, line_count=1)
        assert result['ok'] and result['data']['lines'] == [{'number': 2, 'text': "raise RuntimeError('must never execute this source')"}]
        assert result['data']['next_line'] == 3
        assert result['data']['file_sha256'] == hashlib.sha256(source).hexdigest()
        groups.append('numbered current source is read without execution')

        first = query('list', '.', limit=1)['data']
        second = query('list', '.', offset=first['next_offset'], limit=50)['data']
        paths = [x['path'] for x in first['items'] + second['items']]
        assert '.env' not in paths and 'weights.pth' not in paths and 'results' not in paths
        assert 'train.py' in paths
        groups.append('directory paging excludes hidden credentials, weights and configured outputs')

        for path in ('../outside.py', '/etc/passwd', 'a/../train.py', '.env', 'weights.pth', 'results/output.py', 'data/labels.txt'):
            denied(path)
        assert query(path='data/loaders.py')['ok']
        groups.append('traversal and dataset text rejected while data loader implementation remains readable')

        outside = Path(temporary) / 'outside.py'
        outside.write_text('outside = True', encoding='utf-8')
        (root / 'link.py').symlink_to(outside)
        (root / 'linked_dir').symlink_to(Path(temporary), target_is_directory=True)
        denied('link.py')
        denied('linked_dir/outside.py')
        groups.append('symlink files and intermediate symlink directories cannot escape the root')

        denied('missing.py', 'NOT_FOUND')
        denied('train.py', 'SOURCE_CHANGED', expected_sha256='0'*64)
        digest = result['data']['file_sha256']
        assert query(start_line=3, line_count=1, expected_sha256=digest)['ok']
        (root / 'train.py').write_text('changed = True\n', encoding='utf-8')
        denied('train.py', 'SOURCE_CHANGED', expected_sha256=digest)
        groups.append('missing files and cross-page version changes are explicit errors')

        (root / 'config.json').write_text('{"password":\n "not-for-model"}', encoding='utf-8')
        denied('config.json', 'SENSITIVE_CONTENT', start_line=2)
        (root / 'bad.py').write_bytes(b'\xff\x00')
        denied('bad.py', 'UNSUPPORTED_TEXT')
        groups.append('full-file credential scanning and invalid text rejection happen before excerpt delivery')

        (root / 'large.py').write_bytes(b'x' * (reader.MAX_FILE_BYTES + 1))
        denied('large.py', 'FILE_TOO_LARGE')
        (root / 'long.py').write_text('x' * (reader.MAX_LINES_BYTES + 1), encoding='utf-8')
        denied('long.py', 'LINE_TOO_LONG')
        (root / 'many.py').write_text('value = 1\n' * 400, encoding='utf-8')
        page = query(path='many.py', line_count=60)['data']
        assert len(page['lines']) < 60 and page['next_line'] == page['end_line'] + 1
        groups.append('file and excerpt limits keep whole lines and a usable next cursor')

        os.mkfifo(str(root / 'pipe.py'))
        denied('pipe.py')
        groups.append('special files are rejected without blocking a read')
    return groups


if __name__ == '__main__':
    from app.runner import source_reader
    for group in verify(source_reader):
        print('PASS:', group)
