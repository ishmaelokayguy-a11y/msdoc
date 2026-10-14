# Справочник по API

[English](../en/api.md) · **Русский**

Всё публичное доступно из пакета верхнего уровня:

```python
import msdoc
msdoc.extract, msdoc.to_markdown, msdoc.MarkdownResult
msdoc.Document, msdoc.Image, msdoc.STORY_KEYS
msdoc.Paragraph, msdoc.Table, msdoc.Row, msdoc.Cell, msdoc.Link       # блоки структуры
msdoc.MsDocError, msdoc.NotOleFileError, msdoc.CorruptDocumentError,
msdoc.UnsupportedFormatError, msdoc.EncryptedDocumentError
msdoc.__version__
```

## `msdoc.extract`

```python
extract(source, *, images_enabled: bool = True) -> Document
```

Читает файл Word 97–2003 целиком в память, разбирает его и возвращает `Document`.

**`source`** — любое из:

| Тип | Пример |
|---|---|
| `str` / `os.PathLike` | `"a.doc"`, `pathlib.Path("a.doc")` |
| `bytes` / `bytearray` | содержимое файла из БД, сети |
| файловый объект | `open("a.doc", "rb")`, `io.BytesIO(...)`; нужен метод `read()` |

**`images_enabled`** — `False` пропускает разбор картинок (быстрее, меньше памяти). Тогда `doc.images == []`.

**Исключения.** Наружу выходят только подклассы `MsDocError` (список ниже). Это гарантия API: любой
другой тип исключения (`IndexError`, `struct.error`, `RecursionError`, …) считается дефектом.
Исключения ввода-вывода при открытии файла по пути (`FileNotFoundError`, `PermissionError`) — обычные
`OSError`: их библиотека не скрывает.

## `msdoc.to_markdown`

```python
to_markdown(source, *, images_enabled=True, label="Media",
            image_format="![{label} {n}](image:{n})", notes=True, extra=False) -> MarkdownResult
```

Одним вызовом: `extract` + построение Markdown. Возвращает именованный кортеж
`MarkdownResult(text: str, media: list[Image])`, поэтому допустимо `text, media = msdoc.to_markdown(src)`.
Источники и исключения — как у `extract`.

* Картинка в тексте заменена заглушкой `image_format.format(n=N, label=label)`; `N` — индекс в `media`, **с нуля**.
* `images_enabled=False` — картинки не разбираются, `media == []`, заглушек в тексте нет.
* `notes=True` — сноски внизу: `[^1]: …`, концевые — `[^e1]: …`. `extra=True` — в конце добавляются
  надписи, примечания и колонтитулы (в порядке, который не гарантируется).
* Тот же результат даёт `Document.to_markdown(**options)` (то же, что `doc.markdown` с настройками по умолчанию).

### Что попадает в Markdown

| Элемент | Вид |
|---|---|
| заголовок | `#`…`######` (уровень `Paragraph.heading`, не больше 6) |
| элемент списка | `- текст` или `1. текст` (метка `Paragraph.list_number`, если есть); вложенность — два пробела на уровень |
| таблица | HTML: `<table>`, `<tr>`, `<th>`/`<td>`, `colspan`, `rowspan`; в ячейке абзацы через `<br>`, вложенные таблицы целиком |
| гиперссылка | `[текст](url)` |
| ссылка на сноску | `[^N]` / `[^eN]` |
| картинка | заглушка |
| абзац, похожий на разметку (`# `, `- `, `1. `, `>`) | экранируется первым `\` |

## Структура документа: `Document.blocks`

Основной текст с разметкой; строится в `extract` и не требует отдельного вызова. Если структуру определить
не удалось, `blocks` состоит из обычных `Paragraph` без заголовков, списков и таблиц (текст не теряется).

```python
Block = Paragraph | Table

@dataclass Paragraph:  text: str; heading: int = 0; list_level: int | None = None; list_ordered: bool = False;
                       list_number: str | None = None; images: list[int]; notes: list[tuple[str, int]]; links: list[Link]
@dataclass Link:       start: int; end: int; url: str      # диапазон [start, end) в Paragraph.text
@dataclass Table:      rows: list[Row]
@dataclass Row:        cells: list[Cell]; header: bool
@dataclass Cell:       blocks: list[Block]; colspan: int = 1; rowspan: int = 1; header: bool = False
```

* `heading`: `0` — обычный абзац, `1`…`9` — уровень заголовка.
* `list_level`: `None` — не список, `0` — верхний уровень, `1` — вложенный и т. д.; `list_ordered` — нумерованный;
  `list_number` — метка, как её рисует Word (`"3."`, `"b)"`, `"IV."`, `"3.5.1"`), `None` для маркеров или если метку вычислить нельзя.
* В `Paragraph.text` картинка — символ `U+FFFC` (`model.IMAGE_MARK`), ссылка на сноску — `U+E000` (`model.NOTE_MARK`);
  `images[k]` — индекс в `Document.images` для k-го символа `U+FFFC`, `notes[k] = ("footnote" | "endnote", N)` — для k-го `U+E000`
  (`N` с единицы, номер заметки в `Document.notes`).
* Объединённая ячейка хранится один раз с `colspan`/`rowspan`; покрытые ею ячейки в `cells` не включены (семантика HTML).

Дополнительные поля `Document`:

| Поле | Тип | Описание |
|---|---|---|
| `blocks` | `list[Paragraph \| Table]` | основной текст со структурой |
| `notes` | `dict[str, list[list[Paragraph]]]` | `"footnotes"` / `"endnotes"`: список заметок, каждая — список абзацев |
| `extra` | `dict[str, list[Block]]` | `"textboxes"`, `"comments"`, `"headers"` в виде блоков; порядок не гарантируется |

`Document.stories` не изменился и остаётся «плоским» видом; `blocks` и `stories` строятся из одних данных.

## `msdoc.Document`

`@dataclass`. Поля:

| Поле | Тип | Описание |
|---|---|---|
| `stories` | `dict[str, list[str]]` | шесть ключей (`msdoc.STORY_KEYS`); каждое значение — список абзацев (`str`) без завершающего перевода строки; пустая история — `[]` |
| `images` | `list[Image]` | все найденные картинки |
| `nfib` | `int \| None` | `nFib` из заголовка; для Word 97 и новее это `0x00C1` (193), `0x00D9` (217), `0x0101` (257) и т. д. |
| `warnings` | `list[str]` | нефатальные проблемы: пропущенные повреждённые структуры, восстановленные ошибки |
| `blocks`, `notes`, `extra` | см. ниже | структура документа |

Свойство **`text`** → `str`: `"\n".join(stories["main"])`. Свойство **`markdown`** → `str`: `to_markdown()` с настройками по умолчанию.

### Истории (`stories`)

| Ключ | Содержимое |
|---|---|
| `main` | основной текст. Ячейки таблиц дают по абзацу на каждый абзац ячейки, в порядке чтения (строка за строкой, ячейка за ячейкой). Структура таблицы не передаётся |
| `footnotes` | текст сносок, по абзацу на абзац сноски |
| `endnotes` | текст концевых сносок |
| `headers` | текст колонтитулов (верхних и нижних) всех разделов; порядок **не** гарантируется |
| `comments` | текст примечаний (комментариев); порядок не гарантируется |
| `textboxes` | текст надписей и текстовых рамок (основного текста и колонтитулов); порядок не гарантируется |

Для `headers`, `comments` и `textboxes` рассматривайте результат как набор уникальных абзацев: при
сравнении с LibreOffice они оцениваются без учёта порядка.

## `msdoc.Image`

`@dataclass`. Поля:

| Поле | Тип | Описание |
|---|---|---|
| `data` | `bytes` | картинка в исходном формате, уже распакованная (метафайлы в файле хранятся сжатыми zlib) |
| `mime` | `str` | `image/png`, `image/jpeg`, `image/gif`, `image/bmp`, `image/tiff`, `image/x-wmf`, `image/x-emf`, `image/x-pict` |
| `kind` | `"inline" \| "floating"` | `inline` — картинка в строке текста (символ `0x01`); `floating` — плавающая фигура OfficeArt (символ `0x08`) |
| `story` | `str` | история, в которой найден якорь (по умолчанию `"main"`) |
| `cp` | `int \| None` | позиция якорного символа в тексте, если известна |
| `name` | `str \| None` | имя фигуры, если задано |

Одна и та же картинка может встретиться в документе несколько раз; библиотека не дедуплицирует их.

## Исключения

```
MsDocError
├── NotOleFileError          вход — не контейнер OLE2
├── CorruptDocumentError     контейнер или структуры Word повреждены
├── UnsupportedFormatError   Word 6/95 (nFib < 0x00C1), Word 2.0, или OLE-файл не Word
└── EncryptedDocumentError   документ защищён паролем
```

Типичные причины:

- `NotOleFileError` — `.docx` (это ZIP), текстовый файл, обрезанный `.doc` без сигнатуры
  `D0CF11E0A1B11AE1`.
- `UnsupportedFormatError` — очень старые файлы Word (до 97), а также `.xls`/`.ppt`, у которых нет потока `WordDocument`.
- `EncryptedDocumentError` — стоит флаг шифрования в заголовке; расшифровка не поддерживается.
- `CorruptDocumentError` — битый контейнер, отсутствующие обязательные потоки, не читаемая таблица кусков.

Сообщение исключения (`str(e)`) содержит причину на английском; оно предназначено для логов.

## Потокобезопасность и производительность

- По замыслу `extract` не использует глобального изменяемого состояния, вызовы независимы друг от друга (отдельной проверки на многопоточность не проводилось).
- Вся работа — чистый Python (CPU-bound): потоки не ускоряют, для параллелизма используйте процессы
  (`multiprocessing`, `concurrent.futures.ProcessPoolExecutor`).
- Файл читается целиком в память; пиковое потребление зависит от размера документа и числа картинок.
