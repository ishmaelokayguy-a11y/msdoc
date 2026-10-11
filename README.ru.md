# msdoc

[English](README.md) · **Русский**

Извлечение **текста, структуры и изображений** из бинарных файлов Word 97–2003 (`.doc`) на чистом Python.
Результат — готовый **Markdown** (заголовки, списки, таблицы в HTML, ссылки, сноски) и картинки
отдельным списком байтов с указанием MIME. Подходит, чтобы подавать документы в LLM.

Без зависимостей (только стандартная библиотека), без LibreOffice, Word и внешних программ
на целевой машине. Python ≥ 3.9.

```python
import msdoc

text, media = msdoc.to_markdown("report.doc")   # bytes, путь или файловый объект
print(text)                                      # Markdown; на месте картинок: ![Media 0](image:0)
for i, im in enumerate(media):                   # media[i] <-> ![Media i](image:i)
    print(i, im.mime, len(im.data))              # im.data — байты картинки в исходном формате
```

Реализация сверена с LibreOffice на корпусе из ~500 документов: у обычных документов совпадение
текста около 99 %, картинок (F1) около 99 %, структуры (заголовки/списки/таблицы/места картинок) около 94 %
на dev и 86 % на отложенной выборке — подробности в [docs/ru/quality.md](docs/ru/quality.md).

## Содержание

- [Установка](#установка) (в том числе в закрытом контуре)
- [Markdown и картинки](#markdown-и-картинки) · [Формат Markdown](#формат-markdown)
- [Подача в LLM](#подача-в-llm)
- [Только текст и все части документа](#только-текст-и-все-части-документа)
- [Структура документа (blocks)](#структура-документа-blocks)
- [Ошибки](#ошибки)
- [Пакетная обработка каталога](#пакетная-обработка-каталога)
- [Ограничения](#ограничения)
- [Документация](#документация) · [Лицензия](#лицензия)

## Установка

Из готового колеса (зависимостей нет, интернет не нужен):

```bash
python3 -m pip install msdoc-0.2.0-py3-none-any.whl
```

Из исходников (каталог `src/` достаточно добавить в `PYTHONPATH`, установка не обязательна):

```bash
PYTHONPATH=src python3 -c "import msdoc; print(msdoc.__version__)"
```

Сборка колеса и sdist: `python3 -m pip install build && python3 -m build` → `dist/`.
Подробно про развёртывание без сети: [docs/ru/deployment.md](docs/ru/deployment.md).

## Markdown и картинки

```python
import msdoc

text, media = msdoc.to_markdown(open("report.doc", "rb").read())
```

* `text` — `str` с Markdown. Каждая картинка заменена заглушкой `![Media N](image:N)`.
* `media` — `list[Image]`; `media[N]` — картинка из заглушки `image:N` (нумерация с **0**).
  У `Image`: `data` (байты), `mime`, `kind` (`"inline"`/`"floating"`), `story`, `cp`, `name`.
* Возвращается именованный кортеж `MarkdownResult(text, media)`, поэтому работает и `r = msdoc.to_markdown(...)`
  (`r.text`, `r.media`), и распаковка `text, media = ...`.

Если у вас уже есть `Document`: `doc.markdown` или `doc.to_markdown(**options)`.

Параметры (`msdoc.to_markdown(source, *, images_enabled=True, label="Media", image_format=..., notes=True, extra=False)`):

| Параметр | По умолчанию | Смысл |
|---|---|---|
| `images_enabled` | `True` | `False` — не разбирать картинки (быстрее); заглушек в тексте тогда нет |
| `label` | `"Media"` | подпись в `![label N](image:N)` |
| `image_format` | `"![{label} {n}](image:{n})"` | шаблон заглушки (`{n}` — номер, `{label}` — подпись) |
| `notes` | `True` | сноски и концевые сноски внизу как `[^1]: …` / `[^e1]: …` |
| `extra` | `False` | добавить в конец надписи, примечания и колонтитулы |

Векторные картинки (WMF/EMF/PICT) возвращаются как есть; растеризовать их нужно самостоятельно.

## Формат Markdown

| Что в документе | Что в Markdown |
|---|---|
| заголовки (стили «Заголовок N», уровень структуры) | `#` … `######` |
| маркированные списки / нумерованные | `- пункт` / `1. пункт` (по метке Word, если удалось вычислить), вложенность — отступом |
| таблицы | **HTML** `<table>` с `<th>`/`<td>`, `colspan`, `rowspan`, вложенные таблицы |
| картинки | `![Media N](image:N)` на месте якоря |
| гиперссылки | `[текст](url)` |
| сноски, концевые сноски | `[^1]` / `[^e1]` в тексте и `[^1]: …` внизу |
| абзац, начинающийся как разметка (`# `, `- `, `1. `) | экранируется обратной косой чертой |

Таблицы выводятся в HTML, потому что Markdown-таблицы не умеют объединённые ячейки, вложенность и абзацы в ячейках.

## Подача в LLM

Два способа (готовый код — в [`examples/llm_messages.py`](examples/llm_messages.py)):

```python
import msdoc
from llm_messages import to_openai_content          # из каталога examples/

content = to_openai_content("report.doc")            # список частей text / image_url (data: URI)
messages = [{"role": "user", "content": [{"type": "text", "text": "Кратко перескажи документ:"}, *content]}]
```

1. **Мультимодальная модель:** текст режется по заглушкам, растровая картинка вставляется как `image_url`
   прямо на своём месте. Перед ней в тексте стоит метка `[Media N]`. Векторные картинки остаются меткой
   `[Media N: image/x-wmf, not shown]`.
2. **Текстовая модель:** `to_text_with_captions()` заменяет каждую картинку подписью, которую вы получаете
   от своей vision-модели (функция `caption()` — заглушка под ваш вызов).

Проверьте, принимает ли ваш сервер `image_url` и сколько картинок/токенов допускает контекст:
по умолчанию пример отправляет не более 20 растровых картинок до 5 МБ каждая.

## Только текст и все части документа

```python
doc = msdoc.extract("report.doc")
doc.text                                   # основной текст без разметки, по абзацу в строке
for name in msdoc.STORY_KEYS:              # main, footnotes, endnotes, headers, comments, textboxes
    print(name, len(doc.stories[name]))    # каждая история — список абзацев
```

`doc.stories` — «плоский» вариант, который сверялся с LibreOffice по тексту. Ячейки таблиц идут
подряд (по абзацу на абзац ячейки), структуры в нём нет.

```python
msdoc.extract(blob)                          # bytes / bytearray (из БД, HTTP)
msdoc.extract(open("a.doc", "rb"))           # файловый объект
msdoc.extract(pathlib.Path("a.doc"))         # путь
msdoc.extract("a.doc", images_enabled=False) # без картинок: быстрее и меньше памяти
```

Сохранение картинок на диск:

```python
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/bmp": "bmp",
       "image/tiff": "tif", "image/x-wmf": "wmf", "image/x-emf": "emf", "image/x-pict": "pct"}
for i, im in enumerate(doc.images):
    open(f"image_{i:02d}.{EXT.get(im.mime, 'bin')}", "wb").write(im.data)
```

## Структура документа (blocks)

`doc.blocks` — основной текст с разметкой: список `Paragraph` и `Table`. Из него строится Markdown;
им можно пользоваться напрямую, если нужен другой формат вывода.

```python
from msdoc import Table
for b in doc.blocks:
    if isinstance(b, Table):
        print("таблица", len(b.rows), "строк")
    elif b.heading:
        print("заголовок", b.heading, b.text)
    elif b.list_level is not None:
        print("пункт списка", b.list_level, b.list_number, b.text)
```

`Paragraph`: `text`, `heading` (0 — обычный, 1–9 — уровень), `list_level`, `list_ordered`, `list_number`,
`images` (индексы в `doc.images`), `notes`, `links`. В тексте картинки обозначены символом `U+FFFC`,
ссылки на сноски — `U+E000`. Подробности — в [docs/ru/api.md](docs/ru/api.md).

## Ошибки

| Исключение | Когда |
|---|---|
| `NotOleFileError` | вход — не контейнер OLE2 (не `.doc`) |
| `UnsupportedFormatError` | Word 6/95/2.0 (до Word 97) или OLE-файл без потока `WordDocument` |
| `EncryptedDocumentError` | документ зашифрован паролем |
| `CorruptDocumentError` | контейнер или структуры Word повреждены и восстановить их нельзя |

Все они наследуются от `MsDocError`, поэтому в общем случае достаточно `except msdoc.MsDocError`.
Других исключений наружу не выходит. Некритичные проблемы попадают в `doc.warnings`.

## Пакетная обработка каталога

```bash
python3 examples/to_markdown.py входной_каталог выходной_каталог --jobs 4 --timeout 60
python3 examples/to_markdown.py report.doc                 # -> report.md и report_media/
python3 examples/extract_folder.py входной выходной        # text.txt + stories.json + images/
```

Каждый файл обрабатывается в отдельном процессе с жёстким таймаутом, поэтому «плохой» файл не
останавливает пакет. В `to_markdown.py` заглушки заменяются относительными ссылками на сохранённые
файлы (`--keep-placeholders` оставляет их как есть).

## Ограничения

Коротко (подробнее — [docs/ru/limitations.md](docs/ru/limitations.md)):

- Только `.doc` формата Word 97–2003 (`nFib ≥ 0xC1`). `.docx`, `.rtf`, Word 6/95 — нет.
- Зашифрованные документы не расшифровываются (`EncryptedDocumentError`).
- Из форматирования передаются только структурные элементы (заголовки, списки, таблицы, ссылки, сноски);
  шрифты, цвета, выравнивание, жирный/курсив не передаются.
- Структура сверена с LibreOffice на 94 % (dev) / 86 % (holdout): на сложных документах возможны ошибки
  в уровнях списков, объединённых ячейках и порядке надписей.
- Встроенные OLE-объекты (диаграммы, формулы) и их картинки-превью не извлекаются.
- Библиотека не ограничивает время и память сама: для недоверенных файлов запускайте её в отдельном
  процессе (см. примеры).

## Документация

| Файл | О чём |
|---|---|
| [docs/ru/api.md](docs/ru/api.md) | справочник по публичному API |
| [docs/ru/usage.md](docs/ru/usage.md) | рецепты: Markdown, LLM, поиск, JSON, картинки, многопроцессность |
| [docs/ru/architecture.md](docs/ru/architecture.md) | как устроен разбор `.doc`, модули и поток данных |
| [docs/ru/limitations.md](docs/ru/limitations.md) | что не поддерживается и почему |
| [docs/ru/quality.md](docs/ru/quality.md) | как измерялась точность, цифры, расхождения с LibreOffice |
| [docs/ru/deployment.md](docs/ru/deployment.md) | установка без интернета, безопасность, ресурсы, лицензия |
| [docs/ru/development.md](docs/ru/development.md) | тесты, корпус, оракул, как вносить изменения |

English documentation: [docs/en/](docs/en/).

## Лицензия

MPL-2.0 (см. `LICENSE` и `NOTICE`). Часть логики разбора перенесена из LibreOffice (тоже MPL-2.0);
такие файлы помечены в заголовке строкой «Portions of this file are derived from LibreOffice …».
Тестовые документы и эталонные выходные данные в пакет не входят.
