# Рецепты

[English](../en/usage.md) · **Русский**

Все примеры используют только публичный API (`msdoc.to_markdown`, `msdoc.extract`). Готовые скрипты —
`examples/to_markdown.py`, `examples/llm_messages.py`, `examples/extract_folder.py`.

## Markdown и список картинок

```python
import msdoc

text, media = msdoc.to_markdown("report.doc")
print(text[:500])
print([(m.mime, len(m.data)) for m in media])
```

В `text` картинка выглядит как `![Media 0](image:0)`; `media[0]` — её байты и MIME. Нумерация с нуля.

## Сохранить Markdown и картинки на диск со ссылками

```python
import pathlib, re, msdoc

EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/bmp": "bmp",
       "image/tiff": "tif", "image/x-wmf": "wmf", "image/x-emf": "emf", "image/x-pict": "pct"}

def save(src, out_dir):
    out = pathlib.Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    text, media = msdoc.to_markdown(src)
    names = {}
    for i, im in enumerate(media):
        names[i] = f"{i}.{EXT.get(im.mime, 'bin')}"
        (out / names[i]).write_bytes(im.data)
    text = re.sub(r"!\[([^\]]*)\]\(image:(\d+)\)", lambda m: f"![{m[1]}]({names[int(m[2])]})", text)
    (out / "document.md").write_text(text, encoding="utf-8")
```

Готовая версия с пакетной обработкой — `examples/to_markdown.py`.

## Подать документ в мультимодальную модель

```python
import msdoc
from llm_messages import to_openai_content     # examples/llm_messages.py

parts = to_openai_content("report.doc", max_images=10)
messages = [{"role": "user", "content": [{"type": "text", "text": "Выпиши все числа из таблиц."}, *parts]}]
# client.chat.completions.create(model="…", messages=messages)
```

Растровые картинки вставляются как `image_url` (data URI) на своих местах, векторные остаются пометкой в тексте.
Чтобы подавать только текст, отключите картинки: `msdoc.to_markdown(src, images_enabled=False)`.

## Текстовая модель: картинки подписью

```python
from llm_messages import to_text_with_captions   # caption() — замените вызовом вашей vision-модели
prompt_text = to_text_with_captions("report.doc")
```

## Разобрать документ на разделы по заголовкам

```python
import msdoc

doc = msdoc.extract("report.doc")
sections, current = [], {"title": "(начало)", "paragraphs": []}
for b in doc.blocks:
    if isinstance(b, msdoc.Table):
        current["paragraphs"].append("[таблица]")
    elif b.heading:
        sections.append(current)
        current = {"title": b.text.strip(), "paragraphs": []}
    elif b.text.strip():
        current["paragraphs"].append(b.text)
sections.append(current)
```

Для векторного поиска удобно резать длинный документ по `#`-заголовкам в готовом Markdown:
`re.split(r"(?m)^(?=#{1,6} )", text)`.

## Таблицы в данные

```python
import msdoc

doc = msdoc.extract("report.doc")
for b in doc.blocks:
    if isinstance(b, msdoc.Table):
        for row in b.rows:
            print([" ".join(p.text for p in c.blocks if isinstance(p, msdoc.Paragraph)) for c in row.cells])
```

Объединённые ячейки хранятся один раз (`cell.colspan`, `cell.rowspan`); если нужна прямоугольная сетка,
повторите значение по `colspan`/`rowspan` самостоятельно.

## Полнотекстовый поиск по каталогу

```python
import pathlib
import msdoc

def iter_texts(root):
    for p in pathlib.Path(root).rglob("*.doc"):
        try:
            yield p, msdoc.extract(p, images_enabled=False).text
        except msdoc.MsDocError as e:
            print(f"пропуск {p}: {type(e).__name__}: {e}")

for path, text in iter_texts("/data/docs"):
    if "договор поставки" in text.lower():
        print(path)
```

`images_enabled=False` заметно экономит время, когда картинки не нужны.

## Выгрузка в JSON (все истории)

```python
import json, msdoc

doc = msdoc.extract("a.doc")
print(json.dumps({"nfib": doc.nfib, "stories": doc.stories, "images": len(doc.images),
                  "warnings": doc.warnings}, ensure_ascii=False, indent=1))
```

## Картинки: сохранить, посмотреть размеры

```python
import io, msdoc
from PIL import Image as PILImage          # Pillow — только для этого примера, не зависимость msdoc

doc = msdoc.extract("a.doc")
for im in doc.images:
    if im.mime in ("image/png", "image/jpeg", "image/gif", "image/bmp"):
        with PILImage.open(io.BytesIO(im.data)) as pic:
            print(im.kind, im.story, pic.size)
    else:
        print(im.kind, im.mime, f"{len(im.data)} байт (векторный формат, нужна конвертация)")
```

Только плавающие или только строчные картинки:

```python
floating = [im for im in doc.images if im.kind == "floating"]
inline   = [im for im in doc.images if im.kind == "inline"]
```

## Обработка из памяти (HTTP, БД)

```python
import msdoc

def text_of(blob: bytes) -> str:
    return msdoc.extract(blob, images_enabled=False).text
```

## Параллельная обработка (процессы)

```python
import pathlib
from concurrent.futures import ProcessPoolExecutor
import msdoc

def work(path):
    try:
        d = msdoc.extract(path, images_enabled=False)
        return str(path), len(d.text), None
    except msdoc.MsDocError as e:
        return str(path), 0, type(e).__name__

if __name__ == "__main__":
    files = list(pathlib.Path("/data/docs").rglob("*.doc"))
    with ProcessPoolExecutor(4) as ex:
        for name, n, err in ex.map(work, files, chunksize=8):
            print(name, n, err or "")
```

Этот вариант быстрый, но не защищает от зависания на отдельном файле. Если файлы приходят извне и
нужен жёсткий таймаут, используйте изоляцию по процессам с `subprocess` и таймаутом, как в
`examples/extract_folder.py`.

## Жёсткий таймаут на один файл

```python
import json, subprocess, sys

CODE = """
import sys, json, msdoc
d = msdoc.extract(sys.argv[1], images_enabled=False)
print(json.dumps({"chars": len(d.text)}))
"""

def safe_extract(path, timeout=60):
    try:
        p = subprocess.run([sys.executable, "-c", CODE, path], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}
    if p.returncode != 0:
        return {"status": "error", "stderr": p.stderr[-300:]}
    return {"status": "ok", **json.loads(p.stdout)}
```

## Определить, можно ли открыть файл, не извлекая всё

Отдельной функции «проверки» нет; самый дешёвый способ — вызов с отключёнными картинками:

```python
def is_readable(path) -> bool:
    try:
        msdoc.extract(path, images_enabled=False)
        return True
    except msdoc.MsDocError:
        return False
```

## Разбор по абзацам и поиск пустых документов

```python
doc = msdoc.extract("a.doc")
paragraphs = [p for p in doc.stories["main"] if p.strip()]
print(len(paragraphs), "непустых абзацев")
if not paragraphs and not doc.images:
    print("документ пуст")
```

## Логирование предупреждений

```python
doc = msdoc.extract("a.doc")
for w in doc.warnings:
    print("предупреждение:", w)
```

Непустой `warnings` означает, что часть структуры была повреждена и пропущена; текст при этом,
как правило, всё равно извлечён.
