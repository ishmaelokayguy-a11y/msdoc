# msdoc

[English](README.md) · **Русский**

Извлечение **текста, структуры и изображений** из бинарных файлов Word 97–2003 (`.doc`) — и тем же API из книг Excel 97–2003 (`.xls`), презентаций PowerPoint 97–2003 (`.ppt`) и документов RTF (`.rtf`) — на чистом Python.
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
- [OpenDocument (.odt, .ods, .odp)](#opendocument-odt-ods-odp)
- [Ошибки](#ошибки)
- [Пакетная обработка каталога](#пакетная-обработка-каталога)
- [Ограничения](#ограничения)
- [Документация](#документация) · [Лицензия](#лицензия)

## Установка

Из готового колеса (зависимостей нет, интернет не нужен):

```bash
python3 -m pip install msdoc-0.5.0-py3-none-any.whl
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

## Excel (.xls) и PowerPoint (.ppt)

`msdoc.extract` и `msdoc.to_markdown` определяют формат по потокам OLE (`WordDocument`, `Workbook`,
`PowerPoint Document`), поэтому вызовы те же, что для `.doc`; `doc.format` — `"doc"`, `"xls"` или `"ppt"`.

```python
doc = msdoc.extract("book.xls")
for sheet in doc.sheets:                        # Sheet: name, hidden, cells, merges, links, images
    print(sheet.name, sheet.cells.get((0, 0)))  # cells: {(строка, столбец): показанный текст}, с нуля

doc = msdoc.extract("talk.ppt")
for slide in doc.slides:                        # Slide: index, texts, notes, comments, images
    print(slide.index, slide.texts, slide.notes)
```

- **Excel**: только BIFF8. Текст ячейки — то, что *показывают* Excel/LibreOffice (применены числовые форматы,
  даты, дроби; для формул берётся последнее сохранённое значение). Markdown: заголовок `## Лист` и HTML-таблица
  на каждый видимый лист, объединённые ячейки — `rowspan`/`colspan`. Картинки привязаны к ячейкам.
- **PowerPoint**: последняя сохранённая версия каждого слайда (в том числе при повреждённой истории правок),
  заголовки, основной текст, надписи, заметки докладчика, комментарии рецензентов и картинки. Markdown:
  `## Slide N`, заголовки как `###`, основной текст маркированным списком, затем `Comments` и `Notes`.
- Не поддерживаются (`UnsupportedFormatError`): Excel 2–7 (BIFF2–5), PowerPoint 4/95; файлы с паролем дают
  `EncryptedDocumentError`. Диаграммы, встроенные OLE-объекты, макросы, геометрия фигур и анимации игнорируются.
- Качество измерено относительно LibreOffice так же, как для `.doc` (`oracle/office_*.py`, см.
  [docs/ru/quality.md](docs/ru/quality.md)): около 0,97 (`.xls`) и 0,92–0,95 (`.ppt`) по сводной оценке текст/картинки.

## Rich Text Format (.rtf)

Вход, начинающийся с `{\rtf` (допускаются BOM UTF-8 и пробелы перед ним), читается как RTF ещё до проверки
OLE; `doc.format` — `"rtf"`, а результат устроен так же, как для `.doc`: `stories` (main, footnotes, endnotes,
headers, comments, textboxes), `blocks` (заголовки по `\outlinelevel`, списки по `\listtable`/`\ls`/`\ilvl` и
старым `\pn`, таблицы с объединёнными и вложенными ячейками, гиперссылки, сноски) и `images` (`\pict`
PNG/JPEG/EMF/WMF/DIB в hex или `\bin`; `\shppict` предпочитается `\nonshppict`; картинки фигур — `floating`).

- Текст декодируется как в LibreOffice: `\ansicpg`, `\mac`/`\pc`/`\pca`, `\fcharset` шрифта (кириллица,
  греческий, двухбайтовые CJK, Symbol → U+F0xx), `\uN` с пропуском `\ucN`. Скрытый текст (`\v`) остаётся,
  удалённый в рецензировании (`\deleted`) выбрасывается; у полей берётся результат (PAGE в колонтитулах,
  TITLE, SYMBOL, EQ и элементы форм вычисляются так, как их показывает LibreOffice).
- Абзацы-рамки (`\posx`, `\absw`, ...), плавающие таблицы и текст фигур попадают в `textboxes`.
- Внедрённые OLE-объекты не возвращаются; картинка из `\result` берётся только в таблицах и для связанных
  объектов (как в LibreOffice). Защиты паролем у RTF нет (`\password` игнорируется).
- Повреждённый RTF (несбалансированные скобки, обрезанный файл, огромный `\binN`, очень глубокая вложенность)
  читается насколько возможно (с предупреждением); наружу выходят только наследники `MsDocError`.

## OpenDocument (.odt, .ods, .odp)

Zip-архив, у которого запись `mimetype` (или корневая запись манифеста) — тип OpenDocument (`text`,
`spreadsheet`, `presentation`, а также шаблоны `.ott/.ots/.otp`), читается как ODF до проверки OLE.
`doc.format` равен `"odt"`, `"ods"` или `"odp"`, результат устроен так же, как у старых форматов:

- **`.odt`** как `.doc`: `stories`, `blocks` (заголовки из `text:h`, списки с номерами по стилям списков, таблицы
  с объединёнными и вложенными ячейками, гиперссылки, сноски), `notes`, `extra` и `images`. Удалённый в режиме
  правки текст отбрасывается, поля дают сохранённый текст (кроме номера страницы и главы: LibreOffice
  пересчитывает их по вёрстке — 0 / пусто вне первой страницы).
- **`.ods`** как `.xls`: `doc.sheets` с текстом, который LibreOffice показывает в ячейке (ODF его хранит),
  объединённые диапазоны, скрытые листы, гиперссылки, картинки; повторяющиеся строки и столбцы разворачиваются с
  жёсткими лимитами; комментарии к ячейкам — в `stories["comments"]`. Формулы не вычисляются (RAND, TODAY, ...
  остаются с сохранённым текстом).
- **`.odp`** как `.ppt`: `doc.slides` с заголовками, текстом структуры, надписями, таблицами, группами,
  заметками, комментариями, ссылками и картинками; текст мастер-страниц не выдаётся (как при импорте в LibreOffice).

Картинки — это `draw:image` из пакета и `office:binary-data`; внешние ссылки, встроенные объекты, диаграммы и их
картинки-заменители не выдаются. Пакет считается враждебным: число записей, размеры и степень сжатия ограничены,
«размножающиеся» сущности DTD отвергаются (внешние не загружаются), слишком глубокий XML уплощается.

```python
doc = msdoc.extract("table.ods")
for sheet in doc.sheets:
    print(sheet.name, sheet.hidden, len(sheet.cells))
print(msdoc.to_markdown("deck.odp").text)      # '## Slide N', заголовки, пункты, Notes
```

## Ошибки

| Исключение | Когда |
|---|---|
| `NotOleFileError` | вход — не контейнер OLE2 (`.doc`/`.xls`/`.ppt`), не RTF (`{\rtf`) и не пакет OpenDocument (в том числе другие zip, например `.docx`) |
| `UnsupportedFormatError` | Word 6/95/2.0 (до Word 97), Excel 2–7, PowerPoint 4/95, OLE-файл, не являющийся ни одним из трёх, или пакет OpenDocument другого вида (рисунки `.odg`, формулы, ...) |
| `EncryptedDocumentError` | документ зашифрован паролем (в том числе зашифрованный пакет ODF) |
| `CorruptDocumentError` | контейнер или структуры Word повреждены и восстановить их нельзя (для ODF: повреждённый zip, некорректный или слишком большой XML, запись-«бомба») |

Все они наследуются от `MsDocError`, поэтому в общем случае достаточно `except msdoc.MsDocError`.
Других исключений наружу не выходит. Некритичные проблемы попадают в `doc.warnings`.

## Пакетная обработка каталога

```bash
python3 examples/to_markdown.py входной_каталог выходной_каталог --jobs 4 --timeout 60
python3 examples/to_markdown.py report.doc                 # -> report.md и report_media/
python3 examples/extract_folder.py входной выходной        # text.txt + stories.json + images/
python3 examples/office_formats.py book.xls                # листы / ячейки
python3 examples/office_formats.py deck.ppt --markdown     # слайды в Markdown
python3 examples/office_formats.py letter.rtf              # истории / блоки RTF
python3 examples/rtf_demo.py                               # RTF собирается в коде -> извлечение -> Markdown
python3 examples/odf_demo.py                               # .odt/.ods/.odp собираются в коде -> Markdown
```

В каталоге могут лежать вперемешку `.doc`, `.xls`, `.ppt` и `.rtf`; для остальных `to_markdown.py` пишет
`name.xls.md` / `name.ppt.md` / `name.rtf.md`, одинаковые имена не конфликтуют.

Каждый файл обрабатывается в отдельном процессе с жёстким таймаутом, поэтому «плохой» файл не
останавливает пакет. В `to_markdown.py` заглушки заменяются относительными ссылками на сохранённые
файлы (`--keep-placeholders` оставляет их как есть).

## Ограничения

Коротко (подробнее — [docs/ru/limitations.md](docs/ru/limitations.md)):

- Только `.doc` Word 97–2003 (`nFib ≥ 0xC1`), `.xls` Excel 97–2003, `.ppt` PowerPoint 97–2003, `.rtf`, `.odt`, `.ods` и `.odp`. `.docx`/`.xlsx`/`.pptx` и форматы до 97-го — нет.
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
