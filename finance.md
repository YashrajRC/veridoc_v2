# finance.md — What this tool checks, in plain English

> Written for a reader with **zero finance background**. It explains *what* a
> home‑loan file is, *what* each checkpoint means, and *how* the tool decides
> pass / fail. The technical "how the code does it" is in [logic.md](logic.md).

---

## 1. The big picture

When a bank approves a home loan, it does **not** hand over the money
immediately. Before it **disburses** (releases) the funds, a person called a
**checker** must confirm that every required document is present, genuine, signed,
and consistent. This is the **pre‑disbursement verification**. If something is
wrong (a forged document, a property with a legal dispute, a loan bigger than the
property is worth), the bank could lose money — so this step is the last safety
gate before cash goes out.

Today a human reads a thick folder of scanned PDFs and ticks a long checklist by
hand. **This tool does the first pass automatically:** it reads each document with
Google's Gemini AI, fills in the checklist, and shows a human reviewer exactly
where each answer came from — so the human *adjudicates* instead of *hunting*.

A key principle: **the AI only proposes; a human decides.** The AI never
"approves" a loan. It says "this looks verified / this looks wrong / this is
missing," always with the page and the quoted text it relied on, and a human
accepts or overrides every line. This is called **maker‑checker** (the AI is the
"maker", the human is the "checker").

---

## 2. The documents in a loan file (glossary)

| Document | What it is, in plain English |
|---|---|
| **Technical / valuation report** | An engineer/valuer visited the property and estimated its **market value** and construction stage. |
| **Legal & search report (TSR/LSR)** | A lawyer examined the property's ownership records and reports whether the **title** (legal ownership) is clean and whether there is any existing claim (**encumbrance**) on it. |
| **Sanction letter** | The bank's official approval letter: the **loan amount**, interest, tenure, and the **conditions** attached. |
| **Loan agreement** | The actual contract the borrower signs. A **supplementary** agreement is an add‑on that amends the main one. |
| **Insurance policy** | Insurance on the property or loan. The bank wants its name recorded on it (so it's protected if the property is damaged). |
| **DRL (Disbursement Request Letter)** | The borrower's signed letter saying "please release the money," and how much. |
| **Affidavit** | A sworn statement, which must be **notarised** (certified by a notary) and stamped. |
| **RCU report** | From the **Risk Containment Unit** — a fraud‑screening team that checks documents for forgery. |
| **FI (Field Investigation) report** | Someone physically visited the applicant's **home and office** to confirm they exist and the person is genuine. |
| **End‑use declaration** | The borrower states what the loan will be used for (must fit a home loan). |

Some documents are **referenced by the checklist but not supplied** in this test
set — the tool knows about them and honestly marks them missing rather than
pretending: **CIBIL** (credit‑bureau score/history), **KYC** (identity & address
proof), **CAM** (the bank's internal credit appraisal memo), **LOD** (list of
documents / old‑lender foreclosure letter in balance‑transfer cases), **MITC**
(the "Most Important Terms & Conditions" summary), **CERSAI** (a government
registry where the bank records its charge on the property so it can't secretly
be pledged twice).

---

## 3. The seven answers the tool can give (statuses)

Every checklist line ends in one of these. They are deliberately distinct so the
reviewer knows *why* a line needs them:

| Status | Plain meaning | What the reviewer does |
|---|---|---|
| **Verified** | Checked against a document and it passes. | Accept (or decline if they disagree). |
| **Exception** | Checked and it **fails or mismatches** — needs attention. | Confirm the issue, override‑accept, or raise a query. |
| **Document missing** | A required document isn't in the folder at all. | Note it, or formally **waive** it. |
| **Pending system data** | Needs the bank's internal systems (LOS/CIBIL/CAM), which aren't connected yet. | Note / waive; it will light up automatically once data is available. |
| **Not applicable** | A conditional rule that doesn't apply to this case (e.g. a rule only for loans ≥ ₹5 crore). | Note if they disagree. |
| **Needs sign‑off** | A signature / seal / notary mark: the AI *locates* it, but **a human must authenticate** it. | Confirm the sign‑off, decline, or raise a query. |
| **Manual review** | A policy‑driven, human‑only judgement, or a document the AI couldn't read. | Review by hand. |

The screen is **exception‑first**: things that need a human (exceptions, sign‑offs,
manual) float to the top; clean "verified" lines collapse below. Anything the AI
was *unsure* about (low confidence) is also pushed up even if it "passed," because
a green tick you can't trust is worse than a flagged one.

---

## 4. The checkpoints, section by section

Each line below shows: **the id** · what it means · which document · how it's
decided. (The exact wording of a real bank's A–J sheet would replace the sample
text; the structure and checks are what matter.)

### A. Applicant verification — *is the borrower genuine?*
- **A1 — Residence FI positive** · *FI report* · The field visit to the **home**
  must be positive. The tool reads the residence verdict; if it says
  "negative," that's an **Exception**; if positive, **Verified**.
- **A2 — Office/business FI positive** · *FI report* · Same, for the **workplace**.
- **A4 — RCU screening clear** · *RCU report* · The fraud‑unit verdict must be
  clear. If it reads "negative" or "refer," that's an **Exception** (possible
  fraud); otherwise **Verified**.
- **A5 — CIBIL on file** · *not supplied here* · resolves to **Document missing** —
  an honest "not provided," not a failure.
- **A6 — KYC verified in RCU** · *RCU report* · The KYC checks (Aadhaar, PAN, bank
  statement) are actually performed *inside* the fraud‑unit (RCU) report, so the
  tool reads their **results** and reports each one — e.g. "Aadhaar: matched /
  operative; PAN: valid." If any explicitly says *not matched / inoperative /
  failed* → **Exception**; all clear → **Verified**; none found → **Manual review**.
  (This is stronger than just confirming a KYC document exists.)

### B. Legal & title — *does the borrower really own a clean property?*
- **B1 — Title clear & marketable** · *Legal report* · The lawyer's **title
  status** must contain words like "clear / marketable / mortgageable" →
  **Verified**. Anything else → **Exception** (the title may be disputed).
- **B2 — No encumbrance** · *Legal report* · The property must be free of other
  claims. "Nil / none / no encumbrance" → **Verified**. But if the text contains
  red‑flag words like *mortgage, charge, lien, lis pendens (a pending lawsuit),
  attachment, except…*, the tool refuses to auto‑clear it → **Exception** for a
  human to read.
- **B5 — Advocate signature & seal** · *Legal report* · The lawyer must have
  signed and stamped the report. The AI detects the marks, then a human
  authenticates → **Needs sign‑off**.
- **B7 — Senior‑counsel vetting (loans ≥ ₹5 crore)** · *conditional* · Only
  applies to large loans. If this loan is under ₹5 crore it's **Not applicable**;
  if ≥ ₹5 crore it becomes a **Manual review** for a senior lawyer's check.

### C. Technical / valuation — *is the property real and worth the stated value?*
- **C1 — Property valued** · *Technical report* · A market value must be present
  and parseable → **Verified**; otherwise **Manual review**.
- **C2 — Valuer signature & seal** · *Technical report* · **Needs sign‑off**.

### D. Credit / end‑use — *will the money be used as intended?*
- **D1 — End‑use declared** · *End‑use declaration* · The stated purpose must be
  present → **Verified**; else **Manual review**.

### E. Operations / disbursement documents
- **E1 — DRL on file and signed** · *DRL* · The borrower's release request must
  be **signed**. If no signature is detected → **Exception**; signed → **Verified**.
- **E2 — List of documents / foreclosure letter** (balance‑transfer cases) ·
  *not supplied* → **Document missing**.
- **E11 — RCU referenced in ops file** · *RCU report* · Just needs the RCU
  document present → **Verified** if present.
- **E15 — CERSAI charge registration** · *not supplied* → **Document missing**.

### F. Fees / login
- **F1 — Processing fee / login balance** · *system* · This lives in the bank's
  loan system, not in the PDFs → **Pending system data**.

### G. Sanction & insurance — *do the approval and protection match the loan?*
- **G1 — Sanction letter present with terms** · *Sanction letter* · A sanctioned
  amount must be parsed → **Verified**; else **Manual review**.
- **G2 — Insurance on file** · *Insurance* · A sum assured must be present →
  **Verified**; else **Manual review**.
- **G3 — Bank's interest noted on policy** · *Insurance* · The policy must record
  the **bank** as the protected party. If that flag is detected → **Verified**.

### H. System block (the bank's internal systems)
- **H1 — LOS data matches documents**, **H5 — CIBIL pulled & recorded** · *system*
  → **Pending system data** until the bank's Loan Origination System is connected.
- **H6 — CAM on file** · *not supplied (an internal Excel)* → **Document missing**.

### I. Legal documentation
- **I1 — Loan agreement executed & signed** · *Loan agreement* · **Needs sign‑off**
  (AI finds the borrower's signature; human authenticates).
- **I2 — MITC acknowledged** · *not supplied* → **Document missing**.
- **I3 — Affidavit executed, notarised & stamped** · *Affidavit* · **Needs
  sign‑off** — the AI looks for the signature, notarisation and stamp; a human
  confirms.

### J. Deviations
- **J1 — Deviations approved at correct authority** · *system* → **Pending system
  data**.

### P. Policy / pricing compliance — *do the sanctioned terms obey the bank's grid?*
The bank publishes a **pricing grid** (the L&T HL & LAP grid in the `Policy/`
folder): what interest rate and fees are allowed for a given loan size and credit
band. These checks compare the **sanction letter** against that grid and **show the
arithmetic and the quoted policy clause** as proof — so the reviewer sees the
reasoning, not just a colour.

- **P1 — Interest rate within the grid** · *sanction letter* · The tool reads the
  sanctioned rate, works out the loan‑amount band (e.g. ₹0–50 lakh), and shows the
  **published window** for that band (Salaried vs Self‑Employed) plus the absolute
  floor (7.75%, below which only senior management may approve). If the rate is
  **below the floor** → **Exception** (needs a documented deviation approval); if it
  sits inside the window → **Verified** (confirm the exact CIBIL band); if it is
  above the standard grid → **Manual review** (maybe a special product). It also
  prints the whole grid for that band so the reviewer can pin the exact cell.
- **P2 — Fees within policy** · *sanction letter* · Checks the **processing fee**
  (HL Salaried ₹10,000 + GST, Self‑Employed 0.50%, LAP 1%) and the **login fee**
  (cap ₹1,000). Outside policy → **Exception** with the offending number; within →
  **Verified**.

### R. Cross‑document reconciliation — *does the same fact agree everywhere?*
This is the cleverest part. The same fact appears on many documents; if they
**disagree**, that's a red flag (a possible mix‑up or fraud). Importantly, the tool
does **not** demand the values be *character‑for‑character identical* — scanned
Indian documents legitimately vary (Mr/Dr, OCR slips, co‑applicants, "Khasra No‑"
prefixes). It tolerates those variants and flags only a **genuine** difference.

- **R1 — Borrower name consistent** · across *up to 10 documents* · The tool finds
  the **applicant's core name** (the name tokens that recur across the file),
  tolerating honorifics, **OCR noise** ("Haque" ≈ "Haqull"), **initials** ("A." ≈
  "Anil") and word order, and it **separates co‑applicants** (it tells you a second
  name is present rather than calling it a mismatch). All documents share the core
  name → **Verified**; a document that shares *little* of the core → **Exception**
  (could be a wrong file). It shows each document's % match as proof.
- **R2 — Property identity consistent** · *technical, legal, insurance* · It prefers
  the **survey/plot number**, compared as a **set of numbers** so "Khasra No‑613/49,
  613/154 Part" and "613/49, 613/154" correctly reconcile. A shared plot number →
  **Verified**; none in common → **Exception**. If only addresses exist, it compares
  word‑overlap and flags only a *very* low overlap.
- **R3 — Sanctioned amount = disbursement request** · *sanction vs DRL* · The
  amount the bank approved must equal the amount the borrower asked to release.
  Equal → **Verified**; different → **Exception**.
- **R4 — Loan‑to‑Value (LTV) within bounds** · *technical vs sanction* · **LTV =
  loan ÷ property value.** Lending ₹90 against a ₹100 property is risky. The tool
  computes the percentage; an obviously high LTV (>90%) is flagged for review.
  (The exact policy cap lives in the bank's system, so the tool states that limit
  plainly rather than pretending to know it.)
- **R5 — Insurance adequate vs loan** · *insurance vs sanction* · The insured
  **sum assured** should be **at least** the loan amount, so the bank is fully
  covered. Below the loan → **Exception**.
- **R6 — Sanction conditions tracked (OTC/PDD)** · *sanction* · The sanction
  letter lists conditions. **OTC** ("over‑the‑counter") conditions must be
  satisfied **before** money goes out; **PDD** ("post‑disbursement documents") may
  be collected **after**. The tool counts how many OTC conditions still lack
  evidence and flags those.

---

## 5. The OTC / PDD readiness meter

Because **OTC** conditions gate the disbursement, the console shows a small
readiness meter: "X of Y OTC conditions evidenced." When that reaches 100% (and
the exceptions are cleared), the file is operationally ready for the money to be
released — subject, always, to the human checker's final sign‑off.

---

## 6. What a reviewer actually does

For each line the screen offers only the actions that make sense for that status
(you can't "accept" a missing document; you can "waive" it). Every decision is
recorded with **who** decided, **when**, and a snapshot of **what the AI said and
the evidence at that moment** — an append‑only **audit trail**. Decide the same
line again and it adds a new row (latest wins); history is never overwritten.

The golden rule for the human: **don't trust a green line you haven't traced to
its source.** Every value links to the exact page and quotes the exact words it
came from — click through the flagged ones first.
