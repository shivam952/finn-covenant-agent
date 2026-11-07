# Note – Finn covenant check

## Assumptions

- **Test basis:** Plan 2027 (test date 31.12.2027). Contracts 2–4 treat a plan-basis breach as an
  early warning, not a default, and the reply says so.
- **Shareholder loan** (8,000 T€, qualified subordination), the decisive reading: contract 4
  excludes it from net debt (3.37; including it would give 3.57, a breach), while contract 3
  includes it in the debt service regardless of subordination.
- **Debt service (contract 3):** itemised loan interest, without the accretion of pension
  provisions, plus scheduled repayments, without the 3,000 T€ Sondertilgung.
- **Rounding:** each contract's own rule. Contract 2 has none, so the unrounded value is compared.
- **Plan notes:** liquid funds are freely available, leases are operating leases, no extraordinary
  items. **Result:** 34.32 % / 1.56 / 30.7 % / 3.37, all met; both syndicated-loan covenants are tight.

## Changes to the brief, and why

1. **Fixed pipeline, no agent framework.** It is an audit with the same steps every time. Code
   orchestrates; the LLM is called at four points (intake, contract reading, mapping to cells,
   summary) and returns schema-validated JSON. No RAG: each contract fits into one call, and "no
   covenants" needs the whole text.
2. **Caching.** Contract extraction is cached per file, prompt and model; the mapping is cached on
   the plan's *structure*. A live order therefore needs two small LLM calls, and changed numbers
   are still recalculated.
3. **Email intake instead of keyword rules.** The LLM turns the free-text email into a fixed
   `OrderIntent`, and code decides what to do. The file always wins: a different year in the email
   is noted in the reply, not used.
4. **Guards on every LLM step.** The mapping sees no plan values and can't leave a quantity empty;
   every quote is checked on its page, and each limit must appear in its quote; every summary
   sentence must name its covenants and use only their numbers and statuses. An inconsistent plan
   is refused, not calculated.
5. **Extras.** A stress test per covenant ("EBITDA −3.6 % → breach"); the calculation with its
   cells and quotes; plausibility checks across the Excel sheets; an offline `.eml` mode as the
   demo backup.
6. **No hardcoding, no regex.** Layout, labels, checks, units, language and vendor are
   configuration, enforced by a guard test. Formulas are parsed with openpyxl's tokenizer.

## How I used AI

- **Before any code:** I wrote the PRD with Claude and hand-checked the answer key and the golden
  fixtures. A script verified every quote on its PDF page.
- **Steps 1–4a:** a coding assistant built them, one prompt per step (`docs/PROMPTS.md`). Each was
  reviewed and re-run independently. The reviews found real bugs: a `.env` comment read as the
  model name, worst-case LLM latency over 10 minutes, a hardcoded year and sheet names, and a
  golden fixture missing a contract line.
- **Steps 4b–6:** written directly by Claude in its own workspace while I was away from the IDE.
  They are marked as such in TIMELOG.md.
- **Model choice, measured against the golden specs:** `gemini-3.8-flash` got 35/35 quotes
  verified and every covenant decision right. `gpt-5.4-mini` is the fallback; its endpoints
  rejected the strict-schema flag, so the client retries without it.

## Next steps

A human approval step before sending when a covenant is breached; a covenant database for many
contracts; all plan years plus prior-year plausibility checks; OCR for scanned contracts; routing
for other request types (where an agent loop starts to make sense); tracing and cost per run.
