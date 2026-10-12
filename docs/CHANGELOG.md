# Changelog

What changed in each version. The running version is shown in the app's sidebar. Each version is tagged in git
(`vX.Y.Z`).

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
