# Known issues and limits

Behaviour that is intentional but surprising, and small quirks found while documenting the code. None of them
produces a wrong import file silently. Each entry says where it lives, in case it should be changed later. Broader
limits (landlines, browser refresh, restarts, speed) are in the README under "Known limits".

## Conversion

| # | Issue | Effect | Where |
|---|---|---|---|
| 1 | **Several numbers in one cell** (`516-555-0100 / 516-555-1212`) give one number, not one row per number. A number labelled mobile/cell wins, then an unlabelled one, then home/work, fax last. | The other numbers in that cell are not imported. Several numbers in *separate columns* do get one row each. | `normalizers._parse_phone` |
| 2 | **Mobile vs landline can't be told apart** from the number alone. | Every different valid number gets a row, including landlines that can't receive texts. A carrier lookup (paid per number) would be needed. | — |
| 3 | **Phone typo repair can still be wrong** for the two repairs that remain (an extra digit removed, a foreign country code dropped): e.g. an 11-digit value whose extra digit was really a wrong digit, so the remaining 10 digits form someone else's number. Area codes are never changed or filled in (removed in 0.9.0). | Repaired numbers are imported and marked orange; check them under *Problems only*. Numbers without an area code are bad rows. | `normalizers._repair_phone` |
| 4 | **Birthdays in the last two years** are treated as "no year" (`1600`). | A real anniversary from last year loses its year (month and day are kept). Chosen because Excel silently adds the current year to dates typed without one. | `normalizers.normalize_date` |
| 5 | **A whole birthday column sharing one year** (50%+ of 10+ values, or 80%+ of 5+) is treated as a placeholder year. | A small file of genuinely same-year birthdays would get `1600`. Very unlikely for real customers. | `normalizers._placeholder_year` |
| 6 | **Intersections in a one-line address** (`Main St & 1st Ave`) keep only the first street in address line 1. | Only relevant for formats with address fields (not the current import format). | `transforms._ADDRESS_TAGS` |
| 7 | **The whole file is held in memory** while converting. | Very large files (millions of rows) need a server with more memory. About 150,000 rows took 25 seconds in testing. | `engine.convert` |

## Mapping

| # | Issue | Effect | Where |
|---|---|---|---|
| 8 | **A column about a phone could still be matched in step 1** of the automatic mapping if its *values* look like phone numbers (e.g. a "Phone Verified" column that holds numbers). Step 2 never adds such columns. | Shows on the mapping page; fix with *Goes to* / *Skip*. | `heuristics.auto_map` |
| 9 | **The full-name split doesn't check for consent-looking headers.** "Subscriber Name" can be split into first/last name. | Only relevant for formats with name fields. Consent itself can never be mapped. | `heuristics.auto_map` step 4 |
| 10 | **Templates match on the set of column names**, ignoring order and capitalisation. | Two systems exporting identical column names with different meanings would share a template. Check the mapping page. | `mapping.header_fingerprint`, `storage.find_template` |
| 11 | **Old templates keep old choices.** A template saved before a rule changed (e.g. one that mapped `phone4MarketingPreference`) is applied as saved. | Fix the mapping once and save with *Remember this mapping*, or delete the template. Phone fields using *First non-empty* are upgraded automatically. | `services.initial_mapping` |

## Claude (optional features)

| # | Issue | Effect | Where |
|---|---|---|---|
| 12 | **Phone masking covers the review's sample rows only.** The up-to-15 sample values per column (`AI_SAMPLE_VALUES`) sent with every Claude call, and the failed values sent by *Suggest fixes*, are not masked. | Lower `AI_SAMPLE_VALUES` or leave Claude off if no customer data may leave the company. | `ai._profile_payload`, `checks.mask_phone` |
| 13 | **Suggested fixes for values with a spaced three-character group of look-alike letters** (`516 5OO 0100`) are refused as having too few digits. `516-5OO-0100` works. | Rare; the value stays a bad row. | `ai.accept_repair` |

## App and operations

| # | Issue | Effect | Where |
|---|---|---|---|
| 14 | **`showErrorDetails = false`** (Streamlit 1.65 reads it as `"stacktrace"`): on an unexpected error users see the exception type and traceback, but not the message. | Set `showErrorDetails = "none"` in `.streamlit/config.toml` to hide errors from users completely. Full details are always in the server log. | `.streamlit/config.toml` |
| 15 | **Cancel takes effect at the next progress check:** the start of each field and every 2,000 rows. Not while the file is being read or the output written. | A cancel can take a few seconds on big files. | `jobs.ProgressTracker` |
| 16 | **If saving an uploaded file fails** (e.g. disk full), the import record stays *queued* until the next restart marks it *interrupted*. | Free disk space, restart, re-upload. | `storage.Store.create_import` |
| 17 | **Imports running during a restart** are marked *interrupted*. | Re-run them with one click. | `storage.mark_interrupted` |
| 18 | **Users page:** the "can't delete your own account" message only flashes briefly, because the page reloads right after. | Cosmetic. | `app.page_users` |
| 19 | **The "times included a time zone" count** in the mistake check also counts dash-separated dates ending in a year (`01-15-2024`). | The info line's number can be too high; conversion is unaffected. | `checks._ZONE_HINT_RE` |
| 20 | **No database migrations.** Tables are created if missing but never altered. | A future column needs an explicit `ALTER TABLE` (see DEVELOPMENT.md). | `storage.Store` |
| 21 | **Imports converted before an update keep their old output.** | Use *Re-run this import* to apply new rules. | — |
| 22 | **The light/dark toggle uses a browser-side workaround.** Streamlit can't switch theme per user from the server, so the toggle makes the browser tab report "dark" or "light" to Streamlit's automatic theme (no page reload, so nobody is signed out). It depends on Streamlit internals. | If a future Streamlit version changes them, the toggle stops having an effect (nothing else breaks); the app then follows the computer's setting. On a fresh page load the computer's setting shows for a moment before the saved choice applies. | `app.py` "Light / dark mode", `.streamlit/config.toml` |
