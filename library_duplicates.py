"""Exact audio-file deduplication. Names/tags alone never authorize deletion."""
import hashlib
import json
import os
from pathlib import Path
from audio_files import EXTENSIONS, validate_audio


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe_path(root, path):
    root, path = Path(root).resolve(), Path(path).absolute()
    if not path.resolve().is_relative_to(root) or path.resolve() == root:
        raise ValueError('文件不在指定音乐目录内')
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or part.is_junction():
            raise ValueError('不处理链接或联接目录')
    return path


def find_duplicates(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('音乐目录不存在')
    sizes, errors, groups = {}, [], []
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=lambda exc: errors.append(str(exc))):
        dirs[:] = [d for d in dirs if not Path(folder,d).is_symlink() and not Path(folder,d).is_junction()]
        for name in files:
            path = Path(folder,name)
            if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                continue
            try:
                sizes.setdefault(path.stat().st_size, []).append(path)
            except OSError as exc:
                errors.append(str(exc))
    for size, paths in sizes.items():
        if len(paths) < 2:
            continue
        hashes = {}
        for path in paths:
            try:
                hashes.setdefault(digest(path), []).append(path)
            except OSError as exc:
                errors.append(str(exc))
        for sha, same in hashes.items():
            if len(same) < 2:
                continue
            # Prefer the shortest filename (usually the original, without (2)).
            same.sort(key=lambda p:(len(p.stem),len(p.parts),str(p).casefold()))
            keep = same[0]
            try:
                validate_audio(keep, min_bytes=1, min_seconds=.01)
            except Exception as exc:
                errors.append(f'{keep}: {exc}')
                continue
            for duplicate in same[1:]:
                groups.append(dict(root=str(root), keep=str(keep), delete=str(duplicate), size=size, sha256=sha))
    return groups, errors


def delete_duplicates(plan, report_path):
    """Revalidate every pair, durably record intent, then delete only the copy."""
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    deleted, errors = [], []
    with report_path.open('x', encoding='utf-8') as log:
        for item in plan:
            try:
                keep = safe_path(item['root'],item['keep'])
                duplicate = safe_path(item['root'],item['delete'])
                if keep == duplicate or keep.suffix.lower() not in EXTENSIONS or duplicate.suffix.lower() not in EXTENSIONS:
                    raise ValueError('不是可删除的重复音频')
                if keep.stat().st_size != item['size'] or duplicate.stat().st_size != item['size']:
                    raise ValueError('文件大小已变化，请重新扫描')
                if digest(keep) != item['sha256'] or digest(duplicate) != item['sha256']:
                    raise ValueError('文件内容已变化，请重新扫描')
                validate_audio(keep, min_bytes=1, min_seconds=.01)
                with keep.open('rb') as a, duplicate.open('rb') as b:
                    while True:
                        x, y = a.read(1024*1024), b.read(1024*1024)
                        if x != y:
                            raise ValueError('逐字节复核不一致')
                        if not x:
                            break
                log.write(json.dumps(dict(action='deleting', **item), ensure_ascii=False)+'\n')
                log.flush()
                os.fsync(log.fileno())
                duplicate.unlink()
                deleted.append(str(duplicate))
                log.write(json.dumps(dict(action='deleted', path=str(duplicate)), ensure_ascii=False)+'\n')
                log.flush()
            except Exception as exc:
                errors.append(f'{item["delete"]}: {exc}')
                log.write(json.dumps(dict(action='error', path=item['delete'], error=str(exc)), ensure_ascii=False)+'\n')
                log.flush()
    return deleted, errors
