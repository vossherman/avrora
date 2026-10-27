import json
import itertools
from typing import Any, Dict
PROTOCOL_VERSION = 213
_counter = itertools.count(0)
SENSITIVE_KEYS = {'password', 'key', 'authKey', 'auth_key', 'old_password', 'user_password', 'file_data_url', 'cookies', 'authorization', 'token'}

def next_task_id() -> int:
    return next(_counter)

def reset_counter(start: int=0):
    global _counter
    _counter = itertools.count(start)

def build_request(ser_task: str, data: Any, task_id: int=None, version: int=PROTOCOL_VERSION) -> Dict[str, Any]:
    if task_id is None:
        task_id = next_task_id()
    return {'data': data, 'ser_task': ser_task, 'arm_task_id': task_id, 'v': version}

def build_request_json(ser_task: str, data: Any, task_id: int=None, version: int=PROTOCOL_VERSION) -> str:
    msg = build_request(ser_task, data, task_id, version)
    return json.dumps(msg, ensure_ascii=False)

def mask_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for (k, v) in obj.items():
            if k.lower() in {s.lower() for s in SENSITIVE_KEYS}:
                out[k] = '***'
            elif k == 'data' and isinstance(v, dict):
                out[k] = mask_secrets(v)
            else:
                out[k] = mask_secrets(v)
        return out
    elif isinstance(obj, list):
        return [mask_secrets(x) for x in obj]
    elif isinstance(obj, str):
        return obj
    else:
        return obj

def masked_json(obj: Any) -> str:
    return json.dumps(mask_secrets(obj), ensure_ascii=False)
