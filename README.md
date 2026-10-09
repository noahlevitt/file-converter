# Import File Converter

Internal web app that converts any customer/contact file (CSV, TSV, TXT, XLSX, XLS, ODS) into the exact
import format of the target system. Built for 5–20 internal users; each upload is one file.

```
New import:  upload → detect header/encoding/delimiter → auto-map columns → review/edit (or instruct Claude)
             → Start import (runs in the background; mapping saved as a template for next time)
Imports:     every import with live progress, elapsed time and time left → open one → results, downloads,
             side-by-side original vs converted, what changed per field, re-run, Claude value fixes
```

## Pages

| Page | What it's for |
|---|---|
| **New import** | Upload a file, check the detected header/sheet and client time zone, review the column mapping (edit the field grid, or under *Columns in your file* pick where each column goes, skip a column, or write notes and apply them with Claude), optionally **Check for mistakes**, then **Start import**. |
| **Imports** | Every import by everyone (filter: mine, status, search). Running imports show a progress bar, the current step ("Converting Phone — row 41,000 of 150,000"), elapsed time and estimated time left, with Cancel. Select any import to open it. |
| **Import detail** | Status and timing, the bad-row flag and counts, downloads (converted file, rejected rows, summary, original file, side-by-side Excel), and tabs: **Side by side**, **What changed**, **Converted file**, **Bad & duplicate rows**, **Mapping used** (incl. notes). **Check this import for mistakes**; Re-run and Claude fixes start a new linked import. |
| **Templates** | Saved column mappings per file layout; rename, delete, export. |
| **Target format** | The current target schema, output file settings, dedupe rule and file retention. |
| **Users** | (admins) add users, reset passwords, grant admin. |

**Side by side** shows the original file (left, exactly as uploaded) and the converted output (right) row by
row. Column headers show where each original column went and where each output field came from. Cells are
coloured by what happened: reformatted, built from part of / several columns, default value, Claude fix,
invalid → left blank, invalid/missing → row rejected; the original value is shown under any changed value and
on hover. Filter to rows with changes, problems, bad rows, duplicates, imported or empty rows, search for any
value or row number, and page through large files. The same view downloads as a colour-coded Excel workbook.

## How it works

| Step | What happens |
|---|---|
| **Read** | Every cell is read as text (no lost ZIP zeros, no phones turned into `5.16E+09`). Header row, encoding (UTF-8 / Windows-1252 / etc.), delimiter and Excel sheets are detected; the user can override. |
| **Map** | 1) A saved **template** with the same column headers is applied instantly, including its notes and time zone. 2) Otherwise **rules** match headers, synonyms and value types (phones, dates…), and find every phone column (Phone, Phone 2…, Mobile, Home) — but not columns *about* a phone such as `phone4MarketingPreference`, Phone Type or SMS Opt In. A column only counts as a phone column by its values alone when nearly all of its values are phone numbers, so a stray number typed into e.g. *Occupation* doesn't pull that column into the phone field. 3) Optionally, your **notes on columns** are applied by Claude. |
| **Review** | A grid shows each target field, its source column(s), how it's built, a sample of converted output, and any problem. Edit it directly, add notes per column, and run **Check for mistakes** before importing. |
| **Convert** | Runs in a background worker pool (default 2 at a time; more wait in a queue) so users can keep working. Deterministic Python converts every row — Claude never touches the full file. Phones are validated with Google's libphonenumber, dates are interpreted per column (month-first vs day-first decided once for the whole column, Excel serial dates handled), ZIP/state/email normalized. |
| **Report** | **Bad rows are not imported** and the result is flagged: *"37 bad rows were NOT imported"* with a reason per row in the rejected-rows file. Optional fields with invalid values are left blank (configurable per field). |
| **Fix (optional)** | Claude can suggest reformatting for rejected values (e.g. `516 555-O1OO`, `Sept 3rd twenty26`). Each suggestion is re-validated, and any suggestion that adds or changes digits/data is refused. Users approve before re-converting. |

## Target format (generic import format)

The output follows `generic-import-format.md`, encoded in [`schemas/target_schema.yaml`](schemas/target_schema.yaml):

| # | Header | Required | Output | How the tool handles it |
|---|---|---|---|---|
| 1 | `phone_number` | Yes | `2125551234` | US numbers only. No country code → +1 assumed. See *Phone numbers* below. Missing or invalid → row rejected as a bad row. |
| 2 | `consent` | No | always blank | **Locked.** Blank on every row. It can't be mapped by the rules, set by Claude, given a default, or edited in the mapping grid, and columns about opt-in / consent / subscription are never auto-mapped to anything. |
| 3–8 | `calls_last`, `transactions_last`, `last_contacted`, `in_sms_last`, `redemptions_last`, `spin_last` | No | `03/07/2026 09:05 AM` | Any date/time format in (see *Dates and times*) → `MM/DD/YYYY HH:MM AM/PM`. Seconds dropped; a date with no time gets `12:00 AM`. Placeholders like `0000-00-00`, `Never`, `N/A` → blank. |
| 9–10 | `birthday`, `anniversary` | No | `09/12/1985` | Any date format in → `MM/DD/YYYY`. Month and day with no year (`09/12`, `Sep 12`, `12-Sep`, `September 12th`, `0912`) and placeholder years (`1900`, `0000`) → year `1600`. |

- Headers are written exactly as above: lowercase, in order, no extra columns, all kept even when empty.
  Source columns with no matching field (names, emails, totals…) are left out and shown as "not imported".
- An invalid optional value (e.g. `02/30` as a birthday) is left blank and the row still imports; it is
  listed under *What changed*. Change a field to `on_invalid: reject` to reject those rows instead.
- **Duplicates:** a row is removed only when **every** column matches an earlier row. The same phone with any
  different date imports as its own row.

### Phone numbers

Every value is reduced to the 10-digit US number:

- Any punctuation or spacing: `(516) 555-0100`, `516.555.0100`, `516/555/0100`, `516 555 01 00`, unicode dashes,
  full-width digits, non-breaking spaces.
- Country code present or not: `+1…`, `1-…`, `(+1)…`, `15165550100`, `+5165550100` (the 1 left out),
  international dialling `001…` / `011 1…`.
- Extensions removed: `x12`, `ext. 12`, `extension 9`, `#12`, `;ext=12`.
- Labels and extra text: `Cell: …`, `tel:+1…`, `… (mobile)`, `Maria - …`.
- Several numbers in one cell: a number labelled mobile/cell wins, then an unlabelled one, then home/work, and fax last.
- Spreadsheet artifacts: `5165550100.0`, `5.165550100E+09`, `="5165550100"`, a leading apostrophe.
- Letters typed for digits (`516-555-O1OO`, `5l6…`) and vanity numbers (`1-800-FLOWERS`).
- Rejected, with the reason shown: missing area code (7 digits), too few or too many digits, numbers that can't
  exist (`555-555-5555`, `123-456-7890`), non-US numbers (Canada, UK, Mexico…), and Excel scientific notation
  that already lost digits (`5.163E+09`).
- US territories (Puerto Rico, USVI, Guam, American Samoa, Northern Mariana Islands) count as US. To accept the
  50 states only, set `countries: [US]` in the schema.

#### Several phone columns (e.g. Toast: phone, phone2, phone3, phone4)

Every phone column feeds the phone field, and **each different number always gets its own row**, with all the
other columns (visit dates, birthday…) copied from the original row it came from. There is no setting to turn this
off; older templates that used *First non-empty* for the phone field are upgraded automatically.

- The same number in two columns (`201-555-0111` and `(201) 555-0111`) counts once.
- An invalid extra number (e.g. `123` in phone3) is skipped; the row still imports with the customer's other
  number(s), and the summary lists what was skipped.
- A customer with no valid number at all is one bad row, as before.
- The summary says how many extra rows were made ("5 rows ready to import from 4 file rows (2 extra rows…)"),
  and the side-by-side view marks extra rows `N +` and underlines the number each row was made from.
- Columns that describe a phone (`phone4MarketingPreference`, `Phone Type`, `SMS Opt In`, `Phone Verified`) are
  never used as phone numbers. Marketing-preference columns are treated like consent columns: not imported.

### Dates and times

Read in any mix within one column:

- **Numeric:** `10/9/2026`, `10-09-26`, `10.09.2026`, `10 09 2026`, `2026-10-09`, `2026/10/9`, and day-first
  `9/10/2026` when the column clearly is day-first.
- **Compact:** `20261009`, `10092026`, `100926`, `202610091545`, `20261009T154500Z`.
- **Month names:** `October 9, 2026`, `October 9th 2026`, `Oct. 9 '26`, `9 October 2026`, `9, October, 2026`,
  `9th of October 2026`, `09-Oct-26`, `Oct-09-2026`, `2026 Oct 9`, `OCTOBER 9`, `Sept 9`, and Spanish
  (`9 de octubre de 2026`).
- **Weekdays:** `Friday, October 9, 2026`, `Fri Oct 9 2026` (the weekday is ignored).
- **Times:** `3:45 PM`, `3:45pm`, `3:45 p.m.`, `3pm`, `15:45`, `15:45:12.123`, `15h45`, `noon`, `midnight`,
  `1545` after a date, the time before or after the date, `at` / `@`.
- **Machine formats:** ISO 8601 (`2026-10-09T15:45:00Z`, `…+05:30`), RFC 2822 email dates, JavaScript date
  strings, Unix `date` output, SQL Server `Oct  9 2026  3:45PM`.
- **Spreadsheet values:** Excel serial numbers (`46304`, `46304.65625`, `46304,65625`), `="10/09/2026"`, a
  leading apostrophe, Unix timestamps in seconds or milliseconds.
- **Ambiguous day/month:** month-first (US) by default. A column switches to day-first only when at least two
  values prove it (e.g. `15/03/2026`) and they outnumber month-first evidence 3 to 1. A single value that fits
  only one way (`13/10/2026`) is always read that way. You can force the order per field with the *Option*
  cell (`MDY` / `DMY` / `YMD`) or by telling Claude.
- **Two-digit years:** 20yy unless that lands more than a year in the future, then 19yy (`08/09/26` → 2026,
  `08/09/85` → 1985).
- **Time zones:** choose the client's time zone on *New import*; templates remember it. Times that include a zone
  are converted to it: `Z`, `UTC`, `+00:00`, `GMT-4`, `EST`, `PDT`, `(MDT)`, `Eastern`, `Pacific Time`. Unix
  timestamps are converted too. US abbreviations are read as that region's local time, so `EST` in July means
  Eastern clock time. Times with no zone are kept exactly as written.
- **Incomplete or unreadable values are never guessed:** `October 2026` (no day), `2026`, `today`, `Q4 2026`,
  and `02/30/2026` are left blank. Each one is listed under *What changed* and in the summary.

### Changing the target format

Everything — auto-mapping, Claude's prompt, validation, output layout — is driven by the YAML file; no code
changes are needed and the running app picks up edits automatically. Field options:

```yaml
fields:
  - name: phone_number     # internal id
    header: phone_number   # exact column header in the output file
    type: phone            # string|phone|date|datetime|email|zip|state|integer|decimal|boolean|enum
    required: true         # missing/invalid → row rejected (bad row)
    format: digits10       # phone: E164|digits10|digits11|national|dashed   date/datetime: strftime pattern
    synonyms: [mobile, cell, phone number]   # other names seen in source files (improves auto-mapping)
    # also: on_invalid (reject|blank), fixed_blank (always empty, locked), no_year_value (dates, e.g. 1600),
    #       countries (phone), max_length, truncate, case, allowed_values (enum), default, description
dedupe:
  keys: ["*"]              # "*" = every field; or list specific fields
```

Add source-file header names you see often to `synonyms` so they map automatically.

## Fixing where a column goes

Under **New import → Columns in your file** every column of the uploaded file has:

- **Goes to** — the field(s) it feeds. Pick a different field to move it; picking a field makes this column that
  field's only source (whatever fed it before is dropped from it). Clear it to leave the column out.
- **Skip** — leave the column out and keep it out: rules, saved templates and Claude won't map it again (Claude
  isn't even shown it). Saved with the template, so the next file with the same layout skips it too.
- **Check** — a warning when the column's name and values don't fit the field it goes to.
- **Your note** — free-text notes, applied by Claude with *Apply notes*.

The field grid above it still works the other way round (pick the source column(s) for each field).

## Running it

### On a server (recommended for the team)

Needs Docker. On a small Linux VM (1 vCPU / 2 GB RAM is enough):

```bash
cp .env.example .env            # set ANTHROPIC_API_KEY, ADMIN_USERNAME, ADMIN_PASSWORD
mkdir -p data && sudo chown 1000 data
docker compose up -d --build
```

Open `http://SERVER:8501` and sign in with the admin account from `.env` (then remove `ADMIN_PASSWORD`
from `.env`). Add teammates under **Users**.

**HTTPS / access:** don't expose port 8501 directly to the internet. Either keep it on the office network /
VPN (e.g. Tailscale), or put [Caddy](https://caddyserver.com) in front for automatic HTTPS:

```
# /etc/caddy/Caddyfile
convert.yourcompany.com {
    reverse_proxy localhost:8501
}
```

### On one computer (no Docker)

Install Python 3.11+, then double-click `run_local.bat` (Windows) or run `./run_local.sh` (Mac/Linux).
It opens at `http://localhost:8501`. Set `AUTH_MODE=none` in `.env` for single-person use.

### Users from the command line

```bash
docker compose exec app python scripts/manage_users.py add jane            # --admin for an admin
docker compose exec app python scripts/manage_users.py password jane
docker compose exec app python scripts/manage_users.py list
```

## Claude (optional)

**No API key is needed.** Reading files, matching columns, converting, validating, flagging bad rows, the imports
tracker, the side-by-side view and the rule-based mistake check all work without Claude. Claude is called **only
when someone clicks** one of these buttons:

| Button | Where | What Claude does |
|---|---|---|
| **Apply notes with Claude** | New import → *Columns in your file* | Reads your notes ("this is the last visit date", "actually goes to anniversary", "dates are day-first", "don't import") and proposes mapping changes, with a result for every note (applied / already correct / not applied and why). You accept or discard. |
| **Check for mistakes** | New import (first 1,000 rows) and on every finished import | Rule checks always run. With a key, Claude also reviews a sample of original vs converted rows and per-field stats, then reports likely mistakes (wrong or missed columns, swapped fields, day/month or century problems) with suggestions. It can propose mapping changes: accept them before importing, or *Re-run the import with these changes* afterwards. |
| **Suggest fixes** | Finished import → *Try to fix … unconvertible values* | Suggests reformatting for values that failed (e.g. `Sept 3rd twenty26`). Each suggestion is re-validated, and anything that adds or changes digits is refused. |

Notes are useful without Claude too: they're saved with the template and the import as documentation for the team.

- **Key:** create one at platform.claude.com and set `ANTHROPIC_API_KEY`. Without it, the Claude buttons are disabled.
- **What's sent:**
  - Notes: column names, up to 15 sample values per column, and your notes.
  - Mistake check: additionally up to 30 sample rows (original and converted) and summary counts.
  - Fixes: only the failed values.
  - Phone digits in check samples are partly masked (`516#####00`; turn off with `AI_MASK_PHONES=false`).
  - The full file is never sent.
- **Safety:** structured outputs restrict Claude to real field, transform and column names. `consent` can't be
  touched. Every suggestion is shown for approval before anything changes.
- **Models:** `claude-sonnet-5-5` for notes and checks, `claude-haiku-5-5` for fixes (`MAPPING_MODEL` /
  `REPAIR_MODEL`).
- **Cost (estimate):** a notes or check call is a few thousand tokens — roughly 2–5 cents at Sonnet 5.5 list
  prices ($2 / $10 per million input / output tokens).
- **Automatic mapping:** to also have Claude refine the column matching on every upload, set
  `AI_AUTO_MAP=true` (off by default).

### What the rule check looks for (no Claude needed)

- A field fed by a column whose values mostly can't be converted (likely the wrong column), or a mapped field
  where nothing came through.
- A column whose name and values don't fit the field it feeds — e.g. *Occupation* going to the phone field because
  someone typed a phone number into it. This one also shows straight away in the mapping grids, without clicking
  *Check for mistakes*.
- Activity times in the future, or mostly before 2000.
- Birthdays/anniversaries in the future, or mostly within the last two years (looks like sign-up dates).
- Every row having the same date (an export default).
- Columns mixing month-first and day-first dates.
- Times that carried a zone and were converted.
- One column used for several fields.
- A high bad-row rate and its top reason, including 7-digit phones (area code in another column?).
- Many exact duplicates.
- Unused columns that contain dates or phone numbers.
- Consent-type columns, noted as intentionally not imported.

## Data stored

- `data/converter.db` (SQLite): user accounts (bcrypt-hashed passwords), templates (column names + mapping
  rules), and one record per import (who, when, status, timings, counts, mapping used).
- `data/imports/<id>/`: the original upload, the converted file, rejected rows, summary, and the comparison
  data behind the side-by-side view. These contain customer data and are **deleted automatically after
  `FILE_RETENTION_DAYS` (default 30)**; the import record and counts stay. Set `0` to keep them forever.
- Back up `data/` to keep templates and history.

## Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude API key; leave empty to run without AI |
| `MAPPING_MODEL` / `REPAIR_MODEL` | `claude-sonnet-5-5` / `claude-haiku-5-5` | Claude models |
| `AI_SAMPLE_VALUES` | 15 | sample values per column sent to Claude |
| `AI_AUTO_MAP` | false | also let Claude refine column matching automatically on upload |
| `AI_MASK_PHONES` | true | partly mask phone digits in rows sent for Claude's mistake check |
| `IMPORT_WORKERS` | 2 | imports that run at the same time |
| `FILE_RETENTION_DAYS` | 30 | days to keep each import's files (0 = forever) |
| `APP_TIMEZONE` | America/New_York | time zone for displayed times |
| `DEFAULT_PAGE` | new | first page: `new` or `imports` |
| `AUTH_MODE` | password | `password` or `none` (single user on own computer) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | — | first admin, created on first start |
| `SCHEMA_PATH`, `DATA_DIR`, `APP_TITLE` | | paths and title |

## Command line

```bash
python -m converter.cli inspect file.xlsx                 # detected columns + proposed mapping
python -m converter.cli convert file.csv --out out/       # exit code 2 if there were bad rows; also listed under Imports
python -m converter.cli convert file.csv --template "POS guest export"
python -m converter.cli convert file.csv --ai --save-template "New vendor"
```

## Development

```bash
pip install -r requirements.txt
python scripts/make_sample_files.py   # messy sample files in sample_data/ (incl. restaurant_crm_export.csv)
python -m pytest -q
streamlit run app.py
```

```
app.py                  Streamlit web UI (new import, imports tracker + detail, templates, target format, users)
converter/
  schema.py             target schema model + YAML loader
  reader.py             file ingest, encoding/delimiter/header detection
  normalizers.py        phone/date/email/zip/state/number/boolean/enum/string normalization
  dateparse.py          date/time reader (every numeric, written, machine and spreadsheet format)
  checks.py             rule-based mistake checks + sample prepared for Claude's review
  transforms.py         how a field's value is built: copy, first-non-empty, combine, name part, address part, split, translate, fixed
  mapping.py            mapping spec, column profiling, header fingerprints
  heuristics.py         rule-based auto-mapping
  ai.py                 Claude (optional): apply notes, mistake review, value fixes (+ guards)
  engine.py             conversion, validation, bad rows, dedupe, progress, per-cell audit trail
  jobs.py               background import runner (queue, progress, cancel, retention)
  compare.py            side-by-side view, what-changed summary, comparison Excel
  output.py             converted file, rejects file, summary
  storage.py            SQLite users/templates/imports + per-import file folders
  ui_format.py          time/duration/status display helpers
  services.py           orchestration shared by UI and CLI
  editor.py             mapping-grid helpers
  cli.py                command line
schemas/target_schema.yaml   the target import format (generic import format)
tests/fixtures/              example contact schema used by the generic tests
```

## Known limits

- Phone validation confirms a number is a real, dialable US number; it cannot tell mobile from landline.
  That needs a carrier lookup (e.g. Twilio Lookup, paid per number) and can be added as an optional step.
- Refreshing the browser tab signs the user out and clears a mapping in progress on *New import*. Imports
  already started keep running and stay visible under *Imports*.
- Imports run inside the web server process. If the server restarts mid-import, that import is marked
  *Interrupted* and can be re-run with one click.
- Throughput measured in testing: about 150,000 rows in 25 seconds for a 6-column file on one worker.
