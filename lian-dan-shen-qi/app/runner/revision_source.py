"""固定后端辅助程序：检验修改副本并为 Runner 固定训练源码，不启动训练。"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat


class RevisionError(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def plain(root, relative):
    if not relative or relative.startswith('/') or ':' in relative or Path(relative).is_absolute() or '..' in Path(relative).parts or '\\' in relative:
        raise RevisionError('INVALID_PATH')
    current = Path(root)
    if current.is_symlink(): raise RevisionError('SYMLINK')
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink(): raise RevisionError('SYMLINK')
    return current


def read_file(root,relative):
    path=plain(root,relative)
    if os.name!='posix':
        if not path.is_file() or path.stat().st_size>1048576:raise RevisionError('INVALID_FILE')
        return path.read_bytes()
    directory=os.open(str(root),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in Path(relative).parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            os.close(directory);directory=child
        fd=os.open(Path(relative).name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
    finally:os.close(directory)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>1048576:raise RevisionError('INVALID_FILE')
        data=stream.read(1048577)
    if len(data)>1048576:raise RevisionError('INVALID_FILE')
    return data


def inventory(project):
    rows, total, entries = {}, 0, 0
    for base, dirs, files in os.walk(project, followlinks=False):
        # 用户的隐藏配置不会进入训练；出现链接/特殊文件则不能证明源码副本完整。
        dirs[:] = sorted(name for name in dirs if name != '__pycache__')
        entries += len(dirs) + len(files)
        if entries > 4096: raise RevisionError('SOURCE_TOO_LARGE')
        for name in dirs + files:
            if (Path(base) / name).is_symlink(): raise RevisionError('SYMLINK')
        for name in sorted(files):
            path = Path(base) / name
            relative = path.relative_to(project).as_posix()
            if any(part.startswith('.') for part in Path(relative).parts) or path.suffix.lower() not in {'.py','.yaml','.yml','.json','.toml','.ini','.cfg','.md','.txt'}:
                raise RevisionError('INVALID_FILE')
            if not path.is_file() or path.stat().st_size > 1048576: raise RevisionError('INVALID_FILE')
            raw = read_file(project,relative)
            rows[relative] = digest(raw)
            total += len(raw)
            if len(rows) > 512 or total > 20971520: raise RevisionError('SOURCE_TOO_LARGE')
    if not rows: raise RevisionError('EMPTY_SOURCE')
    return rows


def inspect(request):
    root = Path(request['work_dir'])
    change = request['change_id']
    if not re.fullmatch('[0-9a-f]{32}', change): raise RevisionError('INVALID_CHANGE')
    project = plain(root, 'code_changes/' + change + '/project')
    if not project.is_dir(): raise RevisionError('SOURCE_MISSING')
    rows = inventory(project)
    for row in request['changed_files']:
        if rows.get(row['path']) != row['after_sha256']: raise RevisionError('SOURCE_CHANGED')
    spec = request['spec']
    entry = plain(root, spec['path'])
    if not entry.is_file() or entry.stat().st_size > 1048576 or digest(read_file(root,spec['path'])) != spec['sha256']:
        raise RevisionError('ENTRY_CHANGED')
    # 入口不允许由模型替换；只允许改训练/模型等被入口调用的源码。
    if spec['path'] not in rows: raise RevisionError('ENTRY_CHANGED')
    data = {'files': rows, 'source_digest': digest(canonical(rows).encode()), 'mode': request['mode']}
    if request['mode'] != 'cwd': raise RevisionError('INVALID_MODE')
    return data


def freeze(work, job, launch):
    """在监督器 Popen 前复制固定版本；训练期间不再引用可编辑的候选目录。"""
    revision = launch['revision']
    if revision['mode'] != 'cwd': raise RevisionError('INVALID_MODE')
    project = plain(work, 'code_changes/' + revision['change_id'] + '/project')
    expected = revision['files']
    if digest(canonical(expected).encode()) != revision['source_digest'] or inventory(project) != expected:
        raise RevisionError('SOURCE_CHANGED')
    destination = job / 'revision_source'
    if destination.exists(): raise RevisionError('SOURCE_ALREADY_FROZEN')
    destination.mkdir(mode=0o700)
    for relative, sha in expected.items():
        source = plain(project, relative)
        raw = read_file(project,relative)
        if digest(raw) != sha: raise RevisionError('SOURCE_CHANGED')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    if inventory(project) != expected or inventory(destination) != expected:
        raise RevisionError('SOURCE_CHANGED')
    script = destination / launch['spec']['path']
    return script, destination


if __name__ == '__main__' and 'request' in globals():
    try: print(canonical({'ok': True, 'data': inspect(request)}))
    except (RevisionError, OSError, ValueError, TypeError, KeyError, SyntaxError):
        print('{"ok":false}')
