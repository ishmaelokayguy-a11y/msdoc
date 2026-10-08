# Task: independent review of branch {{BRANCH}}

You did not write this code. Review `git diff main...{{BRANCH}}` and write
`reviews/{{BRANCH_SLUG}}.md` (commit it on the current branch). Do not change
code outside `reviews/`.

Check, with evidence (file:line):
1. Special-casing of corpus files (ids, hashes, sizes, magic constants that only
   make one file pass), edits to oracle/, .claude/, data/ or baseline.
2. Licence: MPL header on every new file; "Portions derived from LibreOffice …"
   lines where logic was ported (compare suspicious code with
   `$DATA/sources/libreoffice-core`); no GPL-derived code.
3. Untrusted-input safety: unbounded loops/recursion over file-provided counts,
   unchecked offsets, exceptions other than MsDocError escaping extract().
4. Spec correctness of the parsed structures (spot-check against [MS-DOC]/[MS-ODRAW]).
5. Tests: do they test behaviour or just mirror the implementation?
6. Run `python3 -m pytest -q` and `python3 oracle/compare.py --check`; report results.

Verdict at the top: MERGE / MERGE AFTER FIXES (list) / REJECT (why).
