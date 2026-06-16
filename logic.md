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
  **Auto‑invalidation**: a cache entry is treated as **stale and re‑extracted** when
  the current field spec (`DOC_FIELDS`) has keys the cached entry lacks — so when new
  fields are added (e.g. the KYC or fee fields) the new checks populate on the next
  run instead of silently reading empty values from an old extraction. See §13.

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
| `kyc_verification` (**A6**) | Reads the **KYC detail recorded in the RCU report** — `aadhaar_result`, `pan_result`, `bank_statement_result`, and a `kyc_documents` list. KYC counts as **present** if *any* of these has a value (a status like "OPERATIVE/VALID", a result like "matched", a masked Aadhaar number, or a documents list) — the report rarely uses one fixed wording, so the rule is **presence‑based**, not keyword‑exact. An explicit negative (`not matched`, `inoperative`, `invalid`, `failed`, `refer`…) → **EXCEPTION**; any detail present and no negative → **VERIFIED** (listing what was found); **nothing at all** → **MANUAL_REVIEW** — which usually means the cached extraction predates these fields, so the message says to re‑run `warm.py`. Surfaces the actual verification, not just "report present." |

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

## 11b. The grounded assistant (Tier‑1 RAG) — `pipeline/assistant.py`

A read‑only Q&A layer on top of search. For a natural‑language question about one
case (`POST /api/cases/{id}/ask`) it does three things, then asks Gemini once:

1. **Retrieve** the top passages via `indexing.search` (the document text).
2. **Read the verified facts** via `evaluate_case` (cache‑backed, so it adds no
   new model calls when the case is already extracted): every checklist
   **finding** plus the **reconciliation values** — i.e. the already‑computed
   cross‑document comparisons (`sanction=… , loan_agreement=…`). This is the half
   that makes *“compare the sanction amount across the documents”* reliable: the
   numbers come from the verification engine, not re‑read from OCR.
3. **Answer** with `extraction._gen` under a strict contract: use ONLY the
   supplied context, quote figures exactly, **cite document + page**, and say
   *“I couldn't find that in this case's documents”* rather than guess. The
   response carries `answer`, deduplicated `sources` (clickable evidence links),
   the `passages` used, and `used_facts`.

It is **purely additive and never mutates verification**. It degrades safely:
answers from the structured findings even when the **search index is empty**, and
returns a clear message (not a 500) when Gemini is unavailable. Two model calls
per question (one query embedding + one generation); the heavy extraction is
served from cache.

## 12. Robustness & degradation

The whole pipeline is built to **degrade, not crash**: if Gemini is unavailable,
extraction returns `ok=False`, every dependent line falls back to MANUAL_REVIEW or
DOCUMENT_MISSING, and the app still boots and serves the checklist honestly
(`GEMINI_AVAILABLE` guard in [extraction.py](extraction.py)). Oversized files are
reported explicitly rather than truncated. Concurrency is bounded by a semaphore
(`MAX_CONCURRENCY`) and each call has a wall‑clock timeout.

## 13. The caching model — why a document is (or isn't) re‑read

This trips people up, so it's worth its own section. **Reading a PDF with Gemini is
the slow, paid step**; everything else is fast local Python. So we cache the result
of reading.

- **What is cached, and by what key.** Two things, both under `cache/` at the project
  root: the **extracted fields** as `cache/<sha256>.json`, and the plain‑text
  **transcript** (for search) as `cache/<sha256>.txt`. The key is the **SHA‑256 of
  the file's bytes** — *not* the filename. So the same PDF re‑uploaded under a
  different name reuses the cache, and two different files never collide.
- **Why content‑keyed.** It means a pre‑warm and the live view share one cache, and
  re‑running never re‑bills for bytes already read.
- **The gotcha you just hit (and its fix).** Because the cache stores *the fields that
  existed when it was written*, adding a **new field** to the spec (the KYC fields on
  the RCU, the fee fields on the sanction letter) would otherwise leave old cache
  entries without those fields — so the new check reads *empty* and reports "not
  found," even though the document clearly shows the data. The fix (§3) is
  **auto‑invalidation**: `_read_cache` compares the cached entry's field names to the
  current `DOC_FIELDS` spec, and if the spec has keys the entry lacks, it **discards
  the entry and re‑extracts**. Net effect: after a code change that adds fields, the
  affected documents are re‑read **once**, automatically, on the next case load — no
  manual cache‑clearing, no stale empties.
- **Forcing a full refresh anyway.** `warm.py` and `evaluate_case` accept
  `use_cache`; the index builder takes `force`. To rebuild everything from scratch you
  can also delete `cache/` (it is regenerated). A *failed* extraction is cached too,
  but is kept as‑is by `_read_cache` (re‑trying it is the caller's choice).

**One‑line mental model:** *we re‑read a document only when its bytes change or when
we start asking it for something new.*

## 14. A worked example — one case, end to end

Follow a real‑shaped case (the borrower "Syed Moinul Haque", a ₹26,00,000 home loan)
from upload to a reviewed checklist. Each step names the function doing the work.

1. **Upload / placement.** The reviewer drops nine PDFs via "New case", or they are
   pre‑placed under `data/<case_id>/`. On upload each file is first **classified**
   (`classify_pdf_sync`) so its type is pre‑filled; on disk it lands as
   `sanction.pdf`, `legal.pdf`, … A second loan agreement would be `loan_agreement__2.pdf`.

2. **Discovery** (`discover_documents`). The folder is scanned; filenames map to
   types via `FILENAME_ALIASES`; each physical file gets a **doc_id** (`sanction`,
   `loan_agreement`, `loan_agreement__2`). Unrecognised files are listed, not guessed.

3. **Extraction** (`extract_documents` → `extract_document`, fanned out, bounded by a
   semaphore). For each document Gemini is asked, in **one call**, for that type's
   field spec. The sanction letter returns `sanctioned_amount = "Rs. 2600000.00"
   (p.1)`, `roi = "8.35%" (p.1)`, `processing_fee`, `login_fee`, and the
   `conditions` list; the technical report returns `market_value = "One Crore Four
   Lakh … Rupees Only" (p.3)` and `survey_or_plot_no = "613/49, 613/154"`; the RCU
   returns `aadhaar_result`, `pan_result`, … Each value carries its **page, a verbatim
   snippet, and a confidence**; anything unclear comes back `null/low` (never
   guessed). Results are cached by content (§13).

4. **Merge** (`merge_extractions`). For types with one document this is a pass‑through;
   for a type with two (the loan agreements) the fields are combined — a value is
   "found" if either supplies it, highest confidence wins — so downstream rules see
   **one** extraction per type while each value keeps its own source doc_id.

5. **Evaluate every checklist line** (`_evaluate_item`), dispatching on the line's
   mode. A few concrete lines from this case:
   - **A6 (KYC)** — `AUTO_DOC` over the RCU: reads `aadhaar_result`/`pan_result`/… If
     the RCU shows "Aadhaar: OPERATIVE; PAN: VALID" → **Verified**, listing them; an
     explicit "not matched" → **Exception**; nothing parsed → **Manual review** (and,
     thanks to §13, the RCU is re‑read once after this field was added so it does get
     parsed).
   - **R1 (name)** — `AUTO_RECON`: builds the applicant **core** "SYED MOINUL HAQUE"
     from the names that recur, tolerates "Haqull"/honorifics/initials, notes "Zahida"
     as a **co‑applicant**, and returns **Verified** with each document's % match.
   - **R2 (property)** — compares survey **number sets**: `{613/49, 613/154}` appears
     in both technical and legal → **Verified**.
   - **R4 (LTV)** — parses the valuation **from words** → ₹1,04,10,480, computes
     `26,00,000 ÷ 1,04,10,480 = 25.0%`, under the 90% trigger → **Verified**, showing
     the calculation.
   - **P1 (ROI)** — slots ₹26L into the "0–50L" band, shows the published window
     (Salaried 7.95–8.90%, SEP 8.30–9.20%, floor 7.75%); 8.35% is inside → **Verified**
     (low confidence: confirm the CIBIL band), with the per‑band grid as proof.
   - **B5 / I1 / I3 (sign‑offs)** — the AI locates the advocate/borrower/notary marks
     and returns **Needs sign‑off** for a human to authenticate.
   - **F1 / H1 / J1 (system)** — **Pending system data** until LOS is connected.

6. **Pack & triage** (`_pack`, then sort). Each line gets its status, evidence, the
   valid reviewer actions, and a **rank**; a *low‑confidence* "verified" is pushed up
   into Needs‑attention because an unsure green tick is the dangerous case. The whole
   list is sorted exception‑first.

7. **Assemble the response** (`evaluate_case`) — checklist + recorded decisions +
   `status_counts` + document inventory + extraction errors → JSON the console renders
   as the Policy, Reconciliation, Needs‑attention, Verified and (collapsed) Sanction‑
   conditions panels.

8. **Human review** (`POST …/decision`). The reviewer clicks through the flagged lines
   first, opening each value's page to confirm it. Every decision is re‑validated
   against the line's current status and **appended** to the audit trail with a
   snapshot of the AI verdict at that moment. Re‑deciding appends a new row; nothing is
   overwritten.

That is the whole loop: **discover → read (cached) → merge → judge each line by its
mode → triage → a human signs off, audited.** The AI only ever *reads and proposes*;
the rules and the human decide.

## 15. The Gemini calls in detail — prompts, output contract, parsing

Every model call goes through **one** function, `_gen(parts, json_mode)` in
[extraction.py](extraction.py), so the SDK is touched in exactly one place. It sends
`contents = [Part.from_bytes(pdf_bytes, "application/pdf"), prompt]` with
`temperature = 0.0` (we want determinism, not creativity) and, in JSON mode,
`response_mime_type = "application/json"` so the model is told to emit JSON. It
returns `resp.text`; if there is **no text part** (a safety block or empty
candidate) it raises `RuntimeError("no text in response (finish_reason=…)")` — which
the caller catches and turns into a failed extraction, never a crash.

There are **four** distinct prompts/calls. All are verbatim from the code.

**(a) Extraction** — `_build_prompt(doc_key)` = a fixed header + the per‑type field
list + (for the sanction letter) a `conditions` addendum. The header *is* the output
contract and the "never guess" safety rule:

```
You are extracting fields from a single lending document for an audit trail. The
document is a SCANNED PDF and may be medium or low quality, skewed, or partly faint.
Read carefully. Where text is unclear or illegible, do not guess: set the value to
null and confidence to low. Return ONLY a JSON object, no prose and no markdown
fences. For each requested field output an object with keys: value, page, snippet,
confidence. 'value' is the extracted value exactly as written (or null if absent or
illegible). 'page' is the 1-based page number you read it from (or null). 'snippet'
is a short verbatim quote from the document supporting the value (or empty string).
'confidence' is one of low, medium, high; use low whenever the scan quality made you
unsure. Keep snippets under 20 words.

Fields:
- <field>: <hint>        # one line per field in DOC_FIELDS[doc_key]
...
```

The field lines come straight from `DOC_FIELDS` (so adding a check is a one‑line
hint, e.g. the KYC and fee fields). For the sanction letter an addendum asks for
`conditions` as a JSON **array** of `{text, type∈{OTC,PDD,UNKNOWN}, page, snippet}`.
Key design point: **one call per document for every field** (not one call per
checklist line) — cheaper, and the model sees the whole document at once.

**(b) Classification** — `classify_pdf_sync`, used by upload so the reviewer needn't
know the type. It lists the `DOC_KEYS` catalogue and demands strict JSON:

```
You are classifying a single document from an Indian home-loan file. From its
content (title, headings, fields, stamps) decide which ONE of these document types
it is:
- technical: Technical / valuation report
- legal: Legal & search report (TSR/LSR)
- ... (one line per DOC_KEYS entry)

Respond ONLY as JSON: {"doc_key": <one key from the list above, or "unknown" if it
matches none>, "confidence": "high"|"medium"|"low", "reason": <short phrase citing
what you saw>}. If you are not sure, use "unknown" with low confidence rather than
guessing.
```

It is **fail‑safe**: any exception, or a `doc_key` outside the catalogue, degrades to
`{"unknown", "low"}` so the human simply picks.

**(c) Transcription** — `transcribe_sync`, used only for the search index. Plain text
is more robust than JSON for long OCR, so it is *not* JSON mode:

```
Transcribe this scanned document to plain text. Preserve reading order. Begin each
page with a line '=== PAGE n ===' (n is the 1-based page number). Output only the
transcription, no commentary.
```

The `=== PAGE n ===` markers are what `indexing._parse_pages` later splits on to keep
each chunk's page number for the evidence link.

**(d) Embeddings** — `embed_texts`, batched at 50 inputs, returns one vector per text
or **`None`** on any failure (so search silently falls back to keyword‑only).

**Parsing the JSON back.** `_coerce_json` is forgiving: it strips a ```` ```json ````
fence if present, tries `json.loads`, and if that fails grabs the **outermost
`{ … }` span** and tries again; only then gives up (`None`). Each field is then read
by `ExtractedField.from_obj`, which **sanitises** every value: `page` is coerced to an
int ≥ 1 or `None` (a bool or 0 becomes `None`); `confidence` is forced into
`{low, medium, high}`, defaulting to `low`; `snippet` is stringified and capped at
300 chars; a missing field becomes an empty `ExtractedField` (value `None`). So a
malformed or partial model response can never inject a bad type downstream — worst
case a field is simply "not found," which routes to a human.

## 16. The fallback ladders — every place the system degrades safely

The recurring principle: **when in doubt, route to a human; auto‑pass only the
unambiguous.** Here is every decision point and the ladder it walks.

**Reading a document (`extract_document`).** cache hit (and fresh) → return it ·
else if Gemini unavailable → `ok=False` "Gemini unavailable" · else call Gemini with
a wall‑clock **timeout** (`GEMINI_TIMEOUT_S`); on timeout or error, **retry** up to
`GEMINI_MAX_RETRIES` with `1.5×(attempt+1)s` backoff · unparseable JSON →
"model did not return parseable JSON" and retry · all attempts exhausted →
`ok=False` with the last error. A failed read is **data**, not an exception.

**The cache (`_read_cache`, §13).** absent → extract · wrong `doc_key` → ignore →
extract · **stale** (current spec has fields the entry lacks) → re‑extract · corrupt
JSON → re‑extract · a cached *failed* read → kept as‑is.

**Per checklist mode (`_evaluate_item`).** `AUTO_DOC`: type absent →
**DOCUMENT_MISSING**; present but `ok=False` → **MANUAL_REVIEW** ("could not read …");
rule id unknown → **MANUAL_REVIEW**. `SIGNOFF`: absent → **DOCUMENT_MISSING**;
unreadable → **NEEDS_SIGNOFF** (a human must still look). `AUTO_RECON` / `AUTO_POLICY`:
a required document missing → **DOCUMENT_MISSING**; a value unparseable →
**MANUAL_REVIEW**. `CONDITIONAL`: attribute false → **NOT_APPLICABLE**, else the inner
mode. `SYSTEM` → **PENDING_SYSTEM_DATA**. `MANUAL` → **MANUAL_REVIEW**.

**Amounts (`parse_amount`).** already numeric → use it · else first **digit group**
`\d[\d,]*\.?\d*` (with the lakh/crore multiplier heuristic, and the "words restate the
figure" guard) · else the **words parser** (`_words_to_number`: "One Crore Four
Lakh …") · else `None` → the caller routes to a human.

**Names (`names_match` / `recon_borrower_name`).** per token: exact → **initial**
(`A` ≈ `Anil`) → **fuzzy** (difflib ratio ≥ `NAME_TOKEN_FUZZ`, so `HAQUE` ≈ `HAQULL`).
For R1: build the applicant **core** from the most‑frequent tokens (so a co‑applicant
isn't mistaken for the applicant), then require each document's coverage ≥
`NAME_CORE_COVERAGE`; below that → exception. No recurring core at all → MANUAL_REVIEW.

**Property (`recon_property_identity`).** survey **number sets** intersect → match ·
no common number → exception · no surveys at all → **address** word‑overlap, flagged
only below `ADDRESS_SIM_FLOOR` · neither available → MANUAL_REVIEW.

**KYC (`_rule_kyc_verification`, §6).** any KYC detail present (status / result /
masked number / documents list) and no negative token → **VERIFIED** · an explicit
negative → **EXCEPTION** · nothing parsed → **MANUAL_REVIEW** (with the re‑warm hint,
since it usually means a stale cache — now auto‑handled by §13).

**Search (`indexing.search`, §11).** keyword tier first (exact → partial), best
keyword score on top · then semantic matches (related) for recall · if embeddings are
`None`, **keyword‑only** mode (`mode:"keyword"`) · no matches → empty list. Search
failure never touches verification.

**The confidence gate (`_pack`).** a `VERIFIED` line with `confidence == "low"` is
marked `needs_attention` and floated into the review queue — an unsure green tick is
treated as more dangerous than an honest flag.

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
