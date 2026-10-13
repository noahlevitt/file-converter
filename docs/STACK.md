# Technology stack

Every library and tool the converter uses, what it does here, and why it was chosen over the alternatives.

**Summary:**
- **App:** a Python web app with a Streamlit interface.
- **Conversion:** a deterministic engine of our own (same file + same mapping = same output), built on specialist
  libraries for phone numbers, dates and file formats.
- **Claude:** optional, called only when a person clicks a button, and every change it proposes needs approval.
- **Storage and packaging:** SQLite for users, templates and import history; Docker for packaging; one machine runs
  everything.

## How a file flows through the stack

```
upload ─► reader.py (python-calamine, charset-normalizer, pandas)
       ─► mapping.profile_columns + heuristics.auto_map (rapidfuzz, phonenumbers, custom date parser)
       ─► review in the browser (Streamlit); optional notes/checks with Claude (anthropic SDK, Pydantic)
       ─► jobs.ImportRunner thread ─► engine.convert (normalizers: phonenumbers, dateparse, zoneinfo …)
       ─► output files (csv / xlsxwriter), audit + source tables (pyarrow Parquet), import record (SQLite)
       ─► Imports page: progress, side-by-side comparison, downloads
```

## Components

| Piece | What it does here | Why it was chosen |
|---|---|---|
| **Python 3.12** | Everything: UI, engine, storage, tests | The strongest data-cleaning libraries (phones, dates, Excel) are Python. One language for the whole app means one person can maintain all of it. |
| **Streamlit** (≥ 1.65, multipage `st.navigation`) | The web front end: upload, mapping grids, imports tracker with live progress, side-by-side view, templates, users | Builds an internal tool's UI in Python with no separate frontend project. Suits 5–20 internal users. **Trade-off:** less control over look and behaviour than a custom React app. Each browser session re-runs the page script, so it isn't built for hundreds of simultaneous users or public use. |
| **pandas** | Holds the uploaded file as a table of text while it's processed | The standard for tabular data in Python. Every cell is kept as text so nothing is silently converted (leading zeros, phone numbers, dates). |
| **pyarrow** (Parquet) | Stores each import's original table (`source.parquet`) and per-cell audit (`audit.parquet`) | Compact, typed and fast to reload, which keeps the side-by-side view quick even on large imports. |
| **python-calamine** | Reads `.xlsx`, `.xlsm`, `.xlsb`, `.xls` and `.ods` | One fast (Rust-based) library covers every spreadsheet format, including old binary `.xls`. |
| **charset-normalizer** | Last-resort guess of a CSV's text encoding | The reader first checks for a UTF-8/UTF-16 BOM, then strict UTF-8, then Windows-1252. Charset-normalizer handles anything else, so names with accents don't turn into garbage. |
| **xlsxwriter** | Writes Excel output (when the schema asks for xlsx) and the colour-coded side-by-side comparison workbook | Full control over cell formats: values are written as text so Excel doesn't turn phones into numbers or scientific notation. |
| **openpyxl** | Only used by `scripts/make_sample_files.py` to build the sample Excel files the tests read | Calamine can only read; the tests need real Excel files with Excel quirks. |
| **phonenumbers** (Python port of Google's libphonenumber) | Phone validation: is the area code and exchange real, is it US, which country/place it belongs to, formatting | The same number data Android uses. Writing our own would mean keeping North American area-code data current by hand. |
| **Custom date parser** (`converter/dateparse.py`) | Reads every date/time spelling: ISO, US, day-first, month names, Excel serial numbers, Unix timestamps, time zones, no-year birthdays (→ 1600) | Generic parsers guess silently: `03/04` becomes March 4 with no warning, and a birthday with no year gets the current year. Ours decides day/month order per column and returns a reason for anything it can't read. |
| **python-dateutil** | Fallback for unusual date text the custom parser doesn't recognise | A safety net, not the main parser. |
| **zoneinfo + tzdata** | Converts times that carry a zone (`Z`, `+00:00`, `EST`, Unix timestamps) to the import's client time zone | `zoneinfo` is built into Python. `tzdata` supplies the time-zone database where the operating system doesn't (Windows, slim containers). |
| **rapidfuzz** | Fuzzy-matches column headers to target fields (`Mobile #` → phone_number, `Last Visit` → transactions_last) | Fast, and tolerant of spelling and wording differences. |
| **email-validator, nameparser, usaddress** | Email syntax; splitting a full name into first/last; parsing a one-line US address into street/unit/city/state/ZIP | The current import format doesn't use them. They stay because the engine is schema-driven: a future format with emails, names or addresses works without new code. The generic tests use them through `tests/fixtures/example_contact_schema.yaml`. |
| **Pydantic v2** | Typed, validated models for the target schema, mappings, templates and Claude's answers | A broken schema file fails loudly at start-up, and a malformed Claude reply is refused before it touches a mapping. Also gives clean JSON for storing templates and import records. |
| **PyYAML** + `schemas/target_schema.yaml` | The output format: columns, order, formats, the 1600 rule, US-only phones, consent mapped by hand only, dedupe rule | If the import format changes, you edit a text file, not code. |
| **Anthropic SDK** (Claude) | Optional. Runs only when a person clicks **Apply notes with Claude**, **Check for mistakes** or **Suggest fixes**. Models: `claude-sonnet-5-5` for mapping, notes and reviews, `claude-haiku-5-5` for value fixes (`MAPPING_MODEL` / `REPAIR_MODEL`). | You wanted Claude for judgement calls, not for transforming data. Answers come back as structured JSON, are re-validated by our code, and need approval before anything changes. Only headers, a few sample values per column and (for checks) a sample of rows are sent, never the whole file. The app works fully without an API key. |
| **SQLite** (WAL mode) | Users, templates, import history and live progress (`data/converter.db`) | Zero setup: one file, easy to back up. WAL mode lets pages read while an import is writing. Plenty for 5–20 users. Postgres would only be worth it for several app servers or much heavier use. |
| **ThreadPoolExecutor** (Python standard library) | Runs imports in the background with progress, cancel and time-left estimates (`IMPORT_WORKERS`, default 2) | Background work without adding Redis, Celery or a separate worker service. **Trade-off:** if the app restarts mid-import, that import is marked *Interrupted* and you re-run it with one click. |
| **bcrypt** | Hashes passwords for the per-user accounts | The standard for password storage; plain-text passwords never reach the database. |
| **Docker + docker-compose** | Packages the app (Python, libraries, app code) for a server or a single computer | Same environment everywhere, one command to start or update. On a server, Caddy goes in front to provide HTTPS (see [OPERATIONS.md](OPERATIONS.md)). |
| **pytest** | About 660 automated tests, including hundreds of phone and date spellings and every business rule | Each fix (phone typos, 1600 birthdays, one row per phone number…) is locked in so later changes can't quietly break it. |
| **Git + GitHub** | History (one commit per file change), version tags (`v0.1.0` … current) | You can see what changed and when, and roll back. |

## What we deliberately didn't use

- **A paid conversion product:** you wanted to own and control the conversion logic.
- **A separate React frontend plus an API backend:** double the code to build and maintain for an internal tool.
- **Claude in the conversion path:** results would vary from run to run, every import would cost money, and every
  customer row would leave the server. Conversion is rule-based and repeatable; Claude only advises.
- **Postgres, Redis or a job queue:** extra services to run, back up and monitor, with no benefit at 5–20 users.
- **A generic "smart" date library as the main parser:** silent guesses are exactly the failure this tool exists to
  prevent.

## When to revisit

- **Many more users, or access from outside the company:** run it on a hosted server behind HTTPS. Consider
  Postgres if more than one app server is needed.
- **Very large files (millions of rows):** the engine holds a whole file in memory. Process it in chunks, or raise
  the server's memory.
- **A polished, customer-facing UI:** replace Streamlit with a dedicated frontend. Apart from the login gate
  (`converter/auth.py`), the `converter/` package has no Streamlit dependency and can be reused as-is.
