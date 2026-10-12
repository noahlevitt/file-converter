# Architecture

How the converter is put together: the path a file takes, what each module is responsible for, the main data
structures, and the design decisions behind them. For the libraries used see [STACK.md](STACK.md); for day-to-day
development see [DEVELOPMENT.md](DEVELOPMENT.md).

## Design principles

1. **Deterministic conversion.** The same file, mapping and target format always give the same output. The engine
   never calls Claude, the network or a random source. The only outside input is today's date, which is used to
   choose 19xx/20xx for two-digit years and to spot "no year" birthdays.
2. **Schema-driven.** The output format (columns, order, types, formats, required fields, duplicate rule, US-only
   phones, the 1600 no-year value, consent locked blank) lives in `schemas/target_schema.yaml`. The code knows no
   column names of the real import format.
3. **Nothing silent.** Every value that can't be read safely is either rejected (the row isn't imported) or left
   blank, always with a reason. Every value the converter repaired (phone typos) is imported and marked. The
   per-cell audit records what happened to every cell.
4. **People decide, Claude advises.** Claude only proposes mapping changes or value fixes. Its output is validated
   against the schema and the file's real columns, shown as a diff, and applied only after approval. `consent` can
   never be mapped by anyone.
5. **One machine, few moving parts.** Streamlit, SQLite and an in-process thread pool. No separate API, queue or
   database server.

## The path of a file

```
 Browser (Streamlit, app.py)                         converter/ package
 ───────────────────────────                         ──────────────────
 1. Upload ──────────────────────────────────────►  reader.read_file
                                                      encoding, delimiter, sheet, header row, title rows
                                                      → DataFrame of text + warnings
 2. Columns profiled ────────────────────────────►  mapping.profile_columns
                                                      samples, fill rate, share of phone/email/date/zip/state values
 3. Initial mapping ─────────────────────────────►  services.initial_mapping
                                                      a. saved template with exactly these headers → use it
                                                      b. else heuristics.auto_map (+ similar template's rules)
                                                      c. Claude refinement only if AI_AUTO_MAP=true
 4. Review: field grid + "Columns in your file" ─►  editor.apply_row_edit / apply_column_edit
    preview of converted values ─────────────────►  engine.preview_values (first 200 rows)
    warnings (column content vs field) ──────────►  checks.column_warnings
    optional: Apply notes / Check for mistakes ──►  ai.ClaudeAssistant.apply_notes / services.check_for_mistakes
 5. Start import ────────────────────────────────►  storage.Store.create_import (snapshot of file, mapping,
                                                      schema, read options) + save template
                                                     jobs.ImportRunner.submit → thread pool
 6. Background run ──────────────────────────────►  jobs.run_import: read again → engine.convert →
                                                      output.write_output / write_rejects / write_summary,
                                                      audit.parquet + source.parquet, counts on the import record
 7. Imports page: live progress, cancel ─────────►  storage (progress, stage, cancel flag), ui_format
    finished import: side by side, what changed ─►  compare.build_compare_html / field_change_summary /
                                                      comparison_xlsx (from source.parquet + audit.parquet)
    optional: check whole import, suggest fixes ─►  services.check_for_mistakes, ai.repair_values +
                                                      services.validate_repairs → re-run with overrides
```

Re-runs (**Re-run this import**, **Re-run the import with these changes**, **Apply fixes and re-run**) create a
*new* import linked by `parent_id`. The original is never modified. A re-run uses the stored original file and the
current target format.

## Modules

All business logic is in `converter/`. Only `app.py` and `converter/auth.py` import Streamlit.

| Module | Responsibility |
|---|---|
| `app.py` | Streamlit UI: login, navigation, the light/dark toggle and the five pages (New import, Imports, Templates, Target format, Users). Holds no conversion logic; it calls the package. |
| `converter/reader.py` | Turns uploaded bytes into a DataFrame of text: encoding detection, delimiter sniffing, sheet choice, header-row detection (skipping title rows), unique names for blank/duplicate headers (`Column 3`, `Phone (2)`…), stray spaces and byte-order marks. Excel artifacts in values (`="…"`, `5.16E+09`, serial dates) are handled later by the normalizers. |
| `converter/schema.py` | Pydantic models of the target format (`FieldSpec`, `OutputSpec`, `DedupeSpec`, `TargetSchema`) and `load_schema`. Validates the YAML and fills derived defaults. |
| `converter/mapping.py` | `FieldMapping` / `Mapping` (target field → transform + source columns + params, plus reviewer notes and skipped columns), `normalized()` (makes a mapping fit a file), column profiling, header fingerprints for template matching. |
| `converter/transforms.py` | Registry of the allowed transforms (copy, coalesce, one row per phone number, combine, name part, address part, split, translate values, fixed value) and `apply_transform`. Transforms only *extract* a raw value; they never validate. |
| `converter/heuristics.py` | Rule-based automatic mapping: header synonyms + fuzzy matching + value evidence. Includes phone-column detection, phone-setting/consent column exclusion, full-name and full-address splitting, and split birthday columns. |
| `converter/normalizers.py` | Per-type cleaning and validation: phone (US-only, limited typo repair that never adds or changes digits), date/datetime (via `dateparse`), email, ZIP, state, boolean, enum, numbers, strings. `prepare()` builds a per-column context first (day/month order, placeholder year, the file's own numbers and area codes as evidence). |
| `converter/dateparse.py` | The date/time parser: every common spelling, Excel serials, Unix timestamps, time zones, two-digit years, no-year dates (`NO_YEAR`). |
| `converter/engine.py` | `convert()`: applies the mapping to every row, expands one row per phone number, normalizes, rejects bad rows, removes exact duplicates, builds output + rejects + summary + per-cell audit. Also `preview_values` and `source_rows`. |
| `converter/output.py` | Writes the import file (CSV/XLSX per `OutputSpec`), the rejected-rows CSV and the text summary; builds download file names. |
| `converter/checks.py` | Rule-based mistake checks (wrong column, future dates, birthdays that look like sign-up dates, mixed day/month order, 7-digit phones…), `column_warnings`, and the sample payload for Claude's review. |
| `converter/ai.py` | `ClaudeAssistant`: `propose_mapping`, `apply_notes`, `review`, `repair_values`. Structured outputs, prompts built from the schema, strict validation of every answer (`parse_mapping_entries`, `accept_repair`). |
| `converter/services.py` | Glue used by the UI and CLI: `make_assistant`, `initial_mapping`, `merge`, `check_for_mistakes`, `validate_repairs`. |
| `converter/editor.py` | Mapping editing for the two grids: field-side (`apply_row_edit`) and column-side (`apply_column_edit`: Goes to / Skip), plus the "Option" cell text. |
| `converter/compare.py` | Side-by-side HTML table, filters and counts, the "What changed" per-field summary, and the colour-coded comparison workbook. |
| `converter/storage.py` | SQLite store (users, templates, imports, user preferences) and the per-import file folders; retention purge. |
| `converter/jobs.py` | `run_import` (one import, start to finish, with progress and cancel) and `ImportRunner` (thread pool, purge loop). |
| `converter/auth.py` | Login gate (password mode with bcrypt accounts, or `AUTH_MODE=none` for one person on their own computer). |
| `converter/config.py` | `Settings` from environment variables / `.env`. |
| `converter/cli.py` | Command line: `inspect` (show detected columns and mapping) and `convert` (run a conversion, exit code 2 when there were bad rows). |
| `converter/ui_format.py` | Small display helpers (sizes, durations, time left, status colours). |
| `scripts/make_sample_files.py` | Generates the messy sample files used by the tests and for trying the app. |
| `scripts/manage_users.py` | Add users, reset passwords, delete and list users from the command line. |

## Main data structures

### Target schema (`schema.py`)

`TargetSchema` = `name`, `version`, `description`, `output` (`OutputSpec`: csv/xlsx, delimiter, encoding,
header, quoting), `dedupe` (`DedupeSpec`: `keys`, `keep`; `["*"]` = every field) and `fields` (list of
`FieldSpec`). A `FieldSpec` carries:
- `name`, `header`, `type`, `required` and `format`;
- the rules: `on_invalid`, `countries` (phones), `no_year_value` (dates) and `fixed_blank`;
- string options: `case`, `max_length` and `truncate`;
- `allowed_values`, `default` and `synonyms`.

### Mapping (`mapping.py`)

```
Mapping
  fields:            {field name → FieldMapping(sources=[columns], transform, params, origin, confidence, reason)}
  column_notes:      {source column → reviewer note}        (applied by Claude on request; saved with templates)
  general_note:      free text about the whole file
  excluded_columns:  source columns marked "Skip"            (never mapped by rules, templates or Claude)
```

`Mapping.normalized(schema, columns)` is called before every use. It makes a mapping fit a file:
- it keeps every schema field and drops unknown sources and skipped columns;
- it enforces the phone rule (several phone columns → `each_row`);
- it locks fixed-blank fields;
- it resets invalid transform/param combinations.

### Conversion result (`engine.py`)

`ConversionResult` holds:
- `output`: the import rows;
- `rejects`: bad and duplicate rows, with reasons and original values;
- counts: total, imported, rejected, duplicates, blank rows, blanked, extra rows, skipped and repaired values,
  each with per-reason counters;
- `fill_rates` and `failed_values`;
- `audit`: one row per converted row with `_row`, `_src`, `_extra`, `_status`, `_reason`, `_issues`, plus
  `out.<field>`, `raw.<field>` and `st.<field>` for every field.

The cell status codes (`CELL_*`) drive the colours and filters of the side-by-side view. The full audit layout is
documented at the top of `engine.py`.

### Database (`storage.py`, `data/converter.db`)

| Table | Holds |
|---|---|
| `users` | username, bcrypt hash, admin flag, created / last login |
| `user_prefs` | per-user display preferences, e.g. `theme` = `light` / `dark` (one row per user and key) |
| `templates` | name, header fingerprint and headers, mapping JSON, read options (sheet, header row, time zone), creator/updater, use count |
| `imports` | who, file name and size, status (queued / running / completed / failed / cancelled / interrupted), progress and stage, timestamps, cancel flag, **snapshots** of the schema, mapping, read options and approved fixes, counts and summary JSON, file names, parent import |

Per-import files live in `data/imports/<id>/`:
- `source.<ext>`: the uploaded original;
- the converted file;
- `rejected.csv`;
- `summary.txt`;
- `source.parquet` and `audit.parquet`.

The files are deleted after `FILE_RETENTION_DAYS`. The database record and its counts are kept.

## Key behaviours and where they live

| Behaviour | Where |
|---|---|
| US numbers only, `+1` assumed, 10-digit output | `normalizers._parse_phone`, schema `countries`, `format: digits10` |
| Phone typo repair (extra digit, foreign country code; never area codes), each repair marked | `normalizers.prepare_phone`, `_repair_phone`; engine `CELL_REPAIRED` |
| One row per different phone number | `transforms` (`each_row`), `mapping.phone_rule`, `engine._expand_rows` |
| Columns *about* a phone (marketing preference, type, opt-in) never used as numbers | `heuristics.is_phone_header`, `is_consent_column` |
| Birthday/anniversary without a real year → year 1600 | `dateparse` (`NO_YEAR`), `normalizers.normalize_date`, `_placeholder_year`; schema `no_year_value` |
| Times with a zone converted to the client's time zone | `dateparse._to_zone`; zone chosen per import on the New import page |
| Consent always blank, can't be mapped | schema `fixed_blank`; enforced in `mapping.normalized`, `engine.convert`, `editor`, `ai` |
| Duplicates only when every column matches | schema `dedupe: keys: ["*"]`; `engine.convert` |
| Bad rows not imported, counted with reasons | `engine.convert` (`RowIssue.rejected`), `output.write_rejects`, summary |
| Skipped columns stay out everywhere | `Mapping.excluded_columns`; `normalized`, `services.merge`, `ai._without_skipped` |

## Why it is built this way

- **Column-at-a-time normalization.** Some decisions need the whole column: whether `03/04/2026` is day-first,
  which numbers and area codes the file uses (evidence for the allowed phone repairs), whether one year stands for
  "unknown". `normalizers.prepare()` looks at the
  column first; `normalize()` then handles each value with that context.
- **Snapshots on every import.** The import row stores the schema, mapping and options used. Later edits to the
  schema file or templates never change what an old import means, and a re-run is reproducible.
- **Audit table instead of re-converting for the comparison.** The side-by-side view, the "What changed" summary,
  the rule checks and Claude's sample all read the audit written once by the engine. They always agree with the
  file that was actually produced.
- **Threads, not a job queue.** Imports are CPU-light and minutes long at most. A thread pool inside the web
  process keeps deployment to one container. The trade-off (an import running during a restart is marked
  *Interrupted*) is acceptable for an internal tool.
- **Claude behind buttons only.** The tool must be usable, predictable and free of per-import AI cost. Claude's
  value is in judgement (what a column means, whether a result looks wrong), so it is offered there and nowhere
  else.
