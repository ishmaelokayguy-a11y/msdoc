# Deployment (including air-gapped environments)

**English** · [Русский](../ru/deployment.md)

## Requirements

- Python ≥ 3.9 (tested on 3.9). **No** third-party dependencies.
- Any OS (pure Python): Linux, macOS, Windows.
- LibreOffice, Word and internet access are **not** needed on the target machine.

## Moving into an isolated environment

Option A — a wheel (recommended):

```bash
# on a machine with internet / a build machine
python3 -m pip install build
python3 -m build                      # dist/msdoc-0.2.0-py3-none-any.whl and .tar.gz
sha256sum dist/*                      # write down the checksums

# inside the isolated environment
python3 -m pip install --no-index msdoc-0.2.0-py3-none-any.whl
python3 -c "import msdoc; print(msdoc.__version__)"
```

`--no-index` guarantees that `pip` will not go to the network. Since there are no dependencies, nothing else
needs to be transferred.

Option B — just a directory: copy `src/msdoc/` next to your code or add `src` to `PYTHONPATH`.
It works without installation.

Option C — the whole source tree (`git clone`/archive) with `docs/` and `examples/`, if you need the
documentation and the batch scripts.

## Check after installation

```python
import msdoc
print(msdoc.__version__)
text, media = msdoc.to_markdown("any_of_your_files.doc")
print(len(text), len(media), [m.mime for m in media])
print(text[:1000])
```

We recommend running 20–50 of your real documents and making sure the result suits you (especially list
numbering, merged table cells and hidden text — see [limitations.md](limitations.md)); compare the Markdown
with how the document looks in Word.

## Security

`.doc` is a format with a complex structure, and parsing input files is a classic attack surface.
What the library does: bounds-checks all reads, limits loops and recursion, does not run macros or embedded code,
does not launch external programs and does not touch the network. All errors are raised as `MsDocError`.

What you need to do if files come from outside:

1. Run parsing in a **separate process** with limits (timeout, memory, privileges): the library does not limit time
   or memory itself. Example — `examples/to_markdown.py` and `examples/extract_folder.py` (isolation via `subprocess`
   with a timeout).
2. Limit the input file size before parsing (`os.path.getsize`).
3. Limit the process memory (`ulimit -v`, cgroups, container limits).
4. Do not trust the extracted text in further processing (template engines, SQL, the command line):
   it is untrusted data. This applies to the Markdown too: it may contain text that looks like instructions to an LLM.
5. Decode and show pictures with safe tools (pictures can be maliciously crafted too); `msdoc` does not
   interpret them, it only cuts them out of the container.

## Resources

- Time: pure Python; an ordinary document takes fractions of a second, bulky ones seconds.
- Memory: the file is read whole, plus the extracted pictures in memory. For large batches process files one at a
  time (or in a process pool) and do not accumulate `Document`s in a list.
- Parallelism: processes, not threads (GIL).

## Licence for internal use

The project is distributed under **MPL-2.0** (file-level copyleft), like LibreOffice, part of whose logic has
been ported into the code. In practice this means:

- You may use the library inside an organisation, including in closed products and services.
- Keep the `LICENSE` and `NOTICE` files and the headers in the source files (they say which parts were ported from LibreOffice).
- If you modify **library files** and pass the result outside the organisation (distribute it), the modified
  files must be passed on under MPL-2.0 together with the source. Your own code that merely calls `msdoc` is not affected.
- Test documents and the oracle reference data are not included in the distribution.

This is a technical summary, not legal advice: consult your organisation's lawyer for final conclusions.
