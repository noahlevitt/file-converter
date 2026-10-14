# Handoff

For whoever picks this repository up next: a person, or Claude in a new chat. It records what the code and the
other documents don't: how changes are made and delivered, the decisions the business made, and what is still
open. It also explains how to start a separate project from this code while this one keeps being updated.

**State:** version 0.21.2 (see [CHANGELOG.md](CHANGELOG.md)), about 1,000 tests. Repository:
github.com/noahlevitt/file-converter. Owner: Noah Levitt. Last updated 2026-10-14. Update this file with every
release (section 4 in particular).

What the tool does, and how, is in the [README](../README.md) and the documents it lists. Read
[ARCHITECTURE.md](ARCHITECTURE.md) before changing code and [KNOWN_ISSUES.md](KNOWN_ISSUES.md) before calling
something a bug.

## 1. How changes are made

**Commits**
- Author and committer: **Noah Levitt <noah@thelevittfamily.com>**, for every commit.
- No AI attribution of any kind: no `Co-Authored-By` lines, no session links, and no "Claude" or "Joey" in commit
  messages. Describe the app's Claude features in commit messages as "the AI suggestions". (The code and the
  documents do name Claude, the optional AI the app uses; that is fine.) Check before delivering:
  `git log vPREVIOUS..main --format='%B' | grep -ci "claude\|joey\|co-authored"` must print 0.
- One commit per changed file, message in the imperative ("Add …", "Fix …", "Document …").
- Setting the identity for a session:

  ```bash
  export GIT_AUTHOR_NAME="Noah Levitt" GIT_AUTHOR_EMAIL="noah@thelevittfamily.com"
  export GIT_COMMITTER_NAME="Noah Levitt" GIT_COMMITTER_EMAIL="noah@thelevittfamily.com"
  ```

**Versions**
- `converter/__init__.py` → `__version__`, shown in the app's sidebar. Patch (0.21.1 → 0.21.2) for fixes, minor
  (0.20 → 0.21) for new behaviour. Changes to documents or development scripts only: no new version.
- Every version gets a [CHANGELOG.md](CHANGELOG.md) entry written for the people using the app, and a git tag
  `vX.Y.Z` on its last commit (the "Version X.Y.Z" commit).

**Checks before a release** (all of them, every time)
1. The whole test suite: `.venv/bin/python -m pytest -q` (about 30 seconds).
2. New tests fail on the previous version: copy them into `git worktree add -f /tmp/old vPREVIOUS`, run them
   there, then `git worktree remove --force /tmp/old`.
3. The regression check: `python scripts/regression/regression.py check vPREVIOUS`. It converts 406 files with
   the old and the new code and compares the results. Expected: `identical`. Any difference is either intended
   (and the changelog says so) or a regression. The changelog's "All 406 test files give identical results"
   line comes from this check.
4. Changes to the web pages: run the real `app.py` with Streamlit's AppTest (see `tests/test_drafts.py`). Pages
   other than New import and Imports can't be opened with `AppTest.switch_page`; instead give
   `AppTest.from_file` a small script in the repository root that makes `st.navigation(...).run()` run the wanted
   page:

   ```python
   import runpy, streamlit as st
   real = st.navigation
   def nav(pages, **kw):
       real(pages, **kw)
       page = next(p for p in pages if p.url_path == "numbers")   # the page to test
       return type("Nav", (), {"run": lambda self: page._page()})()
   st.navigation = nav
   runpy.run_path("app.py", run_name="__main__")
   ```
5. A feature that decides data (which number, which date, which consent): have it tested by someone, or a separate
   Claude agent, that did not build it, working only from the user-facing documents and trying to break it.
   Testing 0.21.0 that way found six bugs (fixed in 0.21.1).

**Documents** — every change in behaviour updates, as they apply: README, docs/USER_GUIDE, docs/ARCHITECTURE
(modules table and "Key behaviours"), docs/KNOWN_ISSUES, docs/DEVELOPMENT (test table), docs/OPERATIONS and
`.env.example` (settings), docs/STACK (libraries), docs/CHANGELOG, this file, and the overview at the top of each
changed source file. A pass that only updates documents changes no code: compare the files' syntax trees with
their docstrings removed before and after.

**Delivery** (when the changes can't be pushed directly)
- Changes go to Noah as a git bundle, `file-converter-updateN.bundle`. The last one was update28, so the next is
  `file-converter-update29.bundle`.

  ```bash
  tags=$(git tag --list 'v0.*' | sort -V | awk -F. '$2>=6')
  git bundle create ../file-converter-updateN.bundle main $tags ^v0.5.0
  ```
- Before sending it, rehearse: clone GitHub into a temporary folder, `git pull --ff-only` the bundle, fetch its
  tags, compare the files with the working copy and run the tests there.
- Noah installs it on a Mac (zsh); his clone is `~/Downloads/file-converter`. Give him complete commands that he
  can paste as they are: start with the `cd`, put **no `#` comments** in them (zsh stops at a `#` followed by an
  apostrophe), add `.venv/bin/pip install -r requirements.txt` when requirements.txt changed, and say which line
  `git log -1` should print:

  ```
  cd ~/Downloads/file-converter
  git pull ~/Downloads/file-converter-updateN.bundle main
  git fetch ~/Downloads/file-converter-updateN.bundle 'refs/tags/*:refs/tags/*'
  git log -1 --format='%h %s'
  git push origin main --tags
  ./run_local.sh
  ```
- The claude.ai project "Noah" keeps a copy of the README (`claude/file-converter-README.md`); replace it after
  each release.

## 2. Decisions the business made (this project)

Each of these was decided by Noah. Don't reverse one without asking; for a new project, ask again (section 5).

| Area | Decision |
|---|---|
| Phone numbers | US numbers only; no country code means +1; the output is 10 digits. |
| Several numbers | Every different phone number of a customer gets its own row, from several phone columns or several numbers in one cell. There is no option to turn this off. Fax numbers are never imported. |
| Guessing data | Features that guess or repair data are approved one by one. Approved: removing a foreign country code in front of a US number; one digit too many; an area code that doesn't exist, one keystroke from an area code the file uses; a missing area code filled from the file's main area code (80%+ of at least 5 numbers); an Area Code column combined with 7-digit numbers (0.21.0). Never: changing a real area code (514 stays a bad row in a 516 file). Every repair is imported and marked orange. |
| Duplicates | A row is a duplicate only when every column matches an earlier one; the same phone with a different date is not. |
| Bad rows | Not imported, counted, and each one has a reason. |
| Consent, last_contacted | Mapped by a person only, for each file; never by rules, templates, Claude or defaults. |
| Dates | The placeholder year 1600 only for birthday and anniversary (month and day without a real year); every other date is imported as written. |
| Files without a header row | No templates. |
| Logins | Off for now (`AUTH_MODE=none`); may come back. |
| Number history | Where each customer's consent came from (file, row, column, import) is kept after the files are deleted. How long was not discussed: it is kept forever by default, and `NUMBER_RECORD_DAYS` limits it. |

## 3. How work was planned

- **Bug review.** A 25-item review of the conversion was fixed in four batches: 0.16.0 (#3, #10, #11, #12), 0.17.0
  (#4, #16, #17, #18), 0.18.0 (#6, #7, #8, #13, #14) and 0.19.0 (#9, #15). The status of #19–#21 wasn't
  recorded. The changelog describes what each version changed.
- **Feature list.** An 11-item list of possible features: #1 (Area Code column) and #11 (number history) were
  built in 0.21.0.

## 4. Open items

- **Decision pending:** 0.21.2 also gives `+1 555-0177` (a 7-digit number with the country code) the file's main
  area code, like `555-0177`. Noah has not confirmed it. To undo: in `normalizers._repair_phone`, rule 4, use
  `digits` instead of `seven`, and update the README's repair table, the changelog and the test
  `test_country_code_and_7_digits_get_the_files_main_area_code`.
- **Not approved or not done** from the 25-item bug review: #1, #2, #5, #22, #23, #24, #25 (and #19–#21, status
  unknown). From the feature list: #2–#10. The descriptions of these items were only in the chat where they were
  made and were not kept; ask Noah for them, and record them here.
- Limits that are known and accepted: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## 5. Starting a new project from this code

This project keeps being updated, so start the new one **with this one's history**. Fixes made here can then be
merged into it.

```bash
git clone https://github.com/noahlevitt/file-converter.git NEW-PROJECT
cd NEW-PROJECT
git remote rename origin upstream
git remote add origin https://github.com/noahlevitt/NEW-REPO.git
git push -u origin main
```

- Push only the new project's own tags (`git push origin v1.0.0`), not `--tags`: the clone also holds this
  project's tags (v0.6.0 … v0.21.2), which mean nothing in the new one. Give the new project its own version
  line and changelog, and start its changelog with the base: "Based on file-converter 0.21.2".
- Taking later changes from this project: `git fetch upstream`, then `git merge upstream/main` for everything, or
  `git cherry-pick <commit>` for one fix. One commit per file (section 1) keeps cherry-picks small. Expect
  conflicts where the new project changed the same files.
- What carries over as it is: reading files (`reader`, `sheetsize`, `sheeterrors`), dates (`dateparse`), value
  cleaning (`normalizers`), the engine, transforms, mappings and templates, background imports, the comparison
  view, storage and the web app. The engine reads its rules from the schema:
  `tests/fixtures/example_contact_schema.yaml` is a second, different format that works with the same code.
- What is specific to this import format: `schemas/target_schema.yaml` (the fields, formats, `manual_only`
  consent and last_contacted); the auto-mapping rules in `converter/heuristics.py` (which date goes to
  transactions_last or calls_last, birthday/anniversary splits, consent columns); the number history (built
  around a phone field and a field named consent); and the tests that use `real_schema`.
- The decisions in section 2 belong to this project. Confirm each one for the new project instead of assuming
  it, and keep the "approve guessing features one by one" rule unless told otherwise.
- Give the new project its own copy of this file, rewritten for it, and its own claude.ai project, so the two
  don't mix.

## 6. Where things live

| What | Where |
|---|---|
| The target import format | `schemas/target_schema.yaml` |
| Settings | `.env` (from `.env.example`); every setting in [OPERATIONS.md](OPERATIONS.md) |
| Data (database, each import's files) | `data/` — never in git |
| Sample files for trying the app | `sample_data/` (made by `scripts/make_sample_files.py`) |
| Regression files and check | `scripts/regression/` (`corpus.zip`: 400 generated files, no real customer data) |
| Users from the command line | `scripts/manage_users.py` |
| Python | 3.11 or newer; the Docker image uses 3.12 |
