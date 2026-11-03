"""Linux 只读源码操作；仅标准库，不导入、编译或执行用户项目代码。"""

import errno
import hashlib
import json
import os
import re
import stat
import shutil
import sys
import subprocess
import csv
import io

MAX_FILE_BYTES = 1048576
MAX_LINES_BYTES = 1800  # 片段与其行号可以完整存入现有 4096 字节工具审计。
EXTENSIONS = {'.py', '.yaml', '.yml', '.json', '.toml', '.ini', '.cfg', '.md', '.txt'}
SKIP_DIRS = {'weights', 'checkpoints', 'pretrained', 'training_outputs',
             'outputs', 'logs', 'node_modules', 'venv', 'env', '__pycache__', 'code_changes'}
SENSITIVE_NAME = re.compile(r'(?i)(?:^|[_.-])(?:secrets?|credentials?|passwords?|passwd|api[_-]?keys?|private[_-]?keys?|access[_-]?tokens?)(?:$|[_.-])')
SENSITIVE_CONTENT = re.compile(r'(?i)-----BEGIN [^-]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{8,}|\bBearer\s+[A-Za-z0-9_.-]{8,}|\b(?:api[_-]?key|password|passwd|private[_-]?key|access[_-]?token|auth[_-]?token)\b["\']?\s*[:=]')


class SourceReadError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)  # 只含固定错误分类，不携带路径、源码或凭据。


def relative_path(value, *, directory=False):
    if directory and value == '.':
        return value
    if (type(value) is not str or not value or len(value.encode('utf-8')) > 512
        or any(ord(c) < 32 or ord(c) == 127 for c in value) or any(c in value for c in '\\:%')
        or any(not part or part.startswith('.') or part.lower() in SKIP_DIRS or SENSITIVE_NAME.search(part)
               or part.lower() in {'id_rsa', 'id_ed25519', 'authorized_keys', 'known_hosts'} for part in value.split('/'))):
        raise SourceReadError('PATH_NOT_ALLOWED')
    if not directory and os.path.splitext(value)[1].lower() not in EXTENSIONS:
        raise SourceReadError('PATH_NOT_ALLOWED')
    # data/loaders.py 等属于实现代码；数据目录下的样本清单、标签和原始文本不属于源码。
    if (not directory and any(p.lower() in {'data', 'dataset', 'datasets'} for p in value.split('/')[:-1])
        and not value.lower().endswith('.py')):
        raise SourceReadError('PATH_NOT_ALLOWED')
    return value


def _bounded(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise SourceReadError('INVALID_ARGUMENTS')
    return value


def _directory(work_dir, relative):
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW'):
        raise SourceReadError('UNSUPPORTED_PLATFORM')
    if type(work_dir) is not str or not os.path.isabs(work_dir):
        raise SourceReadError('INVALID_ARGUMENTS')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(work_dir, flags)
    try:
        for component in (() if relative == '.' else relative.split('/')):
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _list(request):
    path = relative_path(request['path'], directory=True)
    offset = _bounded(request.get('offset', 0), 0, 4096)
    limit = _bounded(request.get('limit', 25), 1, 50)
    fd = _directory(request['work_dir'], path)
    try:
        entries = []
        with os.scandir(fd) as scan:
            for count, entry in enumerate(scan, 1):
                if count > 4096:
                    raise SourceReadError('DIRECTORY_TOO_LARGE')
                candidate = entry.name if path == '.' else path + '/' + entry.name
                try:
                    info = entry.stat(follow_symlinks=False)
                    is_dir = stat.S_ISDIR(info.st_mode)
                    if not is_dir and not stat.S_ISREG(info.st_mode):
                        continue
                    relative_path(candidate, directory=is_dir)
                    if _excluded(candidate, request.get('excluded_directory')):
                        continue
                except (OSError, SourceReadError):
                    continue
                entries.append({'path': candidate, 'kind': 'directory' if is_dir else 'file'})
        entries.sort(key=lambda row: (row['kind'] != 'directory', row['path']))
        rows = entries[offset:offset + limit]
        return {'path': path, 'items': rows, 'offset': offset,
                'next_offset': offset + len(rows) if offset + len(rows) < len(entries) else None}
    finally:
        os.close(fd)


def _excluded(path, excluded):
    return bool(excluded and (path == excluded or path.startswith(excluded + '/')))


def excerpt(data, start_line, line_count):
    """保持物理行号与完整行；凭据模式检查覆盖全文件，而非仅当前窗口。"""
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise SourceReadError('UNSUPPORTED_TEXT') from None
    if any(ord(c) < 32 and c not in '\n\r\t' for c in text) or '\x7f' in text:
        raise SourceReadError('UNSUPPORTED_TEXT')
    if SENSITIVE_CONTENT.search(text):
        raise SourceReadError('SENSITIVE_CONTENT')
    rows = text.split('\n') if text else []
    if rows and rows[-1] == '':
        rows.pop()
    selected = []
    for number in range(start_line, min(len(rows) + 1, start_line + line_count)):
        line = rows[number - 1]
        row = {'number': number, 'text': line[:-1] if line.endswith('\r') else line}
        candidate = selected + [row]
        if len(json.dumps(candidate, ensure_ascii=False, separators=(',', ':')).encode()) > MAX_LINES_BYTES:
            if not selected:
                raise SourceReadError('LINE_TOO_LONG')
            break
        selected = candidate
    end = selected[-1]['number'] if selected else None
    return {'lines': selected, 'total_lines': len(rows), 'start_line': start_line, 'end_line': end,
            'next_line': end + 1 if end is not None and end < len(rows) else None}


def read_bytes(work_dir, path, expected_sha256=None):
    path = relative_path(path)
    parent, _, name = path.rpartition('/')
    directory = _directory(work_dir, parent or '.')
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise SourceReadError('PATH_NOT_ALLOWED')
        if before.st_size > MAX_FILE_BYTES:
            raise SourceReadError('FILE_TOO_LARGE')
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_FILE_BYTES:
        raise SourceReadError('FILE_TOO_LARGE')
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise SourceReadError('SOURCE_CHANGED')
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 not in (None, digest):
        raise SourceReadError('SOURCE_CHANGED')
    return data, digest, after.st_mtime_ns


def _read(request):
    path = relative_path(request['path'])
    start = _bounded(request.get('start_line', 1), 1, MAX_FILE_BYTES + 1)
    count = _bounded(request.get('line_count', 30), 1, 60)
    data, digest, modified_ns = read_bytes(request['work_dir'], path, request.get('expected_sha256'))
    return {'path': path, 'file_sha256': digest, 'file_bytes': len(data), 'modified_ns': modified_ns,
            **excerpt(data, start, count)}


def discover_environments(work_dir):
    """只检查候选解释器文件，不启动 Python、不导入用户代码、不安装依赖。"""
    home=os.path.expanduser('~')
    candidates={};truncated=False
    def add(python,kind,name):
        nonlocal truncated
        if len(candidates)>=512:
            truncated=True;return
        if isinstance(python,str) and python.startswith('/'):
            candidates.setdefault(os.path.normpath(python),(kind,name[:80]))
    for program in ('python3','python'):
        add(shutil.which(program),'system',program+' (PATH)')
    add(sys.executable,'system','系统 Python')
    registry=os.path.join(home,'.conda','environments.txt')
    try:
        with open(registry,encoding='utf-8') as file:
            content=file.read(65537)
        if len(content)>65536:truncated=True
        for prefix in content[:65536].splitlines():
            prefix=prefix.strip()
            if prefix.startswith('/'):
                add(os.path.join(prefix,'bin','python'),'conda',os.path.basename(prefix))
    except (OSError,UnicodeError):pass
    roots=[os.path.join(home,name) for name in ('miniconda3','anaconda3','miniforge3','mambaforge')]
    roots+=['/opt/conda','/opt/miniconda3','/opt/anaconda3']
    for root in roots:
        add(os.path.join(root,'bin','python'),'conda',os.path.basename(root)+' / base')
        try:
            with os.scandir(os.path.join(root,'envs')) as entries:
                for index,entry in enumerate(entries):
                    if index>=128:truncated=True;break
                    if entry.is_dir():add(os.path.join(entry.path,'bin','python'),'conda',entry.name)
        except OSError:pass
    for name in ('.venv','venv','env'):
        add(os.path.join(work_dir,name,'bin','python'),'venv',name+' (项目)')
    items=[]
    for python,(kind,name) in candidates.items():
        if (len(python)<=512 and re.fullmatch(r'(?:/[A-Za-z0-9_.-]+)+',python)
                and os.path.isfile(python) and os.access(python,os.X_OK)):
            if len(items)>=32:truncated=True;break
            # 保留 venv 的入口路径；不能按 realpath 去重，否则不同 venv 会被合并。
            items.append({'name':name,'python':python,'kind':kind})
    return {'items':sorted(items,key=lambda row:(row['kind'],row['python'])),'truncated':truncated}


def gpu_resources():
    program=shutil.which('nvidia-smi')
    if not program:return {'gpu_status':'not_installed','gpus':[]}
    try:
        result=subprocess.run([program,'--query-gpu=index,name,memory.total,memory.used,utilization.gpu',
            '--format=csv,noheader,nounits'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            timeout=10,check=False)
        if result.returncode or len(result.stdout)>16384:raise ValueError('nvidia query failed')
        rows=[]
        for fields in csv.reader(io.StringIO(result.stdout.decode('utf-8'))):
            if len(fields)!=5 or len(rows)>=32:raise ValueError('invalid gpu data')
            index,name,total,used,utilization=(value.strip() for value in fields)
            rows.append({'index':int(index),'name':name,'memory_total_mb':int(total),'memory_used_mb':int(used),
                'utilization_percent':int(utilization) if utilization.isdecimal() else None})
        return {'gpu_status':'available','gpus':rows}
    except (OSError,subprocess.TimeoutExpired,ValueError,UnicodeError):
        return {'gpu_status':'unavailable','gpus':[]}


def handle(request):
    try:
        if type(request) is not dict or request.get('action') not in {'list', 'read', 'environments','resources'}:
            raise SourceReadError('INVALID_ARGUMENTS')
        if request['action']=='environments':
            return {'ok':True,'data':discover_environments(request['work_dir'])}
        if request['action']=='resources':
            return {'ok':True,'data':gpu_resources()}
        relative_path(request['path'], directory=request['action'] == 'list')
        if _excluded(request['path'], request.get('excluded_directory')):
            raise SourceReadError('PATH_NOT_ALLOWED')
        data = _list(request) if request['action'] == 'list' else _read(request)
        return {'ok': True, 'data': data}
    except SourceReadError as error:
        return {'ok': False, 'code': error.code}
    except (KeyError, TypeError, UnicodeError):
        return {'ok': False, 'code': 'INVALID_ARGUMENTS'}
    except OSError as error:
        code = ('NOT_FOUND' if error.errno == errno.ENOENT else 'PATH_NOT_ALLOWED'
                if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM} else 'READ_UNAVAILABLE')
        return {'ok': False, 'code': code}


if __name__ == '__main__' and 'request' in globals():
    print(json.dumps(handle(request), ensure_ascii=True, separators=(',', ':')))
