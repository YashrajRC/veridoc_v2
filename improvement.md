# improvement.md — Where this can go next (in scope)

> "In scope" = the same shape we already have: a FastAPI app + single‑file console
> running on **Vertex AI Workbench**, using **Gemini** (and Vertex embeddings),
> degrading gracefully, with a human maker‑checker. Nothing here needs a new
> platform, a database server, or a rewrite. Each item notes **why**, rough
> **effort**, and **impact**, and points at the code it touches.

Legend — effort: 🟢 small (hours) · 🟡 medium (a day or two) · 🔴 large (week+).

---

## Already shipped (since this list was written)

These items from below are now **done** and live in the code:

- **#5 Amounts‑in‑words parser** — `parse_amount` now reads "One Crore Four Lakh …"
  so LTV/valuation compute instead of falling to manual review.
- **#6 Editable policy thresholds** — LTV review cap, login‑fee cap and the fuzzy
  matching tolerances live in `hl_verifier/config.py`; the full pricing **grid** is
  in `hl_verifier/rules/policy.py`.
- **#7 Stronger name/address matching** — applicant‑core + fuzzy/initials/co‑applicant
  handling (R1) and survey‑number‑set matching (R2); tolerances configurable.
- **#16 "Explain this verdict"** — every reconciliation/policy line renders a
  **calculation block** (steps → formula → verdict → quoted policy clause), each
  figure linked to its page.
- **Policy checks (new section P)** — interest‑rate and fee compliance against the
  L&T pricing grid, with calculation + proof.
- **KYC from RCU (A6)** — reads the Aadhaar/PAN/bank verification results, not just
  "report present."
- **Search** — rebuilt as keyword‑first hybrid (exact matches lead; semantic recall
  underneath; works even without embeddings).
- **Grounded assistant (Tier‑1 RAG)** — `pipeline/assistant.py` + `POST …/ask` +
  an "Ask about this file" panel. Answers free‑text questions ("compare the
  sanction amount across the documents") from retrieved passages **and** the
  verified findings/reconciliation values, with cited evidence; read‑only, no
  new dependencies. Natural next step: **Tier‑2** = let Gemini call tools
  (search / get_field / get_reconciliation) for multi‑step questions.
- **Modular package + teaching doc** — code reorganised under `hl_verifier/`; the
  agentic‑framework options are taught and weighed in [agentic_ai.md](agentic_ai.md)
  for a Phase‑2 decision.

The remaining items below are still open. (Code paths now live under `hl_verifier/`.)

---

## A. Extraction quality (the biggest lever)

1. **Bounding‑box evidence → real in‑document highlighting.** 🟡
   Ask Gemini to also return a normalised box (or line index) per field, store it
   alongside `page/snippet`, and draw a highlight overlay in the PDF drawer instead
   of only jumping to the page. *Why:* the README calls out that highlighting is
   missing on scans; boxes make spot‑checking far faster. *Touches:*
   `DOC_FIELDS`/prompt in [extraction.py](extraction.py), evidence shape in
   [evaluate.py](evaluate.py), the drawer in [static/index.html](static/index.html).

2. **Per‑field re‑ask ("not convinced? re‑extract this line").** 🟡
   A button on a flagged line that re‑queries Gemini for *just that field* with a
   tighter prompt (and optionally a higher‑capability model). *Why:* turns a dead
   end into a one‑click retry without re‑running the whole document. *Touches:* a
   new `/api/.../refield` endpoint + a focused prompt.

3. **Two‑pass self‑consistency on low‑confidence fields.** 🟡
   For fields that come back `low`, run a second independent extraction and only
   auto‑pass when both agree; otherwise force the human. *Why:* cuts silent
   misreads on faint stamps/handwriting at modest extra cost. *Touches:*
   `extract_document` in [extraction.py](extraction.py).

4. **Document AI for OCR, Gemini for understanding.** 🔴
   Run Google **Document AI** to get a clean text layer + tables first, then feed
   that to Gemini for field extraction. *Why:* this is literally the production
   argument the README makes — demo‑grade OCR is the main accuracy ceiling.
   *Touches:* a new integration module; everything else stays.

---

## B. Verification depth

5. **Amounts‑in‑words parser.** 🟢
   Extend `parse_amount` ([reconciliation.py](reconciliation.py)) to convert
   "Forty‑Five Lakhs" → 4,500,000 (a small Indian‑numbering word parser). *Why:*
   today amounts written only in words return `None` → manual review; many sanction
   letters write the figure in words. Reduces manual load on **R3/R4/R5**.

6. **Real policy thresholds, not just heuristics.** 🟢
   Move the LTV cap, insurance‑adequacy ratio, and the ₹5 Cr title threshold into a
   small `thresholds.yaml` (mirroring the prototype) so credit policy is editable
   without code. *Why:* **R4** currently hard‑codes 0.90 and says "cap lives in
   LOS"; a config file lets you encode the actual cap per product. *Touches:*
   [config.py](config.py), `recon_ltv`/`recon_insurance_adequacy`.

7. **Stronger name/address matching.** 🟡
   Add token‑set ratio / initials handling to `names_match` and a configurable
   address‑similarity threshold. *Why:* strict equality flags benign variants
   ("A. Kumar" vs "Anil Kumar") as exceptions; a graded match with a review band
   reduces false alarms while still surfacing genuine divergence on **R1/R2**.

8. **Checklist loaded from a sheet, with exact wording.** 🟡
   Drive `CHECKLIST` from a YAML/CSV export of the real A–J sheet (the code already
   notes the wording is representative). *Why:* makes the checkpoints exactly match
   the bank's policy and lets non‑engineers edit them. *Touches:*
   [checklist.py](checklist.py) → a loader.

---

## C. Multi‑document handling (building on what we just shipped)

9. **Sub‑type labels (main vs supplementary).** 🟢
   Let the classifier return a free‑text `subtype/title`; show it on the doc chip
   and evidence ("loan agreement · supplementary"). *Why:* "we don't know which is
   which" becomes "we show which is which," without changing the merge.

10. **Conflict surfacing across same‑type documents.** 🟡
    When the main and supplementary disagree on a field, don't silently keep the
    higher‑confidence one — raise a small **"amended value"** note so the reviewer
    sees both. *Why:* a supplementary agreement often *amends* the amount/terms;
    that's exactly the case a human should see. *Touches:* `merge_extractions`
    (carry the runners‑up) + a UI note.

---

## D. System integration (lights up the whole right‑hand side)

11. **LOS / CAM / CIBIL connectors.** 🔴
    The `SYSTEM` lines (**F1, H1, H5, J1**) are already wired to show
    `pending_system`; a read‑only connector that pulls the LOS application record
    would let them auto‑evaluate (e.g. "LOS amount matches sanction"). *Why:* it's
    the designed‑in next step — no rework, just data. *Touches:* a new integration
    + a few `SYSTEM`→`AUTO_RECON` rules.

12. **CERSAI / charge‑registry check.** 🟡
    Even a manual‑upload slot for the CERSAI receipt flips **E15** from
    `document_missing` to a real check.

---

## E. Reviewer experience & reporting

13. **One‑click verification report (PDF/Excel).** 🟡
    Generate a signed‑off summary — every line, its status, the evidence page, and
    the reviewer's decision — as a downloadable file. *Why:* this is the artefact a
    credit/ops team actually files. *Touches:* a new `/api/cases/{id}/report`
    endpoint (ReportLab/openpyxl are in the Workbench image).

14. **Cross‑case dashboard.** 🟡
    A landing view listing every case with its readiness meter and open‑exception
    count. *Why:* a checker handles many files a day; today it's one case at a time.

15. **Reviewer identity / light auth.** 🟢→🟡
    Right now `reviewer` is a free‑text box. Tie it to the Workbench/IAM identity so
    the audit trail is trustworthy. *Why:* maker‑checker is only as good as "who
    decided."

16. **"Explain this verdict" expander.** 🟢
    Each line already has the finding + evidence; add a small expander that shows
    the rule that fired (`item.rule`) in plain words. *Why:* trust and training —
    pairs naturally with [finance.md](finance.md).

---

## F. Reliability, evaluation & cost

17. **A golden‑set accuracy harness.** 🟡
    Hand‑label a handful of real documents, then a test that runs extraction and
    reports field‑level precision/recall against the labels. *Why:* you currently
    cannot answer "did a prompt change make extraction better or worse?" This makes
    quality measurable — the single highest‑leverage reliability add.

18. **Cheaper/faster classification & a model ladder.** 🟢
    Classification only needs a page or two — send the first N pages, or a smaller
    model, and reserve the strong model for extraction. Optionally escalate to a
    bigger model only on `low`‑confidence fields. *Why:* cuts cost/latency on the
    per‑file classify call we added. *Touches:* `classify_pdf_sync`.

19. **Structured output via response schema everywhere.** 🟢
    The classifier uses free‑form JSON; pin it (and extraction) to a Pydantic
    `response_schema` like the prototype's `run_structured`. *Why:* fewer parsing
    fallbacks, stricter guarantees. *Touches:* [extraction.py](extraction.py).

20. **Idempotent, resumable pre‑warm.** 🟢
    `warm.py` already caches by content; add a progress line and skip‑summary so a
    large case set can be warmed in the background reliably before a demo.

---

## Suggested order (impact ÷ effort)

1. **#17 accuracy harness** — you can't improve what you can't measure.
2. **#5 amounts‑in‑words** + **#6 thresholds.yaml** — small, removes real manual load.
3. **#1 bounding boxes** — the single biggest UX upgrade for the reviewer.
4. **#10 conflict surfacing** — finishes the multi‑doc story you just asked for.
5. **#4 Document AI** / **#11 LOS** — the two structural upgrades, when you're ready
   to argue for the infrastructure.
