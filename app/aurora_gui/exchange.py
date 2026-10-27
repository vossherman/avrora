"""Task JSON export/import.

Two formats are understood:

* "exchange" (default for export) — the task-exchange JSON used by the files in `json-decisions/`
  (same schema as Xuirora's task-export.js / task-import.js):
      {"version": 1, "method2": {...}, "algo2": [...], "mxGraphScheme": "<algo_graph>",
       "task_text": "...", "code": {"version": 1, "folders": [...],
       "files": [{"name", "folder", "folder_name", "content"}]}, "test-input"?: ...}
  Import takes method2, algo2, test-input, mxGraphScheme (-> task.algo_graph) and code;
  task_text is exported for reference only and never imported (same rule as task-import.js).

* "backup" — the core's full copy (aurora_client.storage): {"format_version": 1, "task": {...},
  "files": [{"name", "file"}], ...}. On import the same editable fields are taken from `task`.

Import never deletes server files: files from the JSON overwrite same-named files, others stay.
"""
from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import render
from .core import ValidationError, storage

CODE_FOLDERS = ["Header Files", "Source Files"]
IMPORTABLE_TASK_FIELDS = ("method2", "algo2", "test-input")
GRAPH_EXPORT_FIELD = "mxGraphScheme"
GRAPH_TASK_FIELD = "algo_graph"
MAX_IMPORT_SIZE = 10 * 1024 * 1024    # task-import.js MAX_IMPORT_FILE_SIZE


def sanitize_file_name(name: str) -> str:
    """task-export.js sanitizeFileName."""
    name = (name or "").strip()
    if not name:
        return "task"
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    return name or "task"


def _folder_id(f: dict) -> int:
    try:
        fid = int(f.get("folder"))
        if fid in (0, 1):
            return fid
    except (TypeError, ValueError):
        pass
    fname = str(f.get("folder_name") or "").strip().lower()
    for i, n in enumerate(CODE_FOLDERS):
        if fname == n.lower():
            return i
    leaf = str(f.get("name") or f.get("path") or "").strip().lower()
    return 0 if leaf.endswith(".h") or leaf.endswith(".hpp") else 1


def normalize_code(files: List[dict]) -> dict:
    """Code snapshot {version, folders, files[name, folder, folder_name, content]} sorted by name.
    Accepts both {"name","content"} and server-style {"name","file"} entries; drops the synthetic
    '<name>.bak' recovery copies the original client adds in getTaskFiles."""
    names = {str(f.get("name") or f.get("path") or "").strip() for f in files if isinstance(f, dict)}
    out, used = [], set()
    for f in files:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or f.get("path") or "").strip()
        if not name or name in used:
            continue
        if name.lower().endswith(".bak") and name[:-4] in names:
            continue
        used.add(name)
        fid = _folder_id(f)
        content = f.get("content") if "content" in f else f.get("file", "")
        out.append({"name": name, "folder": fid, "folder_name": CODE_FOLDERS[fid],
                    "content": "" if content is None else str(content)})
    out.sort(key=lambda x: x["name"])
    return {"version": 1, "folders": [{"id": i, "name": n} for i, n in enumerate(CODE_FOLDERS)],
            "files": out}


def build_exchange(task: dict, files: List[dict]) -> dict:
    """task-export.js buildExchangePayload(task, buildCodeSnapshotFromTaskFiles(files))."""
    doc: Dict[str, Any] = {
        "version": 1,
        "method2": copy.deepcopy(task.get("method2")),
        "algo2": copy.deepcopy(task.get("algo2")),
        GRAPH_EXPORT_FIELD: copy.deepcopy(task.get(GRAPH_TASK_FIELD)),
    }
    if "task_text" in task:
        doc["task_text"] = task.get("task_text")
    code = normalize_code(files)
    if code["files"]:
        doc["code"] = code
    if "test-input" in task:
        doc["test-input"] = copy.deepcopy(task.get("test-input"))
    return doc


def build_backup(task_id: int, task: dict, files: List[dict], protocol_version: int = 213) -> dict:
    return storage.export_task(task_id, task, files, protocol_version)


def unique_path(directory: Path, file_name: str) -> Path:
    path = directory / file_name
    stem, suffix = path.stem, path.suffix
    n = 2
    while path.exists():
        path = directory / f"{stem}_{n}{suffix}"
        n += 1
    return path


def write_json(directory: Path, file_name: str, doc: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = unique_path(directory, file_name)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def downloads_dir() -> Path:
    home = Path.home()
    d = home / "Downloads"
    return d if d.is_dir() else home


# ---------------------------------------------------------------- import
@dataclass
class ImportData:
    kind: str                                  # "exchange" | "backup"
    fields: Dict[str, Any]                     # task fields to apply (method2, algo2, algo_graph, ...)
    files: Optional[List[dict]]                # normalized code files or None (no code in JSON)
    task_id: Optional[int] = None              # only the backup format carries an id
    warnings: List[str] = field(default_factory=list)


def read_import(path: Path) -> ImportData:
    path = Path(path)
    if path.suffix.lower() != ".json":
        raise ValidationError("Допустимы только файлы JSON")
    if path.stat().st_size > MAX_IMPORT_SIZE:
        raise ValidationError("Файл слишком большой (максимум 10 МБ)")
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise ValidationError(f"Некорректный JSON: {e}") from e
    return parse_import(doc)


def parse_import(doc: Any) -> ImportData:
    if not isinstance(doc, dict):
        raise ValidationError("Корень JSON должен быть объектом")
    if "format_version" in doc and "task" in doc:
        storage.validate_export(doc)
        task = doc["task"]
        src = dict(task)
        if GRAPH_TASK_FIELD in task:
            src[GRAPH_EXPORT_FIELD] = task[GRAPH_TASK_FIELD]
        data = _parse_fields(src, "backup")
        data.files = normalize_code(doc.get("files") or [])["files"]
        data.task_id = doc.get("task_id") or task.get("id")
        return data
    data = _parse_fields(doc, "exchange")
    code = doc.get("code", doc.get("code_snapshot"))
    if code is not None:
        raw = code.get("files") if isinstance(code, dict) else code
        files = normalize_code(raw if isinstance(raw, list) else [])["files"]
        data.files = files or None
    if not data.fields and not data.files:
        raise ValidationError("В JSON нет поддерживаемых полей (method2, algo2, mxGraphScheme, code)")
    return data


def _parse_fields(doc: dict, kind: str) -> ImportData:
    fields: Dict[str, Any] = {}
    warnings: List[str] = []
    if isinstance(doc.get("version"), int) and doc["version"] > 1:
        warnings.append(f"Неизвестная версия схемы {doc['version']}, ожидалась 1")
    if "method2" in doc:
        if isinstance(doc["method2"], dict):
            fields["method2"] = copy.deepcopy(doc["method2"])
        else:
            warnings.append("method2 имеет неожиданный тип — пропущено")
    if "algo2" in doc:
        if isinstance(doc["algo2"], list):
            fields["algo2"] = copy.deepcopy(doc["algo2"])
        else:
            warnings.append("algo2 имеет неожиданный тип — пропущено")
    if "test-input" in doc:
        fields["test-input"] = copy.deepcopy(doc["test-input"])
    graph = doc.get(GRAPH_EXPORT_FIELD, doc.get(GRAPH_TASK_FIELD))
    if isinstance(graph, (dict, list)) or (isinstance(graph, str) and graph):
        fields[GRAPH_TASK_FIELD] = copy.deepcopy(graph)
    elif graph not in (None, ""):
        warnings.append("mxGraphScheme имеет неожиданный тип — пропущено")
    return ImportData(kind, fields, None, warnings=warnings)


@dataclass
class ImportPlan:
    task: dict                                  # full merged task document for saveTask
    changed_fields: List[str]
    files_to_save: List[Tuple[str, str]]        # (name, content) new or changed
    files_unchanged: List[str]
    files_kept: List[str]                       # on server, not in the JSON (left untouched)
    code_snapshot: Optional[List[List[str]]] = None  # merged project for editor lint
    original_code_snapshot: Optional[List[List[str]]] = None
    original_task: Optional[dict] = None


def plan_import(base_task: dict, base_files: List[dict], data: ImportData) -> ImportPlan:
    """Merge imported fields onto the fresh server task (task-import.js applyImportData) and
    work out which files actually change."""
    merged = copy.deepcopy(base_task)
    changed = []
    for k, v in data.fields.items():
        if merged.get(k) != v:
            changed.append(k)
        merged[k] = copy.deepcopy(v)
    if "method2" in changed or "algo2" in changed:
        # the method2/algo2 tools rebuild these HTML views on every edit; do the same so the
        # teacher sees what was imported (render.py mirrors method2.js / algo2.js byte-for-byte)
        before = {k: merged.get(k) for k in ("method_description", "algo_text")}
        merged = render.regenerate(merged)
        changed += [k for k, v in before.items() if merged.get(k) != v and k not in changed]
    if changed:
        # setTaskField touches the snapshot on document edits, not on each save.
        # Keep this timestamp stable while the user reviews or retries the import.
        merged["__lastSaved"] = int(time.time())
    server = {f.get("name"): f.get("file", "") for f in base_files if isinstance(f, dict)}
    to_save, same = [], []
    imported_names = set()
    for f in data.files or []:
        imported_names.add(f["name"])
        if server.get(f["name"]) == f["content"]:
            same.append(f["name"])
        else:
            to_save.append((f["name"], f["content"]))
    kept = sorted(n for n in server if n not in imported_names and not str(n).endswith(".bak"))
    original_code = [[f["name"], f["content"]] for f in normalize_code(base_files)["files"]]
    snapshot = dict(original_code)
    snapshot.update(dict(to_save))
    return ImportPlan(merged, changed, to_save, same, kept,
                      [[name, content] for name, content in snapshot.items()],
                      original_code, copy.deepcopy(base_task))
