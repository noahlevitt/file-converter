# User guide

How to convert a customer file, check the result and fix problems. No technical knowledge needed.

The converter takes a customer or contact file in any layout and produces the import file in the exact format the
target system expects:
- 10-digit US phone numbers;
- activity dates and times as `MM/DD/YYYY HH:MM AM/PM`;
- birthdays and anniversaries as `MM/DD/YYYY`, with year `1600` when there's no real year;
- `consent` as `true` / `false` and `last_contacted`, only when you map a column to them yourself (blank otherwise).

## Opening the converter

Open the converter's address in a browser (on one computer: http://localhost:8501). There is no login for now.
When logins are turned on (`AUTH_MODE=password`), sign in with the username and password an administrator gave
you; you can then change your own password under **My account** in the sidebar, with **Sign out** just below it.

**Light or dark mode:** click the small square at the top right of any page. A moon switches to dark mode, a sun
switches back to light. Your choice is remembered (for your account when logins are on, so it follows you to
other computers and after you sign in again). Until you choose, the converter follows your computer's light/dark setting.

## Converting a file

### 1 · Upload

On **New import**, drag the file onto the upload box or click to choose it. Accepted: `.csv`, `.tsv`, `.tab`,
`.txt`, `.xlsx`, `.xlsm`, `.xlsb`, `.xls`, `.ods`. One file at a time.

The **File** section shows what was detected: rows, columns, sheet, text encoding and delimiter. Check two things
here:

- **Client time zone.** Choose the business's time zone. Times in the file that include a zone (`Z`, `UTC`,
  `EST`, `+00:00`…) and Unix timestamps are converted to it. Times without a zone are kept exactly as written.
  Templates remember the zone.
- **File options and raw preview.** If the column names look wrong, open this. You can pick another sheet, or
  say which row holds the headers (`0` = the file has no header row), then click **Re-read file**. The preview
  shows the first rows exactly as they are in the file, numbered like the box. It opens by itself when something
  needs checking: no header row found, a header below title lines, an unsure guess, or a header row that holds a
  phone number, email or date (usually a customer, not a header).
- **Files without a header row** get columns named *Column 1*, *Column 2*, …. Only the phone column is
  recognised (from its values); choose where the others go under **Columns in your file**. Templates aren't used
  for such files: none is applied, and *Remember this mapping* is switched off.

### 2 · Review the column mapping

The converter matches the file's columns to the import fields automatically. If you have uploaded a file with
the same columns before and saved its mapping, that **template** is applied instantly. Check the result in the
two tables.

**The field table** has one row per import field:

| Column | Meaning |
|---|---|
| Field | The import field. `*` = required (rows without it are bad rows). 🔒 = always blank. ✋ = only filled when you map a column to it yourself (consent, last_contacted). |
| How | How the value is built: *Copy column*, *One row per phone number*, *Combine columns*, *Leave blank*, … |
| Source column(s) | Which column(s) of your file feed this field. Click to change. |
| Option | Extra setting, e.g. date order `MDY` / `DMY` for dates (`auto` normally works). |
| Sample output | What the first converted values look like. ⚠ means some values in the first rows can't be converted. |
| Check | A warning when something needs attention (e.g. a required field with no column). |
| Mapped by / Why | Who chose this (Template, Rules, Claude, You), how confident, and why. |

**Columns in your file** has one row per column of your file. This is usually the easiest place to fix things:

- **Goes to:** the field(s) this column feeds.
  - Pick a different field to move the column there. That field takes this column as its only source, *except*
    the phone field: adding a phone column gives each extra number its own row.
  - Clear the cell to leave the column out.
- **Skip:** tick to keep a column out completely. The rules, saved templates and Claude won't map it again, and
  the template remembers it for next time.
- **Check:** a warning when the column's name and values don't fit the field it feeds. Example: *Occupation*
  sent to the phone field because someone typed a phone number into it.
- **Your note:** optional, e.g. "this is the last visit date" or "dates are day-first". With Claude configured,
  **Apply notes with Claude** turns notes into mapping changes. You see each proposed change and choose
  **Accept changes** or **Discard**. Without Claude, notes are kept with the template as documentation.

**Check for mistakes** runs rule checks on the first 1,000 rows, plus a Claude review of a sample when Claude is
configured. It flags things like:
- sign-up dates mapped as birthdays;
- visit dates in the future;
- 7-digit phone numbers;
- day/month order that mixes both conventions.

### 3 · Start the import

- **Remember this mapping** (on by default) saves the mapping as a template under **Template name**, so the next
  file with the same columns maps instantly.
- **Start import** is enabled once every required field has a source. The import runs in the background; you're
  taken to its page and can leave and come back.

**Refreshing the page** before *Start import* doesn't lose your work: the file, how it was read and every mapping
choice come back ("Picked up where you left off…"). Only Claude's answers still waiting for *Accept* have to be
asked for again. The page address carries a code for this (`?draft=…`), so keep using the same tab. A file you
leave for 24 hours without changes is removed; upload it again.

## Following imports

**Imports** lists every import, newest first. Filter with **Show** (Everyone / Mine), **Status**, and **Search**
(file name, template name or `#id`). Running imports appear on top with a progress bar, the current step, elapsed
time and time left. Each has **Open** and **Cancel**. Click a row to open an import.

Statuses:
- **queued:** waiting for a free slot.
- **running:** being converted.
- **completed:** finished.
- **failed:** an error stopped it (the message is shown).
- **cancelled:** someone clicked Cancel.
- **interrupted:** the app restarted while it was running. Use **Re-run this import**.

## Reading a finished import

The banner at the top shows the result in one sentence, for example:

> 5 rows ready to import from 4 file rows (2 extra rows for customers with more than one phone number).
> 1 bad row was NOT imported. 1 phone number was repaired from likely typos — check them under Side by side →
> Problems only.

The numbers below it are:

| Number | Meaning |
|---|---|
| Rows in file | Data rows in the file, not counting empty rows. |
| Converted | Rows in the import file. Can be higher than *Rows in file*: a customer with several different phone numbers gets one row per number. |
| Bad rows | Rows NOT imported, because the required phone is missing or can't be read. |
| Duplicates removed | Rows identical to an earlier row in **every** column. Same phone with a different date is not a duplicate. |
| Empty rows | Completely blank rows, skipped. |

**Downloads:**
- **Converted file:** the file to upload to the target system.
- **Rejected rows:** every bad and duplicate row with its reason and original values. A value that Excel would
  run as a formula (starting with `=`, `+`, `-` or `@`, e.g. `=HYPERLINK(…)`) starts with an apostrophe here, so
  opening the file never runs anything.
- **Summary:** counts and reasons as text.
- **Original file:** exactly what was uploaded.
- **Prepare side-by-side Excel:** builds the comparison as a colour-coded workbook.

**Tabs:**

- **Side by side:** your original file on the left, the converted result on the right, cell by cell.
  - **Filters:**
    - **Rows with changes:** rows where any value was changed.
    - **Problems only:** bad rows, duplicates, values left blank and **repaired** values.
    - **Bad rows**, **Imported rows** and the others: rows with that status.
  - **Find:** search for any value, or a row number like `#14`.
  - **Colours:**
    - blue: reformatted;
    - purple: built from part of or several columns;
    - grey: default or fixed value;
    - green: fix you approved;
    - **orange: repaired from a likely typo — check it;**
    - yellow: invalid, left blank;
    - red: invalid or missing, row rejected.
  - **Original values:** with **Show original under changed values** on, the original is shown under each changed
    value. Hover over any cell for details.
  - **Extra rows:** these are marked `N +`, meaning another row made from file row N, one per phone number. The
    number that row was made from is underlined on the left.
- **What changed:** for every field, how many values were unchanged, reformatted, built, defaulted, fixed,
  repaired, left blank or rejected, with examples. Below it:
  - **Problems found**, grouped by reason;
  - **How full each column is**.
- **Converted file:** a preview of the import file.
- **Bad & duplicate rows:** the rejected rows with reasons.
- **Mapping used:** the exact mapping, notes and options this import ran with.

**After the import:**
- **Check this import for mistakes:** the same checks as before importing, on every row.
- **Try to fix … unconvertible values with Claude** (only when Claude is configured and values failed):
  1. Click **Suggest fixes**. Claude proposes reformatted versions of values the converter couldn't read, for
     example a phone written as words.
  2. Each suggestion is re-checked. Suggestions that change the data instead of reformatting it are refused.
  3. **Apply fixes and re-run** creates a new import with the approved fixes.
- **Re-run this import:** runs the same file and mapping again with the current rules, as a new import. The
  original stays as it was.

## How values are converted

### Phone numbers

- **US only; +1 assumed.** Any punctuation, spacing, extension, label (`Cell:`), Excel artifact (`5165550100.0`,
  `="5165550100"`) or letters typed for digits (`516-555-O1OO`) is handled.
- **Typos are repaired when exactly one US number fits**, without adding or changing any digit:
  - an extra digit is removed (a digit typed twice, or one extra at the start or end);
  - a country code typed in front of a US number is removed (`+44 516 555 0100`).

  Every repair is imported **and shown in orange**. Check them: a wrong guess texts a stranger.
- **Area codes are never assumed or changed.** A number with no area code, or with an area code that looks like
  a typo (`514…` in a file of `516` numbers), is a bad row with its reason. Fix it in the original file if you know
  the right number.
- **Not guessed:** when two numbers fit equally, the row is a bad row and the reason lists both options, e.g.
  "could be (516) 946-1961 or (516) 946-1966".
- **Bad rows with a reason:** missing area code, too few or too many digits, an area code that isn't in use,
  non-US numbers (with the country), and Excel scientific notation that already lost digits.
- **Several phone numbers per customer**, in several phone columns (Phone, Phone 2, Mobile, Home…) or typed into
  one cell (`516-555-0100 / 917-555-1234`, `H: 516-555-1212 C: 516-555-0100`):
  - each **different** number becomes its own row, with the other columns copied from the same file row;
  - the same number twice counts once;
  - an invalid extra number is skipped, and the row still imports with the customer's other number(s);
  - a number labelled fax is skipped when the customer has another number (a fax can't receive texts);
  - the mapping page says how many cells hold several numbers, and in *Side by side* the number each row came
    from is underlined.
- **Columns about a phone** (`phone4MarketingPreference`, Phone Type, SMS Opt In) are never treated as numbers.

### Dates and times

- **Any common format works:** `10/9/2026 3:45 PM`, `2026-10-09T15:45:00Z`, `Oct 9, 2026`, `9-Oct-26`, Excel
  serial numbers, Unix timestamps and more.
- **Ambiguous order:** `03/04` is read month-first unless the column is clearly day-first. You can force it with
  **Option** = `DMY`.
- **Activity fields:** a date with no time becomes `12:00 AM`. Seconds are dropped.
- **Which field a date goes to (one last date per row):** a column that names its activity goes to that field
  (*Last Visit* → transactions_last, *Last Call* → calls_last). If there is none, a column named only *Date*,
  *Start Time* or *Timestamp* is used; if there is none of those either, a creation date (*Date Created*, *Sign Up
  Date*, *First Visit*) - better than no date. That one date goes to **calls_last** in a call log (*From*/*To*,
  *Caller ID*, *Duration*…), otherwise to **transactions_last**. Expiry and opt-in dates are never used. These are
  marked *low*: check them, and change them under **Columns in your file** if needed.
- **Birthday / anniversary** become `MM/DD/1600` when there is no real year. That includes:
  - month and day only: `09/12`, `Sep 12`, `12-Sep`, `--09-12`;
  - placeholder years: `0000`, `XXXX`, `1900`;
  - a year shared by most of the column, e.g. a year Excel added to dates typed without one;
  - years in the last two years or in the future.

  Separate *Birth Month* and *Birth Day* columns are combined automatically.
- **Only birthday and anniversary get a placeholder year.** Every other date is imported as written, also
  `01/01/1900`, `01/01/1970` or a date in the future. *Check for mistakes* warns about future activity dates.

### Mapped by hand only (✋)

`consent` and `last_contacted` (marked ✋) are left blank unless **you** map a column to them for this file, in
the field grid or with **Goes to** under **Columns in your file**. Nothing automatic fills them: not the rules,
not Claude, not a default and not a saved template (templates leave them out, so you choose again for each file).

- **consent:** only map a column that really records the customer's consent. Values become `true` (subscribes the
  customer) or `false` (opts them out); Y/N, yes/no, 1/0, opted in/out and subscribed/unsubscribed are understood.
  Anything else is left blank, which changes nobody's consent.
- **last_contacted:** when *we* last texted the customer (e.g. *Last Campaign Sent*). Files almost never have it.

## Templates, target format, users

- **Templates:**
  - lists saved mappings, with columns, how often each was used, and who created or updated it;
  - you can view, rename, delete (creator or admin only) or download any template as JSON;
  - a template is applied automatically to files with exactly the same columns, in any order or capitalisation;
  - a similar layout (75%+ of the same columns) starts from that template's rules.
- **Target format:** a read-only description of every field of the import format and its rules.
- **Users** (administrators only, and only while logins are on): add users, reset passwords, make or remove
  administrators, delete users.

## Common questions

**A reason says *Excel error (broken formula in the client's file)*.** The cell held `#REF!`, `#VALUE!`,
`#DIV/0!` or a similar code: a formula in the client's spreadsheet was broken when they exported it, so there is
no value to import. If that column matters, ask the client for a fresh export.

**A birthday shows `1600`.** That's intended: it means "month and day known, year unknown". The target system
expects this.

**A phone is orange.** It was repaired from a likely typo. Check the original underneath. If the repair is wrong,
fix the number in the original file and re-run.

**Converted is higher than Rows in file.** Some customers have several different phone numbers; each gets its
own row.

**A column went to the wrong field.** Under **Columns in your file**, change **Goes to** or tick **Skip**, then
start the import again. With **Remember this mapping** on, the template is updated for next time.

**The header row is wrong / column names are numbers or a customer's details.** Open **File options and raw
preview**, set the header row (`0` = the file has no header row) and click **Re-read file**.

**An import says *interrupted*.** The app restarted while it ran. Open it and click **Re-run this import**.
