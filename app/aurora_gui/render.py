"""Regeneration of the derived HTML fields of a task document.

The original web client keeps two structured fields (`algo2` — list, `method2` — object) and two
HTML strings derived from them on every tool hide/shutdown/forceSave:

* `method_description` <- `method2`   (tools/method2.js, drawTable, lines 101-310)
* `algo_text`          <- `algo2`     (tools/algo2.js,   drawTable, lines 331-443); the table numbers
  continue after the tables already present in `method_description`
  (`(method_description || '').split('<table').length`, algo2.js:1098/1194/1209).

This module ports both generators and reproduces the output byte for byte. The JS reads the form
values (`form.getFields()`); the stored `algo2` / `method2` are exactly those values saved by
onChanged (algo2.js:312, method2.js:413), so they are fed to the renderers directly. Load-time
normalisation of method2 (method2.js:22-79, 1311-1334) is mirrored too.

JS semantics mirrored on purpose: `htmlescape` turns every falsy value (incl. 0) into '', quotes
other than `"` are left alone, `row.next == 0` is a *loose* comparison ("0", "", " 0 " all match),
`kind` may be the string "0".."3" (array indexing by string, `>=` numeric compare), and
numbers/None are stringified the way `Array.join` does. Known deliberate differences: none of
the above prototype-key quirks of JS plain objects (`'constructor' in {}`) are reproduced.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

# JS String.prototype.trim(): WhiteSpace + LineTerminator
_JS_WS = (
    "\t\n\v\f\r              "
    "    　﻿"
)
_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


# ---------------------------------------------------------------- JS coercion helpers
def _str(v: Any) -> str:
    """JS String(v) as used by Array.join (None/undefined -> '')."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        return str(int(v)) if v == int(v) else repr(v)
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return ",".join(_str(x) for x in v)
    return "[object Object]"


def _trim(v: Any) -> str:
    """JS String.prototype.trim()."""
    return _str(v).strip(_JS_WS)


def _num(v: Any) -> float:
    """JS ToNumber (enough for loose/relational comparisons of kind/next values)."""
    if v is None:
        return math.nan                      # missing field (undefined)
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip(_JS_WS)
        if s == "":
            return 0.0
        try:
            if s[:2].lower() == "0x":
                return float(int(s[2:], 16))
            if s[:2].lower() == "0o":
                return float(int(s[2:], 8))
            if s[:2].lower() == "0b":
                return float(int(s[2:], 2))
        except ValueError:
            return math.nan
        if _NUM_RE.match(s):
            return float(s)
        if s in ("Infinity", "+Infinity"):
            return math.inf
        if s == "-Infinity":
            return -math.inf
        return math.nan
    return math.nan


def _eq_zero(v: Any) -> bool:
    """JS `v == 0` (loose equality; null/undefined are not == 0)."""
    if v is None:
        return False
    return _num(v) == 0.0


def _lower_first(v: Any) -> str:
    """method2.js:82 toLower: lower-case the first character only."""
    s = _str(v)
    return s[:1].lower() + s[1:]


def _idx(items: List[str], kind: Any) -> str:
    """`[...][kind]` for kind given as number or numeric string; out of range -> 'undefined'."""
    key = _str(kind)
    for i, item in enumerate(items):
        if key == str(i):
            return item
    return "undefined"


# ---------------------------------------------------------------- public API
def htmlescape(value: Any) -> str:
    """DS.util.htmlescape (DSUId.min.js:4677, php.js htmlspecialchars, quote_style ENT_COMPAT).

    Falsy values (None, '', 0, False) -> ''; & < > and " are escaped, ' is not."""
    if not value:
        return ""
    s = _str(value)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def table_no_after(method_description: Optional[str]) -> int:
    """Number of the first algo table: `(method_description || '').split('<table').length`
    (algo2.js:1098, 1194, 1209)."""
    return len((method_description or "").split("<table"))


def _norm_rows(rows: Any, fallback: Dict[str, Any], text_fields: List[str]) -> List[Dict[str, Any]]:
    """method2.js:43 normalizeGridRows (rows are copied, not mutated)."""
    if not isinstance(rows, list) or not rows:
        row = dict(fallback)
        row["id"] = 1
        return [row]
    out = []
    for i, src in enumerate(rows):
        row = dict(fallback)
        if isinstance(src, dict):
            row.update(src)
        row["id"] = i + 1
        for key in text_fields:
            row[key] = _str(row.get(key) or "")
        out.append(row)
    return out


def _norm_bases(bases: Any) -> List[Dict[str, Any]]:
    """method2.js:22 normalizeBasesRows."""
    if not isinstance(bases, list) or not bases:
        return [{"id": 1, "is_virtual": False, "name": "", "mod": "public"}]
    out = []
    for i, row in enumerate(bases):
        row = row if isinstance(row, dict) else {}
        out.append({"id": i + 1, "is_virtual": bool(row.get("is_virtual")),
                    "name": _str(row.get("name") or ""), "mod": row.get("mod") or "public"})
    return out


def render_method_description(method2: Dict[str, Any]) -> str:
    """Port of method2.js drawTable (lines 101-310): the `method_description` HTML."""
    h = htmlescape
    main = method2.get("main")
    if isinstance(main, dict):
        objects = _norm_rows(main.get("objects"), {"id": 1, "name": "", "type": "", "desc": ""},
                             ["name", "type", "desc"])
        functions = _norm_rows(main.get("functions"), {"id": 1, "name": "", "semantics": ""},
                               ["name", "semantics"])
        custom = _norm_rows(main.get("custom"), {"id": 1, "name": ""}, ["name"])
    else:
        objects, functions, custom = [], [], []

    out: List[str] = []
    items: List[str] = []
    for row in objects:
        if row.get("name") or row.get("type"):
            items.append("объект " + h(row.get("name")) + " класса " + h(row.get("type"))
                         + " предназначен для " + h(_lower_first(row.get("desc"))))
    for row in functions:
        if row.get("name") or row.get("semantics"):
            items.append("функция " + h(row.get("name")) + " для " + h(_lower_first(row.get("semantics"))))
    for row in custom:
        if row.get("name"):
            items.append(h(_lower_first(row.get("name"))))
    if items:
        out.append("<p>Для решения задачи используется:</p><ul><li>")
        out.append(";</li><li>".join(items))
        out.append(".</li></ul>")

    classes = [c for c in (method2.get("classes") or []) if c]
    inherited: Dict[str, List[Dict[str, Any]]] = {}
    datas: List[Dict[str, Any]] = []
    no = 0
    for cls in classes:
        no += 1
        data = dict(cls)
        data["name"] = _trim(cls.get("name"))
        bases = [dict(b) for b in _norm_bases(cls.get("bases"))]
        for b in bases:
            b["name"] = _trim(b["name"])
            if b["name"]:
                inherited.setdefault(b["name"], []).append({"name": data["name"], "info": b, "no": no})
        datas.append(data)

        out.append("<p>Класс ")
        out.append(h(data["name"]))
        out.append(":</p>")

        fields = []
        for r in cls.get("fields") or []:
            r = dict(r)
            r["semantics"], r["name"], r["type"] = (_trim(r.get("semantics")), _trim(r.get("name")),
                                                    _trim(r.get("type")))
            if r["semantics"] or r["name"] or r["type"]:
                fields.append(r)
        methods = []
        for r in cls.get("methods") or []:
            r = dict(r)
            r["semantics"], r["name"] = _trim(r.get("semantics")), _trim(r.get("name"))
            if r["semantics"] or r["name"]:
                methods.append(r)

        if fields or methods:
            out.append("<ul>")
            if fields:
                out.append("<li>свойства/поля:<ul>")
                for r in fields:
                    out.append("<li>поле ")
                    out.append(h(_lower_first(r["semantics"])))
                    out.append(":<ul><li>наименование &mdash; ")
                    out.append(h(r["name"]))
                    out.append(";</li><li>тип &mdash; ")
                    out.append(h(r["type"]))
                    out.append(";</li><li>модификатор доступа &mdash; ")
                    out.append(h(r.get("mod")))
                    out.append(";</li></ul></li>")
                out.append("</ul></li>")
            if methods:
                out.append("<li>функционал:<ul>")
                for j, r in enumerate(methods):
                    out.append("<li>метод ")
                    out.append(h(r["name"]))
                    out.append(" &mdash; ")
                    out.append(h(_lower_first(r["semantics"])))
                    out.append("." if j == len(methods) - 1 else ";")
                    out.append("</li>")
                out.append("</ul></li>")
            out.append("</ul>")
        else:
            out.append("<p>Собственные свойства и методы отсутствуют.</p>")

    if no > 1:
        out.append('<table border="1" width="100%">')
        out.append("<tr>")
        out.append("<th>№</th><th>Имя класса</th><th>Классы-наследники</th>"
                   "<th>Модификатор доступа при наследовании</th><th>Описание</th><th>Номер</th>")
        out.append("</tr>")
        for n, data in enumerate(datas, 1):
            inh = inherited.get(data["name"], [])
            span = str(len(inh) + 1)
            out.append("<tr>")
            out.append('<td rowspan="' + span + '">')
            out.append(str(n))
            out.append("</td>")
            out.append('<td rowspan="' + span + '">')
            out.append(h(data["name"]))
            out.append("</td>")
            out.append("<td></td>")
            out.append("<td></td>")
            out.append("<td>")
            out.append(h(data.get("desc")))
            out.append("</td>")
            out.append("<td></td>")
            out.append("</tr>")
            for row in inh:
                out.append("<tr>")
                out.append("<td>")
                out.append(h(row["name"]))
                out.append("</td>")
                out.append("<td>")
                out.append("virtual " if row["info"]["is_virtual"] else "")
                out.append(h(row["info"]["mod"]))
                out.append("</td>")
                out.append("<td></td>")
                out.append("<td>")
                out.append(str(row["no"]))
                out.append("</td>")
                out.append("</tr>")
        out.append("</table>")
    return "".join(out)


def render_algo_text(algo2: List[Dict[str, Any]], first_table_no: int = 1) -> str:
    """Port of algo2.js drawTable (lines 331-443): the `algo_text` HTML.

    `first_table_no` is JS `pageNo` (use table_no_after(method_description)); 0 -> 1."""
    h = htmlescape
    page_no = first_table_no or 1
    out: List[str] = []
    for data in algo2 or []:
        if not data:
            continue
        kind = data.get("kind")
        k = _num(kind)
        cls, name = data.get("class"), data.get("name")
        out.append("<div>")
        if 1 <= k <= 3:                                           # method
            k_int = int(k) if k == k and not math.isinf(k) else 0   # parseInt(kind)
            out.append({1: "<p>Класс объекта: ", 2: "<p>Конструктор класса: ",
                        3: "<p>Деструктор класса: "}.get(k_int, ""))
            out.append(h(cls))
            out.append("</p>")
            out.append("<p>Модификатор доступа: ")
            out.append(h(data.get("access")))
            out.append("</p>")
            if k == 1:
                out.append("<p>Метод: ")
                out.append(h(name))
                out.append("</p>")
        else:
            out.append("<p>Функция: ")
            out.append(h(name))
            out.append("</p>")

        out.append("<p>Функционал: ")
        out.append(h(data.get("func")))
        out.append("</p>")

        out.append("<p>Параметры: ")
        params = data.get("params")
        out.append(h(params) if params else "нет")
        out.append("</p>")

        if k < 2:
            out.append("<p>Возвращаемое значение: ")
            out.append(h(data.get("ret")))
            out.append("</p>")

        out.append("<p>Алгоритм " + _idx(["функции", "метода", "конструктора", "деструктора"], kind)
                   + " представлен в таблице " + str(page_no) + ".</p>")

        title = _idx(["функции " + h(name), "метода " + h(name) + " класса " + h(cls),
                      "конструктора класса " + h(cls), "деструктора класса " + h(cls)], kind)
        out.append("<div><i>Таблица " + str(page_no) + " &mdash; Алгоритм " + title + "</i></div>")
        page_no += 1

        out.append('<table border="1" width="100%">')
        out.append("<tr>")
        out.append("<th>№</th><th>Предикат</th><th>Действия</th><th>№ перехода</th><th>Комментарий</th>")
        out.append("</tr>")
        skip = 0
        for row in data.get("algo") or []:
            out.append("<tr>")
            if skip:
                skip -= 1
            else:
                rs = row.get("__rowspan") if "__rowspan" in row else None
                span = rs.get("no") if isinstance(rs, dict) else None
                if "__rowspan" in row and _num(span) > 1:
                    out.append('<td rowspan="' + _str(span) + '">')
                    skip = int(_num(span)) - 1
                else:
                    out.append("<td>")
                out.append(_str(row.get("no")))
                out.append("</td>")
            out.append("<td>")
            out.append(_str(row.get("predicate")))      # not escaped: preCommit already did it
            out.append("</td>")
            out.append("<td>")
            out.append(_str(row.get("actions")))
            out.append("</td>")
            out.append("<td>")
            nxt = row.get("next")
            out.append("∅" if _eq_zero(nxt) else h(nxt))
            out.append("</td>")
            out.append("<td>")
            out.append(_str(row.get("comment")))
            out.append("</td>")
            out.append("</tr>")
        out.append("</table>")
        out.append("</div>")
    return "".join(out)


def regenerate(task: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow copy of `task` with `method_description` and then `algo_text` recomputed from
    `method2` / `algo2` (algo tables are numbered after the tables of the new description).
    A field whose source is missing or of the wrong type is left as it was."""
    new = dict(task)
    if isinstance(new.get("method2"), dict):
        new["method_description"] = render_method_description(new["method2"])
    if isinstance(new.get("algo2"), list):
        new["algo_text"] = render_algo_text(new["algo2"], table_no_after(new.get("method_description")))
    return new
