"""随固定 SSH bootstrap 传入的标准库监督器；不导入后端包，不接收 shell 命令。"""

import hashlib
import base64
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from uuid import uuid4


def native_path(text):
    # 后端对外仍使用 Linux 目录。此分支只让 Windows 本机测试执行同一监督器。
    if os.name == 'nt' and len(text) > 3 and text[0] == '/' and text[2] == ':':
        text = text[1:]
    return Path(text)


def atomic(path, value):
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as file:
            json.dump(value, file, ensure_ascii=True, separators=(',', ':'))
            file.flush()
            os.fsync(file.fileno())
        # Windows 本机验收中，读句柄短暂占用会阻止 replace；Linux 原子替换无需此等待。
        for attempt in range(20):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if os.name != 'nt' or attempt == 19:
                    raise
                time.sleep(.01)
    finally:
        if temp.exists():
            temp.unlink()


def read_json(path):
    if path.stat().st_size > 131072:
        raise ValueError('oversize metadata')
    return json.loads(path.read_text(encoding='utf-8'))


def output_location(work, launch):
    relative = launch.get('output_directory', 'training_outputs')
    if (type(relative) is not str or not 1 <= len(relative) <= 200
            or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', relative)
            or not re.fullmatch('[0-9a-f]{32}', launch['run_id'])):
        raise ValueError('invalid output directory')
    return work.joinpath(*relative.split('/')) / launch['run_id']


def _inside(path, root):
    # 远端系统 python3 可能是 3.8；不依赖 3.9 才增加的 Path.is_relative_to。
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _plain_directory(root, relative):
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('output directory symlink')
        current.mkdir(mode=0o700, exist_ok=True)
        if not current.is_dir() or not _inside(current.resolve(), root):
            raise ValueError('output directory outside project')
    return current


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def publish_outputs(work, job, manifest, state):
    """终态产物发布为真实可见文件；不覆盖、不移动原文件、不执行训练。"""
    launch = manifest['launch']
    destination = output_location(work, launch)
    public_directory = launch['work_dir'].rstrip('/') + '/' + launch.get('output_directory', 'training_outputs') + '/' + launch['run_id']
    result = {'status': 'not_ready', 'directory': public_directory, 'error_code': None, 'file_count': 0}
    if state['status'] not in {'succeeded', 'failed', 'cancelled'}:
        return result
    staging = None
    phase = 'prepare_directory'
    try:
        parent = _plain_directory(work, destination.parent.relative_to(work))
        if destination.is_symlink():
            raise ValueError('output destination symlink')
        identity = {'run_id': launch['run_id'], 'job_id': job.name, 'launch_digest': manifest['digest']}
        if destination.exists():
            phase = 'check_existing_publication'
            metadata_path = destination / 'run.json'
            if metadata_path.is_symlink():
                raise ValueError('invalid output metadata')
            metadata = read_json(metadata_path)
            if any(metadata.get(key) != value for key, value in identity.items()):
                raise ValueError('output destination belongs to another run')
            # 已发布的文件若被手动删除/改写，明确报错，不悄悄覆盖用户文件。
            rows = metadata['files']
            if type(rows) is not list or len(rows) > 256:
                raise ValueError('invalid output inventory')
            for row in rows:
                relative = Path(row['path'])
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('invalid output inventory path')
                file = destination / relative
                if (file.is_symlink() or not _inside(file.resolve(), destination.resolve())
                        or not file.is_file() or file.stat().st_size != row['bytes']):
                    raise ValueError('published output changed')
            return {**result, 'status': 'published', 'file_count': len(rows)}
        staging = parent / (launch['run_id'] + '.' + uuid4().hex + '.tmp')
        staging.mkdir(mode=0o700, exist_ok=False)
        sources = []
        phase = 'collect_source_files'
        artifacts = job / 'artifacts'
        if artifacts.is_symlink():
            raise ValueError('artifact symlink')
        if artifacts.exists():
            if not artifacts.is_dir():
                raise ValueError('artifacts must be a directory')
            # 有界遍历；不跟随目录符号链接，也不复制任意项目文件。
            entries = 0
            for base, directories, files in os.walk(artifacts, followlinks=False):
                entries += len(directories) + len(files)
                if entries > 256:
                    raise ValueError('too many output entries')
                for name in directories + files:
                    candidate = Path(base) / name
                    if candidate.is_symlink():
                        raise ValueError('artifact symlink')
                sources.extend(Path(base) / name for name in sorted(files))
        log = job / 'output.log'
        if log.exists():
            sources.append(log)
        if len(sources) > 256:
            raise ValueError('too many output files')
        total, inventory = 0, []
        phase = 'copy_and_verify'
        for source in sources:
            if (source.is_symlink() or not source.is_file()
                    or not _inside(source.resolve(), job.resolve())):
                raise ValueError('invalid output source')
            size = source.stat().st_size
            total += size
            if size > 512 * 1024 * 1024 or total > 1024 * 1024 * 1024:
                raise ValueError('output size exceeds publication limit')
            relative = source.relative_to(job)
            target = staging / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(source, target)
            sha = _file_hash(target)
            if source.stat().st_size != size or target.stat().st_size != size or _file_hash(source) != sha:
                raise ValueError('output changed during publication')
            inventory.append({'path': relative.as_posix(), 'bytes': size, 'sha256': sha})
        metadata = {**identity, 'status': state['status'], 'exit_code': state['exit_code'],
                    'script_id': launch.get('script_id'), 'parameters': launch.get('parameters', {}),
                    'published_at': time.time(), 'files': inventory}
        if len(json.dumps(metadata).encode()) > 131072:
            raise ValueError('output metadata exceeds limit')
        atomic(staging / 'run.json', metadata)
        phase = 'commit_publication'
        # 请求锁保证同一 job 只有一个发布者；存在的目录绝不覆盖。
        if destination.exists():
            raise ValueError('output destination already exists')
        staging.rename(destination)
        staging = None
        return {**result, 'status': 'published', 'file_count': len(inventory)}
    except Exception as error:
        try:
            # 可排查到操作和源码位置，不写异常原文、变量值或凭据。
            atomic(job / 'publication_error.json', {'error_code': 'OUTPUT_PUBLICATION_FAILED',
                'component': 'runner.output_publication', 'phase': phase,
                'error_type': type(error).__name__, 'time': time.time(),
                'trace': [{'file': os.path.basename(frame.filename), 'line': frame.lineno,
                           'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)[-8:]]})
        except OSError:
            pass
        return {**result, 'status': 'unavailable', 'error_code': 'OUTPUT_PUBLICATION_FAILED'}
    finally:
        if staging is not None and staging.is_dir() and not staging.is_symlink():
            # 仅清理由本函数创建、已核对仍处于输出根目录内的临时目录。
            if staging.resolve().parent == destination.parent.resolve() and staging.name.endswith('.tmp'):
                shutil.rmtree(staging)


class JobLock:
    def __init__(self, path):
        self.file = path.open('a+b')

    def __enter__(self):
        if os.name == 'posix':
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
        else:  # 仅用于本机 Windows 子进程验收；Linux 生产路径采用 flock。
            import msvcrt
            self.file.seek(0)
            self.file.write(b'0'); self.file.flush(); self.file.seek(0)
            for _ in range(300):
                try:
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise TimeoutError('job lock busy')
        return self

    def __exit__(self, *args):
        if os.name == 'posix':
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        else:
            import msvcrt
            self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


def stop_child(child):
    if os.name == 'posix':
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == 'posix':
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            child.kill()
        child.wait(timeout=3)
    # Linux 的训练子进程组也清理，避免主脚本提前退出留下后代。
    if os.name == 'posix':
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def training_command(launch, script, execution_work, work, job, environment):
    """标准项目运行其原始入口；旧登记脚本继续接收 --config。"""
    project = launch['spec'].get('project')
    if project is None:
        return [sys.executable, '-u', str(script), '--config', str(job / 'parameters.json')]
    artifacts = job / 'artifacts'
    artifacts.mkdir(mode=0o700, exist_ok=True)
    values = {key: ('true' if value else 'false') if type(value) is bool else str(value)
              for key, value in launch['parameters'].items()}
    values.update(source_root=str(execution_work), input_root=str(work),
                  artifact_dir=str(artifacts), config_file=str(job / 'parameters.json'))
    arguments = [argument.format_map(values) for argument in project['arguments']]
    if project.get('cuda_visible_devices') is not None:
        environment['CUDA_VISIBLE_DEVICES'] = project['cuda_visible_devices']
    return [project['python'], '-u', str(script), *arguments]


class MetricNormalizer:
    """将明确配置的普通日志转换为现有 JSONL 协议，同时保留原始日志。"""
    def __init__(self, rules):
        self.rules = [(re.compile(row['pattern'], re.IGNORECASE), row) for row in rules]
        self.pending = b''
        self.discarding = False

    def metric(self, raw):
        if not self.rules or raw.lstrip().startswith(b'{'):
            return b''
        text = raw.decode('utf-8', errors='replace')
        events = []
        for pattern, rule in self.rules:
            match = pattern.search(text)
            if match is None:
                continue
            try:
                groups = {name.lower(): value for name, value in match.groupdict().items()}
                step = int(groups.get('step') or groups['epoch'])
                epoch = int(groups['epoch']) if groups.get('epoch') is not None else None
                if not 0 <= step <= 2**53-1 or epoch is not None and not 0 <= epoch <= 2**53-1:
                    continue
                values = {name.lower(): float(groups[name.lower()]) for name in rule['metrics']}
                if any(not math.isfinite(value) or abs(value)>1e100 for value in values.values()):
                    continue
                event = {'type':'metric', 'dataset':rule['dataset'].lower(), 'split':rule['split'].lower(), 'step':step, **values}
                if epoch is not None:
                    event['epoch'] = epoch
                events.append(json.dumps(event, allow_nan=False, separators=(',',':')).encode() + b'\n')
            except (ValueError, KeyError, TypeError, OverflowError):
                continue
        return b''.join(events)

    def feed(self, data, *, final=False):
        if not self.rules:
            return data
        self.pending += data
        result = []
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            result.append(line + b'\n')
            if not self.discarding and len(line) <= 8192:
                result.append(self.metric(line))
            self.discarding = False
        if len(self.pending)>8192:
            result.append(self.pending)
            self.pending = b''
            self.discarding = True
        if final and self.pending:
            result.append(self.pending)
            if not self.discarding:
                event = self.metric(self.pending)
                if event:
                    result.extend([b'\n', event])
            self.pending = b''
        return b''.join(result)

def worker(job):
    manifest = read_json(job / 'manifest.json')
    launch = manifest['launch']
    state = {'status': 'queued', 'started': False, 'launch_claimed': True, 'exit_code': None, 'error_code': None, 'heartbeat': time.time(), 'log_bytes': 0}
    child, owns = None, False
    try:
        with JobLock(job / 'worker.lock'):
            previous = read_json(job / 'state.json')
            if previous['status'] != 'queued' or previous.get('launch_claimed', False):
                return
            owns = True
            atomic(job / 'state.json', state)  # 在 Popen 前持久化；不对不确定的启动自动重跑。
            if (job / 'cancel').exists():
                state['status'] = 'cancelled'
                return
            work = native_path(launch['work_dir']).resolve(strict=True)
            script = (work / launch['spec']['path']).resolve(strict=True)
            script.relative_to(work)
            if not script.is_file() or script.stat().st_size > 10485760:
                state.update(status='failed', error_code='SCRIPT_MISSING')
                return
            if hashlib.sha256(script.read_bytes()).hexdigest() != launch['spec']['sha256']:
                state.update(status='failed', error_code='SCRIPT_CHANGED')
                return
            execution_work = work
            if 'revision' in launch:
                policy = {'__name__': 'revision_helper'}
                exec(launch['revision_helper_source'], policy)
                try:
                    script, execution_work = policy['freeze'](work, job, launch)
                except (ValueError, OSError):
                    state.update(status='failed', error_code='REVISION_SOURCE_CHANGED')
                    return
            atomic(job / 'parameters.json', launch['parameters'])
            environment = os.environ.copy()
            environment['PYTHONUNBUFFERED'] = '1'
            environment['EXPERIMENT_SOURCE_ROOT'] = str(execution_work)
            environment['EXPERIMENT_INPUT_ROOT'] = str(work)
            environment['EXPERIMENT_ARTIFACT_DIR'] = str(job / 'artifacts')
            if 'revision' in launch:
                environment['EXPERIMENT_SOURCE_ROOT'] = str(execution_work)
                environment['PYTHONPATH'] = str(execution_work)
                environment['EXPERIMENT_INPUT_ROOT'] = str(work)
                environment['EXPERIMENT_ARTIFACT_DIR'] = str(job / 'artifacts')
                environment['PYTHONDONTWRITEBYTECODE'] = '1'
            command = training_command(launch, script, execution_work, work, job, environment)
            normalizer = MetricNormalizer(launch['spec'].get('project', {}).get('metric_rules', []))
            try:
                child = subprocess.Popen(command,
                                         cwd=str(execution_work), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, env=environment, start_new_session=os.name == 'posix')
            except FileNotFoundError:
                state.update(status='failed', error_code='PYTHON_MISSING')
                return
            state.update(status='running', started=True)
            chunks = queue.Queue(maxsize=8)

            def reader():
                try:
                    while True:
                        data = child.stdout.read1(4096)
                        if not data:
                            break
                        chunks.put(data)
                finally:
                    chunks.put(None)

            threading.Thread(target=reader, daemon=True).start()
            started = time.monotonic()
            ended_at, eof = None, False
            with (job / 'output.log').open('wb') as log:
                while True:
                    if child.poll() is None:
                        if (job / 'cancel').exists():
                            stop_child(child); state['status'] = 'cancelled'
                        elif time.monotonic() - started >= launch['spec']['max_runtime_seconds']:
                            stop_child(child); state.update(status='failed', error_code='RUNTIME_LIMIT')
                    try:
                        data = chunks.get(timeout=.2)
                        if data is None:
                            eof = True
                            data = normalizer.feed(b'', final=True)
                        else:
                            data = normalizer.feed(data)
                        if data:
                            available = launch['spec']['max_log_bytes'] - state['log_bytes']
                            if available > 0:
                                log.write(data[:available]); log.flush()
                                state['log_bytes'] += min(available, len(data))
                    except queue.Empty:
                        pass
                    state['heartbeat'] = time.time()
                    # 日志与子进程收尾前仍报告 running，最终退出码只在 finally 一并发布。
                    atomic(job / 'state.json', {**state, 'status': 'running', 'exit_code': None})
                    if child.poll() is not None:
                        if ended_at is None:
                            ended_at = time.monotonic()
                            stop_child(child)
                        if eof or time.monotonic() - ended_at > 2:
                            break
            state['exit_code'] = child.returncode
            if state['status'] == 'running':
                state['status'] = 'succeeded' if child.returncode == 0 else 'failed'
    except FileNotFoundError:
        state.update(status='failed', error_code='SCRIPT_MISSING')
    except Exception:
        state.update(status='failed', error_code='RUNNER_FAILURE')
    finally:
        if child is not None:
            stop_child(child)
        if owns:
            state['heartbeat'] = time.time()
            with JobLock(job / 'request.lock'):
                try:
                    state['outputs'] = publish_outputs(native_path(launch['work_dir']).resolve(strict=True), job, manifest, state)
                except Exception:
                    pass  # 保留训练退出状态；后续 status 可重试产物发布。
                atomic(job / 'state.json', state)


def handle(request, source):
    import re
    if request.get('action') not in {'start', 'status', 'logs', 'stop'}:
        raise ValueError('invalid action')
    job_id = request['job_id']
    if not isinstance(job_id, str) or not re.fullmatch('[0-9a-f]{32}', job_id):
        raise ValueError('invalid job id')
    launch = request['launch']
    digest = hashlib.sha256(json.dumps(launch, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != request['digest']:
        raise ValueError('invalid launch digest')
    work = native_path(launch['work_dir']).resolve(strict=True)
    if not work.is_dir():
        raise ValueError('invalid work directory')
    root = work / '.experiment-assistant'
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('invalid job root')
    job = root / job_id
    if request['action'] not in {'start', 'stop'} and not job.exists():
        result = {'status': 'missing', 'started': False, 'exit_code': None, 'error_code': None, 'stale': True, 'log_bytes': 0}
        if request['action'] == 'logs':
            result.update(content='', next_offset=request['offset'], has_more=False, truncated=False,
                          raw_base64='', log_size=0, anchor_sha256=hashlib.sha256(b'').hexdigest(), source_changed=request['offset'] > 0)
        return result
    job.mkdir(mode=0o700, exist_ok=True)
    if job.is_symlink() or not job.is_dir():
        raise ValueError('invalid job directory')
    with JobLock(job / 'request.lock'):
        manifest_path = job / 'manifest.json'
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            if manifest['digest'] != digest or manifest['launch'] != launch:
                raise ValueError('job binding mismatch')
        elif request['action'] in {'start', 'stop'}:
            atomic(manifest_path, {'digest': digest, 'launch': launch})
        else:
            raise ValueError('missing manifest')
        state_path = job / 'state.json'
        if not state_path.exists():
            if request['action'] not in {'start', 'stop'}:
                raise ValueError('missing state')
            state = {'status': 'cancelled' if request['action'] == 'stop' else 'queued', 'started': False, 'launch_claimed': False,
                     'exit_code': None, 'error_code': None, 'heartbeat': time.time(), 'log_bytes': 0}
            atomic(state_path, state)
            if request['action'] == 'stop':
                return {**{key: state[key] for key in ('status', 'started', 'exit_code', 'error_code', 'log_bytes')}, 'stale': False}
            helper = job / 'supervisor.py'
            helper.write_text(source, encoding='utf-8')
            try:
                subprocess.Popen([sys.executable, str(helper), '--worker', str(job)], cwd=str(work), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name == 'posix',
                                 creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == 'nt' else 0)
            except OSError:
                state.update(status='failed', error_code='RUNNER_FAILURE')
                atomic(state_path, state)
        state = read_json(state_path)
        if request['action'] == 'stop' and state['status'] in {'queued', 'running'}:
            (job / 'cancel').touch(exist_ok=True)
        result = {key: state[key] for key in ('status', 'started', 'exit_code', 'error_code', 'log_bytes')}
        if request['action'] in {'status', 'start'}:
            # 兼容旧监督器产生的已完成运行，也修复中断的发布，不重跑训练。
            result['outputs'] = publish_outputs(work, job, {'digest': digest, 'launch': launch}, state)
        elif 'outputs' in state:
            result['outputs'] = state['outputs']
        result['stale'] = state['status'] in {'queued', 'running'} and time.time() - state['heartbeat'] > 30
        if request['action'] == 'logs':
            offset, limit = request['offset'], request['limit']
            if type(offset) is not int or not 0 <= offset <= 10485760 or type(limit) is not int or not 1 <= limit <= 16384:
                raise ValueError('invalid log range')
            path = job / 'output.log'
            data = b''
            size = path.stat().st_size if path.exists() else 0
            anchor = b''
            if path.exists():
                if path.is_symlink():
                    raise ValueError('invalid log file')
                with path.open('rb') as file:
                    file.seek(max(0, offset - 128)); anchor = file.read(min(offset, 128))
                    file.seek(offset); data = file.read(min(limit, max(0, size-offset)))
            result.update(content=data.decode('utf-8', errors='replace'), next_offset=offset + len(data),
                          has_more=offset + len(data) < size, truncated=size >= launch['spec']['max_log_bytes'],
                          raw_base64=base64.b64encode(data).decode(), log_size=size,
                          anchor_sha256=hashlib.sha256(anchor).hexdigest(), source_changed=offset > size)
        return result


if __name__ == '__main__':
    if 'request' in globals():
        try:
            print(json.dumps({'ok': True, 'observation': handle(request, source)}, ensure_ascii=True))
        except Exception:
            print('{"ok":false}')  # 不把路径、参数或远端 traceback 回传给通用错误日志。
    elif len(sys.argv) == 3 and sys.argv[1] == '--worker':
        worker(Path(sys.argv[2]))
