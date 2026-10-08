# Task: stories and paragraphs (`src/msdoc/stories.py`)

Read CLAUDE.md first. You own `src/msdoc/stories.py` and `tests/test_stories.py`.
cfb, fib, pieces and props are implemented. This is where the text score is won.

Implement `build_stories` per its docstring: split the CP space into stories
using the FIB ccp* counts and the story PLCs; turn raw characters into
paragraphs; keep field results only; drop special characters; collect the CPs
of picture anchors (0x01 with sprmCFSpec, 0x08) for images.py.

Reference: ww8par.cxx (SwWW8ImplReader::ReadText/ReadChars/ReadChar),
ww8par5.cxx (fields), ww8par2.cxx (tables), ww8scan.cxx (WW8PLCFx_SubDoc,
WW8PLCFx_HdFt, WW8PLCFx_FLD).

Loop
1. `python3 oracle/compare.py --kind normal --quiet` and open `reports/latest.md`.
2. Pick the worst files whose problem is text (low `text`, story diffs).
3. `python3 oracle/compare.py --diff <id>`; understand the difference against
   the spec / LibreOffice code; fix the GENERAL rule; add a unit test.
4. Repeat. Watch the per-story numbers: main, footnotes, endnotes, headers,
   comments, textboxes.

Notes
- The oracle normalises whitespace and ignores empty paragraphs, so focus on
  content: field results vs field codes, nested fields, table cell marks,
  hidden text (LibreOffice keeps it), headers per section (compared as a set).
- Do not chase list numbering: LibreOffice's golden excludes it, and so should you.

Done when: the text mean on normal files plateaus, tests pass, and
`python3 oracle/compare.py --check` passes. Report remaining failure classes.
