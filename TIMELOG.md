# Time log

Working time per step. "Built by" says who wrote the code: the coding assistant (from one prompt
per step, see `docs/PROMPTS.md`) or Claude directly. Every step was reviewed and its tests were
re-run independently before the next one started. The commit history shows the same steps.

| Date | Step | Built by | Time | What |
|---|---|---|---|---|
| 06.11. | 0 Read material, PRD, answer key | Shivam + Claude | 0.75 h | Read the 4 contracts and the Excel like a finance officer; wrote the PRD; hand-checked `expected_2027.json` and the golden fixtures (quotes verified on the PDF pages by a script) |
| 06.11. | 1 / 1b Skeleton, data model | coding assistant | 0.25 h | Repo, Pydantic models, CLI stubs; review fixes (`.env` comment parsing, test isolation) |
| 06.11. | 2 Plan reader + calculator | coding assistant | 0.5 h | Formula evaluation without cached values, sign normalisation, plausibility checks, commercial rounding, headroom, stress test; tested against the answer key |
| 06.11. | 3 / 3b Contract extraction | coding assistant | 0.5 h | Page-marked PDF text, LLM extraction with cache, quote-on-page check; hardening (latency bound, truncation, hyphenation, generic prompt) |
| 06.11. | Model comparison | Shivam | 0.25 h | Live `--compare`: `gemini-3.8-flash` 35/35 quotes, all decisions right; golden fixture corrected where the model was right |
| 06.11. | 4 Mapping + offline run | coding assistant | 0.5 h | LLM picks cell ids without seeing values; coverage and double-counting checks; pipeline; audit; sample emails |
| 06.11. | 4a Decoupling refactor | coding assistant | 0.5 h | Year, sheets, labels, checks, units, language, vendor moved to config; structured stress results; guard test |
| 06.11. | 4b Fixes from the live run | Claude directly | 0.25 h | Retry without `require_parameters`, max_tokens 24000, structure differences as INFO |
| 06.11. | 5 Reply email | Claude directly | 1.0 h | Email-safe HTML and text, verdict by code, LLM summary with number check and template fallback, badges and headroom bars, sources per number, threaded MIME; checked at desktop and phone width |
| 06.11. | 5b No regex | Claude directly | 0.5 h | openpyxl formula tokenizer + recursive-descent evaluator; keyword lists in config; guard test forbids `re` |
| 06.11. | 6 Mail agent | Claude directly | 1.0 h | LLM intake into `OrderIntent`, decisions by code, handler for `run` and `watch`, IMAP exactly-once, loop protection, SMTP retries |
| 06.11. | 7 Docs | Claude directly | 0.5 h | README, NOTE, this log |
| 06.11. | Independent review | Claude (separate reviewer agent) | 0.25 h | Re-derived all covenants from the contracts (agree with the answer key); found 13 weaknesses, mainly around the LLM steps |
| 07.11. | 7b Hardening (review batch A) | Claude directly | 1.0 h | Paths independent of the working directory; no reliance on the unread flag; sign rule from the sheet structure; inconsistent plans refused; EBITDA ≤ 0 = breach; limit must appear in its quote (one corrective retry); generic mapping prompt; no all-empty quantities, empty lines shown in the reply; one failing contract doesn't hide the others; clear error without a key; verified cache committed |
| 07.11. | 7c Hardening (review batch B) | Claude directly | 0.75 h | Summary sentences attributed to covenants (numbers and status claims checked per covenant); plan year from the mail if the file has it, else the first plan year; test date from config; unsupported formulas fall back to Excel's saved value with a note; all reply remarks as keys rendered per language; roles carry flow/stock instead of hardcoded names; reply-all, "AW:" subjects, *noreply* senders; restored golden fixtures that had been overwritten in the repo |
| 07.11. | 8 Live run on the laptop | coding assistant + Shivam | 0.5 h | First real run with the fallback model as primary: 2 of 4 covenants right, 1 contract refused, nothing wrong certified; findings logged in PROMPTS.md |
| 07.11. | 8b Hardening after the live run | Claude directly | 0.5 h | Ratio structure check with corrective re-read; unused quantities and weight-0 lines dropped; carve-outs subtracted only from cells that contain them; duplicate notices removed |
| | **Total so far** | | **9.5 h** | Re-run of the live check with the measured model, then Gmail and `finn watch` |
