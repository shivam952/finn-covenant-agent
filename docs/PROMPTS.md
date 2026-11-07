# Finn – Build Prompts Log

Prompts given to the AI coding assistant to build Finn, in order. Each prompt was reviewed before moving to the next one. The review notes after each prompt record what was checked or corrected.

---

## Prompt 1 – Skeleton, data model, answer key

```text
You are helping me build "Finn", an email-driven agent for a Werkstudent case study. Read this whole prompt before writing any code. In this step we only build the project skeleton, the data model and the hand-checked answer key. No LLM calls, no mail, no PDF parsing yet.

## Context
Finn is a "digital financing assistant" for a German municipal utility (Stadtwerke Musterstadt GmbH). The head of finance emails Finn an order ("Covenant-Prüfung Wirtschaftsplan 2027") with Wirtschaftsplan_2027.xlsx attached. Finn:
1. reads four loan contracts (PDFs in data/contracts/),
2. finds out per contract whether it has financial covenants (ratio, definition, limit, source page/§/Anlage),
3. maps every definition term to cells in the Excel (column Plan 2027, test date 31.12.2027),
4. calculates every ratio and its headroom IN CODE,
5. replies to the email in German with a summary, a table and the source for every number.

Core rule, non-negotiable: the language model only reads, structures and writes text. It never calculates. Every number comes from deterministic Python code.

Architecture: a fixed pipeline in plain Python. NO LangChain, NO LangGraph, NO agent/tool-calling loop, NO RAG or vector DB. The LLM is reached through OpenRouter, which has an OpenAI-compatible API (use the `openai` Python SDK with base_url https://openrouter.ai/api/v1). The model name comes from env.

## Task for this step

### 1. Repo layout (create exactly this; empty modules get a docstring + TODO)
finn-covenant-agent/
├── README.md              # placeholder sections: Setup, Configure .env, Run offline, Run live, Tests
├── NOTE.md                # placeholder headings: Assumptions, Changes to the brief, AI usage, Next steps
├── TIMELOG.md             # table: Date | Step | Time | Notes  (first row: "Read material + answer key | 0.5 h")
├── .env.example
├── .gitignore             # .env, cache/, runs/, __pycache__/, .venv/, *.egg-info, .pytest_cache/
├── pyproject.toml
├── data/contracts/        # the 4 PDFs go here (I copy them in)
├── data/samples/          # Wirtschaftsplan_2027.xlsx + order.eml go here
├── prompts/extract_covenants.txt, map_terms.txt, summary.txt   (empty placeholders for now)
├── src/finn/
│   ├── __init__.py
│   ├── main.py            # CLI: `finn watch` and `finn run --eml <file>`; stubs that print "not implemented"
│   ├── config.py          # loads .env via python-dotenv into a frozen pydantic Settings-like dataclass
│   ├── models.py          # see below – implement fully
│   ├── llm.py, pipeline.py, audit.py, mapping.py, calc.py, report.py
│   ├── mail/__init__.py, inbox.py, outbox.py
│   ├── contracts/__init__.py, pdf_text.py, extract.py, verify.py
│   └── plan/__init__.py, reader.py
├── cache/  runs/          # gitignored, create with .gitkeep
└── tests/
    ├── expected_2027.json # see below – write it exactly
    └── test_models.py     # validates models + loads expected_2027.json

### 2. pyproject.toml
Python >= 3.11, package in src/ layout, console script `finn = "finn.main:main"`.
Dependencies: pydantic>=2, openai>=1.40, python-dotenv, openpyxl, pymupdf, pytest (dev extra).
Install command must be: `pip install -e ".[dev]"`.

### 3. .env.example (names only, no real values)
OPENROUTER_API_KEY=
OPENROUTER_MODEL=            # a current cheap model with JSON-schema structured output support
OPENROUTER_MODEL_FALLBACK=   # optional second model, used if the first fails twice
FINN_MAIL_ADDRESS=
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
MAIL_USER=
MAIL_APP_PASSWORD=
POLL_SECONDS=20
CONTRACTS_DIR=data/contracts
CACHE_DIR=cache
RUNS_DIR=runs
config.py must not crash if mail or LLM variables are missing; it raises a clear error only when a command actually needs them.

### 4. models.py (Pydantic v2)
The real contracts need more than a flat numerator/denominator list: one contract weights a term with 50 %, two contracts share a sub-definition (EBITDA), and each contract has its own rounding rule. So use this model:

class Source(BaseModel):
    page: int                    # PDF page number, 1-based
    section: str                 # e.g. "§ 13 Abs. 1", "§ 1 Abs. 1", "Anlage 3 Abs. 6"
    quote: str                   # contract text word for word (short, the defining sentence)

class Component(BaseModel):
    """One line of a definition, e.g. 'zuzüglich der Steuern vom Einkommen und vom Ertrag'."""
    name: str                    # short German label, e.g. "Steuern vom Einkommen und vom Ertrag"
    sign: Literal[1, -1]
    weight: float = 1.0          # e.g. 0.5 for "50 % der Sonderposten"
    ref: str | None = None       # name of another Quantity if this line refers to one (e.g. "EBITDA")
    definition: str              # what the contract says is included/excluded for this line

class Quantity(BaseModel):
    """A named amount defined by the contract, e.g. 'EBITDA', 'Schuldendienst', 'Wirtschaftliches Eigenkapital'."""
    name: str
    components: list[Component]
    source: Source

class Covenant(BaseModel):
    id: str                      # e.g. "V4-EKQ"
    name: str                    # e.g. "Eigenkapitalquote"
    numerator: str               # Quantity name
    denominator: str             # Quantity name
    operator: Literal[">=", "<="]
    limit: float                 # in the unit below: 27.5 for 27,5 %, 1.30 for a ratio
    unit: Literal["percent", "ratio"]
    rounding_decimals: int | None    # None if the contract states no rounding rule
    test_basis: Literal["stichtag", "geschaeftsjahr"]   # balance-sheet date vs. fiscal-year period
    source: Source                # where the obligation + limit is stated
    definition_sources: list[Source]   # where the definitions are
    plan_basis_note: str | None   # what the contract says about a breach on PLAN figures (early warning vs. default)

class ContractCovenants(BaseModel):
    contract_file: str
    contract_label: str          # e.g. "Vertrag 3 – Landesbank Mittelland, Investitionsdarlehen LBM-INV-2025-0412"
    has_covenants: bool
    reason_if_none: str | None
    reason_source: Source | None
    quantities: list[Quantity]
    covenants: list[Covenant]

class PlanItem(BaseModel):
    id: str                      # equals the cell reference, e.g. "GuV!D17"
    label: str                   # row label (Finanzierung sheet: "row label – column header")
    sheet: str
    cell: str                    # "D17"
    raw_value: float             # as stored in Excel (expenses are negative there)
    value: float                 # normalised: expense input lines as positive amounts, totals keep their sign
    is_expense: bool

class TermMapping(BaseModel):
    quantity: str
    component: str
    item_ids: list[str]          # PlanItem ids only; summed
    rationale: str               # one German sentence why these cells match the definition

class Result(BaseModel):
    contract_label: str
    covenant_id: str
    covenant_name: str
    numerator_value: float
    denominator_value: float
    value_raw: float
    value_reported: float        # rounded per contract; raw if no rule
    limit: float
    operator: str
    unit: str
    headroom: float              # positive = buffer, negative = breach (in percentage points for percent)
    met: bool
    stress_text: str             # German, e.g. "EBITDA dürfte um 15,5 % sinken, bevor der Covenant bricht."
    stress_value: float
    formula_text: str            # filled-in formula, e.g. "(40.200 − 3.200) / 23.680 = 1,5625 → 1,56"
    contract_sources: list[Source]
    plan_cells: list[str]
    warnings: list[str] = []

class RunReport(BaseModel):
    results: list[Result]
    contracts_without_covenants: list[ContractCovenants]
    warnings: list[str]

### 5. tests/expected_2027.json – hand-checked answer key. Write it EXACTLY with these values (T€):
{
  "test_date": "31.12.2027",
  "base_values": {
    "jahresueberschuss": 10700, "ertragsteuern": 3200, "zinsaufwand_guv": 5100,
    "aufzinsung_pensionen": 170, "zinsertraege": 300, "abschreibungen": 21500,
    "ebitda": 40200, "eigenkapital": 96100, "sonderposten": 22800, "bilanzsumme": 313200,
    "verb_kreditinstitute": 148200, "gesellschafterdarlehen": 8000, "liquide_mittel": 12600,
    "zins_finanzverbindlichkeiten": 4930, "planmaessige_tilgung": 18750, "sondertilgung": 3000
  },
  "contracts": {
    "Vertrag_1_Sparkasse_Investitionsdarlehen.pdf": {"has_covenants": false},
    "Vertrag_2_Foerderbank_Foerderdarlehen.pdf": {"has_covenants": true, "covenants": {
      "Wirtschaftliche Eigenkapitalquote": {"numerator": 107500, "denominator": 313200,
        "value_raw": 34.3231, "value_reported": 34.3231, "limit": 27.5, "operator": ">=",
        "headroom": 6.8231, "met": true, "stress_equity_loss_teur": 29475.86}}},
    "Vertrag_3_Landesbank_Investitionsdarlehen.pdf": {"has_covenants": true, "covenants": {
      "Schuldendienstdeckungsgrad": {"numerator": 37000, "denominator": 23680,
        "value_raw": 1.5625, "value_reported": 1.56, "limit": 1.30, "operator": ">=",
        "headroom": 0.26, "met": true, "stress_ebitda_drop_pct": 15.46}}},
    "Vertrag_4_Konsortialkredit.pdf": {"has_covenants": true, "covenants": {
      "Eigenkapitalquote": {"numerator": 96100, "denominator": 313200,
        "value_raw": 30.6833, "value_reported": 30.7, "limit": 30.0, "operator": ">=",
        "headroom": 0.7, "met": true, "stress_equity_loss_teur": 3057.14},
      "Dynamischer Verschuldungsgrad": {"numerator": 135600, "denominator": 40200,
        "value_raw": 3.3731, "value_reported": 3.37, "limit": 3.50, "operator": "<=",
        "headroom": 0.13, "met": true, "stress_ebitda_drop_pct": 3.62}}}
  },
  "trap_values_that_must_NOT_appear": {
    "dscr_with_sondertilgung": 1.39, "dscr_with_guv_interest": 1.55,
    "dscr_without_shareholder_loan": 1.65, "dynvg_with_shareholder_loan": 3.57,
    "ekq_v4_with_sonderposten": 37.96
  }
}

### 6. tests/test_models.py
- expected_2027.json loads, and for every covenant numerator/denominator ≈ value_raw (tolerance 1e-3 relative).
- A minimal ContractCovenants with one Quantity/Covenant validates; a Component with sign 2 is rejected.

## Rules
- Code and comments in English. Every user-facing text (reply mail, stress_text, warnings shown to the user) in German.
- Type hints everywhere. No secrets in code. Keep it simple: no extra abstractions beyond what's described.
- Make one git commit at the end: "Step 1: skeleton, data model, hand-checked answer key".

## Done when
`pip install -e ".[dev]"` works, `pytest` is green, `finn --help` shows the two commands. Show me the tree and the test output.
```

**Review notes:** Structure, models, answer key and CLI match the spec, and 3/3 tests pass on a clean machine. Review found three issues:
1. In `.env.example`, inline comments after `KEY=` are parsed as values by python-dotenv. `OPENROUTER_MODEL` became `"# a current cheap model…"`, so `require_llm()` would pass and the API call would fail with a nonsense model name.
2. `test_settings_loads_without_crashing_on_missing_env` reads the real `.env`. It fails as soon as `.env` is filled in (reproduced).
3. Minor: the test guesses the unit from the covenant name. `FINN_MAIL_ADDRESS` is not required or defaulted.
Git commit `ac18ca1 Step 1: …` exists.

---

## Prompt 1b – Review fixes for step 1

```text
Review of step 1 found three issues. Fix exactly these, nothing else:

1. .env.example: python-dotenv parses inline comments after an empty value as the value itself (e.g. OPENROUTER_MODEL becomes "# a current cheap model ..."). Move every comment onto its own line ABOVE the variable, so every empty variable is truly empty. Add a short comment line above each group (LLM, Mail, Paths).
   Also in config.py: treat any value that starts with "#" as empty, as a defensive guard.

2. tests/test_models.py::test_settings_loads_without_crashing_on_missing_env loads the real .env, so it fails once a developer fills in .env. Make Settings.from_env accept env_file=None meaning "load the default .env", and add a parameter `load_env_file: bool = True`. In the test call Settings.from_env(load_env_file=False) after monkeypatch.delenv. Also add a test that a value starting with "#" is treated as empty.

3. In test_expected_2027_loads_and_ratios_match, do not guess the scale from the covenant name. Add "unit": "percent" or "ratio" to each covenant in tests/expected_2027.json and use that field (percent → ×100).

4. require_mail(): also require FINN_MAIL_ADDRESS, or default it to MAIL_USER if empty (Finn needs its own address to ignore its own replies). Pick the default-to-MAIL_USER option.

Then make one commit: "Step 1b: review fixes (.env.example parsing, test isolation, unit field)".

Done when: pytest is green BOTH without a .env file and with a filled-in .env file (create a temporary one to check, then delete it). Show the pytest output for both runs and `git log --oneline`.
```

**Review notes:** Accepted. 4/4 green with and without `.env`, and commit `e416b44` is in.

**Prepared by hand between steps (not by the coding assistant):**
- Copied the 4 contract PDFs to `data/contracts/` and `Wirtschaftsplan_2027.xlsx` to `data/samples/`.
- Wrote `tests/fixtures/golden_covenants.json` (hand-read covenant specs for all 4 contracts, with page, § and word-for-word quotes) and `tests/fixtures/golden_mapping.json` (hand-picked Excel cells per definition line).
- All 26 quotes were checked automatically to appear on their cited page, using both pdftotext and PyMuPDF.
- A throwaway reference evaluation of the fixtures reproduces the answer key exactly: 34.3231 % / 1.5625 / 30.6833 % / 3.3731.

---

## Prompt 2 – Plan reader + calculator, tested against the answer key

```text
Step 2 of Finn. Build the Excel plan reader and the deterministic calculator, and test them against the hand-checked answer key. Still NO LLM, NO mail, NO PDF parsing in this step.

## Already in the repo (do not modify these files)
- data/samples/Wirtschaftsplan_2027.xlsx – the business plan. Sheets: Deckblatt, GuV, Bilanz, Finanzierung. Values in T€.
- data/contracts/*.pdf – the 4 contracts (not used yet).
- tests/expected_2027.json – answer key.
- tests/fixtures/golden_covenants.json – list of ContractCovenants (hand-read from the contracts).
- tests/fixtures/golden_mapping.json – list of term mappings (hand-picked cells). Each entry has the keys contract_file, quantity, component, item_ids, rationale.
Open the fixtures and the workbook first and read them before you write code.

## 1. Model change (models.py)
Quantity names repeat across contracts ("EBITDA", "Bilanzsumme"), so TermMapping needs the contract:
class TermMapping(BaseModel):
    contract_file: str
    quantity: str
    component: str
    item_ids: list[str]      # may be EMPTY = this line has no plan value (e.g. no extraordinary items) → contributes 0
    rationale: str
Also add a model_validator on ContractCovenants: if has_covenants is False, covenants must be empty and reason_if_none must be set; if True, covenants must be non-empty, and every covenant numerator/denominator and every component ref must name a Quantity in `quantities`.

## 2. src/finn/plan/reader.py
def read_plan(path: Path) -> PlanData   (PlanData: items: dict[str, PlanItem], checks: list[str] (German warning texts), meta: dict)
Rules:
- Locate the Plan-2027 column by its HEADER TEXT, never hard-code column letters. GuV header contains "Plan 2027"; Bilanz header contains "2027" and "Plan". Scan the first 10 rows for the header row. On the Finanzierung sheet the whole sheet is the 2027 plan: read every numeric column B..G of each row that has a label in column A, and give it the label "<row label> – <column header>".
- Item id = "<Sheet>!<Cell>", e.g. "GuV!D17". label = text in column A (stripped).
- Formulas: load the workbook TWICE (data_only=False for formulas, data_only=True for cached values). Do NOT trust cached values alone: a file saved by a script has no cache. Implement a tiny formula evaluator for exactly the patterns in this workbook: =SUM(X1:X9), =A1+B2-C3 chains, cross-sheet refs like =Bilanz!D28, a leading unary minus (=-GuV!D13). Evaluate recursively with a cycle guard. If a cached value exists and differs from the evaluated value by more than 0.5, add a warning to checks and use the evaluated value. An unsupported formula raises PlanFormatError naming the cell.
- Sign normalisation: on the GuV sheet, a NON-formula cell with a negative value is an expense input line → is_expense=True, value=abs(raw_value). Formula cells (totals like Betriebsergebnis, Jahresüberschuss) keep their sign. Bilanz and Finanzierung values are taken as is.
- Missing sheet, or missing Plan-2027 column → raise PlanFormatError with a German message naming exactly what is missing (e.g. "Im Wirtschaftsplan fehlt das Blatt „Bilanz“." / "Im Blatt „GuV“ wurde keine Spalte „Plan 2027“ gefunden."). Find sheets case-insensitively.
- Plausibility checks (add German warnings to checks, never raise):
  a) Bilanz: Summe Aktiva == Summe Passiva (Plan column).
  b) Finanzierung: "Summe Kreditinstitute – Stand 31.12.2027" == Bilanz "Verbindlichkeiten gegenüber Kreditinstituten".
  c) GuV "Zinsen und ähnliche Aufwendungen" minus the "davon … Aufzinsung" line == Finanzierung "Summe Darlehen gesamt – Zinsaufwand 2027".
  d) EBITDA identity: Jahresüberschuss + Steuern + Zinsaufwand − Zinserträge + Abschreibungen == Betriebsergebnis + Abschreibungen.
  Find these rows by label keywords, not by row number. If a row can't be found, add a warning that the check was skipped.
- Expose a helper plan_item_catalog(plan) -> str that lists "id | label | value" lines. The LLM will use it later; it's also useful in the audit log.

## 3. src/finn/calc.py – pure functions, no I/O
def evaluate_contract(contract: ContractCovenants, mappings: list[TermMapping], plan: PlanData) -> list[Result]
- Quantity value = Σ sign × weight × value(component). A component with `ref` → recursive value of that Quantity (cycle guard → CalcError). Otherwise → exactly ONE TermMapping must match (contract_file, quantity, component) (none or several → CalcError naming it); value = sum of the PlanItem.value of its item_ids (an unknown item id → CalcError). Empty item_ids → 0.
- Keep a trace for every leaf: component, sign, weight, item ids, values. Use it to build formula_text and plan_cells.
- value_raw = numerator / denominator (× 100 if unit == "percent"). Denominator 0 → CalcError.
- value_reported: if rounding_decimals is set, round HALF UP with decimal.Decimal (kaufmännisch), NOT Python round(). Otherwise value_reported = value_raw.
- met: compare value_reported against limit using operator.
- headroom: ">=": value_reported − limit; "<=": limit − value_reported. Positive = buffer.
- Stress test (based on value_raw, no rounding benefit, everything else held constant):
  • If the covenant tree (numerator or denominator, including refs) contains a Quantity named "EBITDA" (case-insensitive): find the % decline in EBITDA at which the covenant is exactly at its limit. Solve it in closed form or by bisection, overriding the EBITDA quantity value. stress_value = that % (e.g. 15.46). stress_text e.g. "Das EBITDA dürfte um 15,5 % (6.216 T€) sinken, bevor der Grenzwert von 1,30 unterschritten wird."
  • Else, if unit == "percent" (an equity ratio): an additional loss L reduces BOTH numerator and denominator by L. Solve (num − L) / (den − L) = limit/100. stress_value = L in T€. stress_text e.g. "Ein zusätzlicher Jahresverlust von 3.057 T€ würde die Quote unter 30,0 % drücken."
  • If already breached: stress_text says by how much it is breached instead.
- formula_text in German number format (thousands ".", decimals ","). Show the numerator and denominator parts and the rounding step, e.g. "(40.200 − 3.200) / (4.930 + 18.750) = 1,5625 → gerundet 1,56".
- contract_sources = covenant.source + definition_sources + the sources of all quantities used.
- Put the German number formatting helpers in src/finn/fmt.py (fmt_teur, fmt_num(x, decimals), fmt_pct).

## 4. Tests
tests/test_plan_reader.py
- Reads the sample: GuV!D17 raw −3200 → value 3200, is_expense True; GuV!D18 (Jahresüberschuss) = 10700; Bilanz!D15 = 313200; Bilanz!D22 = 96100; Finanzierung!E8 = 3000; Finanzierung!G15 = 4930.
- All 4 plausibility checks pass on the sample (checks list is empty).
- NO-CACHE test: copy the workbook with openpyxl (load + save → cached values are lost), and the reader still returns Bilanz!D15 = 313200 and GuV!D18 = 10700.
- Missing sheet → PlanFormatError with the sheet name in the message. A renamed header "Plan 2027" → PlanFormatError.
tests/test_calc.py
- Load golden_covenants.json + golden_mapping.json + the sample plan. For every covenant, check numerator, denominator, value_raw (abs 1e-4), value_reported, headroom (abs 1e-6 after rounding to 4 dp), met and stress_value (abs 0.01) against tests/expected_2027.json (match by contract file + covenant name).
- None of the "trap_values_that_must_NOT_appear" equals any value_reported.
- Headroom sign is correct for both operators: build a tiny synthetic covenant for each and test met/breached.
- Kaufmännisch rounding: 1.565 → 1.57 and 30.65 → 30.7 (Python round would give 1.56 and 30.6). Use Decimal(str(x)).
- Changed-numbers test: copy the workbook, set Finanzierung!E8 (Sondertilgung) to 0 → no covenant changes. Then set Bilanz!D29 (Gesellschafterdarlehen) higher by 1000 → no change in V4-DYNVG (subordinated loan is excluded). Then increase Bilanz!D28 by 5000 → V4-DYNVG value_raw = (153200 − 12600) / 40200. Note: Bilanz totals are formulas, so the evaluator must recompute them.
- A missing mapping raises CalcError mentioning the component name. A cyclic ref raises CalcError.

## Rules
- calc.py and reader.py never call an LLM and never format mail text, except the German number helpers in fmt.py and stress_text/formula_text.
- Do not loosen a test to make it pass. If a test against expected_2027.json fails, stop and show me the numbers. Do NOT edit the answer key or the fixtures.
- Update TIMELOG.md with the real time spent. Commit: "Step 2: plan reader with formula evaluator, calculator, tests vs. answer key".

## Done when
pytest is fully green. Show the pytest output, plus a printed table (covenant id | value_raw | value_reported | limit | headroom | met | stress_text) from a small script `python -m finn.calc data/samples/Wirtschaftsplan_2027.xlsx` that uses the golden fixtures.
```

**Review notes:** Accepted (commit `ab6c175`). Re-ran 13/13 green independently.
- All 4 values match the answer key, and the stress values match too (29,476 T€ / 15.46 % / 3,057 T€ / 3.62 %).
- formula_text reads well.
- The plausibility checks are empty on the sample, the Plan columns are found by header text, and 95 plan items are read.

Two nits, carried into Prompt 3:
- `round_kaufmaennisch` uses `Decimal(str(x))` directly. A float-noise value like 30.649999999999 would round down, so pre-quantize to 9 dp first.
- The plan checks are copied into every Result's `warnings`. They belong once in RunReport.

---

## Prompt 3 – Contract text, covenant extraction (LLM), cache, quote-on-page check

```text
Step 3 of Finn: read the four contracts and extract their covenant specs with the LLM, cache the results, and verify every citation in code. The LLM only READS and STRUCTURES here. It never calculates anything.

## 0. Two small fixes from the step-2 review
- fmt.round_kaufmaennisch: first quantize Decimal(repr(value)) to 9 decimals with ROUND_HALF_EVEN to strip float noise, then quantize to the target decimals with ROUND_HALF_UP. Add a test: round_kaufmaennisch(30.649999999999, 1) == 30.7 (float noise on 30.65), and the existing rounding tests still pass.
- Plan plausibility warnings belong once in RunReport.warnings, not copied into every Result.warnings. Result.warnings is only for covenant-specific warnings (e.g. an unverified quote). Adjust calc.py and the tests.

## 1. models.py
Add to Source: `verified: bool | None = None` and `verify_note: str | None = None`. Both are set by code only, never by the LLM. Exclude them from the JSON schema sent to the LLM: build the LLM schema from separate "LLM-facing" models, or strip these fields from the schema.

## 2. src/finn/contracts/pdf_text.py
- extract_pages(path) -> list[PageText] with PageText(page: int (1-based), text: str), using PyMuPDF (`import pymupdf`, not `fitz`).
- Per page: join words on one line with single spaces, keep line breaks, remove repeated blank lines. Do NOT drop footer lines (they contain "Seite x von 13", which helps the model).
- render_for_llm(pages) -> str: every page starts with a marker line "=== Seite N ===".
- file_sha256(path) -> str.

## 3. src/finn/contracts/verify.py – the LLM's citations are never trusted
normalize(s): Unicode NFKC; unify quotes („ “ ” " ‚ ‘ ’ ') to one form and dashes (– — ‑ -) to "-"; remove soft hyphens; collapse all whitespace to one space; casefold.
verify_source(source, pages) -> Source (a copy with verified/verify_note set):
  a) normalized quote found on the cited page → verified=True.
  b) not there, but found on exactly one OTHER page → verified=True, page corrected, verify_note="Seitenangabe korrigiert: S. X statt S. Y".
  c) the quote may wrap across a page break: also try cited page + next page joined.
  d) not found anywhere → verified=False, verify_note="Zitat im Vertrag nicht gefunden".
  Quotes shorter than 15 characters → verified=False ("Zitat zu kurz für eine Prüfung").
verify_contract(cc, pages) -> ContractCovenants: applies verify_source to EVERY Source in the object (reason_source, quantity sources, covenant source, definition_sources).

## 4. src/finn/llm.py – one small client, no framework
- class LLMClient with method complete_json(system: str, user: str, schema: type[BaseModel], purpose: str) -> tuple[BaseModel, dict]. The dict holds usage/meta: model, prompt_tokens, completion_tokens, latency_s, attempts.
- OpenRouter via the openai SDK: base_url="https://openrouter.ai/api/v1", api_key from Settings, temperature=0.
  response_format={"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": <pydantic JSON schema>, "strict": False}},
  extra_body={"provider": {"require_parameters": True}} so only providers that actually honour the schema are used.
  Optional headers "HTTP-Referer" and "X-Title": "Finn Covenant Agent".
- Parse: strip ``` fences if present, json.loads, then schema.model_validate. On JSON or validation error: ONE retry with the same messages plus an extra user message containing the validation error ("Your previous answer was invalid: … Return only valid JSON for the schema."). If that also fails and OPENROUTER_MODEL_FALLBACK is set: same procedure with the fallback model. Otherwise raise LLMError with a short reason.
- Timeout 90 s per call. Network/HTTP errors count as a failed attempt.
- Define a Protocol `JsonLLM` with the same complete_json signature, so tests can pass a FakeLLM. Nothing in the codebase may import openai outside llm.py.

## 5. prompts/extract_covenants.txt (English instructions; the contract is German)
Write a careful system prompt. It must be GENERIC: do NOT mention any concrete numbers, bank names or the answers from tests/fixtures (that would be overfitting to the test). Content:
- Role: an experienced German corporate-finance officer (Finanzreferent) reading a loan agreement for a municipal utility.
- Task: decide whether the contract contains binding FINANCIAL COVENANTS, i.e. an obligation of the borrower to keep a financial ratio at or above/below a limit, and if so extract each one with its exact definition as written in THIS contract.
- What is NOT a financial covenant (explain each): figures in the preamble or recitals describing the past; pricing/margin grids that only set the interest margin; information/reporting duties (delivering a compliance certificate); conditions precedent for a drawdown; negative pledge, pari passu, disposal or ownership clauses; general material-adverse-change termination rights. If there is no financial covenant: has_covenants=false, give the reason in German, and cite the passage that best supports the conclusion.
- Definitions: definitions are often spread over § 1 (Begriffsbestimmungen), the covenant paragraph and an Anlage. Follow every cross-reference. Model every defined amount as a Quantity with atomic Components, one per line of the definition ("zuzüglich" → sign +1, "abzüglich" → sign -1, "50 % der …" → weight 0.5). If a component is itself a defined term used elsewhere, make it a separate Quantity and use `ref`. Put every inclusion/exclusion rule into the component's `definition` text, word-close to the contract (e.g. what counts as Finanzverbindlichkeiten, what is excluded from interest or repayments, how subordinated shareholder loans are treated). Exclusions matter more than anything else.
- The covenant's numerator/denominator must be Quantity names. Even a single-line amount (e.g. Bilanzsumme) is a Quantity.
- limit as a plain number (27,5 % → 27.5; 1,30 → 1.30). unit "percent" if the ratio is expressed in %, else "ratio". rounding_decimals only if the contract states a rounding rule, otherwise null. test_basis "stichtag" for balance-sheet-date ratios, "geschaeftsjahr" for period ratios.
- plan_basis_note: what the contract says about a calculation or breach based on PLAN figures (e.g. early warning only, no event of default, duty to discuss measures, cure rights). null if nothing is said.
- Citations: page = the number of the "=== Seite N ===" marker (not the footer). section like "§ 13 Abs. 1" or "Anlage 3 Abs. 4". quote = a SHORT verbatim excerpt (30–200 characters) copied character-for-character from that page. Do not paraphrase, do not fix typos, do not join text from two places. Every quote will be checked automatically against the PDF.
- Answer in German for all free-text fields. Return only JSON matching the schema.
The user message is: contract file name + rendered page-marked text.

## 6. src/finn/contracts/extract.py
extract_covenants(pdf_path, llm: JsonLLM, cache_dir, prompt_version) -> tuple[ContractCovenants, dict meta]
- Cache key = sha256(pdf bytes) + sha256(prompt file) + model name. Cache file: cache/covenants_<key[:16]>.json storing {"contract": …, "meta": …, "created_at": …}. A cache hit performs NO LLM call (meta.cache_hit=True).
- After the LLM call: force contract_file to the actual file name, run verify_contract, then semantic checks in code that add warnings to meta (never silently fix): limit 0–100 for percent; has_covenants and covenants consistent (the model validator already raises → treat a raise as a failed attempt and let llm.py retry); every numerator/denominator/ref resolvable; every component weight in (0, 1].
- Cache only results that passed validation. Unverified quotes ARE cached, but flagged.
- extract_all(contracts_dir, llm, cache_dir) -> list[(ContractCovenants, meta)], sorted by file name.
- CLI: `python -m finn.contracts.extract --compare` runs live extraction on data/contracts (needs .env) and prints a comparison against tests/fixtures/golden_covenants.json per contract:
  has_covenants match; number of covenants; per covenant (matched by unit + operator + limit): rounding_decimals, test_basis, the component signature of numerator and denominator (sorted list of (sign, weight) after resolving refs) vs. golden; percentage of quotes verified; tokens and latency. End with "MATCH" or a list of differences. I will use this to choose the model, so make the output easy to read.

## 7. Tests (no network; use a FakeLLM)
tests/test_pdf_text.py: 13 pages per contract; page markers present; a known phrase from tests/fixtures is found on its page.
tests/test_verify.py:
  - every Source in golden_covenants.json verifies as True (with the real PDFs);
  - a tampered quote (one word changed) → verified=False;
  - a correct quote with a wrong page number → verified=True with the page corrected and a verify_note;
  - a typographic-quote variant („…“ vs "…") still verifies.
tests/test_extract.py:
  - FakeLLM returns the golden contract → result equals golden (except verified fields), cache file written; a second call → FakeLLM not called.
  - Changing the prompt file content → cache miss.
  - FakeLLM returns invalid JSON, then valid → success with attempts=2.
  - FakeLLM always invalid, a fallback model configured → the fallback is used; no fallback → LLMError.
  - FakeLLM returns has_covenants=true with an empty covenants list → treated as invalid → retry.
  - Golden contract 1 (no covenants) passes through with its reason.
Do not call the real API in tests. Mark the live compare as a manual command, not a test.

## Rules
- Only llm.py talks to the network. extract.py is the only caller of the extraction prompt.
- Do not modify tests/fixtures/*, tests/expected_2027.json or data/*.
- Update TIMELOG.md. Commit: "Step 3: page-marked PDF text, LLM covenant extraction with cache, quote verification".

## Done when
pytest is green (show the output). Then STOP and tell me the exact command to run the live comparison. I will fill in .env and run it myself with two models.
```

**Review notes:** 21/21 green. Code review before the first live run found:
1. **Worst-case latency:** the OpenAI SDK retries internally (default `max_retries=2`) on top of our own retry and fallback. With a 90 s timeout, one stuck call can block for well over 10 minutes. That's fatal during a live demo.
2. **No `max_tokens` and no truncation check:** some providers cap output by default. A large contract's JSON can get cut off and then show up as a confusing "invalid JSON" error.
3. **Hyphenated words:** PyMuPDF joins words split across a line break (e.g. "Bilanzierungsund"). A model that writes the correctly hyphenated form would be wrongly flagged as unverified.
4. **Hints in the prompt:** it mentions the case's specific traps (Ausfallbürgschaft, Sonderposten fraction, id examples "V2-/V3-/V4-"). The general domain checklist is legitimate, but contract-specific hints should go, so the prompt works on unseen contracts.
5. **Referer header:** `HTTP-Referer` is set to a real-looking third-party domain. Replace it with the repo URL from env, or drop it.

---

## Prompt 3b – Hardening before the first live run

```text
Review of step 3. Fix exactly these points, then stop.

1. llm.py – bounded latency:
   - Create the OpenAI client with max_retries=0 (we do our own retry/fallback) and timeout=60.
   - Before each retry after a network or HTTP error, sleep 2 s. HTTP 429 or 5xx → retry. HTTP 400/401/402/403/404 → do not retry on the same model (it won't help): log the reason and go straight to the fallback model.
   - Add a total time budget per complete_json call (default 180 s, env LLM_TIME_BUDGET_S). When it is exceeded, raise LLMError immediately.
   - Add the attempted models and per-attempt error strings to meta (meta["attempt_log"]), so the audit log can show them.

2. llm.py – output length:
   - Pass max_tokens (env LLM_MAX_TOKENS, default 12000).
   - If choices[0].finish_reason == "length", treat it as a failed attempt with the error "Antwort abgeschnitten (max_tokens)".
   - If the response has no content (refusal or empty), treat it as a failed attempt.

3. Headers: drop the hard-coded HTTP-Referer. Send X-Title "Finn Covenant Agent", and send HTTP-Referer only if env APP_URL is set. Add APP_URL to .env.example (empty) with a comment line above it.

4. verify.py – line-break hyphenation: if the normal match fails on the cited page, try a second, looser comparison where both sides keep only letters and digits (remove spaces, hyphens and punctuation after normalize()). A match there → verified=True with verify_note="Abgleich ohne Satzzeichen/Trennstriche". Apply the same order for the next-page join and the other-pages search. Add a test against contract 3, page 2. There PyMuPDF yields "Bilanzierungs-\nund Bewertungsmethoden" (hyphen at a line break). Both "Bilanzierungsund Bewertungsmethoden" and "Bilanzierungs-und Bewertungsmethoden" must verify via the looser comparison, and a quote with a different word must still fail.

5. prompts/extract_covenants.txt – keep it generic:
   - Replace the id examples "V2-...", "V3-...", "V4-..." with: id = "V<n>-<Kürzel>", where n is the leading number in the contract file name if there is one, otherwise "V".
   - Remove case-specific hints: the explicit mention of "Ausfallbürgschaften" and "counting only a fraction of special items / grants". Keep the generic rule "a percentage weighting → weight as a decimal".
   - Keep the generic checklist of typical definition pitfalls (treatment of subordinated shareholder loans, non-cash interest, voluntary prepayments, grants/special items, provisions). Rephrase it as a list of questions the reader must answer FROM THE TEXT ("Does the contract include or exclude …? Quote it."), not as statements of what the answer is.
   - Add one sentence: "If the contract is silent on a point, do not assume; say so in the component definition."
   Changing the prompt changes its hash, so the cache is invalidated automatically. That's expected.

6. extract.py --compare:
   - Return exit code 1 when there are differences (0 on MATCH).
   - Add the flag --no-cache to force a live run.
   - Also print, per contract: which model actually answered (meta["model"], which may be the fallback) and attempts.

Tests: update or add tests for 1 (no retry on 401 and the fallback is used; budget exceeded → LLMError), 2 (finish_reason "length" → retry), and 4. No network in tests.
Commit: "Step 3b: bounded LLM latency, truncation handling, hyphenation-tolerant quote check, generic extraction prompt".
Done when pytest is green. Show the output.
```

**Review notes:** Accepted, 24/24 green. Spot-checked the prompt: no case-specific hints are left, the checklist is phrased as questions to answer from the text, and the "silent → don't assume" rule is in. `.env.example` is clean. The assistant correctly found that PyMuPDF keeps the line-break hyphen and adjusted the test.

Next: live `--compare --no-cache` with two candidate models, to pick the model.

**Decisions taken in discussion (recorded for NOTE.md):**
- **No orchestrator LLM.** Code runs a fixed pipeline. The LLM is called at fixed points and only returns schema-validated JSON.
- **Email intake becomes an LLM step** (planned for the mail step). Headers and attachments are handled by code. The free-text body becomes a fixed `OrderIntent` JSON (intent, plan year, test date, focus contracts, reply language, unsupported requests, missing info). Code decides what happens next, and the plan year in the email is cross-checked against the Excel headers, never trusted on its own.
- **No separate UI** (the task sheet excludes it). The reply email is the interface, so effort goes into a polished, mail-client-safe HTML reply.
- **Model candidates:** `google/gemini-3.8-flash` vs. `openai/gpt-5.4-mini` (about $0.10 per comparison run).

**Model comparison result (live, cache off):**
- **`google/gemini-3.8-flash`:**
  - One attempt per contract and 35/35 quotes verified (100 %).
  - Every key decision correct: contract 1 has no covenants, contract 4 has exactly 2 covenants (the margin grid was not counted), and the rounding rules and test basis are right.
  - About 2.3 min for all four contracts, with cold cache.
- **Two signature "differences" both turned out to be the model being *more* literal than my golden file**, checked against the contract text:
  - V2: it added "abzüglich eingeforderter, aber noch nicht eingezahlter Einlagen" from the Eigenkapital definition (§ 1, p. 1). That line really is in the contract. **The golden fixture was incomplete, so I corrected it** (new component, mapped to no plan line, so the value stays 0). The answer key is unchanged.
  - V4 DynVG: it split "Finanzverbindlichkeiten" into its five listed types (a)–(e), which is legitimate. Whether the subordinated shareholder loan is correctly excluded can't be seen from signatures; it shows up only in the mapping. **Conclusion: comparing signatures is too strict. The real test is end-to-end values (`--compare-expected` in step 4).**
- **`openai/gpt-5.4-mini`:** every call returned HTTP 404 "No endpoints found that can handle the requested parameters" together with `require_parameters: true`, so each run fell back to Gemini. Without that flag the call works, and our Pydantic validation still protects against a provider that ignores the schema.
- **Close to the output limit:** one Gemini answer used 11,826 of 12,000 output tokens (reasoning tokens count too). Raising the limit.
- **Decision:** primary `google/gemini-3.8-flash`, fallback `openai/gpt-5.4-mini` (a different vendor, so outages aren't correlated). A model whose endpoints reject `require_parameters` gets one retry without that flag.

---

**Coupling audit (my own grep over `src/`, before step 4).** Golden rule from Shivam: no hardcoding, no tight coupling. Findings:

| # | Where | Hardcoded / coupled | Risk |
|---|---|---|---|
| 1 | plan/reader.py | The plan year "2027" appears in column detection and in two plausibility checks ("stand 31.12.2027", "zinsaufwand 2027") | A Wirtschaftsplan 2028 fails or skips checks |
| 2 | plan/reader.py | The sheet names "GuV", "Bilanz", "Finanzierung" are required literally | "Erfolgsplan" / "Planbilanz" / "Darlehensspiegel" are rejected |
| 3 | plan/reader.py | Loan-schedule columns are capped at B..G (`min(max_column, 7)`) | An extra column is silently ignored |
| 4 | plan/reader.py | The plausibility checks are German label keywords inside the code | Changing them means changing code |
| 5 | calc.py | The stress test triggers on a quantity literally named "EBITDA" | A contract saying "EBITDA (bereinigt)", "operativer Cashflow" or "EBIT" gets no earnings stress test |
| 6 | calc.py | German stress_text and formula_text are built inside the calculator | Calculation is coupled to language and presentation, so an English reply can't be produced |
| 7 | fmt.py / calc.py | The unit "T€" is hardcoded | A plan in € or Mio. € is mislabelled |
| 8 | llm.py | The OpenRouter base URL and the OpenRouter-only `provider` parameter are hardcoded | Can't switch provider via config |
| 9 | docstrings | "2027" in docstrings | Cosmetic |

Fine as is: the Gmail hosts (defaults that env overrides); prompts in files (already outside the code); test data and fixtures containing 2027 (test data, not logic); the JsonLLM protocol (already an interface).

---

## Prompt 4a – Decoupling refactor (no new features)

```text
Refactor step. Golden rule of this project: NO hardcoding of business data, years, sheet names, labels, languages or vendors in code, and no tight coupling between layers. Behaviour on the sample must stay identical (all existing tests green, same numbers). Do not change tests/fixtures/* or tests/expected_2027.json.

Findings to fix:

1. Plan year (reader.py): read_plan(path, plan_year: int | None = None).
   - If plan_year is None: detect it. Collect every 4-digit year from the header rows that match a plan-column pattern (see 2) and take the highest year that has a plan column on BOTH the income statement and the balance sheet. Store it in PlanData.meta["plan_year"] and meta["test_date"] = "31.12.<year>".
   - If plan_year is given but not found → PlanFormatError naming the year and the headers that were found.
   - No literal year anywhere in src/.

2. Layout as configuration, not code: create config/plan_layout.toml (read with stdlib tomllib) and src/finn/plan/layout.py (a typed loader with pydantic models). Content:
   - amount_unit = "T€" (also allow detection from a cover-sheet line like "Alle Beträge in T€"; config wins if set).
   - [roles.income_statement], [roles.balance_sheet], [roles.loan_schedule], each with:
     sheet_aliases (e.g. ["GuV", "Erfolgsplan", "Gewinn- und Verlustrechnung"]),
     content_keywords (labels whose presence identifies the sheet if no alias matches, e.g. ["jahresüberschuss", "umsatzerlöse"]),
     column_mode = "plan_year" | "all_numeric",
     normalise_expenses = true|false.
   - [plan_column] patterns = regexes with a {year} placeholder, e.g. 'plan\s*{year}', '{year}\s*\(plan\)'.
   - Sheet resolution: alias match (case-insensitive) first, then the content-keyword score; ties or no match → PlanFormatError naming the role and the sheets found. PlanItem ids keep the REAL sheet name (e.g. "Erfolgsplan!D17"). PlanItem gets a `role` field.
   - loan_schedule with all_numeric: take EVERY column that has a header and numeric/formula values. No column cap.
   - Expense normalisation applies to the role that has normalise_expenses = true, not to a sheet name.
   - Path via env PLAN_LAYOUT (default config/plan_layout.toml).

3. Plausibility checks as data: [[checks]] entries in the same TOML, evaluated by a small generic engine (plan/checks.py):
   [[checks]]
   id = "bilanz_ausgeglichen"
   description = "Summe Aktiva = Summe Passiva"
   lhs = [{ role = "balance_sheet", label_all = ["summe aktiva"] }]
   rhs = [{ role = "balance_sheet", label_all = ["summe passiva"] }]
   tolerance = 0.5
   Each term: role, label_all (all keywords must appear in the label), optional label_none, optional column_all (keywords in the column header, for the loan schedule), optional sign (default +1). {year} placeholders are allowed and filled with the plan year.
   Move the 4 existing checks into the TOML. A term that matches no item or several → the check is skipped with a warning naming the check id. Check results go into PlanData.checks as structured objects (id, passed, lhs, rhs, message_key), not as German text. Text is produced by the presentation layer (see 6).

4. Covenant stress driver (models.py, calc.py, extraction prompt):
   - Add Quantity.kind: Literal["earnings", "equity", "total_assets", "debt", "cash", "debt_service", "taxes", "other"]. The LLM classifies each quantity when extracting (that's reading, not calculating). Add it to the extraction prompt with one generic line per kind. Update the two golden fixture files? NO, don't touch them; instead give `kind` the default "other" and apply the overlay that already exists in tests/fixtures/golden_kinds.json ({contract_file: {quantity_name: kind}}, written by hand) when loading the golden fixtures in tests. Do not modify it.
   - Stress logic, generic: if any quantity of kind "earnings" is in the covenant tree → earnings-decline stress on that quantity (if several, the one closest to the covenant root). Else if unit == "percent" and the numerator tree contains kind "equity" → the additional-loss stress (L reduces numerator and denominator). Else → the generic numerator/denominator stress. No quantity NAME is compared anywhere.

5. calc.py returns numbers, not prose:
   - Result keeps every number and gets a structured `stress: StressResult(kind: "earnings_decline" | "equity_loss" | "numerator_change" | "already_breached", driver_quantity: str, pct: float | None, amount: float | None, limit: float)` and a structured `formula: FormulaParts(numerator_terms, denominator_terms, scale, value_raw, value_reported, rounding_decimals)`.
   - Remove the German stress_text / formula_text generation from calc.py.

6. Presentation layer: src/finn/texts/ with de.toml and en.toml (message templates with named placeholders), and src/finn/render.py with render_stress(stress, lang, unit), render_formula(formula, lang, unit), render_check(check, lang), using fmt.py with locale-aware number formatting (de: 1.234,5 / en: 1,234.5). The CLI table and later the mail report use render.py. The language comes from a parameter (default from env REPLY_LANGUAGE=de), never hardcoded in logic.
   - Existing tests that asserted German stress_text now assert the structured stress values, plus a render test checks the German sentence for one case, e.g. "Das EBITDA dürfte um 15,5 % (6.216 T€) sinken …".

7. Unit: fmt_amount(x, unit, lang) uses the plan's amount_unit; no "T€" literal outside config/texts.

8. LLM vendor (llm.py, config.py): LLM_BASE_URL (default https://openrouter.ai/api/v1) and LLM_PROVIDER_PARAMS_JSON (default '{"require_parameters": true}', sent as extra_body["provider"] only if non-empty). Rename the env vars to LLM_API_KEY / LLM_MODEL / LLM_MODEL_FALLBACK, keeping the OPENROUTER_* names as accepted aliases for backward compatibility. Update .env.example.

9. Docstrings: remove the year-specific wording.

10. Guard test – tests/test_no_hardcoding.py: scan src/finn/**/*.py and fail if any of these appear outside comments: a 4-digit year 19xx/20xx in a string literal; the strings "GuV", "Bilanz", "Finanzierung", "EBITDA", "T€", "openrouter.ai". Allow-list exactly: config.py may contain the default base URL; nothing else. This test enforces the golden rule from now on.

11. New behaviour tests:
   - A copy of the workbook with the sheets renamed to "Erfolgsplan", "Planbilanz", "Darlehensspiegel" gives identical results.
   - A copy where every "2027" in the headers is replaced with "2028" → plan_year 2028 detected, results identical, the checks still run (not skipped).
   - A copy with an extra numeric column on the loan schedule → the column is read (new item ids exist).
   - A contract fixture variant where the earnings quantity is renamed "Operatives Ergebnis vor Abschreibungen" (kind=earnings) → the same stress values.
   - render_* in "en" gives English sentences with English number formatting.

Commit: "Step 4a: decouple layout/year/labels/language/vendor via config; structured stress and formula; guard test".
Done when the full pytest suite is green. Show the output and the content of config/plan_layout.toml.
```

**Review notes:** Accepted (commit `1d4f23a`); I re-ran 36/36 green independently.
- Checked the decoupling myself on a variant workbook (sheets renamed to Erfolgsplan/Planbilanz/Darlehensspiegel, every "2027" changed to "2028"): plan_year 2028 is detected, all 4 config-driven checks still pass, and the values and stress results are identical.
- My own grep of `src/` finds no year, sheet name, "EBITDA", "T€" or vendor URL outside the allowed default base URL in config.py.
- The stress driver now uses `Quantity.kind`.
- Open: the step-4 section-0 fixes were never applied (Prompt 4 had been pasted before they were added), so they're carried into step 4b.

---

## Prompt 4 – Term-to-cell mapping (LLM) and the first full offline run

```text
Step 4 of Finn: map every definition line to cells of the business plan with the LLM, then run the whole chain offline from an .eml file: contracts → covenants → mapping → calculation. No report formatting and no sending in this step; we just print and write the audit file.

## 0. Fixes from the live model comparison (do these first)
- llm.py: if a call fails with HTTP 404 and the message contains "No endpoints found that can handle the requested parameters", retry THE SAME model once WITHOUT `provider.require_parameters` (keep response_format). Record "require_parameters disabled" in attempt_log. Pydantic validation still guards the output. Add a test.
- Raise the default LLM_MAX_TOKENS to 24000 (.env.example and config): one live answer used 11,826 of 12,000 tokens.
- extract.py --compare: the leaf-signature check is too strict. A model may legitimately split "Finanzverbindlichkeiten" into its listed types, or add a deduction line the contract really contains. Keep printing the signatures, but report them as "INFO (Struktur abweichend)" and do not count them as differences. Differences stay: has_covenants, covenant count, limit/operator/unit, rounding_decimals, test_basis, unverified quotes. The real proof of correctness is the end-to-end value check below (--compare-expected).
- Note: tests/fixtures/golden_covenants.json and golden_mapping.json were updated by me (the V2 "Wirtschaftliches Eigenkapital" now has the deduction line "Eingeforderte, noch nicht eingezahlte Einlagen", which maps to no cells). Do not modify them; just make sure all tests pass with them.

## 1. Plan reader additions (plan/reader.py, models.py)
- PlanItem gets two code-filled fields: `is_total: bool` (the cell is a formula) and `depends_on: list[str]` (all PlanItem ids the formula depends on, transitively; the evaluator already walks them). Default empty / False.
- plan_item_catalog(plan, include_values: bool = False) -> str. Default WITHOUT values: "id | sheet | label | [Summe]". The LLM must pick cells by meaning, never by number, so it never sees a value. Group the lines by sheet, with a header line per sheet that names the sheet, its role and the column used (taken from PlanData.meta, never a literal).

## 2. src/finn/mapping.py
map_terms(contract: ContractCovenants, plan: PlanData, llm: JsonLLM, cache_dir, prompt_path) -> tuple[list[TermMapping], dict meta]
- Leaf components = every Component with ref is None, in every Quantity of the contract. Contracts without covenants → [] (no LLM call).
- LLM response schema MappingResponse { mappings: list[MappingItem{quantity, component, item_ids, rationale}] }. Code sets contract_file afterwards.
- User message: the contract label, then per leaf component: quantity name, component name, sign, weight, the component definition text, and the quantity's source quote. Then the plan catalog WITHOUT values.
- Code validation after the call (collect ALL errors, then decide):
  a) every leaf component is mapped exactly once; no extra or unknown (quantity, component) pairs;
  b) every item id exists in plan.items;
  c) no id used twice within the same component;
  d) double counting: if a component contains a total AND any item that total depends on (depends_on) → error naming both ids;
  e) item_ids may be empty only if the rationale says the plan has no such line.
  On errors: ONE more LLM call with the original messages plus a German/English list of the errors ("Fix these problems: …"). If it still fails → raise MappingError with the error list. Never patch a mapping in code.
- Warnings (meta, not errors): the same cell used with opposite signs within one quantity; a Bilanz cell used in a GuV-period quantity, or vice versa (based on the covenant's test_basis).
- Cache: key = sha256(contract JSON as extracted) + sha256(catalog WITHOUT values) + sha256(prompt) + model. Because values are not part of the key, a plan with changed numbers but the same structure reuses the mapping. That's correct and keeps live runs fast. Cache file cache/mapping_<key[:16]>.json.

## 3. prompts/map_terms.txt (generic; no case-specific answers, no numbers)
- Role: a German Finanzreferent who knows HGB financial statements and municipal-utility business plans (Erfolgsplan/GuV, Planbilanz, Darlehensspiegel/Finanzierungsplan).
- Task: for each definition line, choose the plan cells whose sum best represents that line AS DEFINED IN THE CONTRACT. Respect the exclusions in the definition text strictly (e.g. if interest from the accretion of provisions is excluded, do not take a total that contains it; prefer itemised loan-schedule lines; if subordinated shareholder loans are excluded/included, pick cells accordingly; if voluntary prepayments are excluded, do not take that column).
- Signs and weights are applied by code. Expense lines are provided as positive amounts, so pick the cell and do not think about its sign. Never choose a total together with its own components. A "davon" line is a sub-item of the line above it.
- Choose only ids from the catalog. If the plan has no line for an item (e.g. no extraordinary items), return an empty list and say so in the rationale.
- rationale: one German sentence that names the exclusion rule you applied.
- Return only JSON.

## 4. src/finn/pipeline.py – the only place that knows the order of steps
@dataclass RunContext: run_id (timestamp + short uuid), started_at, settings, llm, paths.
run_check(xlsx_path: Path, ctx) -> RunOutcome(report: RunReport, audit: dict)
 1. plan = read_plan(xlsx_path)            (PlanFormatError → propagate, the caller decides how to reply)
 2. contracts = extract_all(...)           (cached)
 3. mappings = map_terms(...) per contract (cached)
 4. results = evaluate_contract(...) per contract
 5. report = RunReport(results, contracts_without_covenants, warnings = plan.checks + every extraction/mapping warning, each prefixed with the contract label)
Every step records into the audit dict: duration, cache hit/miss, model, tokens, warnings, and the step's input/output (extraction JSON, mapping JSON, plan meta, the full plan catalog WITH values, results).
src/finn/audit.py: write_audit(audit, runs_dir) -> Path writes runs/<run_id>.json (UTF-8, indent 2). Never write secrets: strip any key containing "key", "password" or "token" from settings before dumping.

## 5. CLI `finn run --eml <file>` (main.py)
- Parse the .eml with the email stdlib (policy=default). Extract From, Subject, Message-ID, the plain-text body and the FIRST attachment ending in .xlsx (case-insensitive). Save it to runs/<run_id>/<original filename>.
- No .xlsx attachment → print a clear German message and exit code 2 (no pipeline run). The mail step will turn this into a reply later.
- Run run_check and print a compact console table: Vertrag | Covenant | Wert | Grenzwert | Headroom | eingehalten | Stress. Then the contracts without covenants with their reason, then warnings, then the audit file path. Exit 0.
- Option `--compare-expected` prints, after the table, a check of every result against tests/expected_2027.json (match by contract file + unit + operator + limit) with ✓/✗ and the difference. This is the end-to-end proof that LLM extraction + LLM mapping + code calculation give the hand-checked numbers.

## 6. Sample emails
scripts/make_samples.py (run once, results committed) creates in data/samples/:
- order.eml: From "Katrin Möller <k.moeller@stadtwerke-musterstadt.example>", To finn@example.com, Subject "Covenant-Prüfung Wirtschaftsplan 2027", short German body asking for the covenant check, Wirtschaftsplan_2027.xlsx attached, a Message-ID and a Date.
- order_informal.eml: Subject "WP 27 – bitte Covenants checken", informal German body mentioning "zum 31/12/27" and a focus on the syndicated loan, same attachment renamed "Wirtschaftsplan_2027_Entwurf_v3.xlsx".
- order_english.eml: Subject "Covenant test FY27", English body, same attachment.
- no_attachment.eml, wrong_attachment.eml (a .pdf instead of .xlsx), unrelated.eml ("Können Sie einen Raum für Donnerstag buchen?").
The extra samples are used in the mail step; create them now so the test data exists.

## 7. Tests (no network)
tests/test_mapping.py (FakeLLM):
- FakeLLM returns the golden mapping for the golden contracts → no errors, meta.cache_hit False; a second call is a cache hit.
- Unknown item id → retry with an error message that contains the id; the second answer is valid → success.
- Total + its part (e.g. "Bilanz!D22" together with "Bilanz!D18") → error mentions double counting.
- A missing component → error; still failing after the retry → MappingError.
- The user message sent to the LLM contains NO plan values (assert "313200", "313.200" and "40200" are not in it).
- Changing a plan VALUE (same structure) → mapping cache hit. Renaming a row label → cache miss.
tests/test_pipeline.py (FakeLLM for extraction + mapping returning the golden fixtures):
- run_check on the sample xlsx → four results matching expected_2027.json (value_reported, met, headroom); one contract without covenants; the audit contains every step; the audit file contains no secret values (set a dummy OPENROUTER_API_KEY and assert it does not appear).
- `finn run --eml data/samples/order.eml` via main(argv) with the FakeLLM injected → exit 0; no_attachment.eml → exit 2.
For tests, make it possible to inject an LLM into the pipeline and the CLI: a module-level factory function `make_llm(settings)` that tests can monkeypatch.

## Rules
Do not modify the fixtures or the answer key. Only llm.py talks to the network. calc.py stays pure.
Update TIMELOG.md. Commit: "Step 4: LLM term mapping with validation and cache, pipeline, offline .eml run, sample emails".

## Done when
pytest is green (show it). Then STOP and give me the exact live command:
finn run --eml data/samples/order.eml --compare-expected
I will run it myself with the chosen model.
```

**Review notes:** Accepted on results (30/30 green, including "no plan values in the mapping prompt" and "secrets redacted in the audit"). Note on order: Prompt 4 ran BEFORE Prompt 4a, so the decoupling refactor applies on top of step 4. Code review is done together with 4a.

---

## Steps 4b + 5 – built directly by the assistant (no prompt)

From here Shivam was away from the laptop and could no longer paste prompts into the IDE, so I wrote, tested and reviewed this code myself in my own workspace, then wrote the files into the repo. The specification is the Prompt 5 text below, which was not sent to the coding assistant. Result: 59/59 tests green, the guard test still green. **Not yet committed to git**; Shivam commits it when he's back.

**Step 4b (carried over from Prompt 4, section 0):**
- llm.py: a 404 "No endpoints found that can handle the requested parameters" retries the same model once without `provider.require_parameters`, at most once per model (then the fallback). 2 new tests.
- Default `LLM_MAX_TOKENS` raised to 24000.
- `--compare`: structure differences are now `INFO`. Unverified quotes now count as a difference.

**Step 5 (reply email):**
- `report.py`:
  - Verdict line by code.
  - Overview with 4 columns (metric + contract, value + limit, headroom + bar, status badge). Checked at 760 px and at 390 px phone width.
  - Per covenant: stress test, calculation, contract sources (unverified ones flagged ⚠), plan cells with label and value, plan-basis note.
  - Contracts without covenants, notes, method footer.
  - Plain-text version.
  - Simple replies for missing/wrong attachment, plan format error, unrelated mail, clarification, internal error.
- `summary.py`:
  - The LLM gets only pre-rendered strings plus the verdict ("don't repeat it").
  - A number tokenizer (de/en formats, dates, Unicode minus) checks every number in the answer against the facts.
  - 1 retry naming the offending number, then a template.
  - Any exception means a template, so the reply is never blocked.
  - In testing, a too-long sentence was correctly rejected, which is why the length limit went to 280 characters.
- `mail/outbox.py`: threaded MIME (In-Reply-To, References, the marker header `X-Finn-Reply`), audit JSON attached, SMTP send function (used in step 6).
- `config/report.toml`: thresholds and colours. All words live in `texts/de.toml` and `texts/en.toml`, including the CLI messages.
- CLI: `finn run --eml … [--lang de|en] [--preview] [--compare-expected]` writes `runs/<id>/reply.html|.txt|.eml`.
- Tests: `test_report.py` (13) and `test_summary.py` (7). The existing `test_pipeline` CLI test exposed that the summary must survive *any* LLM exception, which is fixed in code, not in the test.

**Step 5b – no regex in `src/` (built directly; triggered by Shivam: "regex is very close to hardcoding").** Audit of 13 regex uses, in three groups:
- **(A) business knowledge as regex:** the plan-header patterns and the "Alle Beträge in" unit pattern in the config. Replaced by keyword lists: a header is the plan column if it has a 4-digit year and a word starting with a configured keyword; the unit is the first configured candidate after a configured marker.
- **(B) Excel formula parsing:** replaced by openpyxl's formula tokenizer plus a small recursive-descent evaluator. It now also supports × ÷, parentheses, `'quoted sheet'!$A$1` and multi-argument SUM, and raises on any unsupported function instead of guessing.
- **(C) text helpers:** replaced with `str.split`, `partition` and a character scanner.
- The guard test now also fails on any `import re` in `src/`. 4 new tests; 63/63 green.

**Step 6 – mail agent (built directly by the assistant).** Design from the chat (an "intake" LLM step instead of keyword rules, but no orchestrator):
- **Code reads** headers, attachments, Message-ID and References.
- **The LLM reads** only the body, into `OrderIntent` {intent, plan_year, test_date, focus_contracts, reply_language, salutation (never guessing gender or title), unsupported_requests, missing_info, deadline}. Attachment bytes never reach it; a test checks this.
- **Code decides:** `other` → polite "only covenant checks". An .xlsx is attached → run. `unclear` with open questions → ask back. Another attachment → "wrong file". Nothing attached → "please attach the plan".
- **The email never overrides the file:** a different plan year or test date in the mail becomes a note in the reply. Unsupported wishes are listed as "Nicht Teil dieser Prüfung".
- **If the LLM fails,** a structural fallback is used (.xlsx attached = check), so the mail is still answered.
- **One handler** serves `finn run --eml … [--send]` and `finn watch [--once]`. Every exception becomes an apology with the run ID; the traceback goes only to the audit file.
- **Watch loop:**
  - Exactly-once store in `runs/processed.json` (survives restarts).
  - Loop protection: own address, the `X-Finn-Reply` marker, `Auto-Submitted`, `Precedence: bulk`, mailer-daemon/no-reply. All configurable in `config/agent.toml`, no code change needed.
  - Send retries (3, then given up and logged), reconnect after IMAP errors.
- **Tests:** 20 new (intake, decisions for all 6 sample mails, bad input, broken Excel, internal error, threading, English, exactly-once, own reply in the inbox, flaky SMTP, IMAP down). 83/83 green.
- **Not testable from my workspace:** a real Gmail connection (IMAP/SMTP aren't reachable from there) and live LLM calls (openrouter.ai is blocked from there). Both run on Shivam's laptop.

**Step 7 – docs (built directly by the assistant).**
- README: quick start in 5 commands, command table, pipeline sketch, folder map, robustness, Gmail setup. Every command and env name was checked against the code.
- NOTE: the assumptions (above all the shareholder-loan reading), the 6 changes to the brief with reasons, AI use (including the bugs found in review), next steps. Tightened from 779 to about 550 words to stay near one page.
- TIMELOG: a "built by" column (coding assistant / Claude directly / Shivam), 6.5 h total.
- This log is copied into the repo as `docs/PROMPTS.md`.

**Independent review (a separate reviewer agent, not anchored on my conclusions).** It got only the task sheet, the PRD, the contracts, the Excel and the code.
- **Numbers:** it re-derived all four covenants from the contracts, and they match the answer key, the citations and the cell choices.
- **Weaknesses found (13):** mainly around the LLM steps: wrong cell choices or limits not caught by code, a weak summary check, sign handling, relative paths, reliance on the unread flag, EBITDA ≤ 0, a case-specific mapping prompt, one error killing the whole reply.
- I spot-checked two claims myself (the year/column names in the mapping prompt, the UNSEEN search); both were true.

**Step 7b – review batch A (built directly by the assistant).** 93/93 green.
1. Relative paths are resolved against the project root; no contract PDFs → an error, never an empty "all fine".
2. IMAP: list mail since yesterday (read or unread). The processed store decides what's done. Mail from before the start is only answered if unread. A failed send resends the saved reply instead of re-running the pipeline.
3. Sign rule from the sheet structure: a negative input is an expense only if a subtotal adds it up (or it's a "davon" line). The configured cross-checks are now blocking: an inconsistent plan gets a "cannot certify" reply. Verified on the reviewer's two cases (`=-4930-170` → read correctly; a negative Jahresüberschuss typed over the formula → blocked).
4. A ratio over a non-positive denominator: "≤" covenants count as breached, the value shows as "nicht definiert", and the stress test is "not applicable". No more internal-error reply.
5. Each limit must appear in its quoted clause (shared no-regex number scanner). Otherwise one corrective re-extraction naming the covenant, then a ⚠ in the reply. Tested with a limit taken from the margin grid.
6. Mapping prompt rewritten as a method, without the year or column names. The keyword list on rationales is removed and replaced by structural rules (no all-empty quantity) plus a visible note for every "no plan position, set to 0".
7. Errors are isolated per contract (extraction, mapping, calculation). The reply says "Achtung: … unvollständig" and names the failed contract.
8. No key → a clear message and exit code 2. `cache/` is committed (the verified extraction) so a fresh clone needs no contract reading.

**Step 7c – review batch B (built directly by the assistant).** 102/102 green.
1. **Summary:** every LLM sentence carries `covenant_ids`. Its numbers must come from those covenants (or general facts), and status words (from `texts/*.toml`, `[summary_claims]`) may only be used for covenants that have that status. The reviewer's example ("3,37 bereits verletzt" under "alle eingehalten") is now rejected, with a test.
2. **Year:** the year from the email if the file has a plan column for it; otherwise the first plan year (the reviewer saw the latest one being picked in medium-term plans). The test date comes from config (`test_date_template`), not `31.12.` in code.
3. **Unsupported Excel functions** (e.g. ROUND): Excel's saved value is used, the formula's dependencies are kept for the double-counting check, and the reply shows a note. Without a saved value the file is still refused.
4. **Reply remarks are language-neutral keys** (`notices.py` plus `[notices]` in de/en): quote notes, limit warnings, empty plan lines, Excel errors. An English reply no longer carries German remarks. The control flow that compared against a German string is gone. Duplicate quote warnings are removed. The hardcoded role name in the mapping check is replaced by `measure = flow|stock` in the config.
5. **Watcher:** reply-all (To/Cc without Finn, configurable), existing "AW:"/"Antw:" prefixes kept, `noreply` matched anywhere in the address.
6. **Found while testing:** the golden fixtures in the repo were the *old* version (without the V2 "Einlagen" line I added in the afternoon); they must have been overwritten somewhere in between. Restored, and two tests that hardcoded fixture sizes now count from the fixture.
- **Deliberately not done:** the stress test still uses the unrounded value (conservative; the real breach point at the contract's rounding is slightly further away). Noted in the NOTE.

## Prompt 5 (specification only, not sent) – The reply: email-safe HTML report, LLM summary with number check, reply templates

```text
Step 5 of Finn: build the reply email that the head of finance receives. It is the product's only user interface, so it must look professional, read in 20 seconds, and make every number traceable. Use the presentation layer from step 4a (texts/de.toml, texts/en.toml, render.py, fmt.py). No language strings, years, units or thresholds in code.

## 1. src/finn/report.py
build_reply(outcome: RunOutcome, ctx: ReplyContext) -> Reply(subject: str, html: str, text: str, attachments: list[(filename, bytes, mime)], summary_meta: dict)
ReplyContext: lang ("de"|"en"), salutation (str | None), original_subject, focus_contracts (list[str]), plan_meta (plan year, test date, amount unit, file name).

Layout (top to bottom):
 a) Greeting + a 1-line verdict generated by CODE from the results, e.g. "Alle 4 Covenants werden zum 31.12.2027 auf Planbasis eingehalten. Am knappsten: Eigenkapitalquote (Konsortialkredit) mit 0,7 Prozentpunkten Puffer." If any covenant is breached, the verdict says so first.
 b) Summary: 2–3 short sentences from the LLM (see 2). If the LLM fails or the number check fails → a deterministic template summary from texts/*.toml. The reply is never blocked by the LLM.
 c) Overview table: one row per covenant: Vertrag | Kennzahl | Wert | Grenzwert | Puffer | Status. Status is a badge: "eingehalten" (green), "knapp" (amber), "verletzt" (red). "knapp" = met but relative headroom (headroom / limit) below report.tight_threshold from config (default 0.10). Under Puffer, a small horizontal bar built from two table cells with percentage widths (bar length = min(relative headroom / 0.5, 1)), no images.
 d) One block per contract with covenants: heading with the contract label, then per covenant:
    - value, limit, headroom, status (as above);
    - "Belastungstest": the rendered stress sentence;
    - "Rechenweg": the rendered formula with the numbers filled in;
    - "Quelle Vertrag": every contract source as "§ … , S. …: „quote“". Mark unverified quotes with "⚠ nicht verifiziert".
    - "Quelle Wirtschaftsplan": every plan cell as "Sheet!Cell – label – value unit".
    - The contract's plan_basis_note as a small note (e.g. "Planbasis: keine Kündigung, aber Gesprächspflicht").
 e) Contracts without covenants: label + reason + its source quote.
 f) Hinweise: plausibility check results that failed, extraction or mapping warnings, unverified quotes. If there are none, omit the section.
 g) Footer: "Alle Kennzahlen wurden deterministisch im Code berechnet; das Sprachmodell hat Verträge gelesen, Zellen zugeordnet und die Zusammenfassung formuliert." + the run id + the plan file name + the plan year. Text from the templates.
 Order the contracts so that focus_contracts come first, otherwise by the tightest relative headroom.

Email-safe HTML rules (Gmail, Outlook desktop/web, Apple Mail):
- Table layout only (role="presentation"), max width 680 px, centred; inline CSS on every element; no <style> dependency except an optional @media block for mobile; no JS, no external images or fonts (system font stack), no <details>, no flexbox or grid, no CSS variables.
- Colours from config ([report.colors] in a new config/report.toml: ok, tight, breach, text, muted, border, bg). Text contrast ≥ 4.5:1. Status is never shown by colour alone (always the word too).
- Numbers right-aligned, tabular-nums.
- Escape all LLM- or contract-derived text (html.escape).
- Plain-text alternative with the same content (aligned columns, no HTML).
- Subject: "Re: <original subject>" (no double "Re:"); taken from the templates for language.
- Attachment: the audit JSON (runs/<id>.json) if config report.attach_audit = true (default true), named finn_audit_<run_id>.json.

## 2. LLM summary with number check (src/finn/summary.py + prompts/summary.txt)
- Input to the LLM: the verdict facts as a small JSON with ALREADY RENDERED strings (e.g. "30,7 %", "0,7 Prozentpunkte", "3,37", "EBITDA −3,6 %"), the language, and the focus contracts. The LLM gets no raw floats, so it has nothing to compute.
- Schema: Summary { sentences: list[str] (1–3 items, each ≤ 220 chars) }.
- Prompt: write as a concise financing assistant to the head of finance; name the tightest covenant and what would break it; mention that the check is on plan basis; no advice beyond the facts; no new numbers; no markdown; the requested language.
- Number check in code: extract every number token from the summary (handle 1.234,5 / 1,234.5 / 30,7 / 3,37 / −3,6 / percentages / years). Each token must equal (string-normalised) a number from the allowed set = every number in the facts JSON + small counts 1–10 + the plan year. If ANY token is not allowed → one retry with the list of offending tokens; if it fails again → template summary + a warning in the audit log ("Zusammenfassung verworfen: nicht belegte Zahl 31,5"). Log the attempts in the audit.

## 3. Templates for non-result replies (same report module, texts/*.toml)
reply_missing_attachment, reply_wrong_attachment(filename), reply_plan_format_error(message), reply_unrelated, reply_needs_clarification(questions), reply_internal_error(run_id). Each one is short, polite, signed "Finn – digitaler Referent Finanzierung" (from config), and in the requested language. The mail step will use them.

## 4. CLI
`finn run --eml <file> [--compare-expected] [--lang de|en] [--preview]` now also builds the reply and writes runs/<run_id>/reply.html, reply.txt and reply.eml (a complete MIME message: multipart/alternative text+html, plus the attachment, with From = FINN_MAIL_ADDRESS, To = original sender, In-Reply-To and References = original Message-ID). --preview opens reply.html in the default browser. Nothing is sent in this step.

## 5. Tests (no network; FakeLLM)
tests/test_report.py:
- With the golden pipeline outcome: the HTML and text contain every value_reported, limit and headroom as rendered in German; four covenant rows; V4 EKQ and V4 DynVG have the badge "knapp", V2 and V3 "eingehalten"; the contract without covenants appears with its reason; every plan cell id used by the results appears; the footer is present.
- The HTML contains no "<script", no "http" image sources, no "display:flex"/"grid".
- An unverified quote shows the warning marker.
- --lang en: English headings, English number format (30.7 %).
- Subject: "Re: Covenant-Prüfung Wirtschaftsplan 2027"; an incoming "Re: X" stays "Re: X".
- reply.eml parses back with the email stdlib: multipart/alternative, In-Reply-To set, attachment present.
tests/test_summary.py:
- FakeLLM returns sentences with only allowed numbers → accepted.
- FakeLLM returns "31,5 %" (not in the facts) → retry; again invalid → template summary used + warning logged.
- Number tokenizer unit tests for the formats above, including "−3,6 %" with a Unicode minus.
- The LLM user message contains no raw float like "30.6833".

## Rules
No strings in code that are meant for the user: all in texts/*.toml. Thresholds and colours in config/report.toml. The guard test from step 4a must stay green. Do not modify the fixtures or the answer key.
Update TIMELOG.md. Commit: "Step 5: email-safe HTML reply, LLM summary with number check, reply templates, reply.eml preview".

## Done when
pytest is green (show it), and run
finn run --eml data/samples/order.eml --preview
with the FakeLLM disabled (the real model). Tell me the path to reply.html so I can review it.
```

**Review notes:** _(filled in after review)_

---

## Prompt 8 – Live verification on the laptop

```text
Step 8 of Finn: verify the project live on this machine. Do NOT change any source code, prompts, config, tests, fixtures or the answer key in this step. If something fails, STOP and report the exact output; do not "fix" it by editing code or loosening tests. Never print the contents of .env or any key or password.

1. Environment check (read-only):
   - Show `git status --short` and `git log --oneline -5`.
   - Confirm that .env exists and is NOT tracked: `git check-ignore .env` must print ".env". Show only the NAMES of the variables set in .env (no values).
   - Reinstall: `pip install -e ".[dev]"`.

2. Offline tests:
   - Delete the old LLM cache files: every `cache/*.json` (keep cache/.gitkeep). They were made with older prompts.
   - Run `pytest -q`. Expected: 102 passed. Show the summary line. If anything fails, stop and show the failure output.

3. First live run (real LLM via OpenRouter; takes a few minutes because the 4 contracts are read again):
   - Run: `finn run --eml data/samples/order.eml --compare-expected`
   - Show the COMPLETE console output (table, notes, comparison block).
   - Expected: 4 covenant rows, the comparison shows ✓ for all 4 covenants (34.32 % / 1.56 / 30.7 % / 3.37, all met), and contract 1 is listed without covenants.
   - Show the path of the audit file and of runs/<id>/reply.html. From the audit file show only: the extraction meta per contract (model, attempts, quotes_verified/quotes_total, warnings), the mapping meta per contract (model, attempts, warnings) and reply.summary.source.
   - If any ✓ is missing: STOP. Show, for the failing covenant, its extraction (quantities and components) and its mapping (item_ids per component) from the audit file. Do not change anything.

4. Second live run (must be fast, from the cache):
   - Run the same command again and show the output and the duration. Expected: same results, and the contracts and mappings come from the cache.

5. Bad-input replies (offline decisions, real LLM for intake):
   - Run `finn run --eml <file>` for each of: data/samples/no_attachment.eml, data/samples/wrong_attachment.eml, data/samples/unrelated.eml, data/samples/order_english.eml, data/samples/order_informal.eml.
   - For each, show the exit code, the "Entscheidung/Decision" line and the first 6 lines of the reply text. Expected: missing_attachment, wrong_attachment, unrelated (exit 2 each); run in English; run with the syndicated loan listed first.

6. Commit (only if steps 2–4 succeeded):
   - `git add -A`, then show `git status --short` and check that NO .env, no runs/*.json and no key appears in the staged files (search the staged diff for "sk-or" and for the value of MAIL_APP_PASSWORD without printing it; report only found/not found).
   - Commit with the message: "Steps 4b-7c: reply email, mail agent, review hardening, docs, verified cache".
   - Show `git log --oneline -10`.

7. Report at the end: a short table "step | result | notes", plus the full console output of step 3.
Do not run `finn watch` and do not send any email in this step.
```

**Review notes:**
- First attempt stopped at step 2 (97/101): seven files on disk had been rolled back to older versions (most likely stale editor buffers saved over them). Re-synced; 102 passed.
- The live run used `gpt-5.4-mini` as the primary model (the `.env` still had it first), not the measured `gemini-3.8-flash`. Two of four covenants came out right; two problems showed up:
  - Contract 3: the numerator of the coverage ratio also contained the debt service (the denominator), and three quantities no covenant needs (cure right, business plan, annual accounts) were extracted. Mapping those failed, so the contract was reported as failed. Nothing wrong was sent.
  - Contract 4, leverage: 3.17 instead of 3.37. The model listed the subordinated shareholder loan as a subtracted line *and* the mapping took the bank-loan line, which already leaves it out, so it was subtracted twice.
  - Smaller: excluded items listed with weight 0 were reported as "outside 0–1"; a page-correction notice appeared twice.
- The IDE's report contained a `git status` that does not match the repository (it listed files such as `cli.py` and `report/excel.py` that don't exist; the index was checked directly). Its description of contract 3's extraction also didn't match the audit file. The console output and the audit file itself were real.
- Fixes in step 8b (below). Re-run as Prompt 8c with the measured model as primary.

---

## Step 8b – Hardening after the first live run (built directly by the assistant)

Not a prompt: written and tested directly in the assistant's workspace, then copied into the repository.

- **Structural check of every extraction** (`structural_problems`): a numerator that contains its own denominator (or the reverse), or a reference to an undefined quantity, triggers the one corrective re-extraction (together with the limit-in-quote check). If it is still wrong, that contract is refused (`ExtractionStructureError`) and the reply says the result is incomplete.
- **Normalisation** (`normalize_contract`): components with weight 0 are dropped, and quantities that no covenant needs are dropped before mapping. Recorded in the audit under `normalized`, not shown as warnings.
- **Carve-outs**: a new `Component.carve_out` flag for "X, excluding Y". Code subtracts Y only from plan cells that actually contain it (the chosen cells, the inputs of chosen totals, and their "davon" lines via the new `PlanItem.part_of`). Otherwise the cells are dropped from the carve-out and the reply says so (`carve_out_not_contained`). The mapping cache stores the raw LLM choice; the rule is re-applied on every load.
- Duplicate notices are removed. Both prompts state the new rules (only needed quantities; numerator and denominator don't contain each other; no weight-0 lines; when to use `carve_out`).
- `tests/test_robustness.py`: replays the three failure modes. 108 tests pass. Replaying the live run's own extraction through the new checks flags contract 3 and cleans contract 2 as intended.

---

## Prompt 8c – Live verification, re-run with the measured model

```text
Step 8c of Finn: re-run the live verification. The files on disk were updated outside this editor.

Ground rules for this step:
- First close all open editor tabs WITHOUT saving (or revert them to the version on disk). Never save an editor buffer over a file on disk in this step.
- Do not edit any file in src/, tests/, prompts/, config/ or docs/. Do not run git checkout, git restore, git stash or git reset.
- Never print the contents of .env or any key or password.
- Copy all command output verbatim from the terminal. If you didn't run a command, don't show output for it.
- If something fails, STOP and report the exact output.

1. Model settings (read-only; .env was already updated by hand). Show the variable NAMES set in .env, and the VALUES of only these three: OPENROUTER_MODEL, OPENROUTER_MODEL_FALLBACK, LLM_MAX_TOKENS. Expected: google/gemini-3.8-flash, openai/gpt-5.4-mini, 24000. If they differ, STOP and report.

2. Run `pip install -e ".[dev]"`, delete every cache/*.json (keep cache/.gitkeep), then run `pytest -q`. Expected: 108 passed.

3. Run `finn run --eml data/samples/order.eml --compare-expected` and show the COMPLETE console output.
   Expected: ✓ for all 4 covenants (34.32 % / 1.56 / 30.7 % / 3.37, all met), contract 1 listed without covenants.
   From the newest audit file in runs/, show for every contract: steps.extract_covenants.contracts[].meta.model, .attempts, .quotes_verified/.quotes_total, .warnings and .normalized; for every mapping its model and warnings; and reply.summary.source.
   If any ✓ is missing, STOP and show the failing covenant's quantities/components and mapping item_ids exactly as they appear in the audit file.

4. Run the same command a second time and show the output and duration (expected: same results, all from the cache).

5. Run `finn run --eml <file>` for data/samples/no_attachment.eml, wrong_attachment.eml, unrelated.eml, order_english.eml and order_informal.eml. For each, show the exit code, the decision line and the first 6 lines of the reply text.

6. Only if steps 2–4 succeeded: `git add -A`, show `git status --short`, and check the staged diff for "sk-or" and for the MAIL_APP_PASSWORD value (report found/not found only; .env and runs/*.json must not be staged). Commit with the message "Steps 4b-8b: reply email, mail agent, review hardening, live-run fixes, docs, verified cache". Show `git log --oneline -5`.

7. End with a table "step | result | notes".
Do not run `finn watch` and do not send any email.
```

**Review notes:**
- First attempt stopped at step 1: the `.env` that had been edited by hand was the one in the parent FINN folder, which nothing reads. The IDE updated the repo's `.env`, then continued.
- Result: 108 passed; **all 4 covenants ✓** (34.32 % / 1.56 / 30.7 % / 3.37), contract 1 correctly without covenants. Every extraction used `gemini-3.8-flash` in one attempt, with 34/34 quotes verified on their pages and nothing normalised away. First run 344 s; cached run 0.1 s for the pipeline (21 s including the two small LLM calls). The bad-input emails gave the expected decisions and exit codes. Committed as `ac33934`; no secrets staged.
- The step 8b carve-out rule was what made contract 4 come out right this time: the model listed the subordinated shareholder loan as a carve-out *and* mapped the bank-loan line, which already leaves it out. Code refused to subtract it twice and said so in the reply.
- Reply email checked as a rendered page; it looks right. Three small things fixed in step 8d: the same carve-out also produced a contradictory "no plan line" note; six "no plan line" notes for one contract were too noisy; quoted defined terms showed a double opening quotation mark („„).

---

## Step 8d – Reply polish and cache robustness (built directly by the assistant)

Not a prompt: written and tested directly in the assistant's workspace, then copied into the repository.

- A carve-out that code emptied gets only its own notice, not an extra "no plan line" note.
- Several "no plan line" notes for one contract become one line listing the items; their reasons stay in the audit file. A single one keeps its reason.
- A quote that starts with a quotation mark no longer gets a second one from the reply template.
- Cache keys normalise line endings in the prompt files, so a Windows checkout (CRLF) still finds the committed cache. Existing cache keys are unchanged.
- New test: the committed cache reproduces the answer key with no contract or mapping LLM call. The laptop's cache was replayed this way in the assistant's workspace: same 4 results, no LLM call. 110 tests pass.

---

## Prompt 8e – Pick up step 8d and commit

```text
Step 8e of Finn. Files on disk were updated outside this editor.
Rules: close all open editor tabs WITHOUT saving first. Do not edit any file. Do not run git checkout, git restore, git stash or git reset. Never print .env or any key. Copy all output verbatim; if something fails, STOP and report it.

1. Run `pytest -q`. Expected: 110 passed.
2. Run `finn run --eml data/samples/order.eml --compare-expected` (it must use the committed cache; expected: ✓ for all 4 covenants, done in well under a minute). Show the complete "Hinweise / Warnungen" block and the comparison block.
3. `git add -A`, show `git status --short` (.env and runs/*.json must not appear), commit with the message "Step 8d: reply notes grouped, carve-out note deduplicated, quote marks, CRLF-safe cache keys", and show `git log --oneline -3`.
```

**Review notes:** _(filled in after review)_
