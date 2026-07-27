#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Задача 5: прогон образцов без сети.
Запуск: python run_samples.py

Случаи — JSON в samples/cases/*.json.
Виды: call | preview | write_property.
Flask должен быть установлен (requirements.txt). Подмены модулей нет.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "samples"
CASES_DIR = SAMPLES / "cases"

# --- сеть запрещена ---
import documents as _docs

def _no_network(*a, **k):
    raise RuntimeError("СЕТЬ ЗАПРЕЩЕНА в run_samples.py")

_docs.redis_get = _no_network
_docs.redis_set = _no_network
_docs.redis_command = _no_network
if hasattr(_docs, "redis_scan"):
    _docs.redis_scan = _no_network

from schema_live import set_codes, resolve_select_value, UnknownPropertyCode
from registry import is_relation, relation_target, choose_visible_binding, normalize
import registry as _reg
_reg._BINDING_VISIBLE_CACHE = None


def load_schema():
    data = json.loads((SAMPLES / "schema_burevestnik.json").read_text(encoding="utf-8"))
    codes = data.get("codes") or data
    set_codes("burevestnik", codes)
    return codes


def load_names():
    """id синтетические — только для сверки тождества; названия настоящие."""
    return json.loads((SAMPLES / "names_burevestnik.json").read_text(encoding="utf-8"))


def run_call(case: dict):
    call = case["call"]
    args = case.get("args") or []
    expect = case["expect"]

    table = {
        "registry.is_relation": is_relation,
        "registry.relation_target": relation_target,
        "registry.choose_visible_binding": choose_visible_binding,
        "schema_live.resolve_select_value": resolve_select_value,
    }
    if call == "names.names_compatible":
        from names import names_compatible
        fn = names_compatible
    else:
        fn = table.get(call)
    if fn is None:
        raise AssertionError(f"неизвестный call: {call}")

    if expect.get("raises"):
        try:
            fn(*args)
            raise AssertionError(f"ожидался {expect['raises']}")
        except Exception as e:
            if expect["raises"] not in type(e).__name__:
                raise AssertionError(f"ожидался {expect['raises']}, получен {type(e).__name__}: {e}")
        return

    result = fn(*args)
    if "equals" in expect:
        exp = expect["equals"]
        if isinstance(exp, dict) and isinstance(result, dict):
            for k, v in exp.items():
                assert result.get(k) == v, f"result[{k}]={result.get(k)!r} != {v!r}"
        else:
            assert result == exp, f"{result!r} != {exp!r}"


def run_preview(case: dict, names: dict):
    from app import build_preview
    delta = case["delta"]
    project = case.get("project") or "burevestnik"
    preview = build_preview(delta, project, resolver_data=names)
    expect = case["expect"]

    if "ok" in expect:
        assert preview.get("ok") is expect["ok"], f"ok={preview.get('ok')} expected {expect['ok']}"
    if "creates" in expect:
        assert len(preview.get("creates") or []) == expect["creates"]
    if "updates_min" in expect:
        assert len(preview.get("updates") or []) >= expect["updates_min"]
    if "warnings_contain" in expect:
        blob = " ".join(preview.get("warnings") or []) + " " + " ".join(preview.get("questions") or [])
        for needle in expect["warnings_contain"]:
            assert needle.lower() in blob.lower(), f"нет «{needle}» в: {blob[:400]}"


def run_write_property(case: dict, names: dict):
    from app import _prepare_property_for_write
    project = case.get("project") or "burevestnik"
    table = case["table"]
    label = case["label"]
    value = case["value"]
    expect = case["expect"]

    try:
        prepared = _prepare_property_for_write(project, table, label, value, names)
    except UnknownPropertyCode as e:
        if expect.get("ok") is False:
            err = str(e)
            needles = expect.get("error_contains") or []
            if needles and not any(n.lower() in err.lower() for n in needles):
                raise AssertionError(f"ошибка «{e}» не содержит ни одного из {needles}")
            return
        raise

    if expect.get("skipped"):
        assert prepared is None, f"ожидался skip (None), got {prepared!r}"
        return

    assert prepared is not None, "ожидался (code, value), got None"
    code, resolved = prepared

    if "code" in expect:
        assert code == expect["code"], f"code={code!r} expected {expect['code']!r}"

    if expect.get("value_is_id_list"):
        assert isinstance(resolved, list), f"value не список: {resolved!r}"
        assert all(isinstance(x, dict) and "id" in x for x in resolved)

    if "value_ids_from_names" in expect:
        target = relation_target(table, label) or table
        per = (names.get("per_table") or {}).get(target) or {}
        expected_ids = []
        for nm in expect["value_ids_from_names"]:
            matches = per.get(normalize(nm), [])
            assert matches, f"имя «{nm}» нет в names[{target}]"
            expected_ids.append(matches[0][0])
        got_ids = [x["id"] for x in resolved]
        assert got_ids == expected_ids, f"ids {got_ids} != {expected_ids}"


def main() -> int:
    print("=== run_samples.py (без сети, JSON-случаи) ===")
    case_files = sorted(CASES_DIR.glob("*.json"))
    if not case_files:
        print("FAIL: samples/cases/ пуст")
        return 1

    try:
        import flask  # noqa: F401 — должен быть настоящий, без подмены
    except ImportError:
        print("FAIL: Flask не установлен (pip install flask). Подмена модулей запрещена.")
        return 2

    load_schema()
    names = load_names()

    passed = 0
    failed = []
    for path in case_files:
        case = json.loads(path.read_text(encoding="utf-8"))
        name = case.get("name") or path.stem
        kind = case.get("kind")
        try:
            if kind == "call":
                run_call(case)
            elif kind == "preview":
                run_preview(case, names)
            elif kind == "write_property":
                run_write_property(case, names)
            else:
                raise AssertionError(f"неизвестный kind: {kind}")
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}")
            print(f"        {e!r}")
            failed.append(name)

    total = len(case_files)
    print(f"\n=== {passed} из {total} ===")
    if failed:
        print("не прошли:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
