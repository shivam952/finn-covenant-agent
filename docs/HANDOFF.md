# Finn – project handbook (handoff)

*For Carlos. This document should be enough to understand the whole project without asking anyone:
what the problem is, how each contract is read, how the code is built and why, what went wrong on
the way, and how to run, change and extend it. The README is the short version; this is the long one.*

Repository: <https://github.com/shivam952/finn-covenant-agent>

---

## Contents

1. [The task in one paragraph](#1-the-task-in-one-paragraph)
2. [Domain primer: covenants in five minutes](#2-domain-primer-covenants-in-five-minutes)
3. [The material and the correct answers](#3-the-material-and-the-correct-answers)
4. [How each contract is read (and the traps)](#4-how-each-contract-is-read-and-the-traps)
5. [Architecture](#5-architecture)
6. [The pipeline step by step](#6-the-pipeline-step-by-step)
7. [Data model](#7-data-model)
8. [The mail agent](#8-the-mail-agent)
9. [The reply email](#9-the-reply-email)
10. [Configuration and the no-hardcoding rule](#10-configuration-and-the-no-hardcoding-rule)
11. [The LLM: model choice, client, cache, cost](#11-the-llm-model-choice-client-cache-cost)
12. [Tests](#12-tests)
13. [How it was built, and what the live runs found](#13-how-it-was-built-and-what-the-live-runs-found)
14. [Running it, and the demo](#14-running-it-and-the-demo)
15. [How to extend it](#15-how-to-extend-it)
16. [Known limitations and next steps](#16-known-limitations-and-next-steps)
17. [Troubleshooting](#17-troubleshooting)
18. [Glossary (German terms)](#18-glossary-german-terms)

---

## 1. The task in one paragraph

The case study (Werkstudent, due 9 Nov 2026) asks for an agent called **Finn**, a "digital financing
officer" for a municipal utility (*Stadtwerke Musterstadt GmbH*). The head of finance emails Finn
the business plan `Wirtschaftsplan_2027.xlsx`. Finn must read four loan contracts (PDF), find out
which ones contain **financial covenants**, read each covenant's definition, limit and location,
take the needed values from the plan, **calculate every ratio in code** for plan year 2027
(test date 31.12.2027), compute the headroom, and reply to the email with a short summary, a table
per contract and covenant (value, limit, headroom, met yes/no) and **the source of every number**
in the contract and in the plan. The core rule of the brief: *the language model reads, structures
and writes; it does not calculate.* In the interview demo the reviewers send the order from their
own address and wait for the reply, so a real mailbox is required.

---

## 2. Domain primer: covenants in five minutes

A **loan contract** (*Kreditvertrag, Darlehensvertrag*) often obliges the borrower to keep certain
financial ratios within limits for the life of the loan. These obligations are **financial
covenants**. If one is breached, the bank usually gets rights (renegotiation, extra security,
termination).

The ratios in this case:

| Ratio | German | Formula (generic) | Direction |
|---|---|---|---|
| Equity ratio | *Eigenkapitalquote* | equity / total assets | at least |
| Economic equity ratio | *Wirtschaftliche Eigenkapitalquote* | (equity + part of special items) / total assets | at least |
| Debt service coverage ratio (DSCR) | *Schuldendienstdeckungsgrad* | cash earnings available for debt service / debt service | at least |
| Dynamic gearing | *Dynamischer Verschuldungsgrad* | net financial debt / EBITDA | at most |

What makes the task hard is that **every contract defines its terms its own way**. "EBITDA",
"equity" or "debt service" mean slightly different things in each contract, and the definitions
are spread over a definitions section (§ 1), the covenant clause and annexes. Typical details:

- Is a **subordinated shareholder loan** (*Gesellschafterdarlehen mit Rangrücktritt*) debt or not?
- Are **special items for investment grants** (*Sonderposten*) counted as equity, and at what share?
- Does interest include the **accretion of pension provisions** (*Aufzinsung*), which is not cash?
- Does debt service include a **voluntary prepayment** (*Sondertilgung*)?
- How is the result **rounded**, and is a breach "on plan figures" a default or just an early warning?

A finance officer reads each contract exactly; Finn has to do the same. **Headroom** is the
distance between value and limit (in percentage points for percent covenants). The **stress
test** answers "how much worse may it get before the covenant breaks?"

---

## 3. The material and the correct answers

All data is synthetic. Values in the plan are in thousands of euros (T€).

| File | Content |
|---|---|
| `data/samples/Wirtschaftsplan_2027.xlsx` | Business plan: income statement (*GuV*), planned balance sheet (*Bilanz*), loan schedule (*Finanzierung*); columns Ist 2025, Hochrechnung 2026, Plan 2027 |
| `data/contracts/Vertrag_1…4_*.pdf` | Four loan contracts, 13 pages each |

**Plan 2027 base values** (from `tests/expected_2027.json`):

| Item | T€ | Item | T€ |
|---|---|---|---|
| Net income (*Jahresüberschuss*) | 10,700 | Equity | 96,100 |
| Income taxes | 3,200 | Special items (*Sonderposten*) | 22,800 |
| Interest expense (income statement) | 5,100 | Total assets | 313,200 |
| … of which accretion of pensions | 170 | Liabilities to banks | 148,200 |
| Interest income | 300 | Shareholder loan (subordinated) | 8,000 |
| Depreciation | 21,500 | Cash | 12,600 |
| **EBITDA** = 10,700 + 3,200 + 5,100 − 300 + 21,500 | **40,200** | Interest on financial debt (loan schedule) | 4,930 |
| Scheduled repayments | 18,750 | Voluntary prepayment (*Sondertilgung*) | 3,000 |

**The answer key** (hand-derived, in `tests/expected_2027.json`):

| Contract | Covenant | Calculation | Reported | Limit | Headroom | Stress |
|---|---|---|---|---|---|---|
| 1 Sparkasse | none | – | – | – | – | – |
| 2 Förderbank | Economic equity ratio | (96,100 + 50 % × 22,800) / 313,200 = 34.3231 % | 34.32 % (no rounding rule) | ≥ 27.5 % | 6.82 pp | extra loss of 29,476 T€ |
| 3 Landesbank | DSCR | (40,200 − 3,200) / (4,930 + 18,750) = 37,000 / 23,680 = 1.5625 | 1.56 | ≥ 1.30 | 0.26 | EBITDA −15.5 % (6,216 T€) |
| 4 Syndicated | Equity ratio | 96,100 / 313,200 = 30.6833 % | 30.7 % (1 decimal) | ≥ 30.0 % | 0.7 pp | extra loss of 3,057 T€ |
| 4 Syndicated | Dynamic gearing | (148,200 − 12,600) / 40,200 = 135,600 / 40,200 = 3.3731 | 3.37 | ≤ 3.50 | 0.13 | EBITDA −3.6 % (1,457 T€) |

**Trap values** that must not appear (each is what a careless reading gives):

| Trap | Value | Mistake |
|---|---|---|
| DSCR with prepayment | 1.39 | counting the 3,000 T€ *Sondertilgung* as debt service |
| DSCR with income-statement interest | 1.55 | using 5,100 (incl. accretion) instead of loan interest |
| DSCR without shareholder loan | 1.65 | leaving the shareholder loan out of debt service |
| Dynamic gearing with shareholder loan | 3.57 | counting the subordinated loan as debt |
| Equity ratio (contract 4) with *Sonderposten* | 37.96 % | treating special items as equity |

---

## 4. How each contract is read (and the traps)

### Contract 1 – Sparkasse Musterstadt, investment loan 4471 203 118
**No financial covenants.** The preamble mentions an equity ratio of 28.4 % and a dynamic gearing
of 2.9 as of 31.12.2019, but only as the basis of the credit decision; there is no obligation to
maintain them. The loan is secured by a municipal guarantee (§ 10). Finn lists it under
"contracts without financial covenants", with the reason and a quote from the preamble. *Trap:*
extracting the preamble ratios as covenants.

### Contract 2 – Förderbank Musterland, promotional loan FD-2022-08817
**Economic equity ratio ≥ 27.5 %** (§ 13 Abs. 1, p. 6), tested at the balance-sheet date.
Economic equity = balance-sheet equity (§ 266 Abs. 3 A HGB) **+ 50 % of the special items**, minus
called-up but unpaid capital (the plan has no such line, so 0). Denominator: total assets.
**No rounding rule**, so the unrounded value is compared. A shortfall on plan figures is not a
default by itself; the parties start talks (§ 13 Abs. 3). *Trap:* 100 % of the special items, or
rounding although the contract doesn't say so.

### Contract 3 – Landesbank Mittelland, investment loan LBM-INV-2025-0412 (two tranches)
**DSCR ≥ 1.30** (§ 13 Abs. 1, p. 5), per fiscal year, rounded to 2 decimals.
- Numerator: **EBITDA minus income taxes** (Anlage 3). EBITDA is built from the income statement:
  net income + taxes + interest expense (the whole line) − interest income + depreciation ±
  extraordinary items (none in the plan).
- Denominator: **debt service** = interest on financial debt **without the accretion of provisions**
  + **scheduled** repayments **without voluntary prepayments**; financial debt explicitly
  **includes shareholder loans regardless of subordination**. So the loan schedule's interest
  (4,930) and scheduled repayments (18,750), both including the shareholder loan row, are used.
- Plan breach: the borrower must present measures within 30 days; cure by equity injection possible.

*Traps:* the prepayment (1.39), the income-statement interest (1.55), dropping the shareholder loan
(1.65). Note the asymmetry: contract 4 excludes the shareholder loan from debt, contract 3
includes it in debt service.

### Contract 4 – Syndicated loan, EUR 64 m (agent: Weserland Bank AG)
Two covenants (§ 14 Abs. 1), both rounded per § 14 Abs. 2:
- **Equity ratio ≥ 30.0 %** (lit. a, 1 decimal): balance-sheet equity only. Special items,
  provisions and shareholder loans are explicitly *not* equity (§ 1). 30.6833 % → 30.7 %.
- **Dynamic gearing ≤ 3.50** (lit. b, 2 decimals): net financial debt / EBITDA. Financial debt =
  loans, bonds, finance leases, recourse factoring, similar, **but not subordinated shareholder
  loans**; minus freely available cash. EBITDA as in contract 3.
- On plan figures, a deviation is no termination event but obliges talks on request of the
  majority lenders (§ 14 Abs. 3).

Both are **tight** (relative headroom below 10 %). *Traps:* the shareholder loan in debt (3.57),
special items in equity (37.96 %), and subtracting the shareholder loan from a bank-debt figure
that never contained it (3.17; this actually happened in a live run, see section 13).

---

## 5. Architecture

**Decision: a fixed pipeline, not an agent loop.** The job is an audit with the same steps every
time. Code orchestrates; the LLM is called at exactly four points and only returns structured
JSON that code validates. This makes every run reproducible, testable offline, and explainable.

```
             ┌──────────────────────── code ─────────────────────────┐
 email ──►  loop protection ──► ① INTAKE (LLM) ──► decision rules
                                   OrderIntent        run / missing_attachment / wrong_attachment /
                                                      unrelated / needs_clarification / no_reply
 run ──►  read plan (openpyxl + formula evaluator, sheets by role, 4 reconciliation checks)
      ──►  ② EXTRACT per contract (LLM, cached) ──► verify quotes, limit-in-quote, ratio structure,
                                                    normalise (drop unused quantities, weight-0 lines)
      ──►  ③ MAP components → plan cells (LLM, no values, cached) ──► coverage, double counting,
                                                    empty quantities, carve-outs
      ──►  CALCULATE (pure code): ratio, rounding, headroom, met, stress test
      ──►  ④ SUMMARY (LLM) ──► per-sentence number and status check, else template
      ──►  reply HTML + text + audit JSON ──► SMTP, same thread
```

**Why no RAG:** each contract is 13 pages and fits into one call; saying "this contract has no
covenants" requires reading all of it, which retrieval cannot guarantee.

**Why the LLM never sees plan values:** if it can't see the numbers, it can't change them. It
only chooses *which cells* represent a contract term (e.g. `Bilanz!D22`), based on labels and
column headers.

---

## 6. The pipeline step by step

`src/finn/pipeline.py::run_check()` is the only place that knows the order.

### 6.1 Intake (`intake.py`, `prompts/intake.txt`)
The LLM turns the free-text email into an `OrderIntent`: intent (`covenant_check`,
`acknowledgement`, `other`, `unclear`), plan year, test date, focus contracts, reply language,
salutation, unsupported requests, missing info, deadline. `decide()` then applies **fixed rules
on structured data** (no keyword matching): an `.xlsx` attached → run; none → missing or wrong
attachment; `other` → unrelated; a plain thank-you → no reply. If the LLM fails, a fallback intent
is used so the mail is still answered.

### 6.2 Reading the plan (`plan/reader.py`, `plan/layout.py`, `plan/checks.py`)
- Sheets are found by **role** (income statement, balance sheet, loan schedule) through aliases
  and content keywords in `config/plan_layout.toml`, not by fixed names.
- The plan column is the header with a 4-digit year and a word starting with "plan".
- Formulas are evaluated with openpyxl's formula **tokenizer** and a small recursive-descent
  evaluator (no regular expressions). Unsupported functions fall back to Excel's cached value,
  with a note.
- **Sign rule:** the income statement enters expenses as negative numbers. A negative input line
  that a subtotal adds up (or a "davon" sub-line) is an expense and is read as a positive amount.
- Every cell becomes a `PlanItem` with id (`GuV!D13`), label, role, value, whether it is a total
  and which cells it depends on (`depends_on`, transitive), and for "davon" lines the parent
  (`part_of`).
- **Four blocking reconciliation checks** (declarative in the config): assets = liabilities; bank
  debt in the loan schedule = balance sheet; interest reconciliation; EBITDA identity. If one
  fails, Finn replies "plan inconsistent" and calculates nothing.

### 6.3 Contract extraction (`contracts/`, `prompts/extract_covenants.txt`)
- `pdf_text.py` extracts text per page (PyMuPDF) and marks pages (`=== Seite N ===`).
- The LLM returns a `ContractCovenants` object: whether there are covenants, the covenants
  (name, numerator, denominator, operator, limit, unit, rounding, test basis, plan-basis note,
  sources) and the **quantities** they use, each broken into signed, weighted **components**
  (e.g. EBITDA = net income + taxes + interest − interest income + depreciation). Components can
  reference other quantities (`ref`) and can be marked as a **carve-out**.
- Code then:
  1. **verifies every quote** on its cited page (normalised whitespace and hyphenation; if found
     on another page, the page number is corrected and noted);
  2. checks that **each limit appears in its own quote** (catches limits read from a pricing grid);
  3. checks the **ratio structure** (a numerator may not contain its denominator);
  4. on 2 or 3: **one corrective re-read** with the findings; a structural error that persists
     makes this contract fail (the others are still reported);
  5. **normalises**: drops weight-0 components and quantities no covenant uses.
- Result is cached per PDF + prompt + model (`cache/covenants_*.json`).

### 6.4 Mapping (`mapping.py`, `prompts/map_terms.txt`)
- For every leaf component, the LLM chooses the plan cell ids whose sum represents it, given the
  component's definition (with its inclusion/exclusion rules) and a **catalog of cells without
  values** (id, sheet, label, column header, `[Summe]` marks).
- Code validates: every component mapped exactly once, only existing ids, **no total together with
  a cell it is built from**, an empty mapping needs a reason, and **no quantity may be entirely
  empty**. On errors: one retry with the error list, then `MappingError` (that contract fails).
- **Carve-outs:** a carve-out ("X, excluding Y") is only subtracted from cells that contain Y.
  If the chosen cells for X already leave Y out, code drops Y and notes it.
- Cached on the plan's **structure** (labels and ids, not values) plus contract, prompt and model.

### 6.5 Calculation (`calc.py`) — no LLM
- Each quantity is evaluated recursively: Σ sign × weight × cell values (refs followed).
- Ratio = numerator / denominator (× 100 for percent). Rounded **with the contract's own rule**
  (commercial rounding via `Decimal`; float noise stripped first); without a rule the raw value is
  compared. A non-positive denominator gives "not defined", and a "≤" covenant counts as breached.
- Headroom = value − limit (≥) or limit − value (≤).
- **Stress test**, by the kind of the driving quantity:
  - `earnings_decline`: if the covenant contains an earnings quantity (EBITDA), find by bisection
    the largest drop that still meets the limit, recalculating both sides (e.g. in the DSCR,
    EBITDA falls while taxes stay). → "EBITDA may fall by 3.6 % (1,457 T€)".
  - `equity_loss`: for equity-ratio covenants, the extra annual loss L that hits the limit; a loss
    reduces equity and total assets alike: L = (E − p·A) / (1 − p). → "3,057 T€".
  - `numerator_change`, `already_breached`, `not_applicable` for the remaining cases.
- Output: `Result` per covenant, with formula parts, contract sources and plan cells for the reply.

### 6.6 Summary (`summary.py`, `prompts/summary.txt`)
The LLM gets the results as **already-formatted strings** and writes at most 3 sentences, each
tagged with the covenant ids it talks about. Code rejects a sentence if it contains a number that
doesn't belong to those covenants (or to general facts), or a status word (from
`texts/*.toml [summary_claims]`, e.g. "verletzt"/"breached") that doesn't match the real status.
One retry; otherwise a fixed template. The first line of the reply (the verdict) is always code.

---

## 7. Data model

All in `src/finn/models.py` (Pydantic v2).

| Model | Meaning |
|---|---|
| `Source` | page, section, quote (+ code-only `verified`, `verify_note`, `original_page`) |
| `Component` | name, sign (±1), weight, `ref` to another quantity or null (leaf), definition text, `carve_out` |
| `Quantity` | name, kind (`earnings`, `equity`, `total_assets`, `debt`, `cash`, `debt_service`, `taxes`, `other`), components, source |
| `Covenant` | id, name, numerator, denominator, operator, limit, unit, rounding, test basis, plan-basis note, sources |
| `ContractCovenants` | one contract: label, has_covenants, reason if none, quantities, covenants (validated for consistency) |
| `PlanItem` | one plan cell: id, label, sheet, role, value, is_total, depends_on, part_of |
| `TermMapping` | contract, quantity, component → item_ids + rationale |
| `Result` | one covenant's numbers, status, stress, formula, sources |
| `RunReport` | everything the reply needs: results, contracts without covenants, warnings, failed contracts |

**Audit file** (`runs/<run_id>.json`, also attached to the reply): `email`, `intake`, `decision`,
`settings` (without secrets), `steps.read_plan`, `steps.extract_covenants` (per contract: the
full extraction and meta: model, attempts, tokens, latency, quotes verified, warnings,
normalisation), `steps.map_terms`, `steps.calculate`, `report`, `reply`.

---

## 8. The mail agent

- `mail/inbox.py`: IMAP. Lists every mail of the last `lookback_days` (read or unread).
- `watcher.py`: a mail is *new* if it is unread, or arrived after start minus 15 minutes (so an
  order someone already opened is still answered, but old read mail is not). Each Message-ID is
  recorded in `runs/processed.json` **after** sending → exactly once, across restarts. A failed
  send is retried up to 3 times with the saved `reply.eml`.
- `intake.py::is_automatic_or_own`: ignores Finn's own address, mails with `X-Finn-Reply`
  (Finn's replies), auto-replies (`Auto-Submitted`, `Precedence: bulk`, …), and senders like
  `noreply`/`mailer-daemon` (all in `config/agent.toml`).
- `mail/outbox.py`: builds a threaded MIME reply (`In-Reply-To`, `References`, `Re:` unless the
  subject already has a reply prefix), reply-all with Cc minus Finn, HTML + text, audit attached.
- Every outcome produces a reply except loop-protected mail and a plain thank-you.

---

## 9. The reply email

`report.py` builds HTML (inline styles, table layout, works in Gmail/Outlook and on phones) and a
plain-text version. All wording comes from `texts/de.toml` / `en.toml`; remarks are stored as
language-neutral keys (`notices.py`) and rendered at the end, so an English reply contains no
German remarks. Order: greeting → verdict (code) → summary (LLM, checked) → overview table →
details per contract (value, limit, headroom, stress, calculation, contract quotes, plan cells,
plan-basis note) → contracts without covenants → notes → signature and footer ("all figures were
calculated in code").

---

## 10. Configuration and the no-hardcoding rule

A golden rule of the project: **nothing case-specific in `src/`**. `tests/test_no_hardcoding.py`
scans the source for years, sheet names, ratio names like "EBITDA", currency units, the vendor
URL, and any `import re` (regular expressions were banned as "close to hardcoding"; formulas are
parsed with openpyxl's tokenizer, numbers with a small tokenizer in `numbers.py`).

| Where | What |
|---|---|
| `.env` | key, models, mailbox, polling, folders (`.env.example` documents all) |
| `config/plan_layout.toml` | sheet roles, plan column keyword, units, test-date template, reconciliation checks |
| `config/report.toml` | tight threshold (10 %), colours, audit attachment, summary limits |
| `config/agent.toml` | intake limits, thank-you handling, watch window, loop protection, reply-all |
| `prompts/*.txt` | the four prompts (generic: no case-specific hints) |
| `src/finn/texts/*.toml` | all user-facing text, per language |

---

## 11. The LLM: model choice, client, cache, cost

- **Provider:** OpenRouter (OpenAI-compatible); any compatible endpoint works via `LLM_BASE_URL`.
- **Model choice was measured**, not guessed: `python -m finn.contracts.extract --compare` reads
  all contracts live and compares with hand-read golden specs. `google/gemini-3.8-flash` verified
  all quotes and got every covenant decision right; it is primary. `openai/gpt-5.4-mini` is the
  fallback (its endpoints reject the strict-schema routing flag, so the client retries once
  without it).
- **Client (`llm.py`):** JSON-schema structured output, `max_retries=0` in the SDK and an own
  retry policy (one retry, then fallback model), a time budget per call (180 s), max tokens 24,000,
  truncation detection, markdown-fence stripping without regex.
- **Cache (`cache/`, committed):** contract readings keyed on PDF + prompt + model (line endings
  normalised); mappings keyed on the plan's structure. A fresh clone with the preset model
  therefore needs only two small LLM calls per order. A test proves the committed cache reproduces
  the answer key without any extraction or mapping call.
- **Cost:** a cold run (4 contracts + mappings) is a few cents; a cached run is a fraction of a cent.

---

## 12. Tests

`pytest -q` → 112 tests, ~10 s, no network. Key files:

| File | Covers |
|---|---|
| `test_plan_reader.py` | role detection, formula evaluation, sign rule, reconciliation checks, broken files |
| `test_calc.py` | every covenant vs. the answer key, trap values, rounding, headroom signs, stress, changed workbooks |
| `test_extract.py` | caching, retries, fallback, 404 handling, truncation, limit-in-quote correction |
| `test_verify.py`, `test_pdf_text.py` | quote verification, page correction, hyphenation |
| `test_mapping.py` | each validation rule, double counting, structure-based cache key, warnings |
| `test_robustness.py` | replays of live-run failures: ratio structure, normalisation, carve-outs, grouped notes, committed cache |
| `test_summary.py` | number and status checks, template fallback |
| `test_report.py` | HTML/text reply, MIME threading, subjects, languages |
| `test_intake.py` | decisions for every sample mail, thank-you handling, loop protection |
| `test_watch.py` | exactly-once, restarts, send retries |
| `test_pipeline.py` | end to end with the golden fake LLM, no secrets in the audit |
| `test_no_hardcoding.py` | the golden rule |

`tests/golden_llm.py` is a fake LLM that answers with the hand-checked fixtures in
`tests/fixtures/`, so the full pipeline runs offline.

---

## 13. How it was built, and what the live runs found

**Process.** A PRD and the answer key were written first (by hand, with Claude), including golden
fixtures whose every quote was verified on its PDF page by a script. Steps 1–4a were built by a
coding assistant from one prompt per step (`docs/PROMPTS.md`), each reviewed and re-run. Later
steps were written directly by Claude and re-verified on the laptop. `TIMELOG.md` records who built
what (about 11 h in total, more than the 4–6 h guideline; the extra went into robustness, as the
brief suggests).

**Review.** An independent reviewer agent re-derived all covenants from the contracts (agreeing
with the answer key) and found 13 weaknesses, mainly around the LLM steps. All were fixed (steps
7b/7c): e.g. limits must appear in their quote, inconsistent plans are refused, summary sentences
are checked per covenant, the mapping prompt was de-overfitted.

**Live runs.**
1. **First live run used the cheaper fallback model as primary** (a leftover in `.env`). Two of
   four covenants were right. Contract 3: the model put the debt service into the numerator too and
   extracted irrelevant quantities; mapping refused it (nothing wrong was sent). Contract 4: 3.17
   instead of 3.37, because the shareholder loan was subtracted from a bank-debt line that never
   contained it. **Fixes (8b):** ratio-structure check with corrective re-read, normalisation,
   carve-out rule.
2. **Re-run with the measured model:** all four ✓, 34/34 quotes verified, one attempt each. The
   model again modelled the shareholder loan as a carve-out; the new rule kept the result right.
3. **Reply polish (8d):** grouped "no plan line" notes, no contradictory note, no doubled quote
   marks, CRLF-safe cache keys.
4. **Live Gmail test:** order from a second account answered once in the same thread with the
   audit attached; a second poll sent nothing; a mail without attachment got the right reply. The
   first order landed in Finn's **spam** folder (new account) → filter added to the setup. A
   "Danke!" got the "I only do covenant checks" reply → **9b:** thank-you mails are recorded, not
   answered.

---

## 14. Running it, and the demo

Setup and commands: see the README. For the demo:

1. Finn's Gmail has IMAP on, an app password in `.env`, and the "never send to spam" filter.
2. `pytest -q` (112 passed) and `finn run --eml data/samples/order.eml --compare-expected` (4 ✓,
   ~20 s) beforehand.
3. Start `finn watch` a few minutes before; keep the laptop awake.
4. The reviewers send the order; the reply arrives in the thread in ~30 s.
5. To show: the reply (verdict, table, details with sources), the attached audit JSON, the cache
   making it fast, and `runs/processed.json` (exactly once).

Good things to demonstrate on request: an email without attachment, an English email, a broken or
inconsistent Excel (polite refusal), a "Danke!" (no reply), and deleting `cache/*.json` for a cold
read.

---

## 15. How to extend it

| Change | What to do |
|---|---|
| A new contract | Drop the PDF into `data/contracts/`. It is read on the next order and cached. Check with `python -m finn.contracts.extract --compare` if you add golden specs. |
| A differently laid-out plan | Add sheet aliases/keywords in `config/plan_layout.toml`; adjust the reconciliation checks there. No code change. |
| Another plan year | Automatic: the year comes from the email or the file's plan column. |
| Another language | Add `src/finn/texts/<lang>.toml` with the same keys, allow the language in the intake model. |
| Another LLM | Change `LLM_MODEL` (and optionally `LLM_BASE_URL`). The cache re-reads automatically. Measure with `--compare` first. |
| Another mailbox | `IMAP_*`, `SMTP_*`, `MAIL_*` in `.env`. |
| Thresholds, colours | `config/report.toml`. |
| Answer thank-you mails again | `answer_acknowledgements = true` in `config/agent.toml`. |

Rule of thumb: if a change would put a name, year, label or number into `src/`, it belongs in a
config or text file instead; the guard test will tell you.

---

## 16. Known limitations and next steps

- **Human approval before sending a breach** would be the next production step.
- A **covenant database** across many contracts, with history; all plan years and prior-year
  plausibility checks.
- **OCR** for scanned contracts (currently text PDFs only).
- Routing for **other request types** (this is where an agent loop starts to make sense).
- **Tracing and cost per run** in a dashboard (today: in the audit JSON).
- The cheaper fallback model is noticeably weaker at extraction; the guards catch its known
  failure modes, but quality depends on the primary model.
- Gmail-specific: a brand-new account may file mail as spam; mitigated by the filter.

---

## 17. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Run takes ~5 min | Cache miss (other model or prompt) → contracts are re-read once. |
| "No contract PDFs found" | `CONTRACTS_DIR` points to an empty folder. |
| IMAP/SMTP login error | App password (16 letters), IMAP enabled, correct `MAIL_USER`. |
| No reply in live mode | Spam folder; "never spam" filter; mail is from Finn's own address; the order is older than the watch window and already read. |
| Reply "plan inconsistent" | A reconciliation check failed; the reply lists it. |
| A covenant missing, reply "incomplete" | That contract failed extraction or mapping; the reason is in the audit (`report.failed_contracts`). |
| Files reverted after editing | An editor saved stale buffers over newer files; close tabs without saving. |

---

## 18. Glossary (German terms)

| German | English |
|---|---|
| Wirtschaftsplan | business plan (budget) |
| Erfolgsplan / GuV | planned income statement |
| Planbilanz / Bilanz | planned balance sheet |
| Darlehensspiegel / Finanzierung | loan schedule |
| Stichtag | test date (balance-sheet date) |
| Eigenkapital(quote) | equity (ratio) |
| Sonderposten (für Investitionszuschüsse) | special items for investment grants |
| Gesellschafterdarlehen mit Rangrücktritt | subordinated shareholder loan |
| Schuldendienst | debt service (interest + repayment) |
| Tilgung / Sondertilgung | repayment / voluntary prepayment |
| Aufzinsung (von Rückstellungen) | accretion (unwinding of discount) of provisions |
| Dynamischer Verschuldungsgrad | dynamic gearing (net debt / EBITDA) |
| Puffer / Headroom | headroom |
| knapp / eingehalten / verletzt | tight / met / breached |
| Kündigungsgrund | termination event |
| Heilung | cure |
| Konsortialkredit | syndicated loan |
| Förderdarlehen | promotional (development-bank) loan |
