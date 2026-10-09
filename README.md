# Import File Converter

Internal web app that converts any customer/contact file (CSV, TSV, TXT, XLSX, XLS, ODS) into the exact
import format of the target system. Built for one file at a time and 5–20 internal users.

```
Upload → detect header/encoding/delimiter → auto-map columns → review/edit (or instruct Claude) → convert
       → converted file + rejected-rows file + summary   (mapping saved as a template for next time)
```

## How it works

| Step | What happens |
|---|---|
| **Read** | Every cell is read as text (no lost ZIP zeros, no phones turned into `5.16E+09`). Header row, encoding (UTF-8 / Windows-1252 / etc.), delimiter and Excel sheets are detected; the user can override. |
| **Map** | 1) A saved **template** with the same column headers is applied instantly (no AI call). 2) **Rules** match headers, synonyms and value types (phones, emails, dates, ZIPs, states), split full names and full addresses, and combine Mobile/Home phone columns. 3) **Claude** reviews and fills gaps, using only the headers + up to 15 sample values per column. |
| **Review** | A grid shows each target field, its source column(s), how it's built, a sample of converted output, and any problem. Users edit it directly or type an instruction ("Join Date is day-first", "put VIP Level into Notes") and accept/discard Claude's proposed changes. |
| **Convert** | Deterministic Python converts every row — Claude never touches the full file. Phones are validated with Google's libphonenumber, dates are interpreted per column (month-first vs day-first decided once for the whole column, Excel serial dates handled), ZIP/state/email normalized. |
| **Report** | **Bad rows are not imported** and the result is flagged: *"37 bad rows were NOT imported"* with a reason per row in the rejected-rows file. Optional fields with invalid values are left blank (configurable per field). |
| **Fix (optional)** | Claude can suggest reformatting for rejected values (e.g. `516 555-O1OO`, `Sept 3rd twenty26`). Each suggestion is re-validated, and any suggestion that adds or changes digits/data is refused. Users approve before re-converting. |

**Duplicates:** a row is removed only when *all* dedupe keys match an earlier row. With the default keys
`[phone, signup_date]`, the same phone with a different date is **kept** (both rows import).

## Setting the real target schema

Everything — auto-mapping, Claude's prompt, validation, output file layout — is driven by
[`schemas/target_schema.yaml`](schemas/target_schema.yaml). The current file is a **placeholder**.
Replace its `fields` with the real import format; no code changes are needed and the running app picks up
the change automatically.

```yaml
fields:
  - name: phone            # internal id
    header: Phone          # exact column header in the output file
    type: phone            # string|phone|date|datetime|email|zip|state|integer|decimal|boolean|enum
    required: true         # missing/invalid → row rejected (bad row)
    format: E164           # phone: E164|digits10|digits11|national|dashed  date: strftime e.g. "%m/%d/%Y"
    synonyms: [mobile, cell, phone number]     # other names seen in source files
    on_invalid: reject     # reject | blank   (default: reject if required, else blank)
    # also: max_length, truncate, case (upper|lower|title|name), allowed_values (enum), default,
    #       countries (phone, default [US]), true_value / false_value (boolean), description
output:
  format: csv              # csv | xlsx
  delimiter: ","
  encoding: utf-8
dedupe:
  keys: [phone, signup_date]
```

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

## Claude API

- Key: create one at platform.claude.com and set `ANTHROPIC_API_KEY`. Without a key the tool still works
  (rules + manual mapping); the Claude buttons are disabled.
- Models: `claude-sonnet-5-5` for mapping and instructions, `claude-haiku-5-5` for value repair
  (override with `MAPPING_MODEL` / `REPAIR_MODEL`).
- Uses JSON structured outputs, so Claude can only return target fields, transforms and column names that
  exist; anything invalid is discarded. The schema prompt is cached between calls.
- Data sent: column headers + up to `AI_SAMPLE_VALUES` sample values per column (mapping), or the specific
  values that failed (repair, max 200). Never the full file.
- Cost (estimate): a mapping call is a few thousand tokens — roughly 2–5 cents at Sonnet 5.5 list prices ($2 / $10 per million input / output tokens).
  Files whose layout matches a saved template make no Claude call at all.

## Data stored

`data/converter.db` (SQLite): user accounts (bcrypt-hashed passwords), templates (column names + mapping
rules) and job history (counts only). **No customer rows are stored** — uploaded files are processed in
memory. Back up `data/` to keep templates and history.

## Command line

```bash
python -m converter.cli inspect file.xlsx                 # detected columns + proposed mapping
python -m converter.cli convert file.csv --out out/       # exit code 2 if there were bad rows
python -m converter.cli convert file.csv --template "POS guest export"
python -m converter.cli convert file.csv --ai --save-template "New vendor"
```

## Development

```bash
pip install -r requirements.txt
python scripts/make_sample_files.py   # messy sample files in sample_data/
python -m pytest -q
streamlit run app.py
```

```
app.py                  Streamlit web UI (convert, templates, history, target format, users)
converter/
  schema.py             target schema model + YAML loader
  reader.py             file ingest, encoding/delimiter/header detection
  normalizers.py        phone/date/email/zip/state/number/boolean/enum/string normalization
  transforms.py         how a field's value is built: copy, first-non-empty, combine, name part, address part, split, translate, fixed
  mapping.py            mapping spec, column profiling, header fingerprints
  heuristics.py         rule-based auto-mapping
  ai.py                 Claude: mapping, instructions, value repair (+ guards)
  engine.py             conversion, validation, bad rows, dedupe
  output.py             converted file, rejects file, summary
  storage.py            SQLite users/templates/jobs
  services.py           orchestration shared by UI and CLI
  editor.py             mapping-grid helpers
  cli.py                command line
schemas/target_schema.yaml   ← replace with the real import format
```

## Known limits

- Phone validation confirms a number is a real, dialable US number; it cannot tell mobile from landline.
  That needs a carrier lookup (e.g. Twilio Lookup, paid per number) and can be added as an optional step.
- Refreshing the browser tab signs the user out and clears the current file (templates and history persist).
- One file at a time by design.
