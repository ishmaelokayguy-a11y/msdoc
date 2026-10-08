# Task: improve the oracle score — focus: {{FOCUS}}

Read CLAUDE.md first. Other fixers work in parallel on other focuses; stay on
yours to avoid conflicts: **{{FOCUS}}**.

Loop until the score for your focus stops improving (or 3 iterations in a row
without progress):
1. `python3 oracle/compare.py --quiet`; read `reports/latest.md` and
   `reports/latest.json`; select files where your focus is the cause.
2. `python3 oracle/compare.py --diff <id>` for 2–3 of them; find the common
   root cause in the spec / LibreOffice sources (`$DATA/sources/libreoffice-core`).
3. Fix the general rule, add a unit test, re-run those files, then the full
   `python3 oracle/compare.py --check`.
4. Commit with a message that says which files improved and why.

If LibreOffice is clearly wrong, document it in docs/lo_disagreements.md instead
of bending the code.
