# schema_v7.py
# Единый источник истины модели данных Detective Engine v7
# Используется: провижининг (Т3), валидация DELTA, сверка/миграция (Т5)
# Не дублировать структуру нигде больше.
#
# Роллапы (Сводка):
#   Создаются/настраиваются через POST /api/v1/wiki/properties/command/execute
#   с code = "schema_property_update".
#   options:
#     - bindingCode  → code колонки-связи
#     - propertyCode → code свойства, по которому агрегировать
#     - formulaCode  → "countRows" | "cancel" | (вероятно sum/min/max/...)
#   Сам create колонки-роллапа (type) пока не пойман в чистом виде;
#   при провижининге: создаём колонку, затем сразу update-ом настраиваем.

from typing import Any

SOURCE_OPTIONS = [
    "авторская заметка",
    "написанный текст",
    "устное обсуждение",
    "выведено ИИ",
]

# ---------------------------------------------------------------------------
# Таблицы
# ---------------------------------------------------------------------------

SCHEMA: dict[str, dict[str, Any]] = {

    # -----------------------------------------------------------------------
    "world": {
        "emoji": "🌍",
        "title": "Мир",
        "key": "world",
        "description": "Только то, что невозможно нарушить (законы мироустройства).",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Статус",
                "type": "select",
                "format": "select",
                "options": ["черновик", "утверждено", "на пересмотре"],
            },
            {
                "name": "Источник",
                "type": "select",
                "format": "select",
                "options": SOURCE_OPTIONS,
            },
        ],
        "relations": [
            {
                "name": "Родительский элемент",
                "type": "binding",
                "format": "binding",
                "target": "world",          # рекурсивная
                "multiple": False,
            },
        ],
        "body_sections": [
            "Суть",
            "Жёсткие правила и цена",
            "Связь с текущей Аркой",
            "Синонимы и алиасы",
            # обязательный блок только для ветки «Каналы»
            "Кто может использовать",
            "Дальность и скорость",
            "Оставляет ли след / можно ли отследить",
            "Можно ли перехватить, подслушать, подделать",
            "Цена и ограничения",
            "Что ломает канал",
        ],
        "roots": [
            "Время и эпохи",
            "Законы мироустройства",
            "Системы магии",
            "Каналы",                     # → Перемещение | Связь
            "Общество и быт",
        ],
    },

    # -----------------------------------------------------------------------
    "locations": {
        "emoji": "📍",
        "title": "Локации",
        "key": "locations",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Тип",
                "type": "select",
                "format": "select",
                "options": [
                    "город",
                    "учреждение",
                    "помещение",
                    "природный объект",
                    "временная-перемещаемая",
                ],
            },
            {
                "name": "Статус",
                "type": "select",
                "format": "select",
                "options": ["существует", "разрушена", "недоступна", "скрыта"],
            },
            {
                "name": "Источник",
                "type": "select",
                "format": "select",
                "options": SOURCE_OPTIONS,
            },
        ],
        "relations": [
            {
                "name": "Родительская локация",
                "type": "binding",
                "format": "binding",
                "target": "locations",
                "multiple": False,
            },
            {
                "name": "Правила мира",
                "type": "binding",
                "format": "binding",
                "target": "world",
                "multiple": True,
            },
            {
                "name": "Контролирующая организация",
                "type": "binding",
                "format": "binding",
                "target": "organizations",
                "multiple": False,
            },
        ],
        "body_sections": [
            "Суть",
            "Сейчас",
            "История",
            "Чем отличается от соседних похожих",
            "Связи и влияние",
            "Открытые вопросы",
            "Авторские заметки",
        ],
    },

    # -----------------------------------------------------------------------
    "characters": {
        "emoji": "👤",
        "title": "Персонажи",
        "key": "characters",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Статус",
                "type": "select",
                "format": "select",
                "options": [
                    "живой",
                    "мёртвый",
                    "тень",
                    "призрак",
                    "проекция",
                    "неизвестно",
                ],
            },
            {
                "name": "Год рождения",
                "type": "number",
                "format": "number",
            },
            {
                "name": "Год выбытия",
                "type": "number",
                "format": "number",
            },
            {
                "name": "Значимость",
                "type": "select",
                "format": "select",
                "options": ["POV", "основной", "второстепенный", "фоновый"],
            },
            {
                "name": "Источник",
                "type": "select",
                "format": "select",
                "options": SOURCE_OPTIONS,
            },
        ],
        "relations": [
            {
                "name": "Связанные персонажи",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": True,
            },
            {
                "name": "Организации",
                "type": "binding",
                "format": "binding",
                "target": "organizations",
                "multiple": True,
            },
            {
                "name": "Артефакты",
                "type": "binding",
                "format": "binding",
                "target": "artifacts",
                "multiple": True,
            },
            {
                "name": "Ключевые события",
                "type": "binding",
                "format": "binding",
                "target": "events",
                "multiple": True,
            },
            {
                "name": "Ключевые локации",
                "type": "binding",
                "format": "binding",
                "target": "locations",
                "multiple": True,
            },
            {
                "name": "Линии",
                "type": "binding",
                "format": "binding",
                "target": "lines",
                "multiple": True,
            },
        ],
        "body_sections": [
            "Суть",
            "Сейчас",
            "Биография",          # якорный формат
            "Эпистемика",         # якорный формат
            "Связи и влияние",
            "Открытые вопросы",
            "Авторские заметки",
        ],
    },

    # -----------------------------------------------------------------------
    "artifacts": {
        "emoji": "🗝️",
        "title": "Артефакты",
        "key": "artifacts",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Тип",
                "type": "text",          # свободный текст, пока нет жёсткого списка
                "format": "text",
            },
            {
                "name": "Уникальность",
                "type": "select",
                "format": "select",
                "options": ["единственный", "один из немногих", "массовый"],
            },
            {
                "name": "Статус",
                "type": "select",
                "format": "select",
                "options": [
                    "в наличии",
                    "утрачен",
                    "уничтожен",
                    "скрыт",
                    "местонахождение неизвестно",
                ],
            },
            {
                "name": "Источник",
                "type": "select",
                "format": "select",
                "options": SOURCE_OPTIONS,
            },
        ],
        "relations": [
            {
                "name": "Текущий владелец",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": False,
            },
            {
                "name": "Текущая локация",
                "type": "binding",
                "format": "binding",
                "target": "locations",
                "multiple": False,
            },
            {
                "name": "Управляющие правила",
                "type": "binding",
                "format": "binding",
                "target": "world",
                "multiple": True,
            },
            {
                "name": "Связанные события",
                "type": "binding",
                "format": "binding",
                "target": "events",
                "multiple": True,
            },
            {
                "name": "Организации",
                "type": "binding",
                "format": "binding",
                "target": "organizations",
                "multiple": True,
            },
            {
                "name": "Линии",
                "type": "binding",
                "format": "binding",
                "target": "lines",
                "multiple": True,
            },
        ],
        "body_sections": [
            "Суть",
            "Сейчас",
            "Хронология владения и перемещений",  # якорный
            "Правила и цена использования",
            "Связи и влияние",
            "Открытые вопросы",
        ],
    },

    # -----------------------------------------------------------------------
    "organizations": {
        "emoji": "🏛️",
        "title": "Организации",
        "key": "organizations",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Тип",
                "type": "select",
                "format": "select",
                "options": [
                    "государственная",
                    "тайная",
                    "криминальная",
                    "учебная",
                    "семья-клан",
                    "религиозная",
                ],
            },
            {
                "name": "Статус",
                "type": "select",
                "format": "select",
                "options": ["действует", "распущена", "в подполье", "поглощена"],
            },
            {
                "name": "Влияние",
                "type": "select",
                "format": "select",
                "options": ["локальное", "национальное", "мировое"],
            },
            {
                "name": "Источник",
                "type": "select",
                "format": "select",
                "options": SOURCE_OPTIONS,
            },
        ],
        "relations": [
            {
                "name": "Родительская организация",
                "type": "binding",
                "format": "binding",
                "target": "organizations",
                "multiple": False,
            },
            {
                "name": "Руководство",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": True,
            },
            {
                "name": "Члены",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": True,
            },
            {
                "name": "Базовые локации",
                "type": "binding",
                "format": "binding",
                "target": "locations",
                "multiple": True,
            },
            {
                "name": "Противники",
                "type": "binding",
                "format": "binding",
                "target": "organizations",
                "multiple": True,
            },
            {
                "name": "Артефакты",
                "type": "binding",
                "format": "binding",
                "target": "artifacts",
                "multiple": True,
            },
            {
                "name": "Линии",
                "type": "binding",
                "format": "binding",
                "target": "lines",
                "multiple": True,
            },
        ],
        "body_sections": [
            "Суть",
            "Сейчас",
            "История",
            "Установленные правила",   # юридические запреты и регламенты
            "Цели и методы",
            "Связи и влияние",
            "Открытые вопросы",
        ],
    },

    # -----------------------------------------------------------------------
    "hooks": {
        "emoji": "🪝",
        "title": "Крючки",
        "key": "hooks",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Тип",
                "type": "select",
                "format": "select",
                "options": [
                    "предмет",
                    "фраза",
                    "деталь",
                    "поведение",
                    "несоответствие",
                ],
            },
            {
                "name": "Статус",
                "type": "select",
                "format": "select",
                "options": [
                    "Заявлен",
                    "Развивается",
                    "Выстрелил",
                    "Забыт",
                    "Отменён",
                ],
            },
            {
                "name": "Масштаб",
                "type": "select",
                "format": "select",
                "options": ["сцена", "арка", "цикл"],
            },
            {
                "name": "Приоритет",
                "type": "select",
                "format": "select",
                "options": ["высокий", "средний", "низкий"],
            },
            {
                "name": "Источник",
                "type": "select",
                "format": "select",
                "options": SOURCE_OPTIONS,
            },
        ],
        "relations": [
            {
                "name": "Заявлен в событии",
                "type": "binding",
                "format": "binding",
                "target": "events",
                "multiple": False,
            },
            {
                "name": "Выстрелил в событии",
                "type": "binding",
                "format": "binding",
                "target": "events",
                "multiple": False,
            },
            {
                "name": "Заявлен в главе",
                "type": "binding",
                "format": "binding",
                "target": "chapters",
                "multiple": False,
            },
            {
                "name": "Выстрелил в главе",
                "type": "binding",
                "format": "binding",
                "target": "chapters",
                "multiple": False,
            },
            {
                "name": "Связанные персонажи",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": True,
            },
            {
                "name": "Линии",
                "type": "binding",
                "format": "binding",
                "target": "lines",
                "multiple": True,
            },
        ],
        "rollups": [
            {
                "name": "Дистанция",
                "description": "Хронопорядок выстрела − Хронопорядок заявки",
                # Настройка через schema_property_update после создания колонки:
                # options: bindingCode (код связи), propertyCode, formulaCode
                # Известные formulaCode: "countRows", "cancel" (и вероятно sum/min/max)
                # Тип самой колонки при create пока не пойман — при провижининге
                # создаём как обычное свойство, затем update-ом превращаем в сводку.
                "config": {
                    "binding_code_field": "Выстрелил в событии",  # пример
                    "formula_code": "countRows",                  # placeholder
                },
            },
        ],
        "body_sections": [
            "Суть",
            "Как заявлен",
            "Как должен выстрелить",
            "Открытые вопросы",
        ],
    },

    # -----------------------------------------------------------------------
    "secrets": {
        "emoji": "🔒",
        "title": "Секреты",
        "key": "secrets",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Достоверность",
                "type": "select",
                "format": "select",
                "options": ["правда", "ложь", "полуправда"],
            },
            {
                "name": "Статус",
                "type": "select",
                "format": "select",
                "options": ["скрыт", "частично раскрыт", "раскрыт"],
            },
            {
                "name": "Масштаб",
                "type": "select",
                "format": "select",
                "options": ["сцена", "арка", "цикл"],
            },
            {
                "name": "Источник",
                "type": "select",
                "format": "select",
                "options": SOURCE_OPTIONS,
            },
        ],
        "relations": [
            {
                "name": "Носитель",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": False,
            },
            {
                "name": "Кто знает",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": True,
            },
            {
                "name": "Кто заблуждается",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": True,
            },
            {
                "name": "Событие раскрытия",
                "type": "binding",
                "format": "binding",
                "target": "events",
                "multiple": False,
            },
            {
                "name": "Раскрыт читателю в главе",
                "type": "binding",
                "format": "binding",
                "target": "chapters",
                "multiple": False,
            },
            {
                "name": "Линии",
                "type": "binding",
                "format": "binding",
                "target": "lines",
                "multiple": True,
            },
        ],
        "body_sections": [
            "Суть",
            "Правда",
            "Что думают остальные",
            "Цена раскрытия",
            "Открытые вопросы",
        ],
    },

    # -----------------------------------------------------------------------
    "events": {
        "emoji": "⚡",
        "title": "События",
        "key": "events",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Хронопорядок",
                "type": "number",
                "format": "number",
            },
            {
                "name": "Узловой",
                "type": "select",
                "format": "select",
                "options": ["да", "нет"],
            },
            {
                "name": "Статус",
                "type": "select",
                "format": "select",
                "options": ["Идея", "Черновик", "Написано", "Отменено"],
            },
            {
                "name": "Модус подачи",
                "type": "select",
                "format": "select",
                "options": [
                    "Настоящее",
                    "Воспоминание",
                    "Пересказ",
                    "Реконструкция",
                ],
            },
            {
                "name": "Источник",
                "type": "select",
                "format": "select",
                "options": SOURCE_OPTIONS,
            },
        ],
        "relations": [
            {
                "name": "Участники",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": True,
            },
            {
                "name": "POV",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": False,
            },
            {
                "name": "Локации",
                "type": "binding",
                "format": "binding",
                "target": "locations",
                "multiple": True,
            },
            {
                "name": "Родительское событие",
                "type": "binding",
                "format": "binding",
                "target": "events",
                "multiple": False,
            },
            {
                "name": "Задействованные правила",
                "type": "binding",
                "format": "binding",
                "target": "world",
                "multiple": True,
            },
            {
                "name": "Артефакты",
                "type": "binding",
                "format": "binding",
                "target": "artifacts",
                "multiple": True,
            },
            {
                "name": "Линии",
                "type": "binding",
                "format": "binding",
                "target": "lines",
                "multiple": True,
            },
        ],
        "body_sections": [
            "Суть",
            "Что происходит",
            "Последствия",
            "Связи и влияние",
            "Открытые вопросы",
        ],
    },

    # -----------------------------------------------------------------------
    "chapters": {
        "emoji": "📖",
        "title": "Главы / Части",
        "key": "chapters",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Порядок",
                "type": "number",
                "format": "number",
            },
            {
                "name": "Статус",
                "type": "select",
                "format": "select",
                "options": ["План", "Черновик", "Написана", "Вычитана"],
            },
            {
                "name": "Настроение",
                "type": "select",
                "format": "select",
                "options": [],          # набор значений задаёт автор (ожидает дозаполнение)
            },
            {
                "name": "Источник",
                "type": "select",
                "format": "select",
                "options": SOURCE_OPTIONS,
            },
        ],
        "relations": [
            {
                "name": "Родительская",
                "type": "binding",
                "format": "binding",
                "target": "chapters",
                "multiple": False,
            },
            {
                "name": "POV",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": False,
            },
            {
                "name": "Связанные события",
                "type": "binding",
                "format": "binding",
                "target": "events",
                "multiple": True,
            },
            {
                "name": "Связанные локации",
                "type": "binding",
                "format": "binding",
                "target": "locations",
                "multiple": True,
            },
            {
                "name": "Связанные персонажи",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": True,
            },
            {
                "name": "Линии",
                "type": "binding",
                "format": "binding",
                "target": "lines",
                "multiple": True,
            },
            {
                "name": "Референсы",
                "type": "binding",
                "format": "binding",
                "target": "references",
                "multiple": True,
            },
        ],
        "body_sections": [
            "Суть",
            "Что читатель узнаёт в этой главе",
            "Заметки по тексту",
            "Саундтрек",
            "Открытые вопросы",
        ],
    },

    # -----------------------------------------------------------------------
    "lines": {
        "emoji": "🧵",
        "title": "Линии",
        "key": "lines",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Тип",
                "type": "select",
                "format": "select",
                "options": [
                    "главная",
                    "расследование",
                    "романтическая",
                    "политическая",
                    "фоновая",
                ],
            },
            {
                "name": "Статус",
                "type": "select",
                "format": "select",
                "options": ["открыта", "развивается", "закрыта", "оборвана"],
            },
            {
                "name": "Приоритет",
                "type": "select",
                "format": "select",
                "options": ["высокий", "средний", "низкий"],
            },
        ],
        "relations": [
            {
                "name": "Родительская линия",
                "type": "binding",
                "format": "binding",
                "target": "lines",
                "multiple": False,
            },
            {
                "name": "Ключевые персонажи",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": True,
            },
            {
                "name": "Арка",
                "type": "binding",
                "format": "binding",
                "target": "chapters",
                "multiple": True,
            },
        ],
        "body_sections": [
            "Суть",
            "Как развивается",
            "Чем должна закончиться",
            "Открытые вопросы",
        ],
    },

    # -----------------------------------------------------------------------
    "references": {
        "emoji": "🎬",
        "title": "Референсы",
        "key": "references",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Тип",
                "type": "select",
                "format": "select",
                "options": [
                    "фильм",
                    "книга",
                    "песня",
                    "клип",
                    "миф",
                    "реальное событие",
                    "картина",
                ],
            },
            {
                "name": "Роль",
                "type": "select",
                "format": "select",
                "options": [
                    "источник вдохновения",
                    "прямая отсылка",
                    "скрытая пасхалка",
                    "структурный образец",
                ],
            },
            {
                "name": "Явность",
                "type": "select",
                "format": "select",
                "options": ["заметно", "для внимательных", "только для автора"],
            },
        ],
        "relations": [
            {
                "name": "Использовано в главах",
                "type": "binding",
                "format": "binding",
                "target": "chapters",
                "multiple": True,
            },
            {
                "name": "Использовано в событиях",
                "type": "binding",
                "format": "binding",
                "target": "events",
                "multiple": True,
            },
            {
                "name": "Связанные персонажи",
                "type": "binding",
                "format": "binding",
                "target": "characters",
                "multiple": True,
            },
            {
                "name": "Связанные локации",
                "type": "binding",
                "format": "binding",
                "target": "locations",
                "multiple": True,
            },
            {
                "name": "Линии",
                "type": "binding",
                "format": "binding",
                "target": "lines",
                "multiple": True,
            },
        ],
        "body_sections": [
            "Суть",
            "Что именно взято",
            "Как замаскировано",
        ],
    },

    # -----------------------------------------------------------------------
    "archive": {
        "emoji": "🗄️",
        "title": "Архив",
        "key": "archive",
        "properties": [
            {
                "name": "Название",
                "type": "text",
                "format": "text",
                "required": True,
            },
            {
                "name": "Откуда",
                "type": "text",          # или select по ключам таблиц
                "format": "text",
            },
            {
                "name": "Причина отклонения",
                "type": "text",
                "format": "text",
            },
            {
                "name": "Дата",
                "type": "text",          # пока text, после уточнения date
                "format": "text",
            },
        ],
        "relations": [],
        "body_sections": [
            "полное содержание отклонённого",
        ],
    },
}


# Порядок создания таблиц (сначала те, на которые ссылаются другие)
CREATION_ORDER = [
    "world",
    "locations",
    "characters",
    "organizations",
    "artifacts",
    "lines",
    "events",
    "chapters",
    "hooks",
    "secrets",
    "references",
    "archive",
]


def get_table(key: str) -> dict:
    return SCHEMA[key]


def all_table_keys() -> list[str]:
    return list(SCHEMA.keys())


def relation_targets(key: str) -> list[str]:
    """Список целевых таблиц, на которые ссылается данная."""
    rels = SCHEMA[key].get("relations", [])
    return sorted({r["target"] for r in rels})
