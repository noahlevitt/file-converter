# Development guide

Setting up, running the tests, the conventions the code follows, and how to make the most common changes. Read
[ARCHITECTURE.md](ARCHITECTURE.md) first for how the pieces fit together.

## Setup

Requires Python 3.11 or newer (the Docker image uses 3.12).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env              # then edit: admin account, optional ANTHROPIC_API_KEY
.venv/bin/streamlit run app.py    # http://localhost:8501
```

`./run_local.sh` (Mac/Linux) or `run_local.bat` (Windows) does the same in one step. For a quick single-user
session without a login, set `AUTH_MODE=none` in `.env`.

## Tests

```bash
.venv/bin/python -m pytest -q            # everything (about 440 tests, a few seconds)
.venv/bin/python -m pytest -q tests/test_formats.py -k phone     # one area
```

The suite needs no network and no API key. Claude is replaced by fake clients. The sample files in
`sample_data/` are regenerated into a temporary folder for each test session by
`scripts/make_sample_files.py`, so tests always use exactly what the script produces. To refresh the copies in
`sample_data/` (for trying the app by hand), run `python scripts/make_sample_files.py`.

| Test module | Covers |
|---|---|
| `test_formats.py` | Hundreds of date, time and phone spellings against the real format, plus every rejection reason |
| `test_phone_repair.py` | Phone typo repair (allowed repairs only; area codes never changed or filled in), ambiguity, tie-breaking, marking |
| `test_multi_phone.py` | Several phone columns → one row per different number; phone-setting columns |
| `test_birthdays.py` | No-year birthdays/anniversaries → 1600 (placeholders, Excel-added years, split columns) |
| `test_real_schema.py` | The real import format end to end: headers, consent blank, dedupe, sample exports |
| `test_engine.py` | End-to-end conversion with the generic example schema |
| `test_normalizers.py` | Normalizers in isolation |
| `test_reader.py` | Encodings, delimiters, header detection, Excel files |
| `test_column_edits.py` | Goes to / Skip edits, wrong-column warnings |
| `test_mapping_ai_storage.py` | Auto-mapping, the Claude layer (faked), the grid editor, SQLite store, output |
| `test_notes_checks.py` | Notes applied by Claude, rule checks, review payload, phone masking |
| `test_jobs_compare.py` | Background runner, cancel, retention, side-by-side view and comparison workbook |

**Rule:** every behaviour change comes with a test that would have failed before it. Several past fixes exist
only because a test pinned down the exact case (e.g. birthdays losing `1600` on macOS).

## Conventions

- **Line length:** 120 characters.
- **Comments and docstrings:**
  - every module starts with an overview, and every function and class has a docstring;
  - comments explain *why* (the business rule, the edge case, the trade-off), not what the next line obviously
    does;
  - keep examples in docstrings real: they're checked against the code when documentation is updated.
- **No silent guesses:** a value that can't be converted safely gets a reason (`(output, error)` from every
  normalizer). A value that is repaired is imported **and** marked (`CELL_REPAIRED`).
- **Determinism:**
  - `engine.convert` must not call the network, Claude or anything random;
  - the only outside input it may use is today's date;
  - per-column decisions go in `normalizers.prepare()`, not in global state.
- **Schema first:** don't hard-code column names of the import format in code. If a rule depends on a field, give
  `FieldSpec` an attribute (like `no_year_value`, `fixed_blank`, `countries`) and set it in the YAML.
- **Portability:** the app runs on macOS, Linux (Docker) and Windows. Avoid platform-specific behaviour, such as
  control characters inside `strftime` formats (macOS truncates at a NUL byte).
- **Claude:** only behind a button, and only through structured outputs validated by `ai.parse_mapping_entries`
  / `ai.accept_repair`. Never let a Claude answer change data without a person approving it.

## Common changes

### Change the import format

Edit `schemas/target_schema.yaml`; the README section "Changing the target format" lists every attribute. The
app reads the file on each page load (cached until the file changes), so no restart is needed. In Docker the
`schemas/` folder is mounted from the host.

To check a change:
- the **Target format** page shows the parsed result;
- `python -m pytest -q tests/test_real_schema.py` should still pass, or be updated on purpose.

### Teach the auto-mapping a new header

Add the header to the field's `synonyms:` in the YAML. For patterns rather than single names, see
`heuristics.py`:
- `PHONE_WORDS` / `PHONE_ATTRIBUTE_WORDS`: phone column vs a column about a phone;
- `CONSENT_WORDS`: opt-in columns never mapped;
- `FULL_NAME_HEADERS` and `FULL_ADDRESS_HEADERS`;
- `_date_part_columns`: split birthday columns.

### Accept a new phone or date spelling

1. Add the failing value to `tests/test_formats.py` (`PHONE_OK`, the date tables, or `test_phone_rejected`).
2. Fix `normalizers._prep_phone` / `_parse_phone` / `_repair_phone`, or the date parser
   (`dateparse._prep` / `_parse_prepped`).
3. Run the whole suite. Date and phone rules interact: phone-looking values are also checked as dates during
   column profiling.

### Add a rule check

Add it to `checks.run_checks` (or `column_warnings` for per-column warnings shown on the mapping page). A
`Finding` needs:
- a severity (`error` / `warning` / `info`);
- the field and column;
- the issue, in plain words;
- a suggestion.

Add a test in `test_notes_checks.py`.

### Add a field type or transform

- **Type:** add it to `schema.FieldType`, write `normalize_<type>` in `normalizers.py` and register it in
  `NORMALIZERS`. Add `prepare` support if it needs per-column context.
- **Transform:** add a `TransformInfo` to `transforms.TRANSFORMS` and handle it in `apply_transform`. Claude's
  allowed transforms and the editor's "How" options are built from that registry automatically.

### Change Claude's behaviour

- **Prompts:** `ai.build_system_prompt` (schema description, rules such as "never map consent") and each
  method of `ClaudeAssistant`.
- **Models and sample sizes:** settings in `.env` (`MAPPING_MODEL`, `REPAIR_MODEL`, `AI_SAMPLE_VALUES`).
- **Tests:** fake the client like the existing tests do (`tests/test_notes_checks.py`, `test_mapping_ai_storage.py`).

## Versions and releases

- **Version:** the number is `converter/__init__.py` → `__version__`, and it is shown in the sidebar.
  - patch (`0.8.2` → `0.8.3`): fixes;
  - minor (`0.8` → `0.9`): new behaviour.
- **Tags:** every release is tagged `vX.Y.Z` in git.
- **History:** recorded in [CHANGELOG.md](CHANGELOG.md).
- **Commits:** one commit per changed file, message in the imperative ("Fix …", "Add …"), authored by the person
  making the change.
- **Release steps:**
  1. Run the tests.
  2. Bump `__version__` and update `docs/CHANGELOG.md`.
  3. Commit, then tag: `git tag vX.Y.Z`.
  4. `git push origin main --tags`.
  5. Deploy as described in [OPERATIONS.md](OPERATIONS.md#updating).

## Database changes

`storage.py` creates tables with `CREATE TABLE IF NOT EXISTS` and has no migration framework. Adding a column to
an existing table needs an explicit `ALTER TABLE` in `Store.__init__`, guarded so it runs once (check
`PRAGMA table_info`). Import records store JSON snapshots (mapping, schema, summary), so new optional keys can be
added there without a database change. Read them with `.get(...)` and a default, because old imports won't have
them.
