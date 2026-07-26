# names.py
# Единый резолвер тождества имён. Единственная реализация в проекте.
# Используется и phase_engine, и слоем записи (build_preview / apply_delta).
# Алгоритм не меняется в этом пакете (слой 1, задача 6).

from __future__ import annotations


def names_compatible(short: str, full: str) -> bool:
    """
    «Том» покрывается «Том Реддл»; «Том (бармен)» — нет.
    Точное совпадение всегда True.
    Уточнение в скобках = другой человек.
    short — первое слово full (и только если у full ≥ 2 слов).
    """
    s = short.casefold().strip()
    f = full.casefold().strip()
    if s == f:
        return True
    # уточнение в скобках = другой человек
    if "(" in s or "(" in f:
        return s == f
    # short — первое слово full
    f_parts = f.split()
    if len(f_parts) >= 2 and s == f_parts[0]:
        return True
    return False


# Алиас для обратной совместимости внутри phase_engine
_names_compatible = names_compatible
