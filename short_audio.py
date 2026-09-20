"""Preview, revalidate and audit deletion of valid audio shorter than 90s."""
import json
import os
from pathlib import Path
from audio_files import EXTENSIONS, MIN_SECONDS, validate_audio, file_identity
from library_duplicates import digest, safe_path
from matching import identity


def find_short_audio(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('音乐目录不存在')
    plan, errors = [], []
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: errors.append(str(e))):
        dirs[:] = [d for d in dirs if not Path(folder,d).is_symlink() and not Path(folder,d).is_junction()]
        for name in files:
            path = Path(folder,name)
            if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                continue
            try:
                info = validate_audio(path, min_bytes=1, min_seconds=.001)
                if info.duration < MIN_SECONDS:
                    plan.append(dict(root=str(root), delete=str(path), keep=f'{info.duration:.3f} 秒 < 90 秒',
                                     duration=info.duration, size=info.size, sha256=digest(path),
                                     identities=file_identity(path)))
            except Exception as exc:
                errors.append(f'{path}: {exc}')
    return plan, errors


def delete_short_audio(plan, report_path):
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True,exist_ok=True)
    deleted, errors = [], []
    with report_path.open('x', encoding='utf-8') as log:
        for item in plan:
            try:
                path = safe_path(item['root'],item['delete'])
                if path.stat().st_size != item['size'] or digest(path) != item['sha256']:
                    raise ValueError('文件已变化，请重新扫描')
                info = validate_audio(path,min_bytes=1,min_seconds=.001)
                if not 0 < info.duration < MIN_SECONDS:
                    raise ValueError('时长不满足删除条件')
                record = dict(item, duration=info.duration, identities=file_identity(path))
                log.write(json.dumps(dict(record,action='deleting'),ensure_ascii=False)+'\n')
                log.flush();os.fsync(log.fileno())
                path.unlink()
                deleted.append(str(path))
                log.write(json.dumps(dict(record,action='deleted'),ensure_ascii=False)+'\n')
                log.flush();os.fsync(log.fileno())
            except Exception as exc:
                errors.append(f'{item["delete"]}: {exc}')
                log.write(json.dumps(dict(action='error',path=item['delete'],error=str(exc)),ensure_ascii=False)+'\n')
                log.flush()
    return deleted, errors


def mark_deleted_short(tracks, report_directory):
    records = {}
    by_path = {}
    for path in Path(report_directory).glob('short-audio-*.jsonl'):
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
                if record.get('action') != 'deleted':
                    continue
                by_path[record['delete'].casefold()] = record
                for title, artist in record.get('identities',[]):
                    records[identity(title,artist)] = record
            except (ValueError,TypeError):
                continue
    for track in tracks:
        item = records.get(identity(track.title,track.artist)) or by_path.get(track.note.casefold())
        if item:
            track.status = '时长不足'
            track.note = f'已删除本地短音频：{item["duration"]:.3f} 秒 < 90 秒；{item["delete"]}'
