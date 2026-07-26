# -*- coding: utf-8 -*-
"""
D. Сборка промта для фазы извлечения.
Префикс (system) стабилен внутри фазы → cache hit у DeepSeek.
Кусок + известные сущности + ответы автора — в user.
"""
from __future__ import annotations

from typing import Any

# ---------- Фазы (Протокол §2) ----------

PHASES: dict[int, dict[str, Any]] = {
    0: {
        "name": "Приём сырья и инвентаризация",
        "tables": [],
        "goal": "Оценить объём, список глав/кусков, ничего не извлекать.",
    },
    1: {
        "name": "Действующие лица и источники вдохновения",
        "tables": ["characters", "references"],
        "goal": (
            "ТОЛЬКО персонажи (позывные) и явные референсы. Маски комедии дель арте — не персонажи. "
            "Запрещено создавать записи в locations, events, organizations, artifacts, world, lines, hooks, secrets, chapters. "
            "Упоминания мест/событий/предметов — в not_taken с причиной «не таблица фазы 1», не в delta."
        ),
    },
    2: {
        "name": "Пространство",
        "tables": ["locations"],
        "goal": "Извлечь локации (иерархия через родителя, без транзитного мусора).",
    },
    3: {
        "name": "Силы и предметы",
        "tables": ["organizations", "artifacts"],
        "goal": "Извлечь организации и артефакты по тестам.",
    },
    4: {
        "name": "Скелет хронологии — только корни",
        "tables": ["events"],
        "goal": "Только корневые события (2.6.1). При сомнении — в вопросы.",
    },
    5: {
        "name": "Рамка",
        "tables": ["world", "lines"],
        "goal": "Правила мира и сквозные линии замысла.",
    },
    6: {
        "name": "Сверка с рамкой",
        "tables": ["characters", "locations", "organizations", "artifacts", "events"],
        "goal": "Найти расхождения уже извлечённого с утверждёнными Миром и Линиями.",
    },
    7: {
        "name": "Углубление событий",
        "tables": ["events"],
        "goal": "Дочерние события по приоритету автора.",
    },
    8: {
        "name": "Кандидаты в ружья",
        "tables": ["hooks", "secrets"],
        "goal": "Только кандидаты. Никогда не создавать без утверждения автора.",
    },
    9: {
        "name": "Скелет подачи",
        "tables": ["chapters"],
        "goal": "Главы/части как карточки подачи.",
    },
    10: {
        "name": "Тела карточек",
        "tables": ["*"],
        "goal": "Наполнение тел (ТЕЛО / ТЕЛО-ДОПОЛНИТЬ) по уже известным сущностям.",
    },
    11: {
        "name": "Связи",
        "tables": ["*"],
        "goal": "Простановка relation-полей по именам.",
    },
}

# Тесты вычленения — короткий канон для префикса (Протокол §3)
EXTRACTION_TESTS = """
## Тесты вычленения (обязательны)

### 👤 Персонаж
Кандидат, если: назван (именем, прозвищем или **позывным**) И (совершает действие ИЛИ является объектом действия, влияющего на сюжет).
В этой рукописи действующие лица часто ходят под **позывными** (Гарпия, Василиск, Грифон…) — позывной = карточка персонажа.
**Не кандидат (часто путают):**
- маски/архетипы комедии дель арте (Pantalone, Arlecchino, Colombina, Dottore, Pulcinella, Capitano, Gnaga и т.п.) — это **свойства/обличья**, не отдельные персонажи; писать в not_taken: «маска, не персонаж»;
- безымянные функции («стражники», «толпа»), категории без влияния.
Смена формы (живой→тень/призрак) или смена маски — та же карточка, не новая.
**Именование:** копируй написание **буквально из текста куска**. Не подменяй орфографию из внешних знаний (запрещено: «Риддл» вместо «Реддл» и любые «исправления» из Гарри Поттера и др.).

### 🎬 Референс
Кандидат только если автор прямо назвал источник ИЛИ в тексте явная отсылка.
Никогда не выдумывать по стилистическому сходству.

### 📍 Локация
Кандидат, если: названа И (в ней действие ИЛИ нужна как родитель).
Не кандидат: транзитные коридоры/проходы, помещения без смысловой нагрузки.
Именование экземпляров — по стабильным признакам, не по событию.

### 🏛️ Организация
Кандидат, если: названа И (субъект ИЛИ задаёт правила ИЛИ ≥2 персонажа принадлежат).
Не кандидат: фон без влияния.

### 🗝️ Артефакт
Кандидат, если: назван И (меняет владельца ИЛИ объект действия ИЛИ свойства влияют на исход).
Не кандидат: бытовой декор.

### ⚡ Корневое событие
По 2.6.1 Библии. Сомнение → всегда в вопросы, не решать самому.

### 🌍 Мир
Устойчивый механизм/закон, который невозможно нарушить.
Нарушаемые запреты → в тело организации, не в Мир.

### 🧵 Линия
Сквозная причинно-следственная цепь через ≥3 события, со своим вопросом/целью.

### 🪝 Крючок / 🔒 Секрет
Только кандидаты. Без утверждения автора — не создавать.
"""

PROTOCOL_RULES = """
## Жёсткие правила
1. В базу только то, что есть в сырье или названо автором. Выводы ИИ → Источник=«выведено ИИ» + в вопросы.
2. Не создавать дубликаты известных сущностей (список ниже). Сомневаешься в тождестве — в вопросы с вариантом.
3. Крючки и Секреты — только кандидаты, никогда карточки без утверждения.
4. Вопросы — пачкой в конце, каждый с вариантом по умолчанию. Не останавливай разбор куска.
5. Единица выдачи — DELTA-пакет, не по одной карточке.

6. В delta разрешены ТОЛЬКО таблицы текущей фазы (см. «Таблицы фазы»). Любая другая таблица — нарушение. Вынеси такие упоминания в not_taken.
"""

DELTA_FORMAT = """
## Формат ответа (строго JSON)

Верни ОДИН json-объект:
{
  "phase": <number>,
  "chunk_id": "<string>",
  "delta": [
    {
      "table": "characters|locations|organizations|artifacts|events|world|lines|hooks|secrets|chapters|references",
      "action": "create|update",
      "title": "<название карточки>",
      "properties": { "<поле>": "<значение или список имён>" },
      "body": null,
      "body_append": null,
      "source": "сырьё|выведено ИИ"
    }
  ],
  "candidates": [
    { "table": "hooks|secrets", "title": "...", "why": "...", "default": "отложить|утвердить" }
  ],
  "questions": [
    { "id": "q1", "text": "...", "default": "...", "options": ["...", "..."] }
  ],
  "not_taken": [
    { "mention": "...", "why": "транзит|фон|нет действия|..." }
  ]
}

Ссылки на другие сущности — только по именам (не id).
action=update — если сущность уже в списке известных.
body — полная замена тела; body_append — дополнение (для фаз 10+).
В фазах 1–5 обычно body/body_append = null (только свойства и названия).
"""


def _tables_hint(phase: int) -> str:
    p = PHASES.get(phase) or {}
    tables = p.get("tables") or []
    if tables == ["*"]:
        return "Таблицы фазы: все, по необходимости."
    if not tables:
        return "Таблицы фазы: нет (только инвентаризация)."
    return (
        "Таблицы фазы (единственные разрешённые в delta): " + ", ".join(tables) + ". "
        "Любая другая таблица в delta запрещена."
    )


def build_system_prefix(phase: int) -> str:
    """Стабильный префикс для cache hit внутри одной фазы."""
    p = PHASES.get(phase)
    if not p:
        raise ValueError(f"неизвестная фаза: {phase}")
    return "\n".join([
        "Ты — модуль извлечения Детективного движка. Работаешь строго по протоколу.",
        f"=== ФАЗА {phase}: {p['name']} ===",
        f"Цель: {p['goal']}",
        _tables_hint(phase),
        PROTOCOL_RULES.strip(),
        EXTRACTION_TESTS.strip(),
        DELTA_FORMAT.strip(),
        "В system или user обязательно присутствует слово json — отвечай только валидным json.",
    ])


def build_user_message(
    *,
    phase: int,
    chunk: dict,
    known_entities: dict[str, list[str]] | None = None,
    author_answers: list[dict] | None = None,
) -> str:
    """
    known_entities: { "characters": ["Марта", ...], "locations": [...], ... }
    author_answers: [ {"id": "q1", "answer": "..."}, ... ]
    """
    known_entities = known_entities or {}
    author_answers = author_answers or []

    lines = [
        f"chunk_id: {chunk.get('id')}",
        f"глава: {chunk.get('chapter_title')} (index={chunk.get('chapter_index')})",
        f"часть: {chunk.get('part')}/{chunk.get('parts_total')}",
        f"фаза: {phase}",
        "",
        "## Уже известные сущности (не дублируй; update при новых фактах)",
    ]
    if not any(known_entities.values()):
        lines.append("(пусто — первый кусок / фаза)")
    else:
        for table, names in known_entities.items():
            if not names:
                continue
            lines.append(f"- {table}: " + "; ".join(names))

    lines.append("")
    lines.append("## Ответы автора на прошлые вопросы")
    if not author_answers:
        lines.append("(нет)")
    else:
        for a in author_answers:
            lines.append(f"- {a.get('id')}: {a.get('answer')}")

    lines.append("")
    lines.append("## Текст куска")
    lines.append(chunk.get("text") or "")
    lines.append("")
    lines.append("Верни json по схеме выше.")
    return "\n".join(lines)


def build_messages(
    phase: int,
    chunk: dict,
    known_entities: dict[str, list[str]] | None = None,
    author_answers: list[dict] | None = None,
) -> dict:
    system = build_system_prefix(phase)
    user = build_user_message(
        phase=phase,
        chunk=chunk,
        known_entities=known_entities,
        author_answers=author_answers,
    )
    return {
        "phase": phase,
        "phase_name": PHASES[phase]["name"],
        "chunk_id": chunk.get("id"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "meta": {
            "system_chars": len(system),
            "user_chars": len(user),
            "system_tokens_est": max(1, round(len(system) / 1.5)),
            "user_tokens_est": max(1, round(len(user) / 1.5)),
            "cache_hint": "system-префикс стабилен для всех кусков одной фазы",
        },
    }
