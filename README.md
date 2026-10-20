# Import File Converter

Internal web app that converts any customer/contact file (CSV, TSV, TAB, TXT, XLSX, XLSM, XLSB, XLS, ODS) into
the exact import format of the target system. Built for 5–20 internal users; each upload is one file.

**Quick start (one computer):** `./run_local.sh` (Mac/Linux) or `run_local.bat` (Windows), then open
http://localhost:8501. There is no login for now (see [Logins](#logins)). **Tests:** `python -m pytest -q`.

## Documentation

| Document | For | What's in it |
|---|---|---|
| This README | everyone | what the tool does, the output format, phone and date rules, settings |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | people running imports | step-by-step: upload, review the mapping, fix columns, check, import, read the results |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | developers | how a file flows through the code, every module, the data structures and design decisions |
| [docs/STACK.md](docs/STACK.md) | developers, decision makers | every library and tool used, what it does here and why it was chosen |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | developers | setup, tests, conventions, how to change the format or add a rule, release steps |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | whoever runs it | running locally, Docker, a server with HTTPS, users, backups, updates, troubleshooting |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | everyone | what changed in each version |
| [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) | developers | limits and quirks worth knowing before changing code |
| [docs/HANDOFF.md](docs/HANDOFF.md) | whoever continues the work (or starts a project from it) | how changes are made, checked and delivered, the business's decisions, open items, starting a new project from this code |

Every source file also starts with an overview, and every function has a docstring explaining what it does and why.

```
New import:  upload → detect header/encoding/delimiter → auto-map columns → review/edit (or instruct Claude)
             → Start import (runs in the background; mapping saved as a template for next time)
Imports:     every import with live progress, elapsed time and time left → open one → results, downloads,
             side-by-side original vs converted, what changed per field, re-run, Claude value fixes
Numbers:     look up a phone number → every import it was converted in, the file and row it came from, and the
             consent sent with it (kept after the files are deleted)
```

## Pages

Every page has a **light/dark mode toggle** at the top right: a small square with a moon (switch to dark) or a sun
(switch to light). The choice is saved per user; until someone chooses, the app follows the computer's setting.

| Page | What it's for |
|---|---|
| **New import** | Upload a file (a box shows each step while it is read: reading, looking at the columns, matching), check the detected header/sheet and client time zone, review the column mapping (edit the field grid, or under *Columns in your file* pick where each column goes, skip a column, or write notes and apply them with Claude), optionally **Check for mistakes**, then **Start import**. |
| **Imports** | Every import by everyone (filter: mine, status, search). Running imports show a progress bar, the current step ("Converting Phone — row 41,000 of 150,000"), elapsed time and estimated time left, with Cancel. Select any import to open it. |
| **Import detail** | Status and timing, the bad-row flag and counts, downloads (converted file, rejected rows, summary, original file, side-by-side Excel), and tabs: **Side by side**, **What changed**, **Converted file**, **Bad & duplicate rows**, **Mapping used** (incl. notes). **Check this import for mistakes**; Re-run and Claude fixes start a new linked import. |
| **Number history** | Type a phone number to see every import it was converted in (since 0.21.0): when and by whom, the file and its row, the number and consent as written in the file, the consent sent (`true` / `false` / blank) and the column(s) it came from, each with its consent setting (opt-ins only, opt-ins and opt-outs, opt-outs only). Kept after the import's files are deleted (`NUMBER_RECORD_DAYS`, default forever). Select a row to open the import; download as CSV. |
| **Templates** | Saved column mappings per file layout; rename, delete, export. |
| **Target format** | The current target schema, output file settings, dedupe rule, file retention and how long the number history is kept. |
| **Users** | Only while logins are on: (admins) add users, reset passwords, grant or remove admin, delete users. |

**Side by side** shows the original file (left, exactly as uploaded) and the converted output (right) row by
row. Column headers show where each original column went and where each output field came from. Cells are
coloured by what happened: reformatted, built from part of / several columns, default value, Claude fix,
repaired from a likely typo (orange — check these), invalid → left blank, invalid/missing → row rejected; the original value is shown under any changed value and
on hover. Filter to rows with changes, problems, bad rows, duplicates, imported or empty rows, search for any
value or row number, and page through large files. The same view downloads as a colour-coded Excel workbook.

## How it works

| Step | What happens |
|---|---|
| **Read** | Every cell is read as text (no lost ZIP zeros, no phones turned into `5.16E+09`). Header row, encoding (UTF-8 / Windows-1252 / etc.), delimiter and Excel sheets are detected (the first visible sheet with data: an empty *Sheet1* or a hidden lookup sheet is passed over, with a warning); the user can override. The header row is found below any title or report lines, and files without one are recognised (see [Files without a header row](#files-without-a-header-row)). A quote mark that is never closed in a CSV (`"VIP - comps dessert`) costs only its own row, with a warning naming it, instead of joining the rest of the file into one cell; cells of any size are read. |
| **Map** | 1) A saved **template** with the same column headers (case, spaces and punctuation ignored: `Last Visit Date` = `Last_Visit_Date`) is applied instantly, including its notes and time zone. A similar layout (75%+ of the same columns) starts from the template; a phone column that is new since then (e.g. *Mobile* added to the export) is added to the phone field. 2) Otherwise **rules** match headers, synonyms and value types (phones, dates…), and find every phone column (Phone, Phone 2…, Mobile, Home) — but not columns *about* a phone such as `phone4MarketingPreference`, Phone Type or SMS Opt In. A column only counts as a phone column by its values alone when nearly all of its values are phone numbers, so a stray number typed into e.g. *Occupation* doesn't pull that column into the phone field. An *Area Code* column next to 7-digit numbers is combined with them (see [Area code in its own column](#area-code-in-its-own-column)). 3) Optionally, your **notes on columns** are applied by Claude. |
| **Review** | A grid shows each target field, its source column(s), how it's built, a sample of converted output, and any problem. Edit it directly, add notes per column, and run **Check for mistakes** before importing. |
| **Convert** | Runs in a background worker pool (default 2 at a time; more wait in a queue) so users can keep working. Deterministic Python converts every row — Claude never touches the full file. Phones are validated with Google's libphonenumber, dates are interpreted per column (month-first vs day-first decided once for the whole column, Excel serial dates handled), ZIP/state/email normalized. |
| **Report** | **Bad rows are not imported** and the result is flagged: *"37 bad rows were NOT imported"* with a reason per row in the rejected-rows file. Optional fields with invalid values are left blank (configurable per field). |
| **Fix (optional)** | Claude can suggest reformatting for rejected values (e.g. `516 555-O1OO`, `Sept 3rd twenty26`). Each suggestion is re-validated, and any suggestion that adds or changes digits/data is refused. Users approve before re-converting. |

## Target format (generic import format)

The output follows `generic-import-format.md`, encoded in [`schemas/target_schema.yaml`](schemas/target_schema.yaml):

| # | Header | Required | Output | How the tool handles it |
|---|---|---|---|---|
| 1 | `phone_number` | Yes | `2125551234` | US numbers only. No country code → +1 assumed. See *Phone numbers* below. Missing or invalid → row rejected as a bad row. |
| 2 | `consent` | No | `true` / `false` / blank | **Mapped by hand only** (✋). Blank unless a person maps a column to it on the mapping page for that file: the rules, Claude, saved templates and defaults never fill it. Values are read as yes or no (Y/N, yes/no, true/false, T/F, 1/0, on/off, X, ✓, opted in/out, opt-in/opt-out, subscribed/unsubscribed, active/inactive; case, spaces, hyphens and underscores ignored); anything else (`-1`, *maybe*) stays blank, which changes nobody's consent. **Each consent column has a setting, chosen by whoever maps it** (since 0.23.0; the import, or a re-run of an older import, can't start until every column has one): **Opt-ins only** — yes → `true`, no → blank (e.g. *Email Opt In*: not opting into email isn't opting out of texts); **Opt-ins and opt-outs** — yes → `true`, no → `false` (a column of text opt-ins and opt-outs); **Opt-outs only** — yes → `false`, no → blank (e.g. *Unsubscribed*, *Opt Out*). *Subscribed* / *Active* / *Opted out* keep their meaning in every setting, and so does a translated value: *Translate values* on consent takes only opt-in, opt-out or nothing (`DNT=opt-out; OK=`), since true/false or yes/no could be read either way (older translations' `true` / yes mean subscribed, `false` / no opted out). Several consent columns are read together per row: an opt-out in any of them wins, then an opt-in, else blank (0.22.0 imports re-run as they are keep *First non-empty*: the first column with a value decides). A fixed value needs no setting. |
| 3–4, 6–8 | `calls_last`, `transactions_last`, `in_sms_last`, `redemptions_last`, `spin_last` | No | `03/07/2026 09:05 AM` | Any date/time format in (see *Dates and times*) → `MM/DD/YYYY HH:MM AM/PM`. Seconds dropped; a date with no time gets `12:00 AM`. Placeholders like `0000-00-00`, `Never`, `N/A` → blank. Which column feeds which field: see [Which date goes where](#which-date-goes-where). |
| 5 | `last_contacted` | No | `03/07/2026 09:05 AM` / blank | **Mapped by hand only** (✋), like consent: it means the last time *we* texted the customer, which imported files almost never contain. Columns such as *Last Campaign Sent* are not imported unless a person maps them. |
| 9–10 | `birthday`, `anniversary` | No | `09/12/1985` | Any date format in → `MM/DD/YYYY`. Month and day with no year (`09/12`, `Sep 12`, `12-Sep`, `September 12th`, `0912`, `--09-12`, `Birthday: 9/12`), placeholder years (`0000`, `XXXX`, `YYYY`, a year of `0`, `1900` and earlier), a year shared by most of the column (all `1970`, all `2000`, or the year Excel added to dates typed without one), and years in the last two years or the future → year `1600`. Month and day in separate columns (`Birth Month` + `Birth Day` [+ `Birth Year`]) are combined automatically, unless the file also has a complete *Birthday* column with years filled for as many customers (then that one is used, so the year isn't lost). A time zone written with a birthday (`1985-09-12T00:00:00Z`) is ignored: it is a calendar date, so it can't shift to the day before. Dates *about* a birthday (*Birthday Reward Sent*, *Birthday Email Date*) are never the birthday. |

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
- Several numbers in one cell: each different number gets its own row (see below).
- Spreadsheet artifacts: `5165550100.0`, `5.165550100E+09`, `="5165550100"`, a leading apostrophe.
- Letters typed for digits (`516-555-O1OO`, `5l6…`) and vanity numbers (`1-800-FLOWERS`).
- `+1` put in front of a number that already had a `+` (`+1 +15165550100`, `+1 +5165550100`,
  `+1 +51 63 171059`): the extra `+1` is ignored and the rest is read normally. `+` with 10 digits is read as a
  US number with the 1 left out only when that US number can exist; otherwise it's read as the foreign number it is
  (`+51 1 4567890` is Lima, Peru).
- Rejected, with the reason shown: missing area code (7 digits), too few or too many digits, area codes that
  aren't in use (`433`, `555`, `123`), exchanges that don't exist in an area code, non-US numbers with the country
  and place (`not a US number — Canada (area code 514, Quebec)`), numbers with two country codes that aren't valid
  either way, and Excel scientific notation that already lost digits (`5.163E+09`).
- Text far longer than any phone number (over 1,000 characters, e.g. notes mapped by mistake): *too long to be a
  phone number*. Dates over 200 characters likewise (*too long to be a date*).
- Excel error values (`#VALUE!`, `#REF!`, `#DIV/0!`, `#NAME?`…, any field): reason *Excel error (broken formula in
  the client's file)*. They hold no data, so a phone row is a bad row and any other value is left blank. `#N/A`
  counts as empty. *Check for mistakes* reports a column with such values, and *Suggest fixes* skips them. In
  `.xlsx`, `.xlsm`, `.xls` and `.ods` files too (since 0.19.0; the spreadsheet library reads error cells as empty,
  so the codes are read from the file itself; `.xls` needs the `xlrd` package, `.xlsb` isn't covered).
- US territories (Puerto Rico, USVI, Guam, American Samoa, Northern Mariana Islands) count as US. To accept the
  50 states only, set `countries: [US]` in the schema; to also accept Canada, add `CA` to that list.

#### Repairing typos (US numbers only)

Only US numbers are expected, so a number that isn't a valid US number is treated as a likely typo. It is repaired
only when exactly **one** US number fits (each repair below was approved one by one):

| Typo | Example | Repaired to |
|---|---|---|
| Country code in front of a US number | `+44 516 555 0100` | 516-555-0100 (not done for a real foreign number whose area code isn't used in the file, e.g. a London number) |
| `+1` added to a number that already had a `+` | `+1 +51 63 171059` | 516-317-1059 |
| One digit too many (typed twice, extra at the end or start) | `51655501444` | 516-555-0144 |
| An area code that doesn't exist anywhere, one keystroke (one digit, or two swapped) from an area code the file uses | `(156) 555-0199` in a file of 516 numbers | 516-555-0199 |
| No area code (7 digits, also after a country code: `+1 555-0177`), when 80%+ of the file's numbers (at least 5) share one area code | `555-0177` in a file of 516 numbers | 516-555-0177 |

The last two were removed in 0.9.0 and restored in 0.20.0. **A real area code is never changed:** `514-968-9696`
in a file of 516 numbers stays a bad row (`not a US number — Canada (area code 514, Quebec)`), because it may be
someone's real number. A 7-digit number in a file with a mix of area codes stays a bad row (`missing area code
(only 7 digits)`). A filled-in or corrected area code decides whose phone gets the texts: check these repairs.

- When two or more numbers fit, the one that already appears elsewhere in the file wins; otherwise nothing is guessed
  and the reason lists the options ("could be (516) 946-1961 or (516) 946-1966").
- Every repaired number is imported **and marked**: orange in the side-by-side view (with the original shown under
  it), listed under *Problems only*, counted in the summary and in *What changed → Repaired typo*. Check them: a
  wrong guess is someone else's phone.
- Real US numbers are never changed, even if their area code isn't common in the file.

#### Area code in its own column

Some exports keep the area code in a column of its own (*Area Code* `516`, *Phone* `555-0177`). Since 0.21.0 the
rules combine them: the phone field's *How* is **Phone + area code column**, with the area code column and the
phone column(s) as its sources.

- A 7-digit number gets the area code from **the same row**: `555-0177` → 516-555-0177. Several numbers in one
  cell, labels (`Cell: 555-0177`), extensions, letters typed for digits (`555-O177`) and a spreadsheet's `.0`
  (`5550177.0`) work as usual; with several phone columns each different number still gets its own row.
- **The country code 1** is read in both columns: an area code cell `1-516`, `+1 516`, `+1 (516)` or `1516` is
  area code 516, and `+1 555-0177` or `1-555-0177` in the phone column is a 7-digit number (it gets the row's area
  code). `15550177`, 8 digits run together, is not read that way (it stays a bad row).
- **Several area code columns** (*Home Area Code* + *Home Phone*, *Cell Area Code* + *Cell Phone*; *Area Code (2)*
  + *Phone (2)*): each phone column uses the area code column named for the same line (*Cell* = *Mobile*). A plain
  *Area Code* column serves the phone columns without one of their own. A phone column that has neither (*Work
  Phone* next to only *Home Area Code*) never borrows another line's area code: its 7-digit numbers are treated
  like any number without an area code.
- A number that already has an area code is kept as written: `212-555-0188` stays 212, whatever the *Area Code*
  column says.
- A row with an empty area code cell, or `000` / `N/A`, is treated like any 7-digit number: it gets the file's
  main area code when 80%+ of the numbers share one (marked orange), otherwise it is a bad row (`missing area code
  (only 7 digits)`). An area code cell that looks like one but isn't in use (`999`) makes a bad row naming it.
  Nothing in an area code column is read as a phone number, unless a whole number was typed into it.
- Found automatically when the column is named like one (*Area Code*, *AreaCode*, *Phone Area Code*, *Area*,
  *NPA*, *Home Area Code*) and 80%+ of its filled cells, counted over the whole file, are area codes in use
  (zeros count as empty); the 7-digit numbers may be anywhere in the file, also after thousands of full numbers.
  Otherwise choose *Phone + area code column* under *How* in the field grid, with both columns; the column order
  doesn't matter. Adding an *Area Code* column to the phone field under *Columns in your file* does the same.
- Not combined when every number already has its area code. *Delivery Area*, *Zone*, store numbers, and area
  codes of something else (*Store Area Code*, *Delivery Area Code*, *Area #*) are never taken for the customer's
  area code.

#### Several phone numbers per customer (several columns, or several in one cell)

Every phone column feeds the phone field, and **each different number always gets its own row**, with all the
other columns (visit dates, birthday…) copied from the original row it came from. That holds for numbers in
separate columns (Toast: `phone`, `phone2`, `phone3`, `phone4`) and for several numbers typed into one cell
(`516-555-0100 / 917-555-1234`). There is no setting to turn this off; older templates that used *First non-empty*
for the phone field are upgraded automatically.

- The same number twice (`201-555-0111` and `(201) 555-0111`, in two columns or in one cell) counts once.
- An invalid extra number (e.g. `123` in phone3, or `555-1212` after a full number in the same cell) is skipped;
  the row still imports with the customer's other number(s), and the summary lists what was skipped. A number
  without an area code is completed only from the file's main area code (see above), never from the number next
  to it.
- **Fax numbers are never imported** (a fax line can't receive texts). A number labelled fax in a cell
  (`Fax: 516-555-0199`, `516-555-0199 (fax)`) is skipped when the customer has another number; when it is their
  only number the row is a bad row, *labelled as a fax number* (before 0.17.0 it was imported then). Columns named
  *Fax*, *Fax Number*, *Business Fax*, *Facsimile* or *Phone/Fax* never feed the phone field from the rules, a
  template or Claude; the mapping page names them. A person can still map one by hand (*Goes to* →
  `phone_number`), for that file.
- A customer with no valid number at all is one bad row, as before.
- The summary says how many extra rows were made ("5 rows ready to import from 4 file rows (2 extra rows…)"),
  and the side-by-side view marks extra rows `N +` and underlines the number each row was made from (just that
  number when the cell holds several).

How the numbers in one cell are told apart (no digit is ever added, removed or moved):

| Cell | Rows |
|---|---|
| `516-555-0100 / 917-555-1234`, `…, …`, `…; …`, `… or …`, `… and …`, `… \| …`, one per line | one per number |
| `5165550100 9175551234`, `(516)555-0100(917)555-1234`, `516 555 0100 917 555 1234` | one per number |
| `H: 516-555-1212 C: 516-555-0100`, `516-555-0100 (cell) 516-555-0199 (home)` | one per number; each label stays with its own number |
| `516-555-0100 x12, 917-555-1234` | one per number; the extension stays with the first |
| `516.555.0100 917.555.1234`, `1. 516-555-0100 2. 917-555-1234`, `+1 631 555 0199 1-718-555-0142` | one per number (before 0.16.0 one of them, or both, could be lost) |
| `516-555-0100/0101` (shorthand for a second number) | one row, 516-555-0100; the shorthand is not expanded |
| `51655501005165551212` (digits run together) | bad row, `too many digits (20)`: digits are never cut apart |
- Columns that describe a phone (`phone4MarketingPreference`, `Phone Type`, `SMS Opt In`, `Phone Verified`) or
  texting activity (*Last SMS Received*, *SMS Last*, *Last Text Sent*, `in_sms_last`) are never used as phone
  numbers, even when empty. Marketing-preference columns are treated like consent columns: not imported.
- Columns named for another kind of number (*Account Number*, *Order #*, *Card Number*, *Customer Number*,
  *Loyalty ID*) are never taken for the phone column because of their name: ten-digit IDs are often valid phone
  numbers by chance. Such a column is only used when nearly all its values are phone numbers (some loyalty systems
  use the phone number as the member number). *WhatsApp* columns are phone columns.

### Which date goes where

- A column that names its activity goes to that field: *Last Visit* / *Last Order* → `transactions_last`,
  *Last Call* / *Call Time* → `calls_last`, *Last Text Received* → `in_sms_last`, *Last Reward Redeemed* →
  `redemptions_last`, *Last Spin* → `spin_last`, *Birthday* / *DOB* → `birthday`.
- **One last date per row.** Unlike phone numbers, a row has one last date, chosen in this order:
  1. columns that name their activity, as above (if a file names both a last call and a last transaction, both
     are kept);
  2. otherwise the best **generically named date** (*Last Updated*, *Visit Date*, *Date*, *Start Time*,
     *Timestamp*, *Updated*, *End Time*);
  3. only if the file has no other date, a **creation date** (*Date Created*, *Created At*, *Sign Up Date*,
     *Joined*, *Member Since*, *First Visit*): better than no date at all, never alongside a real last date.
  *Date Added*, *Import Date* and *Date Uploaded* count as creation dates too.
- Cases 2 and 3 go to `calls_last` in a **call log** (a header like *Caller ID*, *Duration*, *Direction*,
  *Missed*, or both a *From* and a *To* column) and to `transactions_last` in any other file. Consent columns
  (*Do Not Call*, *Call Opt In*), *Membership Duration* and *Phone Extension* don't make a file a call log. They
  are marked *low* confidence for review.
- **Separate date and time columns are combined** into one date-time: *Date* + *Time*, and a named pair such as
  *Last Visit Date* + *Last Visit Time* or *Call Date* + *Call Time* (before 0.18.0 the named pair's time was
  dropped). Not when the date column already holds times.
- A column named for a date field is mapped even when its values are in a machine format: Excel serial numbers,
  Unix timestamps, `20261009`, or `09/12/1600` birthdays.
- **Never used:** expiry, due and opt-in dates (*Expiration Date*, *Due Date*, *Opt-In Date*); birthdays go to
  `birthday`.
- Every phone number of a call log is imported, the client's own line included (cell-phone validation happens
  later).

### Dates and times

Read in any mix within one column:

- **Numeric:** `10/9/2026`, `10-09-26`, `10.09.2026`, `10 09 2026`, `2026-10-09`, `2026/10/9`, and day-first
  `9/10/2026` when the column clearly is day-first.
- **Compact:** `20261009`, `10092026`, `100926`, `202610091545`, `20261009T154500Z`; day-first `15032026` in a
  day-first column.
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
  only one way (`13/10/2026`) is always read that way. Values without separators count as evidence when they can
  only be read one way (`15032026`; not `20261009`, and not 6 digits that could be year-month-day). You can force
  the order per field with the *Option* cell (`MDY` / `DMY` / `YMD`, where `YMD` also reads `26/10/09` as
  2026-10-09) or by telling Claude. An input format such as `%d/%m/%Y` sets the order too, also for values that
  don't match it exactly (`03/10/2026 3:15 PM` → October 3).
- **Spreadsheet date cells with a separate Time column** (`2026-03-15 00:00:00` + `15:15:00`) give one
  date-time.
- **Two-digit years:** 20yy unless that lands more than a year in the future, then 19yy (`08/09/26` → 2026,
  `08/09/85` → 1985).
- **Time zones:** choose the client's time zone on *New import*; templates remember it. Times that include a zone
  are converted to it: `Z`, `UTC`, `+00:00`, `GMT-4`, `EST`, `PDT`, `(MDT)`, `Eastern`, `Pacific Time`. Unix
  timestamps are converted too. US abbreviations are read as that region's local time, so `EST` in July means
  Eastern clock time. Times with no zone are kept exactly as written. Birthdays and anniversaries are calendar
  dates: a zone written with them is ignored (`1985-09-12T00:00:00Z` stays September 12).
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
    # also: on_invalid (reject|blank), fixed_blank (always empty, locked), manual_only (filled only when a person
    #       maps a column to it; never by rules, templates, Claude or a default), opt_out_choice (boolean: whoever
    #       maps a column to it chooses that column's consent setting: opt-ins only, opt-ins and opt-outs or
    #       opt-outs only), no_year_value (dates, e.g. 1600),
    #       generic_date (call_log|default: receives date columns with a generic name, see Which date goes where),
    #       countries (phone), max_length, truncate, case, allowed_values (enum), default, description
dedupe:
  keys: ["*"]              # "*" = every field; or list specific fields
```

Add source-file header names you see often to `synonyms` so they map automatically.

## Fixing where a column goes

Under **New import → Columns in your file** every column of the uploaded file has:

- **Goes to** — the field(s) it feeds. Pick a different field to move it; picking a field makes this column that
  field's only source (whatever fed it before is dropped from it) — except the phone field, where the column is
  added to the others and each different number gets its own row, and consent, where the column is added with
  what it records (`consent — opt-ins only` / `— opt-ins and opt-outs` / `— opt-outs only`). Clear it to leave the
  column out.
- **Skip** — leave the column out and keep it out: rules, saved templates and Claude won't map it again (Claude
  isn't even shown it). Saved with the template, so the next file with the same layout skips it too.
- **Check** — a warning when the column's name and values don't fit the field it goes to.
- **Your note** — free-text notes, applied by Claude with *Apply notes*.

The field grid above it still works the other way round (pick the source column(s) for each field).

## Files without a header row

The header row is found by comparing each row with the column below it, not by looking at cells on their own: a
header is a row of labels sitting over columns that hold phone numbers, emails or dates; a customer row holds
values of the same kind as the rest of its column.

- **Titles and report lines above the header** (`Guest export`, `Store, Kravit Bayside`, `Generated, 10/12/2026`,
  blank rows) are skipped. The header is the label row just above the first customer.
- **No header row:** when the first full row already holds a customer (its phone number, email or date matches its
  column), the file has no header row. Columns are named `Column 1`, `Column 2`, … and every row is data.
- **A customer is never lost.** A row with a valid phone number or email (in any column, even when an unquoted
  comma has shifted it) is never used as the header, and the header is never looked for below it. Only rows
  without one can end up as column names, so no customer who could be imported is dropped.
- **Unsure cases** (e.g. a file of names only, a header with no customers, a single customer) show a warning asking
  you to check *File options and raw preview*. A header row that holds a phone number, email or date (for
  example one picked by hand) also shows a warning.
- **Mapping:** without names, only the phone column can be recognised (from its values). Choose where the other
  columns go under *Columns in your file*; the sample values show what each column holds.
- **No templates:** files without a header row don't use templates. They are never matched to one and never
  saved as one (*Remember this mapping* is switched off for them): every headerless file with the same number of
  columns has the same stand-in names, so a template would be applied to another client's file.
- To override: *File options and raw preview → Header is on row* (`0` = no header row) → **Re-read file**. The
  preview's row numbers match that box.

## Running it

The short version is below; [docs/OPERATIONS.md](docs/OPERATIONS.md) has the full guide (Docker on one computer,
server with HTTPS, settings, backups, updates, troubleshooting).

### On a server (recommended for the team)

Needs Docker. On a small Linux VM (1 vCPU / 2 GB RAM is enough):

```bash
cp .env.example .env            # set ANTHROPIC_API_KEY, ADMIN_USERNAME, ADMIN_PASSWORD
mkdir -p data && sudo chown 1000 data
docker compose up -d --build
```

Open `http://SERVER:8501`. With logins on (`AUTH_MODE=password`, see [Logins](#logins)), sign in with the admin
account from `.env` (then remove `ADMIN_PASSWORD` from `.env`) and add teammates under **Users**. A shared server
should have logins on.

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
It opens at `http://localhost:8501`.

### Logins

**Logins are switched off for now** (`AUTH_MODE=none`, the default since 0.12.2): there is no sign-in page, everyone
uses the app as the local administrator, and the Users page and *My account* are hidden. Anyone who can open the
address can use the app and see past imports, so keep it on your own computer or a trusted network.

To turn logins back on, set `AUTH_MODE=password` in `.env` and restart. Accounts created earlier are still in the
database and work again; on a fresh install the account from `ADMIN_USERNAME` / `ADMIN_PASSWORD` is created, or the
first visitor creates the administrator.

### Users from the command line

```bash
docker compose exec app python scripts/manage_users.py add jane            # --admin for an admin
docker compose exec app python scripts/manage_users.py password jane
docker compose exec app python scripts/manage_users.py list
docker compose exec app python scripts/manage_users.py delete jane
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

- **Key:** create one at platform.claude.com and set `ANTHROPIC_API_KEY`. Without it, *Apply notes* is disabled, *Suggest
  fixes* is hidden and *Check for mistakes* runs the rule checks only.
- **What's sent:**
  - Notes: column names, up to 15 sample values per column, and your notes.
  - Mistake check: additionally up to 30 sample rows (original and converted) and summary counts.
  - Fixes: only the failed values.
  - Phone digits in the mistake check's sample rows are partly masked (`516#####00`; turn off with
    `AI_MASK_PHONES=false`). The up-to-15 sample values per column (notes, checks, automatic mapping) and the
    failed values sent by *Suggest fixes* are not masked.
  - The full file is never sent.
- **Safety:** structured outputs restrict Claude to real field, transform and column names. Claude can't fill
  `consent` or `last_contacted` (hand-mapped only). Every suggestion is shown for approval before anything
  changes.
- **Models:** `claude-sonnet-5-5` for notes and checks, `claude-haiku-5-5` for fixes (`MAPPING_MODEL` /
  `REPAIR_MODEL`).
- **Cost:** a notes or check call is a few thousand tokens, so a few cents per click; see current model prices at
  [anthropic.com/pricing](https://www.anthropic.com/pricing).
- **Automatic mapping:** to also have Claude refine the column matching on every upload, set
  `AI_AUTO_MAP=true` (off by default).

### What the rule check looks for (no Claude needed)

- A field fed by a column whose values mostly can't be converted (likely the wrong column), or a mapped field
  where nothing came through.
- A column whose name and values don't fit the field it feeds — e.g. *Occupation* going to the phone field because
  someone typed a phone number into it. This one also shows straight away in the mapping grids, without clicking
  *Check for mistakes*.
- Activity times in the future, or mostly before 2000.
- Birthdays/anniversaries with a year in the future (the year is dropped → 1600), or mostly within the last two
  years as written in the file (looks like sign-up dates, or years Excel added).
- Every row having the same date (an export default).
- Columns mixing month-first and day-first dates.
- Times that carried a zone and were converted.
- One column used for several fields.
- A high bad-row rate and its top reason, including 7-digit phones (pointing to *Phone + area code column* when
  the area code is in its own column).
- Many exact duplicates.
- Unused columns that contain dates or phone numbers.
- Consent-type columns, noted as not imported unless you map them to consent yourself.

## Data stored

- `data/converter.db` (SQLite): user accounts (bcrypt-hashed passwords), templates (column names + mapping
  rules), and one record per import (who, when, status, timings, counts, mapping used).
- The **number history** (table `number_log` in the same database, since 0.21.0): for every number each completed
  import converted, the import, the file row, the phone and consent cells as written in the file, and the consent
  sent (an import that ends cancelled or failed keeps nothing there).
  It is customer data too, kept on purpose after the files are deleted so a complaint about consent can be
  answered later; **kept forever unless `NUMBER_RECORD_DAYS` is set**. Imports completed before 0.21.0 are added
  from their stored files when the server starts on 0.21.0 (only those whose files weren't deleted yet).
- `data/imports/<id>/`: the original upload, the converted file, rejected rows, summary, and the comparison
  data behind the side-by-side view. These contain customer data and are **deleted automatically after
  `FILE_RETENTION_DAYS` (default 30)**; the import record and counts stay. Set `0` to keep them forever.
- `data/drafts/<random id>/`: a file still being mapped on *New import* and the mapping so far, so a browser
  refresh doesn't lose the work. Deleted on *Start import*, *Start over* or another upload, and after 24 hours
  without changes.
- The rejected-rows file repeats the client's original values. A value that a spreadsheet would run as a formula
  (starting with `=`, `+`, `-` or `@`, e.g. `=HYPERLINK(…)`) gets a leading apostrophe there, so opening the file
  in Excel never runs anything. Plain numbers and dashes are left as they are.
- Back up `data/` to keep templates and history.

## Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude API key; leave empty to run without AI |
| `AI_ENABLED` | true | `false` turns Claude off without removing the key |
| `MAPPING_MODEL` / `REPAIR_MODEL` | `claude-sonnet-5-5` / `claude-haiku-5-5` | Claude models |
| `AI_SAMPLE_VALUES` | 15 | sample values per column sent to Claude |
| `AI_AUTO_MAP` | false | also let Claude refine column matching automatically on upload |
| `AI_MASK_PHONES` | true | partly mask phone digits in rows sent for Claude's mistake check |
| `IMPORT_WORKERS` | 2 | imports that run at the same time |
| `FILE_RETENTION_DAYS` | 30 | days to keep each import's files (0 = forever) |
| `NUMBER_RECORD_DAYS` | 0 | days to keep the number history (each number, its consent and the file row it came from; 0 = forever) |
| `APP_TIMEZONE` | America/New_York | time zone for displayed times |
| `DEFAULT_PAGE` | new | first page: `new` or `imports` |
| `AUTH_MODE` | none | `none` = no login (current default); `password` = per-user logins |
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
app.py                  Streamlit web UI (new import, imports tracker + detail, number history, templates, target format,
                        users)
converter/
  schema.py             target schema model + YAML loader
  reader.py             file ingest, encoding/delimiter/header detection
  sheetsize.py          size check before a spreadsheet is read (refuses sheets too big for this computer)
  sheeterrors.py        Excel error cells (#DIV/0!…) in .xlsx/.xls/.ods, which the spreadsheet library reads as empty
  normalizers.py        phone/date/email/zip/state/number/boolean/enum/string normalization
  dateparse.py          date/time reader (every numeric, written, machine and spreadsheet format)
  checks.py             rule-based mistake checks + sample prepared for Claude's review
  transforms.py         how a field's value is built: copy, first non-empty, one row per phone number, phone + area
                        code column, combine, name part, address part, split, translate, fixed
  mapping.py            mapping spec, column profiling, header fingerprints
  heuristics.py         rule-based auto-mapping
  ai.py                 Claude (optional): apply notes, mistake review, value fixes (+ guards)
  auth.py               login gate (per-user accounts, bcrypt)
  config.py             settings from environment variables / .env
  engine.py             conversion, validation, bad rows, dedupe, progress, per-cell audit trail
  jobs.py               background import runner (queue, progress, cancel, retention)
  compare.py            side-by-side view, what-changed summary, comparison Excel
  output.py             converted file, rejects file, summary
  storage.py            SQLite users/templates/imports/number history + per-import file folders
  numberlog.py          number history: records each import's numbers and consent values, looks a number up
  ui_format.py          time/duration/status display helpers
  services.py           orchestration shared by UI and CLI
  editor.py             mapping-grid helpers
  cli.py                command line
schemas/target_schema.yaml   the target import format (generic import format)
scripts/                     make_sample_files.py (test data), manage_users.py (logins from the command line),
                             regression/ (406-file before/after check run before every release)
tests/                       pytest suite; tests/fixtures/ has the example contact schema used by generic tests
docs/                        user guide, architecture, stack, development, operations, changelog, known issues,
                             handoff
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how these fit together and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)
for conventions and how to make common changes.

## Known limits

- Phone validation confirms a number is a real, dialable US number; it cannot tell mobile from landline.
  That needs a carrier lookup (e.g. Twilio Lookup, paid per number) and can be added as an optional step.
- Refreshing the browser tab keeps a mapping in progress on *New import* (since 0.15.0: the file, how it was
  read and every mapping choice come back). Claude's answers still waiting for *Accept* aren't kept, and with
  logins on (`AUTH_MODE=password`) a refresh signs the user out first. Imports already started keep running and
  stay visible under *Imports*.
- Imports run inside the web server process. If the server restarts mid-import, that import is marked
  *Interrupted* and can be re-run with one click.
- A spreadsheet sheet that would need more memory than the computer has is refused before it is read, with a
  message naming the farthest cell (usually a stray value far from the table, or a formula copied to the bottom;
  Ctrl+End in Excel finds it). Before 0.15.2 such a file, even a 5 KB one, crashed the whole app. Checked for
  .xlsx, .xlsm and .ods; .xls can't get that big; .xlsb isn't checked. Everything that fits in memory reads as
  before.
- Throughput measured in testing: about 150,000 rows in 25 seconds for a 6-column file on one worker.
- More quirks found while documenting the code are listed in [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md).
