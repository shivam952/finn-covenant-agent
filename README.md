# Finn – covenant check by email

Finn is an email agent for a municipal utility. The head of finance emails Finn a business plan
(`Wirtschaftsplan_2027.xlsx`). Finn reads the financial covenants from four loan contracts,
takes the matching plan values from the Excel file, **calculates every ratio in code**, and replies
in the same thread with a table, a stress test and the source of every number.

> The language model reads, structures and writes. It never calculates.

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                 # Windows: copy .env.example .env
```

Fill in `.env`:

| Variable | Value |
|---|---|
| `LLM_API_KEY` | OpenRouter API key (`OPENROUTER_API_KEY` also works) |
| `LLM_MODEL` | `google/gemini-3.8-flash` (chosen by a measured comparison, see NOTE.md) |
| `LLM_MODEL_FALLBACK` | `openai/gpt-5.4-mini` |
| `MAIL_USER`, `MAIL_APP_PASSWORD` | Gmail address and app password (only for live mail) |
| `FINN_MAIL_ADDRESS` | Finn's address (defaults to `MAIL_USER`) |

Then:

```bash
pytest                                                          # 110 tests, no network needed
finn run --eml data/samples/order.eml --compare-expected --preview   # offline run, opens the reply
finn watch                                                      # live: answer emails to Finn's inbox
```

The repository ships the verified contract extractions and mappings in `cache/`, so a run only
needs two small LLM calls (reading the email and writing the summary). Delete `cache/*.json` to
force a fresh reading of the contracts (about 2–3 minutes). The cache is keyed on file, prompt and
model, so a changed contract, prompt or model is re-read automatically. An API key is always needed.

## Commands

| Command | What it does |
|---|---|
| `finn watch [--once]` | Polls the inbox every `POLL_SECONDS`, answers every new order once, in the same thread |
| `finn run --eml FILE` | Handles one `.eml` file offline and writes `runs/<id>/reply.html`, `.txt`, `.eml` |
| `  --compare-expected` | Also checks every result against the hand-checked answer key `tests/expected_2027.json` |
| `  --lang de\|en` | Reply language (otherwise taken from the email) |
| `  --preview` | Opens the reply in the browser |
| `  --send` | Also sends the reply via SMTP (for a test from a second account) |
| `python -m finn.contracts.extract --compare [--no-cache]` | Live extraction of the contracts, compared with the hand-read specs (used to choose the model) |

Exit codes of `finn run`: `0` checked, `2` no check run (for example a missing attachment; a polite
reply was still prepared), `1` error or mismatch with the answer key.

## How it works

A fixed pipeline in plain Python. There is no agent framework: code decides every step, and the
LLM is called at four fixed points. Each time it returns only JSON that is validated against a
schema.

```
email ─▶ loop protection (code) ─▶ intake (LLM → OrderIntent) ─▶ decision (code)
      ─▶ read plan (code: openpyxl + formula tokenizer, sheets found by role)
      ─▶ read contracts (LLM → covenant specs, cached; every quote checked on its page in code)
      ─▶ map definitions to plan cells (LLM picks cell ids, sees no values; code checks
         coverage and double counting)
      ─▶ calculate (code: ratio, commercial rounding per contract, headroom, stress test)
      ─▶ reply (code builds table and sources; LLM writes 2–3 sentences, every number in
         them is checked against the results) ─▶ SMTP reply in the same thread + audit file
```

| Folder / file | Content |
|---|---|
| `src/finn/pipeline.py` | The order of the steps (the only place that knows it) |
| `src/finn/calc.py` | All arithmetic. No LLM, no I/O |
| `src/finn/plan/` | Excel reader, formula evaluator, plausibility checks |
| `src/finn/contracts/` | PDF text with page markers, extraction, quote verification |
| `src/finn/mapping.py`, `intake.py`, `summary.py` | The other LLM steps, each with its own validation |
| `src/finn/report.py`, `render.py`, `texts/` | Reply email (HTML and text); all wording in `de.toml` / `en.toml` |
| `src/finn/handler.py`, `watcher.py`, `mail/` | One email end to end; polling, exactly-once, loop protection |
| `config/plan_layout.toml` | Sheet roles, plan-column keywords, plausibility checks |
| `config/report.toml` | Thresholds ("tight" < 10 % relative headroom), colours |
| `config/agent.toml` | Loop protection, retry limits |
| `prompts/` | The four LLM prompts |
| `runs/` | One audit JSON per email: input, extraction, cells, calculation, reply (git-ignored) |
| `cache/` | Contract extractions and mappings (git-ignored) |

## Robustness

- **Every email gets an answer, normally exactly one.** Missing or wrong attachment, unreadable or
  inconsistent Excel, unrelated email, unclear request and internal errors each get a short reply.
  Message-IDs are stored in `runs/processed.json`, which survives restarts. Finn does not rely on
  the unread flag, so an order someone already opened in the inbox is still answered. (A crash in
  the moment between sending and recording could cause one duplicate.)
- **Numbers Finn cannot certify are not sent.** If the plan's sheets don't reconcile (balance,
  bank debt, interest, EBITDA identity), Finn says so instead of calculating. A ratio over a
  non-positive EBITDA is reported as breached. A covenant limit must appear in the quoted
  contract clause. A ratio whose numerator contains its own denominator is re-read once and
  otherwise refused, and an exclusion is never subtracted from plan cells that don't contain it.
  If one contract fails, the others are still reported and the reply says the
  result is incomplete.
- **No reply loops.** Finn ignores its own replies, auto-replies, bulk mail and mailer-daemons
  (configurable in `config/agent.toml`).
- **The LLM cannot change a number.** Plan values are never shown to it, every citation is checked
  on its PDF page, and every summary sentence must name the covenants it is about, use only their
  numbers and claim no status they don't have; otherwise a fixed template is used.
- **No hardcoding.** Years, test date, sheet names, labels, units, language and vendor live in
  config or text files; every remark in the reply is a key rendered in the reply language. `tests/test_no_hardcoding.py` fails if one of them, or a regular expression, appears in `src/`.

## Gmail setup (live mode)

1. Create a test Gmail account and turn on 2-step verification.
2. Create an app password (Google account → Security → App passwords). Put it in `MAIL_APP_PASSWORD`.
3. Gmail settings → Forwarding and POP/IMAP → turn on IMAP.
4. Run `finn watch` and send an order from another address. The reply arrives in the same thread
   within about a minute.

No keys or passwords are in the repository; `.env` is git-ignored.
