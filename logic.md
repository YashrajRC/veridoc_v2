# logic.md — How the checking actually works

> The engineering companion to [finance.md](finance.md). It traces a request end
> to end and documents every rule.
>
> **Note on paths:** the code is now a package. Modules referenced below by their
> old flat names live under `hl_verifier/` — `config.py`, `models.py`,
> `checklist.py`, `extraction.py`; `rules/reconciliation.py`, `rules/policy.py`;
> `pipeline/evaluate.py`, `pipeline/indexing.py`; `storage/store.py`,
> `storage/vectorstore.py`; `api/app.py`; `web/index.html`. The **File map** at the
> end lists the exact paths.

---

## 0. One‑paragraph summary

A case is a folder of scanned PDFs. The system **discovers** the files, **detects**
each one's type, **extracts** structured fields from each with Gemini (every value
carrying its page, a verbatim quote and a confidence), **merges** multiple
documents of the same type, then runs a fixed **checklist**: some lines are decided
by a deterministic rule over one document (`AUTO_DOC`), some by comparing values
across documents (`AUTO_RECON`), some by checking a sanctioned term against the
lender's **pricing grid** (`AUTO_POLICY`), some are detect‑then‑human (`SIGNOFF`),
and some are deferred to bank systems (`SYSTEM`) or human policy (`MANUAL`). Each line gets
a **status**, evidence, and the set of reviewer actions valid for that status. A
human adjudicates; decisions are appended to an audit trail.

```
PDFs ─▶ discover (doc_ids) ─▶ classify ─▶ extract (per doc) ─▶ merge (per type)
                                                                      │
                                              ┌───────────────────────┘
                                              ▼
        checklist ─▶ evaluate each item ─▶ status + evidence ─▶ triage sort ─▶ UI
                          │  (AUTO_DOC / AUTO_RECON / SIGNOFF / MANUAL / SYSTEM)
                          └─▶ reconciliation rules (pure Python)
        human decision ─▶ validate vs status ─▶ append to audit (SQLite)
```

---

## 1. Discovery & document identity — [evaluate.py](evaluate.py)

`discover_documents(case_id)` walks `DATA_DIR/<case_id>/*.pdf` and assigns each
physical file a **doc_id**:

- The first document of a type gets the bare type — `loan_agreement`.
- Additional documents of the same type get a suffix — `loan_agreement__2`, `__3`…

The **type** is recovered from a doc_id by `doc_type_of()` (everything before
`__`). A filename is mapped to a type by `_canonical_key()` using
`config.FILENAME_ALIASES` (lower‑cased, punctuation stripped). Files that match no
type are returned as `unrecognised` and ignored. This is the only place identity is
established, so URLs (`/document/<doc_id>`) and evidence links stay stable across
requests.

`group_ids_by_type()` then produces `{type: [doc_id, …]}` for the merge step.

## 2. Classification — [extraction.py](extraction.py) · `classify_pdf_sync`

Used by the upload UI (`POST /api/classify`, [app.py](app.py)) so the reviewer
never has to know the document type. The PDF bytes are sent to Gemini with the
catalogue of `DOC_KEYS` + labels and a strict JSON instruction; the model returns
`{doc_key, confidence, reason}`. It is **fail‑safe**: any error, or a type not in
the catalogue, degrades to `{"unknown", "low"}` so the human simply picks. The UI
pre‑selects the result and lets the reviewer confirm/override.

## 3. Extraction — [extraction.py](extraction.py)

The substitute for Document AI. **One Gemini call per document**, not per checklist
line — the prompt asks for *every* field that document can supply.

- **Field spec per type** lives in `DOC_FIELDS`. The prompt (`_build_prompt`)
  instructs the model to return, for each field, an object
  `{value, page, snippet, confidence}` and — crucially — to **set value `null`
  and confidence `low` when the scan is unclear, never to guess**. That "never
  guess" rule is the entire safety basis: a null/empty field becomes a human
  review downstream instead of a hallucinated pass.
- **Grounding**: every field carries the **1‑based page** and a **verbatim
  snippet** → that's the "view in document" audit hook.
- **Robustness**: malformed JSON, safety blocks, quota errors, timeouts and
  oversized files are all caught (`extract_document`) and converted into an
  explicit failed extraction (`ok=False`, `error=…`) rather than an exception that
  takes down the request. Retries with backoff up to `GEMINI_MAX_RETRIES`.
- **Caching**: keyed by **SHA‑256 of the file bytes** (`_read_cache`/`_write_cache`),
  so a pre‑warm run ([warm.py](warm.py)) makes the live demo instant and a re‑upload
  of the same bytes never re‑bills Gemini. The cache is content‑pure; the physical
  `source` doc_id is stamped onto fields *after* cache read/write so the same bytes
  placed under two ids share a cache entry but keep correct provenance.

`ExtractedField` = `{value, page, snippet, confidence, source}`. `source` is the
doc_id the value came from — this is what makes a multi‑doc evidence link open the
*right* PDF.

## 4. Multi‑document merge — [extraction.py](extraction.py) · `merge_extractions`

A type may hold several documents (main + supplementary loan agreement). The
checklist and reconciliation are written against **one** extraction per type, so we
collapse them:

- **A field is "found" if any document of the type supplies a non‑null value.**
- On conflict, the **highest‑confidence** value wins (ties keep the first /
  primary document).
- Each kept field retains its own `source`, `page`, `snippet`.
- A **single document passes straight through** — byte‑for‑byte identical to the
  old single‑doc behaviour. If *all* documents of a type failed to read, the merge
  is `ok=False` with the combined error.

This keeps every downstream rule untouched while honouring "use the value wherever
it appears."

## 5. The five evaluation modes — [checklist.py](checklist.py) + [evaluate.py](evaluate.py) · `_evaluate_item`

Each `ChecklistItem` declares **how** it is judged. `_evaluate_item` dispatches on
the mode:

| Mode | Meaning | Resolution |
|---|---|---|
| `AUTO_DOC` | One document + one deterministic rule. | If the type is absent → **DOCUMENT_MISSING**; if present but unreadable → **MANUAL_REVIEW**; else run the rule. |
| `AUTO_RECON` | Compare values across documents. | Dispatch to a reconciliation function. |
| `SIGNOFF` | Signature / seal / notary. | Absent → **DOCUMENT_MISSING**; unreadable → **NEEDS_SIGNOFF** (still needs a human); else detect the marks and return **NEEDS_SIGNOFF** with what was/wasn't found. |
| `MANUAL` | Human‑only policy check. | Always **MANUAL_REVIEW**. |
| `SYSTEM` | Needs LOS/CIBIL/CAM. | Always **PENDING_SYSTEM_DATA**. |
| `CONDITIONAL` | May not apply. | If the case attribute is false → **NOT_APPLICABLE**; else fall through to the inner mode. |

`CONDITIONAL` uses **derived attributes** (`derive_attributes`): currently
`ticket_ge_5cr`, computed by parsing the sanctioned amount and comparing to
`config.TITLE_VETTING_THRESHOLD` (₹5 crore). That's how **B7** turns into N/A vs
Manual review.

## 6. The `AUTO_DOC` rules — [evaluate.py](evaluate.py) · `AUTO_DOC_RULES`

Each rule receives the merged `DocumentExtraction` and returns
`(status, finding, confidence)`. The design bias everywhere: **when in doubt, route
to a human; auto‑pass only the unambiguous.**

| Rule | Logic |
|---|---|
| `present` | Document exists → **VERIFIED** "Document present." (used for CIBIL/KYC/LOD/CERSAI/CAM/MITC, which are absent → DOCUMENT_MISSING). |
| `fi_residence_positive` / `fi_office_positive` | Read the verdict field. `null` → MANUAL_REVIEW; contains "negative" → **EXCEPTION**; else **VERIFIED**. |
| `rcu_clear` | `null` → MANUAL_REVIEW; contains "negative" *or* "refer" → **EXCEPTION**; else **VERIFIED**. |
| `legal_title_clear` | Title text contains *clear / marketable / mortgageable* → **VERIFIED**; otherwise **EXCEPTION**. |
| `legal_no_encumbrance` | "nil/none/no encumbrance/clear" with **no** red‑flag word (*except, mortgage, charge, lien, lis pendens, attachment, subsisting, pending*) → **VERIFIED**; anything else → **EXCEPTION**. Deliberately conservative: a carve‑out buried in an otherwise‑negative sentence still forces a human look. |
| `technical_value_present` | Market value must parse as an amount → **VERIFIED**; else MANUAL_REVIEW. |
| `enduse_present` | End‑use value present → **VERIFIED**; else MANUAL_REVIEW. |
| `drl_present_signed` | Borrower signature truthy → **VERIFIED** (with amount); not detected → **EXCEPTION**. |
| `sanction_present` | Sanctioned amount present → **VERIFIED**; else MANUAL_REVIEW. |
| `insurance_present` | Sum assured present → **VERIFIED**; else MANUAL_REVIEW. |
| `insurance_bank_interest` | "Bank's interest noted" flag truthy → **VERIFIED**. |
| `kyc_verification` (**A6**) | Reads the **KYC results recorded in the RCU report** — `aadhaar_result`, `pan_result`, `bank_statement_result`. Any explicit *not matched / inoperative / failed* → **EXCEPTION**; all clear → **VERIFIED** (listing each result); none found → **MANUAL_REVIEW**. Surfaces the actual verification, not just "report present." |

Evidence for an `AUTO_DOC` line is the rule's **primary field** (`_PRIMARY_FIELD`),
so the line links straight to the page/quote that drove the verdict.

## 7. The reconciliation rules — [reconciliation.py](reconciliation.py) · `RECON_RULES`

Pure Python over already‑extracted fields — **no model calls**, fully auditable.
Every rule returns a `ReconResult` carrying per‑document cells
(`{value, display, page, snippet, doc_id}`) so the UI can render a side‑by‑side
table with a source link per cell.

Every rule that does arithmetic or a comparison also attaches a **`calculation`**
block to `extra` (`{title, steps[], result, verdict, references[]}`) — the steps,
the formula, the verdict and any quoted policy clause — which the console renders as
a "reasoning & calculation with proof" panel. Every figure in it links to its page.

**Normalisation / matching helpers** (the heart of "do these agree?"). These are
deliberately **tolerant of the same value written differently** but still catch a
genuine difference; the tolerances are configurable in `config.py`:
- `norm_name` — upper‑cases, cuts relationship clauses (`S/o`, `D/o`, `W/o`, `C/o`),
  drops honorifics and punctuation. `name_tokens` reduces a name to its token set.
- `_token_matches` / `core_coverage` — match name tokens **exactly, as initials**
  (`A` ≈ `Anil`), **or fuzzily** (difflib ratio ≥ `NAME_TOKEN_FUZZ`, so `HAQUE` ≈
  `HAQULL`). `names_match` passes when one name's tokens are (fuzzily) covered by the
  other's — tolerant of honorifics, OCR and extra co‑applicant tokens.
- `parse_amount` — Indian‑format money: `45,00,000`, `45 Lakh`, `4.5 Cr`, the
  restated‑figure trap, **and amounts written only in words** ("One Crore Four Lakh
  …" → 1,04,10,480 via `_words_to_number`). `None` only if neither figure nor words
  can be read → routes to a human.
- `fmt_inr` — formats a derived number with Indian digit grouping for the
  calculation read‑outs (the verbatim document value is always linked alongside).
- `survey_numbers` — extracts the **set** of plot/survey/khasra numbers (e.g.
  `613/49`, `613/154`) ignoring surrounding words, so `Khasra No‑613/49, 613/154
  Part` and `613/49, 613/154` reconcile.
- `address_similarity` — token‑overlap (Jaccard) of address words.

**The rules:**
- `recon_borrower_name` (**R1**) — collects the name from each present document
  (field name varies per doc, e.g. `deponent_name`). It builds the **applicant
  "core"** = the most‑frequent name tokens across the file (so a co‑applicant named
  in only some documents is *not* mistaken for the applicant), then measures each
  document's **fuzzy coverage** of that core. All cover ≥ `NAME_CORE_COVERAGE` →
  **VERIFIED** (variants tolerated; co‑applicants reported separately); a document
  covering little of the core → **EXCEPTION** naming it. Each document's % match is
  shown.
- `recon_property_identity` (**R2**) — **survey/plot numbers as number sets**:
  any number common to all documents → **VERIFIED**; none in common → **EXCEPTION**.
  Falls back to **address overlap** only when surveys are absent: lowest pairwise
  overlap < `ADDRESS_SIM_FLOOR` → EXCEPTION; else a low‑confidence VERIFIED.
- `recon_sanctioned_amount` (**R3**) — sanction vs DRL. Unparseable → MANUAL_REVIEW;
  equal → **VERIFIED**; different → **EXCEPTION**. Shows the two amounts + difference.
- `recon_ltv` (**R4**) — `loan / value` (now computes even when the valuation is in
  words). LTV > `config.LTV_REVIEW_CAP` (default 0.90) → **EXCEPTION**; else
  **VERIFIED**. Shows `loan ÷ value = pct%` and cites that the HL grid sets no single
  LTV cap (LAP special ≤ 70%). Returns `ltv_pct` in `extra`.
- `recon_insurance_adequacy` (**R5**) — sum assured ≥ loan → **VERIFIED**; below →
  **EXCEPTION**. Shows the cover as a % of the loan.
- `recon_conditions` (**R6**) — parse the sanction letter's `conditions` list
  (each `{text, type∈{OTC,PDD,UNKNOWN}}`). Keyword‑map each condition to an
  evidence document (e.g. "insurance" → the insurance doc) and mark it satisfied if
  that document is present. Count **OTC** conditions still unevidenced
  (`otc_open`); >0 → **EXCEPTION**; else MANUAL_REVIEW (mapping is heuristic, so
  always human‑confirmed). Feeds the OTC/PDD readiness meter via `extra`. In the UI
  this large tracker is moved to its own collapsed panel at the bottom for
  readability.

## 7b. The policy rules — `rules/policy.py` · `POLICY_RULES` (`AUTO_POLICY`)

Same shape as the reconciliation rules (return a `ReconResult` with a
`calculation`), but they compare a sanctioned term against the **lender's published
pricing grid**, transcribed from the PDF in `Policy/`. The grid (ROI bands by CIBIL
× loan slab, PF rules, login‑fee cap, deviation floors) and the **quoted source
clauses** (`REFERENCES`) live in `policy.py`. They are conservative: where the exact
grid cell can't be pinned without data we don't have (CIBIL band, profile), they
state the published window and route to a human rather than guess.

- `policy_roi` (**P1**) — reads the sanctioned ROI and amount, derives the loan
  **slab** (`loan_slab`), and shows the published HL window for that slab (Salaried
  vs SEP) plus the floor (`ROI_FLOOR_STANDARD` = 7.75%). ROI below the floor →
  **EXCEPTION** (needs a BH/CE deviation on file); within the window → **VERIFIED**
  (low confidence — confirm the CIBIL band); above the standard grid →
  **MANUAL_REVIEW**. Also returns the per‑band grid table for the slab so the
  reviewer can pin the exact cell.
- `policy_fees` (**P2**) — checks the processing fee (HL Salaried ₹10,000, Self‑
  Employed 0.50%, LAP 1%) and the login fee (`config.LOGIN_FEE_CAP` = ₹1,000).
  Anything outside policy → **EXCEPTION** quoting the number; within → **VERIFIED**;
  neither fee stated → **MANUAL_REVIEW**. Shows the fee as a % of the loan and the
  quoted clauses as proof.

`pipeline/evaluate.py` dispatches `AUTO_RECON` and `AUTO_POLICY` through the same
branch (to `RECON_RULES` and `POLICY_RULES` respectively), so the evidence and
calculation plumbing is shared.

## 8. Status, confidence gate, and triage — [evaluate.py](evaluate.py) · `_pack`

`_pack` finalises a line:
- **needs_attention** = the status is EXCEPTION / NEEDS_SIGNOFF / MANUAL_REVIEW,
  **or** it's VERIFIED but **low confidence** — a low‑confidence pass is pushed to
  the human, because an unsure green tick is the dangerous case.
- **allowed_actions** = `models.allowed_actions_for(status)` — the adaptive
  maker‑checker action set (see §9).
- **rank** = `_RANK[status]` — EXCEPTION(0) < NEEDS_SIGNOFF/MANUAL(1) <
  DOCUMENT_MISSING(2) < PENDING_SYSTEM(3) < VERIFIED(4) < NOT_APPLICABLE(5). The UI
  sorts by this, so the riskiest lines are on top.

`evaluate_case` ([evaluate.py](evaluate.py)) ties it together: discover → extract
per doc_id → merge per type → evaluate every checklist item → merge recorded
decisions → sort → summarise (`status_counts`, reviewed/open), and reports
`documents_present`, per‑type counts (`documents`), `documents_missing`,
`documents_unrecognised`, and `extraction_errors`.

## 9. Maker‑checker & the audit trail — [models.py](models.py) + [store.py](store.py)

The AI proposes a `VerificationStatus`; the human records a `ReviewAction`. They
are kept strictly separate. `ALLOWED_ACTIONS` maps each status to the only actions
that make sense for it (e.g. a `DOCUMENT_MISSING` line offers only **NOTE** /
**WAIVE**; a `VERIFIED` line offers **ACCEPT** / **DECLINE**). Every recorded
decision is **re‑validated against the line's *current* AI status** at write time
(`POST /api/cases/{id}/decision`, [app.py](app.py)) — an action that doesn't fit is
rejected with the list of allowed ones.

Each decision is an **append‑only** row ([store.py](store.py)) capturing the
reviewer, timestamp, note, and a **snapshot of the AI verdict + evidence at the
moment of decision**. Deciding a line again appends a new row (latest wins);
history is never overwritten. `GET …/audit` returns the full history.

## 10. Evidence provenance

Evidence items are `{doc_key, page, snippet}` where `doc_key` is actually the
**source doc_id** (so the right physical PDF opens, even for a supplementary).
`_evidence_from_field` and the SIGNOFF loop use `ef.source`; reconciliation cells
carry `doc_id` and `_evidence_from_recon` uses it. The PDF is served inline by
`GET /api/cases/{id}/document/{doc_id}` with a path‑containment check, and the
browser jumps to `#page=N`.

## 11. The search index (optional) — `pipeline/indexing.py` + `storage/vectorstore.py`

Independent of verification. Each document is transcribed to page‑marked text
(cached), chunked, embedded (`text-embedding-005`), and stored as float32 vectors in
SQLite, keyed by **doc_id** so a hit links to the exact physical PDF.

Search is **keyword‑first hybrid**. For a query it computes, for every passage, a
**lexical** score (`_lexical_score`: 1.0 per exact word, 0.6 for a substring/prefix
hit, partial credit for a close fuzzy match so `aadhar`/`aahaar` still hit, plus a
phrase bonus) and, when embeddings are available, a **semantic** cosine score. It
then returns results in **tiers**: passages that actually contain the query words
come **first** (best keyword match on top, semantic only breaking ties), tagged
*exact* / *partial*; semantic‑only matches follow underneath as *related* for
paraphrase recall. This guarantees exact word matches lead, and it still works (as
keyword‑only, `mode: "keyword"`) when embeddings are unavailable. A failure here
yields an empty/partial index and **never** affects verification.

## 12. Robustness & degradation

The whole pipeline is built to **degrade, not crash**: if Gemini is unavailable,
extraction returns `ok=False`, every dependent line falls back to MANUAL_REVIEW or
DOCUMENT_MISSING, and the app still boots and serves the checklist honestly
(`GEMINI_AVAILABLE` guard in [extraction.py](extraction.py)). Oversized files are
reported explicitly rather than truncated. Concurrency is bounded by a semaphore
(`MAX_CONCURRENCY`) and each call has a wall‑clock timeout.

---

## File map

| File | Role |
|---|---|
| [hl_verifier/config.py](hl_verifier/config.py) | Paths, model ids, document keys + aliases, and all tunable thresholds. |
| [hl_verifier/checklist.py](hl_verifier/checklist.py) | The A–J + P + R checklist: each item's mode, source doc, rule. |
| [hl_verifier/extraction.py](hl_verifier/extraction.py) | Gemini calls: classify, extract, transcribe, embed; caching; merge. |
| [hl_verifier/rules/reconciliation.py](hl_verifier/rules/reconciliation.py) | Cross‑document rules + Indian name/amount(+words)/address matching + calculations. |
| [hl_verifier/rules/policy.py](hl_verifier/rules/policy.py) | Pricing‑grid checks (ROI, fees) vs the L&T grid, with calculation + quoted proof. |
| [hl_verifier/pipeline/evaluate.py](hl_verifier/pipeline/evaluate.py) | Discovery, the `AUTO_DOC`/KYC rules, status derivation, `evaluate_case`. |
| [hl_verifier/pipeline/indexing.py](hl_verifier/pipeline/indexing.py) | Transcribe → chunk → embed → store; keyword‑first hybrid search. |
| [hl_verifier/models.py](hl_verifier/models.py) | Statuses, reviewer actions, adaptive action map, decision record. |
| [hl_verifier/storage/store.py](hl_verifier/storage/store.py) | SQLite append‑only decision/audit store. |
| [hl_verifier/storage/vectorstore.py](hl_verifier/storage/vectorstore.py) | SQLite + NumPy vector store. |
| [hl_verifier/api/app.py](hl_verifier/api/app.py) | FastAPI routes. |
| [hl_verifier/warm.py](hl_verifier/warm.py) | Pre‑warm the extraction cache and index before a demo. |
| [hl_verifier/web/index.html](hl_verifier/web/index.html) | The single‑file review console. |
| [app.py](app.py) · [warm.py](warm.py) | Thin root entrypoints (preserve `uvicorn app:app` / `python warm.py`). |
