# HL Document Verification Console

An assistant that reads a Home Loan file's documents with Gemini (multimodal,
standing in for Document AI), reconciles values across them, and presents a
maker-checker review surface where a human adjudicates every line. Runs on a
Vertex AI Workbench instance. No extra Python packages required beyond what
Workbench provides plus FastAPI/uvicorn; the front-end is one dependency-free
HTML file.

> Status: complete (engine, API, and review console). Nothing here has been
> executed — it has been written and hand-reviewed only, per request. A syntax
> pass (`python -m compileall .`) before first run is advisable.

## File structure

```
hl_verifier/
  config.py          settings: paths, Vertex/Gemini ids, document keys + aliases, thresholds
  models.py          status enum, reviewer actions, adaptive action map, decision record
  checklist.py       the A–J checklist + reconciliation items, each tagged with how it is evaluated
  extraction.py      google-genai calls (extract + transcribe + embed), caching, retry/timeout
  reconciliation.py  cross-document rules + Indian-format name/amount/address normalisation
  evaluate.py        composes checklist + extractions + reconciliation + decisions -> evaluated case
  store.py           SQLite append-only decision/audit store
  vectorstore.py     SQLite + numpy vector store (embedded passages, cosine search)
  indexing.py        transcribe -> chunk -> embed -> store; semantic search over a case
  app.py             FastAPI routes (list, evaluate, serve PDF, decisions, upload, index, search)
  warm.py            one-off script: pre-extract + pre-index every case before a demo
  static/
    index.html       the review console (HTML/CSS/JS, no build step, no external deps)
  README.md          this file

  # created at runtime:
  cache/             extracted JSON + plain-text transcripts per document, keyed by file hash
  review.db          SQLite decisions/audit
  vectors.db         SQLite vector store (embedded passages for search)
  data/              YOUR input — see "Adding a case"
```

## What you MUST confirm before running

These are environment-specific and are the most likely cause of a first-run
failure. Set them as environment variables or edit `config.py`:

- `GCP_PROJECT` — your project id (often inferred on Workbench, but read
  explicitly here so failures are loud).
- `GCP_LOCATION` — defaults to `us-central1`.
- `GEMINI_MODEL` — defaults to `gemini-2.0-flash-001`. Confirm the exact model
  enabled in your project.
- `HL_EMBED_MODEL` — defaults to `text-embedding-005`, used for the search
  index. Confirm it is enabled in your project.
- Model calls use the `google-genai` SDK (installed) via a Vertex client
  (`genai.Client(vertexai=True, ...)`), isolated in `extraction.py`
  (`_gen`, `transcribe_sync`, `embed_texts`).

## Adding a case

Two ways:

1. **Upload in the UI (primary).** Click "New case", add the PDF documents,
   assign a document type to each (a guess is pre-filled from the filename), and
   start verification. Files are uploaded as multipart form data (one request
   per file, using `python-multipart`) and stored as `<doc_key>.pdf` under a
   generated case folder. Types you do not upload show as missing on the
   checklist.

2. **Pre-place folders.** Drop files under `DATA_DIR/<case_id>/`:

```
data/
  case_01/
    technical.pdf
    legal.pdf
    sanction.pdf
    ...
```

Filenames are matched to document types via `config.FILENAME_ALIASES`
(case-insensitive, punctuation ignored). Files that don't match are reported as
"unrecognised" rather than guessed at. Document types referenced by the
checklist but not present resolve to `document_missing`.

## Run

```bash
cd hl_verifier

# (optional but recommended) catch syntax errors first — this only compiles,
# it does not start anything or call Gemini:
python -m compileall .

# 1. (once, before a demo) populate the extraction cache so the live run is instant.
#    This is the step that actually calls Gemini.
python warm.py

# 2. start the API + console
uvicorn app:app --host 0.0.0.0 --port 8080
```

`warm.py` also initialises the SQLite decision database (`review.db`).

Then open the console in a browser. On a Workbench instance use the proxy URL
(e.g. forward port 8080), or browse to `http://<host>:8080/`. Pick a case from
the dropdown, enter your name in the Reviewer box (required before any decision),
and review.

## How to verify the results

Work out=>in: confirm the plumbing, then confirm the extraction is actually
correct, then confirm decisions persist.

**1. Plumbing (works even before Gemini is wired).** With empty/failed
extraction, the app still boots and is honest about it: `GET /api/cases` lists
your folders, `GET /api/cases/<id>` returns the checklist, document presence is
correct, missing documents show as `document_missing`, and lines that needed a
document fall back to `manual_review` rather than crashing. If this works, your
routing, file discovery, and the checklist config are sound.

```bash
curl -s localhost:8080/api/cases | python -m json.tool
curl -s localhost:8080/api/cases/case_01 | python -m json.tool | less
```

**2. Extraction quality (the real test).** After `warm.py`, its per-document
line prints how many fields it populated — a quick first signal. Then verify
field-by-field in the console, which is built precisely for this: every
extracted value carries a page number and a verbatim snippet, and the "p.N"
links open that document inline at that page. Click through a few values
(borrower name, sanctioned amount, survey number) and confirm the value matches
what is actually on the page. This spot-check loop is the verification mechanism
— do not trust a green line you have not traced to its source. Anything the
model was unsure of is flagged "low confidence" and pushed into Needs attention,
so start there.

**3. Reconciliation behaves.** Open the Cross-document reconciliation panel and
confirm the side-by-side values are the ones you'd compare by hand, and that a
genuine mismatch (e.g. name spelled differently on one document) is flagged as
an Exception rather than passed. A missing document should produce "cannot
reconcile, missing X", never a fabricated mismatch.

**4. Decisions persist and audit correctly.** Record a decision in the UI (or
via curl), reload the case, and confirm it is still shown against that line and
that the completion count moved. Then check the append-only audit:

```bash
# record a decision
curl -s -X POST localhost:8080/api/cases/case_01/decision \
  -H 'Content-Type: application/json' \
  -d '{"item_id":"R1","action":"accept","reviewer":"asha","note":"checked"}' | python -m json.tool

# read the full history (every decision, newest first, with the AI verdict captured at the time)
curl -s "localhost:8080/api/cases/case_01/audit" | python -m json.tool
```

Deciding the same line again appends a new row (latest wins) — the history is
never overwritten. An action that doesn't fit the line's status (e.g. `accept`
on a `document_missing` line) is rejected with the list of allowed actions,
which you can confirm by sending a deliberately wrong action.

## API

- `GET  /api/cases` — list case ids.
- `GET  /api/doc-types` — document types and their labels (for the upload UI).
- `POST /api/cases` — create a new empty case; returns a generated `case_id`.
- `POST /api/cases/{case_id}/files/{doc_key}` — upload one document as the raw
  request body (Content-Type `application/pdf`); stored as `<doc_key>.pdf`.
  Validated for PDF signature and size.
- `GET  /api/cases/{case_id}` — full evaluation: checklist lines with AI status,
  finding, confidence, evidence (doc/page/snippet), the latest reviewer
  decision, plus document presence and a summary. Lines are returned
  exception-first.
- `GET  /api/cases/{case_id}/document/{doc_key}` — serves the PDF inline; open
  with `#page=N` to jump to the evidence page.
- `POST /api/cases/{case_id}/decision` — body `{item_id, action, reviewer, note?}`.
  The action is validated against the line's current AI status; the AI verdict
  and evidence are snapshotted into the audit row.
- `POST /api/cases/{case_id}/accept-auto-verified` — body `{reviewer}`. Endorses
  every still-open auto-verified line in one step.
- `GET  /api/cases/{case_id}/audit` — full append-only decision history.
- `POST /api/cases/{case_id}/index` — build/rebuild the semantic search index
  (cheap if already built; `?force=true` to rebuild).
- `GET  /api/cases/{case_id}/search?q=...` — semantic search over the document
  text; returns passages with `doc_key`, `page`, score, and snippet.

## Storage (why two stores)

Two different jobs, hence two stores:

- **`review.db` (SQLite, relational).** The maker-checker decision/audit trail —
  append-only rows queried by case and item, with the AI's verdict snapshotted
  at decision time. This is core to verification. SQLite is standard library, so
  no install.
- **`vectors.db` (SQLite + numpy, vector store).** Embedded document passages for
  semantic search — a nearest-neighbour access pattern, not relational. There is
  no dedicated vector DB in the environment (no FAISS/Chroma/Qdrant/pgvector), so
  it is implemented as a SQLite table of float32 vectors with brute-force cosine
  in numpy; at a few hundred passages per case this is instant and swappable for
  a managed vector DB later.

The split into two files is a soft choice (separation and different rebuild
lifecycles); they could be two tables in one file. The vector store is **not
required** for verification or the review UI — it only powers "Search this file"
and can be removed without affecting the verification path.

## Status model (maker-checker)

The AI proposes a `VerificationStatus`; the human adjudicates with a
`ReviewAction`. The two are never conflated. Sign/seal/notary lines are
detection-only (`needs_signoff`) — the AI locates the mark and page, the human
authenticates. Actions offered adapt to the status (see `models.ALLOWED_ACTIONS`).

## What is stubbed / needs your input

- **Exact checklist wording and pass/fail criteria.** `checklist.py` reconstructs
  the A–J structure and document mapping we discussed, with representative line
  text. Replace `text` with your exact sheet wording and confirm each `rule`
  against your real acceptance criteria. Lines marked `# TODO-WORDING` are
  placeholders.
- **System-data lines (sections F, H, J and parts of D)** resolve to
  `pending_system` until LOS/Salesforce/CAM access lands. They are wired so they
  light up with no rework once data is available.
- **Large files.** Vertex inline-data has a size ceiling (`INLINE_MAX_BYTES`).
  Files above it are reported as an explicit error; GCS staging is not
  implemented.

## The console

`static/index.html` is a single dependency-free file (system fonts, no CDN, no
framework — deliberate, since the browser may sit behind a locked-down network).
It is exception-first: a Needs-attention group at the top, Pending-system and
Verified groups collapsed below. It has a per-check cross-document reconciliation
panel with source links, an OTC/PDD readiness meter, adaptive maker-checker
buttons per line, an "Accept all auto-verified" bulk action, an evidence drawer
that opens a document inline at the cited page, keyboard navigation
(`j`/`k` to move, `a`/`d` to accept/decline, `o` to open evidence, `Esc` to
close the drawer), a live "N of M reviewed" tracker, and the "New case" upload
flow described above.

The input documents are scanned PDFs, so evidence viewing is page jump plus the
verbatim snippet (which works on scans); in-document text highlighting is not
applicable without a text layer and is intentionally omitted. Browser PDF
viewers still navigate to the cited page for scans.

## Known limitations (be honest with the room)

- Gemini extraction here is demo-grade, not production-grade — this is precisely
  the argument for granting Document AI. Field extraction can miss or
  misread; the snippet + page on each field, and the confidence gate that forces
  low-confidence results to a human, are the guardrails.
- Reconciliation favours flagging for human review over silently passing.
  Amounts written only in words, names with heavy variation, and addresses with
  no shared survey number are routed to a human rather than auto-judged.
- Inputs are medium-quality scans, so OCR-style misreads are expected — faint
  stamps, skew, and handwriting are the usual culprits. The model is told to mark
  unclear text as low confidence (which forces a human look), and every value is
  traceable to its page, but spot-checking the flagged lines matters more here
  than on clean digital PDFs.
