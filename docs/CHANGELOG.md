# Changelog

What changed in each version. The running version is shown in the app's sidebar. Each version is tagged in git
(`vX.Y.Z`).

## 0.23.1 — the mapping tables keep their place

- **Editing a row of a mapping table no longer jumps back to the first row.** Both tables on New import (the field
  grid and *Columns in your file*) showed at most ten rows and scrolled inside themselves; each edit rebuilt the
  table at its first row, so in a file with more than ten columns the row just edited disappeared. Both tables are
  now as tall as their content and the page scrolls instead (Customer Records fixed the same page this way in its
  1.0.2). In a file with a very large number of columns (about 280 or more) *Columns in your file* still scrolls
  inside itself past that height.
- All 406 test files give identical results (no conversion change).

## 0.23.0 — consent settings per column: opt-ins only, opt-ins and opt-outs, opt-outs only

- **Each consent column now has its own setting, chosen when it is mapped.** 0.22.0 had one choice for the whole
  file and no way to import a column of opt-outs. Now every column sent to consent says what it records:
  - **Opt-ins only** (e.g. *Email Opt In*): yes → `true`; no → blank.
  - **Opt-ins and opt-outs** (a column of text opt-ins and opt-outs): yes → `true`; no → `false`.
  - **Opt-outs only** (new; e.g. *Unsubscribed*, *Opt Out*, *Do Not Text*): yes → `false`; no → blank.

  Values that name a status keep their meaning in every setting (*Subscribed* and *Active* are opt-ins, *Opted
  out* an opt-out); the setting decides which ones are imported. So does a translated value: on consent,
  *Translate values* now takes only **opt-in**, **opt-out** or nothing (`DNT=opt-out; OK=`), because true / false
  and yes / no could be read either way. Translations saved before 0.23.0 keep their meaning (`true` / yes
  subscribed, `false` / no opted out), so a re-run can't turn them around. An empty cell is never translated. A fixed value needs no
  setting.
- **Chosen with the column mapping.** Under *Columns in your file*, *Goes to* offers `consent — opt-ins only`,
  `consent — opt-ins and opt-outs` and `consent — opt-outs only`; consent columns add up like phone columns. Under
  the field table each consent column has its own question, and the grid's Option cell takes the settings too
  (`Email Opt In: opt-ins only; Unsubscribed: opt-outs only`; a setting alone only when consent has one column).
  Start import stays disabled until every consent column has one; a column added later is asked for, one removed
  takes its setting with it. Templates never keep a setting.
- **Several consent columns are read together**, row by row: an opt-out in any of them wins (`false`), otherwise
  an opt-in (`true`), otherwise blank. *Email Opt In* = Y with *Unsubscribed* = Yes gives `false`. The grid's How
  for consent is the new **Consent columns** (a copy or *First non-empty* on consent becomes it). Imports made with
  0.22.0 that combined consent columns with *First non-empty* keep it when re-run as they are (the first column
  with a value decides), so they give the same result; any change to their consent turns it into *Consent
  columns*.
- **Reports:** the result banner and the summary file count consent in the converted file ("Consent in the
  converted file: 120 true (subscribed), 15 false (unsubscribed), 300 left blank by the consent settings"); *What
  changed* counts every row read, like its other columns.
  *Side by side* shows a value left blank by its setting in teal and a `false` from an opt-outs-only column in
  pink, with the deciding column ("Unsubscribed: Yes"); *What changed* counts both. Number history, *Mapping used*
  and *What changed* show each column's setting, also for imports mapped before 0.23.0 once they have one.
- **Yes/no values:** `-1` is no longer read as 1 (it can mean "unknown"); *Active* is an opt-in in every setting,
  so an opt-outs-only column never unsubscribes it.
- **Imports made with 0.22.0** keep their choice: opt-ins only becomes each column's *opt-ins only*, opt-ins and
  opt-outs each column's *opt-ins and opt-outs*, and *First non-empty* stays. Re-running an import from before
  0.22.0 asks for each consent column's setting first (naming its translation, if it has one) and then reads its
  columns together.
- All 406 test files give identical results (consent is only ever mapped by a person).

## 0.22.0 — consent: opt-ins only, or opt-ins and opt-outs

- **Whoever maps consent now says what a "no" means.** A client's *Email Opt In* column says whether the customer
  opted into email; a "no" there isn't an opt-out of texts, but it was imported as `false`, which unsubscribes the
  customer. When consent has a column, the mapping page now asks:
  - **Opt-ins only:** yes → `true`; no → left blank, so the import opts nobody out.
  - **Opt-ins and opt-outs:** yes → `true`; no → `false` (as before; only for a column of text opt-outs).

  Neither is assumed: **Start import** stays disabled until one is chosen, and choosing another column for consent
  asks again. The choice is shown wherever the mapping is (*Mapping used*, *What changed*, the side-by-side
  headers, Number history's *Consent from*). With opt-ins only, each "no" left blank is teal in *Side by side* and
  counted in the result banner, *What changed* and the summary file. New target-format setting `opt_out_choice`
  (boolean fields), set on consent.
- **Re-running an import made before 0.22.0** that had consent mapped asks the same question on the import's page
  first: the *Re-run* buttons stay disabled until it is answered, so an old mapping can't quietly write `false`.
- **Removing a consent column asks again.** Taking one column out of a multi-column consent (or skipping it, or
  re-reading a file where it's gone) clears the choice, because it was about all of them.
- **The grid's preview waits for the choice:** before it is made, consent's sample reads "choose below what a no
  means" instead of showing `false`.
- **More spellings of yes and no:** hyphens, underscores and spaces are ignored and full-width letters and
  spreadsheet numbers (`1.0` / `0.0`) are read, so *Opt-Out*, *Opted_Out*, *Un-subscribed*, *Opt-In* and `1.0` are
  understood. Before, *Opt-Out* was left blank: a real opt-out was dropped.
- **Suggest fixes no longer proposes values for yes/no fields** (it could have turned *maybe* into `false`).
- The count of "no"s left blank includes every row, like the other counts, so the banner, the summary and *What
  changed* always agree.
- All 406 test files give identical results (consent is only ever mapped by a person).

## 0.21.2 — the country code next to an area code column

- **Area code cells with the country code** (`1-516`, `+1 516`, `+1 (516)`, `1516`) were not recognised, so the
  column could be missed and its rows had no area code. They are read as area code 516 now.
- **`+1 555-0177` or `1-555-0177` in the phone column** (country code, but no area code) was a bad row (`too few
  digits (8)`). It now counts as a 7-digit number: it gets the row's area code from an area code column, or the
  file's main area code (the 0.20.0 rule, marked orange). `15550177`, 8 digits run together, is still a bad row:
  the 1 may be a typo.
- All 406 test files give identical results.

## 0.21.1 — area code column and number history fixes

Found by testing 0.21.0 with files built to break it.
- **The 7-digit numbers are found anywhere in the file.** Detection read only the first values of the phone
  column: when a file started with full numbers, the 7-digit ones further down got the file's main area code
  instead of the one in their own row (or, with fewer full numbers, the phone field wasn't mapped and every row
  was a bad row). The whole column is read now.
- **One odd value no longer switches it off.** One `000` among nine `516` cells meant the area code column
  wasn't used and every row was a bad row. Cells are now counted by row (80%+ must be area codes in use; zeros
  count as empty). An area code cell that isn't in use (`999`) makes a bad row that says so.
- **Several area code columns.** *Home Area Code* + *Home Phone* and *Cell Area Code* + *Cell Phone* used the home
  area code for both. Each phone column now uses its own (*Cell* = *Mobile*, *(2)* with *(2)*); a phone column
  with none of its own never takes another line's.
- **Letters typed for digits (`555-O177`) and a spreadsheet's `.0` (`5550177.0`)** now get the row's area code
  (`5550177.0` also gets the file's main area code when there is no area code column).
- *Store Area Code*, *Delivery Area Code* and *Area #* are not taken for the customer's area code.
- Editing: removing the area code column leaves *One row per phone number*; choosing *Phone + area code column*
  before the second column no longer turns into *Copy column*; adding a second area code column says so.
- **Number history:** an import cancelled or failed after its numbers were recorded no longer shows up; looking
  up two numbers at once says to look them up one at a time.
- All 406 test files give identical results.

## 0.21.0 — area code columns; number history

- **Area code in its own column.** Exports with *Area Code* (`516`) and *Phone* (`555-0177`) had no usable phone
  column: the phone field stayed unmapped, and mapping *Phone* by hand made every row a bad row (`missing area
  code`). The rules now combine them with the new *How* **Phone + area code column**: each 7-digit number gets
  the area code from its own row. Numbers that already have an area code are kept as written; several phone
  columns and several numbers per cell still give one row per number. A row with an empty area code cell is
  treated like any 7-digit number (the file's main area code from 0.20.0, or a bad row). Found when the column is
  named like an area code (*Area Code*, *AreaCode*, *Area*, *NPA*) and nearly all its values are area codes in use;
  otherwise choose it under *How*. A similar template takes a new *Area Code* column the same way it takes a new
  phone column. *Check for mistakes* now points to this option for 7-digit numbers.
- **Number history** (new page). Type a phone number to see every import it was converted in: when and by whom,
  the client's file and row, the number and consent as written in the file, the consent sent (`true` / `false` /
  blank) and the column it came from. Each completed import records its numbers in the database, where they stay
  after the files are deleted, so "where did this opt-in come from?" can be answered months later. Download as
  CSV. Kept forever unless the new setting `NUMBER_RECORD_DAYS` is set. On the first start of 0.21.0, earlier
  imports whose files still exist are recorded in the background.
- All 406 test files give identical results.

## 0.20.0 — area codes filled in and corrected again (business decision)

Two repairs removed in 0.9.0 are back, decided by the business. Both are imported and marked orange like every
repair: a filled-in or corrected area code decides whose phone gets the texts, so check them.
- **A missing area code is filled in** from the file's main area code: a 7-digit number (`555-0177`) gets the area
  code that 80%+ of the file's valid numbers share (at least 5 of them). In a file with a mix of area codes it
  stays a bad row (`missing area code (only 7 digits)`). Also for a second number in a cell
  (`516-555-0150 / 555-1212`).
- **An area code that doesn't exist is corrected**, only when it is one keystroke (one digit, or two neighbouring
  digits swapped) from an area code the file uses: `(156) 555-0199` in a 516 file → 516-555-0199. When it is one
  keystroke from two of the file's area codes, nothing is guessed and both are listed. Unlike before 0.9.0, **a
  real area code is never changed**: `514-968-9696` in a file of 516 numbers stays a bad row (514 is Quebec).

## 0.19.0 — Excel errors in spreadsheets; day/month order edge cases

- **Excel error codes in spreadsheets.** The spreadsheet library reads a cell holding `#DIV/0!`, `#REF!`,
  `#VALUE!`… as empty, so in .xlsx, .xls and .ods files the reason added in 0.15.1 was lost: a phone said "required
  value is missing" and other fields were blank without a word. The codes are now read from the file itself, so
  these files report *Excel error (broken formula in the client's file)* like a CSV. .xls files need the new `xlrd`
  package (run `.venv/bin/pip install -r requirements.txt` once); without it they read as before. .xlsb isn't
  covered. Cost: about 0.1 s on a 150,000-row .xlsx (3% of reading it).
- **Spreadsheet date cells with a Time column.** A date cell reads as `2026-03-15 00:00:00`; joined with a *Time*
  column that gave `2026-03-15 00:00:00 15:15:00`, which wasn't a readable date, so every date was lost (0.18.0
  dropped the time instead). Now it gives 03/15/2026 03:15 PM.
- **An input format sets the order.** With *Option* `%d/%m/%Y`, values with a time after the date
  (`03/10/2026 3:15 PM`) fell back to month-first and became March 10; now October 3.
- **`YMD` works for 2-digit years.** `26/10/09` with *Option* `YMD` became October 26, 2009; now October 9, 2026.
- **Day-first dates without separators.** In a column of `15032026`, `28022026`, `01042026` the last one was read
  as January 4; such values now count as day-first evidence like `15/03/2026` (also `150390`-style values that
  can only be day-first, and `1503` birthdays). `20261009` and six-digit values that could be year-month-day don't
  count.
- All 406 test files give identical results.

## 0.18.0 — birthdays keep their day and year; dates go to the right field

- **Birthdays stored as UTC midnight no longer move a day.** `1985-09-12T00:00:00Z` (how many systems store a
  date) was converted to the client's time zone and became September 11. Birthdays and anniversaries are calendar
  dates now: a zone written with them is ignored. Activity dates and times are still converted.
- **A complete Birthday column keeps its year.** Files with *Birthday* (09/12/1985) and *Birth Month* + *Birth Day*
  columns used the parts, so every birthday became 09/12/1600. The complete column is now used when most of its
  values have a year and it is filled for as many customers. A *Birthday Day of Week* column is never read as the
  day, and a *Birth Year* of `0` means "no year" (it used to blank the birthday).
- **Dates go to the right field.** *Birthday Reward Sent*, *Birthday Email Date* and similar dates about a birthday
  or anniversary were mapped as the birthday itself (high confidence); now they aren't. *Do Not Call*, *Call Opt
  In*, *Membership Duration* and *Phone Extension* columns made a file count as a call log, so its date went to
  calls_last; they no longer do. *Date Added*, *Import Date* and *Date Uploaded* are creation dates (used only when
  there is no other date).
- **Date columns in machine formats are mapped by name.** A *Last Visit* column of Excel serial numbers, Unix
  timestamps or `20261009`, or a *Birthday* column of serials, `19850912` or `09/12/1600`, wasn't mapped at all.
- **Named date and time columns are combined.** *Last Visit Date* + *Last Visit Time* (or *Call Date* + *Call
  Time*) kept only the date, so every time was 12:00 AM.
- All 406 test files give identical results.

## 0.17.0 — fax numbers never imported; better sheet choice; templates take new phone columns

- **Fax numbers are never imported** (a fax line can't receive texts). Columns named *Fax*, *Fax Number*,
  *Business Fax*, *Facsimile* or *Phone/Fax* were added to the phone field when their values looked like phone
  numbers, so every customer's fax got a row of its own; a file with only a Fax column imported it as the phone.
  Now the rules, a template (also one saved earlier) and Claude never use a fax column, and the mapping page names
  it. A person can still map one by hand, for that file. A number labelled fax in a cell (`Fax: 516-555-0177`) was
  imported when it was the row's only number; that row is now a bad row (*labelled as a fax number*), and *Suggest
  fixes* leaves it alone.
- **Texting-activity columns are not phone columns.** An empty *Last SMS Received*, *SMS Last*, *Last Text Sent* or
  `in_sms_last` column counted as an extra phone column (a phone word in the header), e.g. when a file in the import
  format itself was uploaded.
- **A similar template takes new phone columns.** A template saved from an export with *Phone* only, applied to a
  later export that also has *Mobile* (75%+ of the same columns), left *Mobile* out silently. A phone column that
  is new since the template was saved is now added (one row per different number), with a note. Phone columns the
  template's file already had and the template leaves out stay out.
- **Which sheet is read.** An empty first sheet (a leftover *Sheet1*) stopped the file with "No data found", and a
  hidden first sheet (a lookup table) was read instead of the customers. Without a chosen sheet, the first visible
  sheet with data is now read, with a warning naming the sheets passed over; hidden sheets show as *(hidden)* in
  the sheet box. A sheet you pick is read as before.
- All 406 test files give identical results.

## 0.16.0 — no ID columns as phones; stray quotes, template headers and multi-number cells fixed

- **ID columns are never taken for the phone column because of their name.** *Account Number*, *Card Number*,
  *Customer Number*, *Invoice Number*, *Ticket Number* look like "contact number" / "phone number" to the header
  comparison, and ten-digit IDs are often valid phone numbers by chance: in a file whose real phone column couldn't
  be used (7-digit numbers), the IDs became the phone numbers. Such a column is now only used when nearly all its
  values are phone numbers (some loyalty systems use the phone as the member number). *WhatsApp* columns are now
  recognised as phone columns.
- **A quote mark that is never closed in a CSV costs one row, not the rest of the file.** `"VIP - comps dessert`
  with no closing quote joined every later row into that cell: a 1,000-row file came out as 3 rows, with no
  warning. Now the quote is read as an ordinary character, every row imports, and a warning names the row. Real
  multi-line cells (addresses, notes) are kept, and a quote inside a cell (`5" tall`) is an ordinary character, as
  in Excel. A cell over 128 KB no longer stops the file. Limits: KNOWN_ISSUES #30.
- **Templates find their columns despite punctuation.** A template made on *Last Visit Date* matched a file with
  *Last_Visit_Date* (templates ignore spaces and punctuation), but then lost that column, so its dates were
  silently left out. Columns are now found the same way, when only one column of the file fits.
- **Several numbers in one cell: none lost any more** in cells such as `516 555 0100 x12 917-555-1234`,
  `1. 516-555-0100 2. 917-555-1234`, `+1 631 555 0199 1-718-555-0142 1-201-555-0111` (one number was dropped) and
  `516.555.0100 917.555.1234` (both were rejected). Numbers in the usual North American shapes are now found
  directly, and used only when that finds every number found before plus more. Nothing is guessed; run-together
  digits and `516-555-0100/0101` stay as before.
- All 406 test files give identical results.

## 0.15.3 — one odd cell can't stop a file; long notes no longer slow uploads

- **One value never stops a file.** A garbage date such as `10/09/2026 12:1111…` used to stop the mapping page and
  fail the import; now that cell is "not a date" and everything else imports. `+1 ` repeated many times in a phone
  cell no longer runs out of recursion depth. Any unexpected error on a value now costs only that value (reason
  *couldn't be read (unexpected error…)*, details in the server log).
- **Repeated column names:** `Phone, Phone, Phone (2)` used to crash the page and `Phone, phone, Phone (2)` silently
  dropped one phone column. Repeats now get the next free number (`Phone (3)`), so every column is kept and used.
- **Long Notes columns no longer slow the upload.** 1,000 rows of 5,000-character notes took 3.9 s to show the
  mapping page (30,000 characters: 23 s); now 0.25 s (1 s). When guessing what columns hold, values over 200
  characters are treated as text.
- **Too-long values are rejected at once:** a phone cell over 1,000 characters (*too long to be a phone number*;
  ~40 labelled numbers still fit) or a date over 200 (*too long to be a date*). A specially shaped long value can
  no longer take seconds.
- Files with several phone columns convert exactly as before, and all 406 test files give identical results.

## 0.15.2 — a stray cell can no longer crash the app

- **Fix:** a spreadsheet with a value far away from its table (e.g. one stray value in the sheet's last cell,
  XFD1048576) made the spreadsheet library try to reserve hundreds of GB and stopped the whole app for everyone,
  interrupting running imports. A 5 KB file was enough; so was a tiny .ods that repeats a row a million times.
- Such a sheet is now refused when it is uploaded, with a message naming the far cell and how to fix the file
  (Ctrl+End in Excel). Only sheets that need more memory than the computer has are refused (80% of its memory, or
  of the container's limit), so every file that could be read before is read exactly as before. Formatting far
  away (formatted but empty cells and rows) doesn't count.
- Checked for .xlsx, .xlsm and .ods (an .xls can't get that big; .xlsb isn't checked). Speed: about 0.015 s for a
  5,000-row file and 0.5 s for 150,000 rows (reading that file takes about 3 s); a file isn't checked twice
  (re-read, refresh, the background import). CSV files are unaffected.

## 0.15.1 — Excel error values explained

- A cell holding an Excel error code (`#VALUE!`, `#REF!`, `#DIV/0!`, `#NAME?`, `#NUM!`, `#NULL!`, newer codes such
  as `#SPILL!`, Google Sheets `#ERROR!`, LibreOffice `Err:502`) now gets the reason *Excel error (broken formula in
  the client's file)* in every field, instead of misleading ones like *too few digits (1)* or *unrecognized word
  'value'*. The outcome is unchanged: a phone row is still a bad row, any other value is left blank. `#N/A` still
  counts as empty.
- *Check for mistakes* reports a column with such values ("ask the client for a fresh export") instead of calling
  it the wrong column, and *Suggest fixes* no longer sends them to Claude.

## 0.15.0 — refresh keeps your mapping; no live formulas in downloads

- **A browser refresh no longer loses a mapping in progress.** The file being mapped on *New import* is kept as a
  draft (`data/drafts/`), and its id is in the page address (`?draft=…`). After a refresh the file, how it was
  read (sheet, header row), every mapping choice, notes, skipped columns, time zone, template name and the
  *Remember* box come back ("Picked up where you left off…"). Claude answers still waiting for *Accept* are not
  kept. A draft is only restored for the user who uploaded it, and is deleted on *Start import*, *Start over*,
  another upload, or after 24 hours without changes.
- **Security: no live spreadsheet formulas in the rejected-rows file.** It repeats the client's original values;
  a value starting with `=`, `+`, `-` or `@` (e.g. `=HYPERLINK(…)`) now gets a leading apostrophe so Excel shows
  it as text instead of running it. Plain numbers and dashes are unchanged. Rejected-rows files of earlier imports
  are fixed the same way when downloaded. An Excel (`.xlsx`) output file now stores every value as text, never as
  a formula (the current import format writes CSV, so its converted file is unchanged).
- **Activity dates stay as written** (decision, no change in behaviour): only birthday and anniversary get the
  placeholder year `1600`. `01/01/1900`, `01/01/1970` and future dates in activity fields are imported as they are;
  a test now pins this down.

## 0.14.0 — consent and last_contacted can be mapped by hand

- **consent** and **last_contacted** are no longer locked blank. A person can map a column to them on the mapping
  page (field grid or *Goes to*), for one file at a time. They are marked ✋.
- Nothing automatic ever fills them: not the rules, not Claude (including *Apply notes*), not a default and not a
  saved template. Templates leave them out when saved and drop them when applied (also older templates), so a
  person chooses the column again for every file. Re-running an import keeps that import's choice.
- Mapped consent values become `true` (subscribes) or `false` (opts out): Y/N, yes/no, 1/0, opted in/out,
  subscribed/unsubscribed. Anything else is left blank, which changes nobody's consent.
- New target-format setting `manual_only` (can't be combined with a default, `required` or `fixed_blank`). To lock
  a field blank again, use `fixed_blank: true` instead.

## 0.13.1 — one last date per row; creation date as the fallback

- **One last date per row.** A generic date (*Date*, *Start Time*, *Timestamp*…) is only used when no column names
  a last activity, and only one is used (0.13.0 put a second one in the other field). A file that names both a
  last call and a last transaction still keeps both.
- **Creation date as the last resort.** When a file has no other date, *Date Created*, *Created At*, *Sign Up
  Date*, *Joined*, *Member Since* or *First Visit* is imported (to `calls_last` in a call log, `transactions_last`
  otherwise) - never alongside a real last date. Expiry, due and opt-in dates are still never used.

## 0.13.0 — every date gets imported; last_contacted always blank

- **Dates with a generic name are imported.** A date column named only *Date*, *Start Time*, *Timestamp*,
  *Updated*… used to stay unmapped. It now goes to `calls_last` in a call log (headers like *Caller ID*, *Duration*,
  *Direction*, or *From* + *To*) and to `transactions_last` in any other file; a second one goes to the other
  field. Separate *Date* and *Time* columns are combined. Marked *low* confidence for review.
- Dates that aren't a last activity are never used: *Date Created*, *Created At*, *Sign Up Date*, *Joined*,
  *Member Since*, *First Visit*, *Expiration Date*, *Opt-In Date*.
- **`last_contacted` is always blank** (locked like consent): it means when we last texted the customer, which no
  imported file contains. *Last Campaign Sent* and similar columns are no longer imported anywhere.
- **Fix:** in a call log whose *To* column holds the same number on every row (the client's own line), the
  mapping could take only that column and drop the *From* column with the customers' numbers. Every phone column
  is now kept, so every number is imported.
- New target-format setting `generic_date` (`call_log` / `default`) marks which fields receive generic dates.

## 0.12.2 — logins switched off

- No login for now: `AUTH_MODE` defaults to `none`, so the app opens straight away and everyone uses it as the local
  administrator. The Users page, *Signed in as* and *My account* are hidden while logins are off.
- Nothing was deleted: accounts and passwords stay in the database. `AUTH_MODE=password` in `.env` turns logins back
  on, with the same accounts.
- An existing `.env` that says `AUTH_MODE=password` keeps the login until that line is changed to `none`.

## 0.12.1 — header detection hardened; no templates for files without a header row

- **No templates for files without a header row.** They are never matched to a template and never saved as one
  (*Remember this mapping* is switched off for them; the CLI skips `--save-template` and refuses `--template`).
  The *Use a saved template* picker added in 0.12.0 is removed, so files with headers work exactly as before it.
  Templates saved from headerless files by older versions are never matched; they can be deleted on the Templates
  page.
- Header detection, checked against 0.11.0 on 54,000 generated exports (clean and messy) and end to end on 400
  files with headers plus the sample files:
  - a row holding a valid phone number or email - in any column, also when an unquoted comma shifted it - is never
    the header and the header is never looked for below it, so no customer is dropped (0.11.0 dropped the first
    customer in about a third of files without a header);
  - files with a header: the right row in 99.9% of clean files (0.11.0: 83%); every file where both versions agree
    on the header row converts to byte-identical output, and every difference is a header 0.11.0 got wrong;
  - "N/A", "none" and the like count as empty; a report line such as `Generated, 10/12/2026` or
    `Date range, 09/01/2026 - 09/30/2026` is no longer mistaken for a customer or a phone number; columns that only
    shifted rows reach no longer count.
- A cell holding several phone numbers now counts as a phone value when a column is recognised by its values, so the
  phone column of a file without a header row is still found when some customers have two numbers in one cell.

## 0.12.0 — files without a header row

- **Fix:** in most files without a header row, the first customer was taken as the header and silently left out
  (e.g. `Ana Lee,(516) 555-0100` as the first line). The header row is now found by comparing each row with the rest
  of its columns: a header is a row of labels over columns of phone numbers, emails or dates; a row whose phone
  number, email or date fits its column is a customer and never becomes the header.
- Title and report lines above the header (`Guest export`, `Store, Kravit Bayside`, `Generated, 10/12/2026`) are
  still skipped, wherever the header is.
- Unsure cases (names only, a single row, a header with no customers) show a warning asking to check the raw
  preview, and a header row that holds a phone number, email or date shows a warning too (also when picked by hand).
- Files without a header row are never matched to a template automatically: every headerless file with the same
  number of columns has the same stand-in names (Column 1, Column 2…), so one client's template would be applied to
  another's file. The mapping page explains this.
- New **Use a saved template** on the mapping page: apply any saved template by hand (its time zone too). Files
  without a header row reuse their mapping this way, matched by column position.
- The raw preview numbers its rows like the "Header is on row" box.

## 0.11.0 — several numbers in one cell

- A cell holding several phone numbers now gives **one row per different number**, exactly like separate phone
  columns: `516-555-0100 / 917-555-1234`, `H: 516-555-1212 C: 516-555-0100`, `5165550100, 9175551234`, one number
  per line, `516 555 0100 917 555 1234`. Before, only one number per cell was imported (mobile first).
- Numbers are only split where they are clearly separate. No digit is added, removed or moved: run-together digits
  stay one (bad) value, and shorthand such as `516-555-0100/0101` imports just the full number.
- Labels and extensions stay with their own number, whether written before (`C: 516…`) or after
  (`516… (cell)`) it.
- A number labelled fax is skipped when the customer has another number (a fax line can't receive texts); a fax
  number that is a customer's only number is still imported.
- An invalid second number in a cell (`516-555-0150 / 555-1212`) is skipped and listed in the summary; the row
  imports with its valid number. Before, it disappeared without a note.
- The mapping page says how many cells hold several numbers. *Side by side* underlines just the number each row
  came from.
- **Fix:** the bad-rows report could fail when the file already had a column with the phone field's name (e.g.
  `phone_number`) and several phone columns; the report's extra column is now named `phone_number (this row)`.

## 0.10.0 — light and dark mode

- A light/dark toggle at the top right of every page: a small square with a moon (switch to dark mode) or a sun
  (switch to light mode). The switch is instant and keeps everything on the page (no sign-out, no lost mapping).
- The choice is saved per user (new `user_prefs` table, created automatically) and in the browser, so it applies
  after signing in again, on other computers and on the login page. Until a user chooses, the app follows the
  computer's light/dark setting.
- Streamlit's real dark theme is used for every element, including the mapping grids. The side-by-side comparison
  has matching dark colours with the same meanings (orange = repaired, red = rejected, …).

## 0.9.0 — no assumed area codes (compliance)

Every feature that guesses or assumes data was reviewed and approved or removed one by one.

**Removed** (a guessed area code sends texts to someone who never gave consent):
- **Filling in a missing area code.** A 7-digit number is never given the file's most common area code; it stays a
  bad row: `missing area code (only 7 digits)`.
- **Changing an area code that looks like a typo.** `514-968-9696` in a file of 516 numbers is no longer imported
  as 516-968-9696; it stays a bad row: `not a US number — Canada (area code 514, Quebec)`. The same applies to an
  area code that isn't in use, such as 433.

**Kept** (no digit is added or changed; every repair is marked orange for review):
- Removing one extra digit (typed twice, or extra at the start or end), with the tie-break on a number found
  elsewhere in the file.
- Dropping a foreign country code typed in front of a US number (`+44 516 555 0100`).
- Reading a misplaced `+` (`+5165550100`, `+1 +51 63 171059`) as a US number.
- Reading letters typed for digits (`516-555-O1OO`).
- Choosing a phone column from its values alone when 90%+ are phone numbers (shown for review before import).
- Treating birthday/anniversary years in the last two years or in the future as "no year" (1600).
- Treating a year shared by most of a birthday/anniversary column as "no year" (1600).

## Documentation update (after 0.8.2; no code changes, so the version stays 0.8.2)

- Every source file now starts with an overview, and every function and class has a docstring. Comments explain
  the business rules and edge cases. No code changed.
- New guides in `docs/`: user guide, architecture, technology stack, development, operations, known issues and this
  changelog. The README links them.
- Configuration files (`.env.example`, `Dockerfile`, `docker-compose.yml`, `schemas/target_schema.yaml`,
  `requirements.txt`, run scripts) are commented line by line.

## 0.8.2

- **Fix:** on macOS, birthdays and anniversaries without a year came out as `10/18/` instead of `10/18/1600`. The
  year was inserted through a hidden marker character that the Mac's date formatter cuts off at; it is now
  written as plain text.

## 0.8.1 — birthdays without a real year

- `MM/DD/1600` now also for:
  - placeholder years (`09/12/0000`, `09/12/XXXX`, `YYYY-09-12`);
  - labels in the cell (`Birthday: 9/12`, `DOB 9/12`);
  - a year shared by most of the column (all 1970, all 2000, or the year Excel adds to dates typed without one);
  - years in the last two years or in the future.
- Birthday/anniversary split over *Birth Month* + *Birth Day* (+ *Birth Year*) columns is combined automatically.
- The "looks like sign-up dates, not birthdays" check now looks at the years as written in the file.

## 0.8.0 — phone typo repair

- A number that isn't a valid US number is repaired when exactly one US number fits, using the file's own area
  codes as evidence:
  - area code one digit off or two digits swapped (`514` in a `516` file);
  - one digit too many;
  - a country code in front of a US number;
  - a missing area code when 80%+ of the file shares one.
- Every repaired number is imported and marked: orange in the side-by-side view, listed under *Problems only*,
  counted in the summary and in *What changed*.
- When several numbers fit, the one already elsewhere in the file wins; otherwise nothing is guessed and the reason
  lists the options.
- **Fix:** a phone-like value such as `+1 +51694619661` crashed the upload. Date detection read it as a day number
  in the billions.

## 0.7.2

- `+1` in front of a number that already had a `+` (`+1 +51 63 171059`) is ignored and the rest read normally.
- `+` followed by 10 digits is read as a US number missing its `1` only when that number can exist.
- Clearer rejection reasons:
  - the country and place for non-US numbers (`not a US number — Canada (area code 514, Quebec)`);
  - "area code 433 isn't in use";
  - exchanges that don't exist in an area code;
  - numbers with two country codes.

## 0.7.1

- One row per phone number is always on when the phone field has several columns. *First non-empty* can no longer
  be chosen for phones, and older templates that used it are upgraded automatically.

## 0.7.0 — several phone columns

- Several phone columns (e.g. Toast's `phone`, `phone2`, `phone3`, `phone4`): each **different** number becomes
  its own row, with the other columns copied from the same file row. The same number in two columns counts once.
  Invalid extra numbers are skipped and counted.
- Columns *about* a phone (`phone4MarketingPreference`, Phone Type, SMS Opt In) are never used as numbers.
  Marketing-preference columns are treated as consent columns.
- Side by side: extra rows are marked `N +`, and the number each row came from is underlined.

## 0.6.0 — fixing where a column goes

- *Columns in your file*:
  - **Goes to** moves a column to another field or clears it;
  - **Skip** keeps a column out for good, including from templates and Claude's suggestions.
- A column whose name and values don't fit the field it feeds is flagged right away. Example: *Occupation* with a
  phone number typed into it.
- A stray phone number in a non-phone column no longer pulls that column into the phone field.
- **Fix:** editing the field grid erased the notes written on columns.

## 0.5.0

- Claude limited to three buttons: **Apply notes with Claude**, **Check for mistakes** (rules always, Claude
  optionally) and **Suggest fixes**. Mapping uses rules by default; `AI_AUTO_MAP=true` restores automatic Claude
  mapping.

## 0.4.0

- US-only phone numbers, `+1` assumed when there is no country code.
- Exhaustive date and time parsing.
- Per-import client time zone.

## 0.3.0

- Imports tracker with live progress, time left and cancel; side-by-side comparison of original and converted
  files.
- The real import format (`schemas/target_schema.yaml`).

## 0.1.0

- First version: conversion engine, rule-based and Claude-assisted column mapping, Streamlit UI, templates, bad-row
  report, duplicate removal.
