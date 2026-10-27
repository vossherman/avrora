import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List
from .exceptions import ValidationError
FORMAT_VERSION = 1

def export_task(task_id: int, task_data: Dict[str, Any], files: List[Dict[str, Any]], protocol_version: int=213) -> Dict[str, Any]:
    return {'format_version': FORMAT_VERSION, 'exported_at': datetime.now(timezone.utc).isoformat(), 'source': 'aurora', 'task_id': task_id, 'task': task_data, 'files': files, 'protocol': {'version': protocol_version}}

def write_export(task_id: int, task_data: Dict[str, Any], files: List[Dict[str, Any]], directory: Path=Path('.'), protocol_version: int=213) -> Path:
    doc = export_task(task_id, task_data, files, protocol_version)
    path = directory / f'task_{task_id}.json'
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
    return path

def read_export(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        raise ValidationError(f'Invalid JSON file {path}: {e}') from e
    validate_export(data)
    return data

def validate_export(doc: Dict[str, Any]):
    if not isinstance(doc, dict):
        raise ValidationError('Root must be object')
    if doc.get('format_version') != FORMAT_VERSION:
        raise ValidationError(f"format_version must be {FORMAT_VERSION}, got {doc.get('format_version')}")
    if 'task' not in doc:
        raise ValidationError("Missing 'task' field")
    if 'files' not in doc:
        raise ValidationError("Missing 'files' field")
    if not isinstance(doc['files'], list):
        raise ValidationError("'files' must be list")
    task = doc['task']
    if not isinstance(task, dict):
        raise ValidationError("'task' must be object")
    if 'id' not in task:
        if 'task_id' not in doc:
            raise ValidationError('Missing task id (task.id or task_id)')
    tid_top = doc.get('task_id')
    tid_inner = task.get('id')
    if tid_top is not None and tid_inner is not None and (tid_top != tid_inner):
        raise ValidationError(f'task_id mismatch: top {tid_top} vs task.id {tid_inner}')
    for f in doc['files']:
        if not isinstance(f, dict):
            raise ValidationError('Each file must be object')
        if 'name' not in f or 'file' not in f:
            raise ValidationError("Each file must have 'name' and 'file'")
    if 'protocol' not in doc:
        raise ValidationError("Missing 'protocol'")
    return True
