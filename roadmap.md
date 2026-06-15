# roadmap.md — The agentic phases, taught in detail

> The detailed sequel to [agentic_ai.md](agentic_ai.md). That doc taught the
> vocabulary and the framework choices; this one walks each **future phase** of
> *this* project — what we build, **which agentic concept is used where and how**,
> why it helps a home‑loan verification team, what could go wrong, and a plain
> script for explaining it in the room.
>
> Written for a reader new to this. Every term is defined the first time it
> appears. Nothing here is built yet — it is the plan you present *after* the
> Phase‑1 demo. Read top to bottom once; each phase builds on the last.

---

## 0. How to use this document

Two audiences, one document:
- **You, learning.** Read the "concept, taught" parts. They assume nothing.
- **You, presenting.** Each phase ends with **"How to say it"** — a short, jargon‑light
  script you can paraphrase so you sound clear and grounded, not buzzwordy.

The golden rule to repeat in every room, because it is what makes a lender trust
this: **the AI reads and reasons; deterministic rules and a human decide.** Every
phase below preserves that. We add autonomy only where a human still signs off and
only after we can *measure* quality.

---

## 1. Where we are now (Phase 1) — the baseline

A **workflow**: a fixed sequence of steps. The only AI step is Gemini reading a
scanned PDF into structured fields (each with a page, a verbatim quote, and a
confidence). Everything after — the pass/fail of each checklist line — is plain,
inspectable Python. A human reviews every line (this is the **maker‑checker** gate).

Why start here: it is **deterministic** (same input → same output), **auditable**
(every line traces to a page), and it already runs on Workbench. It is the trust
anchor. Phases 2+ add capability *around* this anchor without dissolving it.

---

## 2. The concept ladder (the map)

Each phase introduces one or two genuinely new ideas. Keep this table handy; it is
the whole story on one screen.

| Phase | New concept(s) you'll learn | What it buys the HL team |
|---|---|---|
| 1 (now) | workflow · tool · grounding · human‑in‑the‑loop | a trustworthy, auditable baseline |
| 2 | **state graph** (LangGraph): nodes, edges, routing, **checkpointing**, **streaming**, **human‑in‑the‑loop interrupt**, **reflection** | resilience, live progress, far better document reading |
| 3 | **tools / function calling** · **agent loop (ReAct)** · **RAG** | a read‑only research assistant that finds evidence and answers questions, with citations |
| 4 | **MCP** (tool servers) · system **connectors** | the "pending system" checklist lines light up automatically |
| 5 | **multi‑agent**: supervisor/worker, handoffs, shared "blackboard" | specialist reasoning per checklist section |
| 6 | **memory**: short vs long‑term, episodic/semantic · **feedback learning** | the tool gets more consistent and faster with use |
| always‑on | **eval harness** · **LLM‑as‑judge** · **observability/tracing** · **guardrails** · **cost control** | you can *prove* it works and keep it cheap and safe |

---

## 3. Phase 2 — Orchestrate as a graph, and read documents properly

### In one line
Re‑express the pipeline as an explicit **graph** so it can pause, resume, stream
progress and self‑check — and swap demo‑grade reading for production‑grade reading.

### The concepts, taught

**State.** The "working memory" of one run: the case id, the list of documents, the
fields extracted so far, the checklist results, the errors. Think of it as a shared
clipboard every step reads from and writes to.

**Graph, node, edge (LangGraph).** Instead of one long script, you describe the work
as a **graph**: each **node** is a small function `state -> state` (it does one job
and updates the clipboard); each **edge** says "after this node, go to that one." An
edge can be **conditional** — "if a field came back low‑confidence, go to the
re‑extract node; otherwise continue." This is just a flowchart your code actually
executes. Why bother vs a plain script? Because the graph form gives you the next
four things almost for free:

**Checkpointing.** After each node, the state is **saved** (LangGraph has a
SQLite/Postgres "checkpointer"). If a 20‑document case fails on document 15, you
**resume from 15**, not from scratch. For a demo or a flaky network, this is gold.

**Streaming.** Because each node finishes distinctly, you can show **live progress**
in the console — "reading sanction letter… reconciling amounts…" — instead of a
spinner. Reviewers trust what they can watch.

**Human‑in‑the‑loop interrupt.** The graph can **pause** at a node (`interrupt`),
hand control to a person, and **resume** when they act. Our maker‑checker becomes a
first‑class pause in the flow rather than a bolt‑on screen.

**Reflection (self‑consistency).** "Reflection" = the system **double‑checks its own
work**. For any field Gemini marked low‑confidence, a small node re‑reads just that
field a second time (and/or with a stronger model); if the two reads disagree, it is
forced to a human instead of quietly passing. This is cheap insurance on faint
stamps and handwriting.

### What we build (concretely)

A LangGraph `StateGraph` whose nodes wrap the **existing, tested functions** — we are
re‑orchestrating, not rewriting the rules:

```
 discover ─▶ classify ─▶ extract(fan‑out per doc) ─▶ [low‑conf?]─▶ re_extract
                                                          │ no
                                                          ▼
                          ocr(Document AI) ──▶ merge ─▶ reconcile ─▶ policy ─▶ evaluate
                                                                                   │
                                                                                   ▼
                                                                     await_review (INTERRUPT)
                                                                                   │ reviewer acts
                                                                                   ▼
                                                                                finalize
```

- **`ocr` node — Document AI.** Today Gemini reads the scan directly (demo‑grade).
  **Document AI** (`google-cloud-documentai`, already installed) is Google's
  specialist OCR: it returns a clean text layer, tables, and the **bounding box** of
  each field. We run it first, then hand the clean text to Gemini for understanding.
  This is the single biggest accuracy lever (see [improvement.md](improvement.md) #4),
  and the boxes let the console **highlight the exact words** on the page, not just
  jump to it.
- **State** (a typed object): `case_id`, `doc_ids`, `raw_extractions`, `merged`,
  `checklist`, `decisions`, `errors`, `progress`.
- Maps onto today's code: `discover`→`discover_documents`, `extract`→`extract_documents`,
  `merge`→`merge_extractions`, `reconcile`/`policy`/`evaluate`→`_evaluate_item`. The
  deterministic rules are untouched; they just become nodes.

### Why it matters here
Resilience (resume), trust (live progress + self‑check), and the accuracy jump from
Document AI — all without changing a single verdict rule.

### Risks & safety
LangGraph is a new dependency and concept to learn; mitigate by wrapping tested code
node‑by‑node and keeping the rules deterministic. Document AI adds cost per page;
mitigate by caching (we already cache by file hash) and only OCR’ing once.

### How to say it
"Right now it's a straight‑line script. In Phase 2 we turn it into a flowchart the
software actually runs, so it can save its place and resume if something fails, show
the reviewer live progress, pause for human sign‑off as a built‑in step, and
double‑check anything it wasn't sure about. We also switch to Google's specialist
document reader, Document AI, which is the main thing that improves accuracy and lets
us highlight the exact words on the page."

---

## 4. Phase 3 — Tools and a read‑only research assistant

### In one line
Give the reviewer a Q&A assistant that can **look things up across the file and
answer with citations** — without ever touching a verdict.

### The concepts, taught

**Tool / function calling.** A **tool** is just a normal function we *allow the model
to call* — it has a name, a one‑line description, and a typed input (e.g.
`search_documents(query: str)`). **Function calling** is the model, instead of
answering directly, emitting a structured request like `search_documents("insurance
sum assured")`; our code runs it and feeds the result back. The model never runs code
itself — it *asks*, we execute, we return. Tools are how an LLM gets "hands."

**Agent loop (ReAct).** An **agent** is an LLM in a loop: **Reason** ("I need the sum
assured") → **Act** (call a tool) → **Observe** (read the result) → repeat until it
can answer. "ReAct" is just the name for that reason‑act‑observe cycle. This is the
first place real *autonomy* appears — and we deliberately keep it **read‑only**.

**RAG (retrieval‑augmented generation).** "Don't answer from memory; fetch the
relevant text first, then answer using it." Our keyword‑first search is the *retrieve*
half; RAG adds the *generate* half — the model composes an answer **grounded in** the
retrieved passages and cites them.

### What we build (concretely)
An **"Ask this file"** assistant beside the search box. Its tools (all read‑only):
- `search_documents(query)` — the hybrid search we already have.
- `get_field(doc, field)` — a value we already extracted (with its page).
- `open_page(doc, page)` — return the text of a page.
- `compute(expression)` — safe arithmetic (e.g. LTV) so numbers aren't guessed.

A reviewer types "Is the property insured for at least the loan amount?" The agent
calls `get_field("insurance","sum_assured")` and `get_field("sanction",
"sanctioned_amount")`, runs `compute`, and replies "Sum assured ₹26,00,000 ≥ loan
₹26,00,000 — adequate (insurance p.1, sanction p.1)." Every answer **cites pages**;
the agent **cannot record a decision** — it informs the human, who still acts.

### Why it matters here
It turns the search box into a domain assistant and collapses the reviewer's "hunt
for evidence" into a question. Risk is low because the tools only *read*.

### Risks & safety
An agent loop can wander or over‑spend; cap the number of tool calls, force every
claim to carry a citation, and forbid any write/decision tool. If the assistant
can't ground an answer, it must say "not found," not invent one.

### How to say it
"We give the reviewer a research assistant. They ask a plain question; the assistant
looks across the documents using a fixed set of *read‑only* tools and answers with
the page it found it on. It speeds up evidence‑hunting, but it can't approve or
reject anything — it only helps a person see faster."

---

## 5. Phase 4 — Connect the bank's systems (MCP)

### In one line
Plug the live systems (LOS, CAM, CIBIL, CERSAI) in as **reusable tools** so the
"pending system" checklist lines verify themselves.

### The concept, taught
Today the lines F1, H1, H5, J1 read **Pending system data** because the truth lives
in the bank's Loan Origination System (LOS), not in the PDFs. **MCP (Model Context
Protocol)** is an open **standard** for exposing tools and data to *any* AI agent —
think of it as a universal adapter ("USB‑C for AI"). You wrap a system **once** as an
**MCP server** that offers typed tools — `get_application(lan)`, `get_cibil(pan)`,
`get_cersai(pan)` — and then any MCP‑aware agent or graph node can call it without
custom glue each time. (`mcp` is already installed.)

### What we build (concretely)
- An **MCP server per system** (LOS, CAM, CIBIL, CERSAI), each exposing a few
  **read‑only** tools with a clear input/output schema.
- New graph nodes / rules that call them and **reconcile** document values against
  system values — e.g. "LOS sanctioned amount **==** sanction‑letter amount,"
  "CIBIL pull date within policy," "CERSAI charge registered." These convert the
  `SYSTEM` checklist lines from *pending* into real `AUTO_RECON` checks (the code is
  already wired so they "light up" when data arrives).

### Why it matters here
It completes the right‑hand side of the checklist and catches a whole class of
errors (data entered in LOS not matching the documents) that paper review misses.
Because connectors are MCP tools, the same `get_cibil` is reusable by a future agent
or a different app.

### Risks & safety
Live systems mean **auth and data sensitivity**: use read‑only service accounts,
never write back, log every system read into the same audit trail, and keep PII
handling tight. Start with one system (LOS) end‑to‑end before adding the rest.

### How to say it
"Some checks can't be done from the documents alone — they need the loan system.
We wrap each system once, in a standard way, as a set of read‑only lookups, and then
the tool can confirm that what's typed in the system matches what's in the
documents. The checklist lines that currently say 'pending system' start verifying
themselves."

---

## 6. Phase 5 — A team of specialists (multi‑agent)

### In one line
Split the reasoning into **role‑specialised agents** coordinated by a **supervisor**,
mirroring how a bank actually divides credit, legal and technical review.

### The concepts, taught
A **multi‑agent** system is several agents, each with its own focused prompt and
toolset, that collaborate. Common patterns:
- **Supervisor / worker.** A **supervisor** agent reads the case, **routes** parts to
  workers, and **compiles** their findings. (LangGraph has prebuilt supervisor
  helpers — `langgraph-prebuilt`.)
- **Handoff.** One agent passing control *and the relevant context* to another
  ("Legal agent, your turn, here's the title report").
- **Blackboard / shared state.** A common scratch space all agents read and write —
  in LangGraph this is just the graph **state** again.

### What we build (concretely)
- **Legal agent** — title status, encumbrances, vetting (sections B/I).
- **Valuation agent** — technical value, LTV, construction stage (section C, R4).
- **Reconciliation agent** — names, property, amounts across documents (R1–R5).
- **Policy agent** — ROI/fees vs the pricing grid (P1/P2).
- **Supervisor** — routes each section to its agent, then compiles everything into
  the one checklist the reviewer already sees.

Crucially, each agent still **proposes**; the **deterministic rules remain the
backstop / ground truth**, and the human signs off. The agents add *reasoning and
explanation*, not final authority.

### Why it matters here
Focused agents with narrow prompts and tools usually reason better than one
do‑everything prompt, and the structure mirrors the bank's real division of labour,
so it's easy to explain and extend (add a "Fraud agent," a "BT agent," etc.).

### Risks & safety
This is the **most moving parts** — hardest to debug, highest cost/latency. Do it
**only after** the eval harness (below) exists, so you can prove the multi‑agent
version is actually better, not just fancier. Keep the deterministic rules as the
safety net.

### How to say it
"We give each part of the review its own specialist — a legal one, a valuation one, a
policy one — with a coordinator that hands each specialist its section and then
assembles one checklist. It mirrors how your team already splits the work. The
specialists explain and reason; the rules and the human still decide."

---

## 7. Phase 6 — Memory and learning from reviewers

### In one line
Let the tool **remember** how cases and exceptions were handled, so it gets more
consistent and faster over time — while never auto‑learning a verdict.

### The concepts, taught
- **Short‑term memory** = this run's state (the clipboard from Phase 2).
- **Long‑term memory** = what persists across cases. Useful flavours:
  - **Episodic** — past events: "this builder's NOC always reads like X," "last time
    we saw this exception, the reviewer waived it with note Y." Your **audit trail
    (`review.db`) is already episodic memory.**
  - **Semantic** — durable facts/policies: the pricing grid, accepted title phrasings.
  - **Procedural** — how‑to: the prompts and rules themselves.
- **Feedback learning** = using reviewer actions to improve the system: when a
  reviewer repeatedly overrides a particular auto‑exception, that's a signal the
  rule or prompt needs tuning.

### What we build (concretely)
- **Precedent surfacing.** On an exception, show "3 similar past cases — reviewers
  waived 2, queried 1," drawn from the audit trail. The human decides; memory just
  *informs*.
- **Reviewer‑consistency aids.** Flag when this file is being handled differently
  from precedent.
- **A tuning loop.** Aggregate overrides → a report of "rules/prompts most often
  overridden" → fixes, each validated by the eval harness.

### Why it matters here
Consistency across reviewers and across time, and a system that visibly improves
with use — a strong story for the team.

### Risks & safety
**Never auto‑learn a verdict** (that would erase auditability and can entrench a past
mistake — a feedback‑loop bias). Memory **suggests**; humans decide. Keep precedents
explainable ("shown because survey number / builder matches").

### How to say it
"The tool keeps a memory of how past cases and exceptions were handled and surfaces
relevant precedents to the reviewer — 'here's how similar files went.' It also tells
us which automated checks reviewers keep overriding, so we can fix them. It never
teaches itself to approve loans; it just helps people be consistent."

---

## 8. Always‑on — how we prove it works (and keep it safe and cheap)

These are not a phase; they run **alongside every phase**. This section is the one
that most makes you sound credible, because it answers "how do you *know* it works?"

- **Evaluation harness (the most important).** Hand‑label a handful of real
  documents (the right answers), then a test runs extraction and reports **precision
  and recall per field** against the labels. **Precision** = of what it claimed, how
  much was right; **recall** = of what was there, how much it caught. Without this you
  cannot say whether a change helped — it is the prerequisite for adding any
  autonomy. (See [improvement.md](improvement.md) #17.)
- **LLM‑as‑judge.** Using a model to *grade* outputs against a rubric or labels (e.g.
  "did the extracted name match the document?"). Useful to scale evaluation, but it
  is itself fallible — calibrate it against human labels, never let it grade itself
  into production unchecked.
- **Observability / tracing.** Record every step, prompt, tool call and token so you
  can debug and audit a run. **LangSmith** (`langsmith`, already installed) does
  exactly this for LangChain/LangGraph — you see the whole graph execution.
- **Guardrails.** Validate model output against a strict schema (we already do, via
  the JSON field shape); force citations; refuse/flag on uncertainty rather than
  guess; handle PII carefully (Aadhaar/PAN masking).
- **Cost & latency control.** A **model ladder** (cheap model for classification, a
  stronger one only for hard fields), aggressive caching (we cache by file hash),
  batching embeddings, and capping agent tool‑calls.

### How to say it
"For every step of this roadmap we keep a labelled test set so we can measure whether
a change makes extraction more accurate, full tracing so we can audit any run, strict
output checks so it can't return garbage, and a cost ladder so we use the expensive
model only where it's needed. That's how we add capability responsibly instead of
hoping."

---

## 9. Sequencing and a one‑line pitch for each phase

A sensible order (each step de‑risks the next):

1. **Eval harness first** (always‑on) — you can't improve what you can't measure.
2. **Phase 2** — graph orchestration + Document AI: resilience and the big accuracy win.
3. **Phase 3** — read‑only research assistant: high value, low risk.
4. **Phase 4** — MCP system connectors: lights up the pending checks.
5. **Phase 6** — memory/precedents: consistency, using data you already store.
6. **Phase 5** — multi‑agent: last, because it's the most complex and only worth it
   once you can prove it beats the simpler version.

One‑liners for a slide:
- *Phase 2:* "Make it resilient and read documents properly."
- *Phase 3:* "A research assistant that finds evidence, with citations."
- *Phase 4:* "Connect the loan systems so more checks are automatic."
- *Phase 5:* "A team of specialist reviewers, coordinated."
- *Phase 6:* "It remembers how past cases went and stays consistent."
- *Always‑on:* "We measure accuracy, trace every run, and control cost."

---

## 10. Tough questions you'll be asked (and confident answers)

- **"Will the AI approve loans?"** No. The AI reads documents and reasons about
  evidence; **deterministic rules and a human make every decision.** Even in the
  agentic phases, the agents propose and explain — they don't sign off.
- **"What if it hallucinates / misreads a scan?"** Three guardrails: every value is
  **grounded** in a page + quote, anything uncertain is marked low‑confidence and
  **forced to a human**, and the **eval harness** measures the real error rate. Phase 2
  adds Document AI and a self‑check on top.
- **"Why not go fully autonomous now?"** Because a disbursement gate rewards
  **auditability and predictability**. We add autonomy only where a human still signs
  off and only after we can measure it. That's a feature, not timidity.
- **"Is our data safe?"** Documents stay in your project; system access is read‑only
  with audited service accounts; PII (Aadhaar/PAN) is masked; every action is logged
  in an append‑only trail.
- **"Why these frameworks?"** LangGraph because our process is naturally a graph and
  it gives resilience + human‑in‑the‑loop for free; Document AI because reading the
  scan correctly is the #1 accuracy lever; MCP because it makes system connectors
  reusable; multi‑agent only when measurement proves it's worth the complexity.
- **"What does it cost?"** Mostly per‑page model calls, controlled by caching, a
  cheap‑then‑expensive model ladder, and doing OCR once. We can put real numbers on it
  with the eval/observability tooling before any rollout.

---

## 11. A pocket glossary (say these with confidence)

- **Workflow** — a fixed sequence of steps (Phase 1).
- **Agent** — an LLM in a loop: reason → use a tool → observe → repeat.
- **Tool / function calling** — a function the model may ask us to run.
- **State** — the shared "clipboard" carried through a run.
- **Node / edge / graph** — a step / a transition / the flowchart the code runs.
- **Checkpoint** — a saved state you can resume from.
- **Human‑in‑the‑loop** — a required human approval step (maker‑checker).
- **Reflection / self‑consistency** — the system double‑checks its own output.
- **RAG** — retrieve relevant text first, then answer grounded in it.
- **Grounding** — tying every answer to source evidence (page + quote).
- **MCP** — a standard way to expose tools/data to any agent.
- **Multi‑agent / supervisor** — several specialist agents with a coordinator.
- **Memory (episodic/semantic)** — remembered past events / durable facts.
- **Eval harness** — a labelled test set that measures accuracy.
- **LLM‑as‑judge** — using a model to grade outputs (calibrated against humans).
- **Observability / tracing** — recording every step to debug and audit (LangSmith).
- **Guardrails** — schema checks, citations, refuse‑on‑uncertainty, PII handling.

> Keep returning to the anchor: **read and reason with AI; decide with rules and a
> human; measure everything.** If you can say that and explain why each phase keeps
> it true, you will sound exactly as informed as you are.
