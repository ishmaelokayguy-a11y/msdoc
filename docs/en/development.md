# Development

**English** · [Русский](../ru/development.md)

## Repository layout

```
src/msdoc/        the library (standard library only); blocks.py and markdown.py — structure and Markdown
tests/            unit tests (pytest)
oracle/           the checking rig: corpus, LibreOffice reference, scoring, ratchet
examples/         to_markdown.py, llm_messages.py, extract_folder.py
docs/             documentation (en/, ru/)
prompts/          the task prompts the implementation was written from
scripts/          agent launch, status, branch merging
reviews/          reports of independent reviews (if any)
data/             (outside git) corpus, references, baseline.json, LibreOffice/POI clones
```

## Tests

```bash
python3 -m pip install -r requirements-dev.txt      # pytest, olefile, Pillow, rapidfuzz — development only
python3 -m pytest -q
```

The tests use synthetic bytes for edge cases and real files from the corpus (if `data/` exists; otherwise
such tests are skipped).

## Corpus and oracle

Requirements: LibreOffice (for the reference), Pillow, rapidfuzz.

```bash
scripts/bootstrap.sh                       # dependencies, corpus (~500 files), references (10–30 min), baseline
python3 oracle/compare.py --quiet          # run over dev
python3 oracle/compare.py --check          # same + ratchet (exit code 1 on a regression)
python3 oracle/compare.py --diff <id>      # difference from LibreOffice for one file
python3 oracle/compare.py --split holdout --quiet --label holdout   # held-out set
```

Reports go to `reports/`. For regular files `text mean`, `image F1` and the structure score are computed, along with a separate line
`text mean excluding fuzzer-origin files` (files from LibreOffice's fuzzer corpus are scored separately: the
reference is unreliable for them).

### Structure scoring

`make_golden.py` puts `struct` into the reference (headings, lists, tables, picture positions — taken from the ODT), `run_one.py` dumps the same
structure from `Document.blocks`, and `oracle/structscore.py` compares them by tokens (`H1`, `L0o`, `T3x4s1`, `I`). The ratchet uses
`data/struct_baseline.json`: `compare.py --check` fails if the `struct` of any file went down. References created before 0.2.0 have no
`struct`: regenerate them with `python3 oracle/make_golden.py --missing-struct` (or `--force`).

**Important.** Run `--update-baseline` in the same environment where the check runs (with the same `rapidfuzz`): text scores depend slightly
on it, and a baseline raised in another environment produces false “regressions”.

### Add your own documents

This is the best way to check quality on your real files:

```bash
python3 oracle/fetch_corpus.py --no-lo --no-poi --extra /path/to/your/doc
python3 oracle/make_golden.py              # LibreOffice reference for the new files
python3 oracle/compare.py --quiet
python3 oracle/compare.py --diff <id>      # look at the differences
python3 oracle/compare.py --update-baseline --quiet   # lock in when you are happy with the result
```

New files are automatically split into dev and holdout 85/15. If the documents are confidential, make sure
`data/` never leaves the machine (it is in `.gitignore`).

Documents with known expected content can be generated automatically:
`python3 oracle/synth_corpus.py --out data/synth --count 40` (needs LibreOffice and Pillow);
check against the expected strings — `python3 oracle/synth_corpus.py --check data/synth`.

## Rules for changes

1. Standard library only in `src/` (Python ≥ 3.9).
2. Only `MsDocError` goes out; everything read from the file is untrusted: check bounds,
   bound loops and recursion.
3. No fitting to specific files (by id, hash, name, size). Fix the general rule and cite the section of the
   specification or the LibreOffice file you rely on.
4. An MPL-2.0 header in every file; if logic was ported from LibreOffice — the line
   `# Portions of this file are derived from LibreOffice <path> (MPL-2.0).`
5. Code from GPL/LGPL projects (antiword, wvWare, catdoc, AbiWord) must not be copied.
6. After a change: `python3 -m pytest -q` and `python3 oracle/compare.py --check` (no file may get
   worse). Look at the mean reference score separately on dev and holdout.

### If you need to relax the ratchet

When you remove a rule that earned points only on fuzzer files, `compare.py --check` will fail. In that case use the
switch (only for fuzzer-origin files):

```bash
MSDOC_WAIVE_FUZZ=1 python3 oracle/compare.py --check
MSDOC_WAIVE_FUZZ=1 python3 oracle/compare.py --update-baseline    # recompute the baseline downwards for them
```

## Releasing a version

1. Update the version in `pyproject.toml` **and** `src/msdoc/__init__.py` (`__version__`).
2. `python3 -m pytest -q` and both scores (dev, holdout).
3. `git tag vX.Y.Z && python3 -m build` → `dist/`.
4. Check the wheel in a clean environment:
   `python3 -m venv /tmp/t && /tmp/t/bin/pip install dist/msdoc-X.Y.Z-py3-none-any.whl && /tmp/t/bin/python -c "import msdoc"`.

## How the implementation was written (for the record)

The skeleton, the oracle and the prompts (`prompts/`) were prepared by hand; the modules `cfb`, `fib`, `pieces/props`, `stories`,
`images` were written by separate Claude Code agents, each in its own git worktree and branch `agent/<name>`,
with a Stop hook that runs the ratchet. Branches were merged with `scripts/integrate.sh` only after the tests
and the ratchet passed. Then independent reviewers checked licence cleanliness (MPL headers and LibreOffice attribution),
absence of fitting to individual files, and parsing safety; the findings were fixed.

The structure layer (`blocks.py`, `markdown.py`) was added later in the same way: the skeleton and the structure scoring were prepared by hand, and
headings, lists, tables, pictures, footnotes, links and list labels were implemented by agents from `prompts/40-blocks.md` and
`prompts/41-blocks-rest.md` (branches `agent/blocks-headings`, `agent/blocks-rest`).
