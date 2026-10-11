# Recipes

**English** · [Русский](../ru/usage.md)

All examples use only the public API (`msdoc.to_markdown`, `msdoc.extract`). Ready-made scripts:
`examples/to_markdown.py`, `examples/llm_messages.py`, `examples/extract_folder.py`.

## Markdown and a list of pictures

```python
import msdoc

text, media = msdoc.to_markdown("report.doc")
print(text[:500])
print([(m.mime, len(m.data)) for m in media])
```

In `text` a picture looks like `![Media 0](image:0)`; `media[0]` holds its bytes and MIME type. Numbering starts at zero.

## Save Markdown and pictures to disk with links

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

A ready-made version with batch processing: `examples/to_markdown.py`.

## Send a document to a multimodal model

```python
import msdoc
from llm_messages import to_openai_content     # examples/llm_messages.py

parts = to_openai_content("report.doc", max_images=10)
messages = [{"role": "user", "content": [{"type": "text", "text": "List all numbers from the tables."}, *parts]}]
# client.chat.completions.create(model="…", messages=messages)
```

Raster pictures are inserted as `image_url` (data URI) at their positions; vector ones remain a note in the text.
To send text only, disable pictures: `msdoc.to_markdown(src, images_enabled=False)`.

## Text-only model: pictures as captions

```python
from llm_messages import to_text_with_captions   # caption() — replace with a call to your vision model
prompt_text = to_text_with_captions("report.doc")
```

## Split a document into sections by headings

```python
import msdoc

doc = msdoc.extract("report.doc")
sections, current = [], {"title": "(start)", "paragraphs": []}
for b in doc.blocks:
    if isinstance(b, msdoc.Table):
        current["paragraphs"].append("[table]")
    elif b.heading:
        sections.append(current)
        current = {"title": b.text.strip(), "paragraphs": []}
    elif b.text.strip():
        current["paragraphs"].append(b.text)
sections.append(current)
```

For vector search it is convenient to cut a long document at the `#` headings of the finished Markdown:
`re.split(r"(?m)^(?=#{1,6} )", text)`.

## Tables to data

```python
import msdoc

doc = msdoc.extract("report.doc")
for b in doc.blocks:
    if isinstance(b, msdoc.Table):
        for row in b.rows:
            print([" ".join(p.text for p in c.blocks if isinstance(p, msdoc.Paragraph)) for c in row.cells])
```

Merged cells are stored once (`cell.colspan`, `cell.rowspan`); for a rectangular grid, repeat the value across
`colspan`/`rowspan` yourself.

## Full-text search over a directory

```python
import pathlib
import msdoc

def iter_texts(root):
    for p in pathlib.Path(root).rglob("*.doc"):
        try:
            yield p, msdoc.extract(p, images_enabled=False).text
        except msdoc.MsDocError as e:
            print(f"skipping {p}: {type(e).__name__}: {e}")

for path, text in iter_texts("/data/docs"):
    if "supply agreement" in text.lower():
        print(path)
```

`images_enabled=False` saves noticeable time when pictures are not needed.

## Export to JSON (all stories)

```python
import json, msdoc

doc = msdoc.extract("a.doc")
print(json.dumps({"nfib": doc.nfib, "stories": doc.stories, "images": len(doc.images),
                  "warnings": doc.warnings}, ensure_ascii=False, indent=1))
```

## Pictures: save, look at sizes

```python
import io, msdoc
from PIL import Image as PILImage          # Pillow is for this example only, not a dependency of msdoc

doc = msdoc.extract("a.doc")
for im in doc.images:
    if im.mime in ("image/png", "image/jpeg", "image/gif", "image/bmp"):
        with PILImage.open(io.BytesIO(im.data)) as pic:
            print(im.kind, im.story, pic.size)
    else:
        print(im.kind, im.mime, f"{len(im.data)} bytes (vector format, needs conversion)")
```

Only floating or only inline pictures:

```python
floating = [im for im in doc.images if im.kind == "floating"]
inline   = [im for im in doc.images if im.kind == "inline"]
```

## Processing from memory (HTTP, database)

```python
import msdoc

def markdown_of(blob: bytes) -> str:
    return msdoc.to_markdown(blob, images_enabled=False).text
```

## Parallel processing (processes)

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

This is fast but does not protect against a hang on a single file. If files come from outside and you need a
hard timeout, isolate each in a `subprocess` with a timeout, as in `examples/to_markdown.py`.

## Hard timeout for one file

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

## Check whether a file is readable without extracting everything

There is no dedicated “check” function; the cheapest way is a call with pictures disabled:

```python
def is_readable(path) -> bool:
    try:
        msdoc.extract(path, images_enabled=False)
        return True
    except msdoc.MsDocError:
        return False
```

## Paragraphs and empty documents

```python
doc = msdoc.extract("a.doc")
paragraphs = [p for p in doc.stories["main"] if p.strip()]
print(len(paragraphs), "non-empty paragraphs")
if not paragraphs and not doc.images:
    print("the document is empty")
```

## Logging warnings

```python
doc = msdoc.extract("a.doc")
for w in doc.warnings:
    print("warning:", w)
```

A non-empty `warnings` means part of the structure was damaged and skipped; the text is usually still extracted.
