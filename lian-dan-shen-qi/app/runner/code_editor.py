"""Linux 标准库代码副本编辑器；仅写 code_changes/<ID>，从不覆盖源项目文件。"""
import ast
import difflib
import errno
import fcntl
import hashlib
import importlib.machinery
import json
import os
import re
import signal
import stat
import subprocess
import sys

MAX_SNAPSHOT_BYTES = 20971520
MAX_FILES = 512
MAX_ENTRIES = 4096


class EditError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value): return hashlib.sha256(value).hexdigest()


def _open_dir(parent, name, create=False):
    if create:
        try: os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError: pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _parent(root, path):
    parts = path.split('/')
    fd = os.dup(root)
    try:
        for part in parts[:-1]:
            child = _open_dir(fd, part, True)
            os.close(fd); fd = child
        return fd, parts[-1]
    except BaseException:
        os.close(fd); raise


def _write(root, path, data, *, replace=False):
    parent, name = _parent(root, path)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_TRUNC if replace else os.O_EXCL)
        fd = os.open(name, flags, 0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise EditError('WORKSPACE_CONFLICT')
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    finally:
        os.close(parent)


def _read_json(root, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise EditError('WORKSPACE_CONFLICT')
        raw = stream.read(131073)
    if len(raw) > 131072: raise EditError('WORKSPACE_CONFLICT')
    try: return json.loads(raw)
    except (ValueError, UnicodeError): raise EditError('WORKSPACE_CONFLICT')


def _atomic_json(root, name, value):
    _write(root, name + '.tmp', canonical(value).encode())
    os.rename(name + '.tmp', name, src_dir_fd=root, dst_dir_fd=root)


def _replacements(raw, edits):
    try: text = raw.decode('utf-8-sig')
    except UnicodeError: raise EditError('UNSUPPORTED_TEXT')
    eol = '\r\n' if '\r\n' in text else '\n'
    if '\r' in text.replace('\r\n', '') or eol == '\r\n' and '\n' in text.replace('\r\n', ''):
        raise EditError('UNSUPPORTED_TEXT')
    current = text.replace('\r\n', '\n')
    for edit in edits:
        old, new = edit['old_text'], edit['new_text']
        if not old or old == new or current.count(old) != 1:
            raise EditError('REPLACEMENT_CONFLICT')
        current = current.replace(old, new, 1)
    output = current.replace('\n', eol).encode('utf-8')
    if raw.startswith(b'\xef\xbb\xbf'): output = b'\xef\xbb\xbf' + output
    if len(output) > 1048576: raise EditError('FILE_TOO_LARGE')
    return output


def _snapshot(policy, work_dir, source, target, prefix, excluded, counters):
    with os.scandir(source) as scan:
        entries = []
        for entry in scan:
            if len(entries) + counters['entries'] >= MAX_ENTRIES: raise EditError('SNAPSHOT_TOO_LARGE')
            entries.append(entry)
        entries.sort(key=lambda entry: entry.name)
    for entry in entries:
        counters['entries'] += 1
        if counters['entries'] > MAX_ENTRIES: raise EditError('SNAPSHOT_TOO_LARGE')
        path = prefix + entry.name
        try:
            info = entry.stat(follow_symlinks=False)
            directory = stat.S_ISDIR(info.st_mode)
            if not directory and not stat.S_ISREG(info.st_mode):
                counters['skipped'] += 1; continue
            policy['relative_path'](path, directory=directory)
            if any(policy['_excluded'](path, folder) for folder in excluded):
                counters['skipped'] += 1; continue
        except (OSError, policy['SourceReadError']):
            counters['skipped'] += 1; continue
        if directory:
            child = _open_dir(source, entry.name)
            dest = _open_dir(target, entry.name, True)
            try: _snapshot(policy, work_dir, child, dest, path + '/', excluded, counters)
            finally: os.close(child); os.close(dest)
            continue
        try:
            raw, _, _ = policy['read_bytes'](work_dir, path)
            policy['excerpt'](raw, 1, 1)  # 全文件凭据/编码检查；不执行源码。
        except policy['SourceReadError']:
            counters['skipped'] += 1; continue
        counters['files'] += 1; counters['bytes'] += len(raw)
        if counters['files'] > MAX_FILES or counters['bytes'] > MAX_SNAPSHOT_BYTES:
            raise EditError('SNAPSHOT_TOO_LARGE')
        _write(target, entry.name, raw)


def _redact(text):
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)', '[REDACTED]', text, flags=re.S)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[REDACTED]', text)
    text = re.sub(r'(?i)\bBearer\s+[^\s"\'<>]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|password|passwd|access[_-]?token|private[_-]?key|secret)\s*["\']?\s*[:=]\s*)[^\r\n,;]+', r'\1[REDACTED]', text)
    return ''.join(c for c in text if c in '\n\r\t' or ord(c) >= 32)[:1200]


def _checks(project_path, changed, checks, workspace):
    results = []
    python_files = [(path, raw) for path, raw in changed.items() if path.endswith('.py')]
    if not python_files:
        results.append({'kind': 'syntax', 'name': 'python', 'status': 'not_configured', 'detail': '本次未修改 Python 文件。'})
    for path, raw in python_files:
        try:
            tree = ast.parse(raw.decode('utf-8-sig'), filename=path)
        except (SyntaxError, UnicodeError) as error:
            results.append({'kind': 'syntax', 'name': path, 'status': 'failed', 'detail': 'Python 语法无法解析，行号：' + str(getattr(error, 'lineno', None))})
            continue
        results.append({'kind': 'syntax', 'name': path, 'status': 'passed', 'detail': 'ast.parse 通过；未导入或执行此文件。'})
        missing = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import): names = [item.name.split('.')[0] for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0: names = [(node.module or '').split('.')[0]]
            elif isinstance(node, ast.ImportFrom):
                package = path.split('/')[:-1]
                if node.level > len(package):
                    missing.add('.' * node.level + (node.module or '')); continue
                base = package[:len(package)-node.level+1] if node.level <= len(package) else []
                candidate = os.path.join(project_path, *(base + (node.module or '').split('.')))
                if not (os.path.isfile(candidate+'.py') or os.path.isdir(candidate)):
                    missing.add('.' * node.level + (node.module or ''))
                continue
            else: continue
            for name in names:
                if not name or name in sys.builtin_module_names: continue
                try: found = importlib.machinery.PathFinder.find_spec(name, [project_path] + sys.path)
                except (ImportError, ValueError, OSError): found = None
                if found is None: missing.add(name)
        detail = '静态检查当前辅助 Python 的顶层模块可发现性；未执行导入，不证明依赖版本或运行时兼容。'
        if missing: detail += ' 未找到：' + ', '.join(sorted(missing))[:600]
        results.append({'kind': 'imports', 'name': path, 'status': 'incomplete' if missing else 'passed', 'detail': detail})
    if not checks:
        results.append({'kind': 'project', 'name': 'registered_checks', 'status': 'not_configured', 'detail': '部署者没有为此项目登记测试；未执行项目测试。'})
    elif any(row['kind'] == 'syntax' and row['status'] == 'failed' for row in results):
        results.append({'kind': 'project', 'name': 'registered_checks', 'status': 'incomplete', 'detail': '语法失败，未继续执行项目测试。'})
    else:
        for index, check in enumerate(checks):
            log = os.open('check_' + str(index+1) + '.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=workspace)
            proc = None; timed_out = False
            try:
                env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8', 'CUDA_VISIBLE_DEVICES': '',
                    'OMP_NUM_THREADS': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
                # 文件输出上限放在子进程；避免测试无限输出占满服务器磁盘。
                def limits():
                    import resource
                    resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
                proc = subprocess.Popen([sys.executable, '-B', '-m', check['module']] + check['arguments'],
                    cwd=project_path, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True, preexec_fn=limits)
                try: proc.wait(timeout=check['timeout_seconds'])
                except subprocess.TimeoutExpired: timed_out = True
            finally:
                if proc is not None:
                    try: os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    proc.wait()
                os.close(log)
            fd = os.open('check_' + str(index+1) + '.log', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=workspace)
            with os.fdopen(fd, 'rb') as stream: output = _redact(stream.read(4096).decode('utf-8', errors='replace'))
            results.append({'kind': 'project', 'name': check['name'], 'status': 'failed' if timed_out or proc.returncode != 0 else 'passed',
                'detail': ('检查超时。' if timed_out else '退出码：' + str(proc.returncode) + '。') + output})
    status = ('failed' if any(row['status'] == 'failed' for row in results) else 'incomplete'
              if any(row['status'] in {'incomplete', 'not_configured'} for row in results) else 'passed')
    return results, status


def apply(request, policy):
    identifier, plan = request['change_id'], request['plan']
    if not re.fullmatch(r'[0-9a-f]{32}', identifier) or digest(canonical(plan).encode()) != request['plan_digest']:
        raise EditError('INVALID_PLAN')
    expected = {row['path']: row['before_sha256'] for row in plan['files']}
    if not 1 <= len(expected) <= 3 or not 1 <= len(plan['edits']) <= 6 or len(plan['project_checks']) > 3:
        raise EditError('INVALID_PLAN')
    root = policy['_directory'](request['work_dir'], '.')
    parent = workspace = lock = None
    try:
        parent = _open_dir(root, 'code_changes', True)
        workspace = _open_dir(parent, identifier, True)
        lock = os.open('lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=workspace)
        if not stat.S_ISREG(os.fstat(lock).st_mode): raise EditError('WORKSPACE_CONFLICT')
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise EditError('WORKSPACE_BUSY')
        marker = _read_json(workspace, 'request.json')
        if marker is not None:
            if marker != {'plan_digest': request['plan_digest']}:
                raise EditError('WORKSPACE_CONFLICT')
            saved = _read_json(workspace, 'result.json')
            if saved is not None: return saved
            raise EditError('WORKSPACE_INCOMPLETE')
        if set(os.listdir(workspace)) != {'lock'}: raise EditError('WORKSPACE_CONFLICT')
        changes, modified, originals = [], {}, {}
        for path, sha in expected.items():
            policy['relative_path'](path)
            if any(policy['_excluded'](path, folder) for folder in [request.get('excluded_directory')]+plan.get('excluded_directories',[])):
                raise EditError('PATH_NOT_ALLOWED')
            raw, _, _ = policy['read_bytes'](request['work_dir'], path, sha)
            policy['excerpt'](raw, 1, 1)
            edits = [edit for edit in plan['edits'] if edit['path'] == path]
            if not edits: raise EditError('INVALID_PLAN')
            new = _replacements(raw, edits)
            if new == raw: raise EditError('INVALID_PLAN')
            policy['excerpt'](new, 1, 1)
            pieces = difflib.unified_diff(raw.decode('utf-8-sig').splitlines(True), new.decode('utf-8-sig').splitlines(True), fromfile='a/'+path, tofile='b/'+path)
            diff = ''.join(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n' for line in pieces)
            if len(diff.encode()) > 49152: raise EditError('DIFF_TOO_LARGE')
            changes.append({'path': path, 'before_sha256': sha, 'after_sha256': digest(new), 'diff': diff})
            modified[path], originals[path] = new, raw
        if any(edit['path'] not in expected for edit in plan['edits']): raise EditError('INVALID_PLAN')
        _atomic_json(workspace, 'request.json', {'plan_digest': request['plan_digest']})
        project = _open_dir(workspace, 'project', True)
        before = _open_dir(workspace, 'before', True)
        counters = {'entries': 0, 'skipped': 0, 'files': 0, 'bytes': 0}
        try:
            _snapshot(policy, request['work_dir'], root, project, '', [request.get('excluded_directory')]+plan.get('excluded_directories',[]), counters)
            for path, sha in expected.items():
                policy['read_bytes'](request['work_dir'], path, sha)
                # 用完整当前版本覆盖刚复制的目标，模型仅能改这些已读文件。
                _write(before, path, originals[path])
                _write(project, path, modified[path], replace=True)
        finally:
            os.close(project); os.close(before)
        project_path = os.path.join(request['work_dir'], 'code_changes', identifier, 'project')
        checks, status = _checks(project_path, modified, plan['project_checks'], workspace)
        result = {'change_id': identifier, 'plan_digest': request['plan_digest'], 'workspace_directory': project_path,
            'files': changes, 'checks': checks, 'check_status': status, 'snapshot_file_count': counters['files'],
            'skipped_entry_count': counters['skipped'], 'python_executable': sys.executable,
            'notice': '仅修改独立源码副本；未覆盖原项目或启动训练。副本排除凭据、隐藏文件、数据/权重及产物；不同文件并非原子快照。静态导入检查不执行导入；项目测试仅运行部署者登记项。'}
        if len(canonical(result).encode()) > 131072: raise EditError('RESULT_TOO_LARGE')
        _atomic_json(workspace, 'result.json', result)
        return result
    finally:
        for fd in (lock, workspace, parent, root):
            if fd is not None: os.close(fd)


def handle(request, policy):
    try: return {'ok': True, 'data': apply(request, policy)}
    except (EditError, policy['SourceReadError']) as error: return {'ok': False, 'code': error.code}
    except (KeyError, ValueError, TypeError, UnicodeError): return {'ok': False, 'code': 'INVALID_PLAN'}
    except OSError as error:
        return {'ok': False, 'code': 'PATH_NOT_ALLOWED' if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM} else 'EDIT_UNAVAILABLE'}


if __name__ == '__main__' and 'request' in globals():
    policy = {'__name__': 'source_policy'}
    exec(request.pop('_reader_source'), policy)
    print(canonical(handle(request, policy)))
