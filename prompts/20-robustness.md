# Task: robustness on broken and hostile input

Read CLAUDE.md first. You may touch any module in `src/msdoc/`, but only to
add bounds checks, cycle detection and recovery — do not change behaviour on
valid files (the ratchet will catch you).

1. Run `python3 oracle/compare.py --kind fail notole legacy encrypted --quiet`
   and fix every crash/timeout.
2. Write `tests/test_fuzz.py`: deterministic mutation fuzzing (seeded RNG) of
   20 small dev corpus files — truncation, bit flips in the FIB, in the table
   stream (Clx, PLCFs, FKPs), in the Data stream and in OfficeArt records.
   Assert: returns a Document or raises MsDocError, within 5 s per case.
   Keep the default test run under ~60 s; put a longer run behind
   `MSDOC_FUZZ_ITERATIONS`.
3. Look for quadratic behaviour: the largest dev files must extract in < 2 s.

Done when: zero crash/timeout in the oracle, fuzz tests pass, ratchet passes.
