# Patient Discharge Summary Agent

An agentic AI system that reads raw clinical source documents and produces a structured, 
clinically safe discharge summary draft for clinician review.

Built for the Dscribe (Unriddle Technologies) AI Engineer take-home assignment.

---

## What It Does

Given a patient's hospital records as a PDF (admission notes, ER charts, nursing notes, 
lab reports, drug charts, ICU charts, consultation sheets), the agent:

1. Renders every PDF page to an image using PyMuPDF — handling scanned, image-based, 
   and handwritten documents that text extraction tools cannot read
2. Uses a vision LLM (Llama 4 Scout via Groq) to read page images and extract clinical fields
3. Detects conflicts when two documents disagree on the same field
4. Reconciles admission vs discharge medications and flags every change
5. Checks discharge medications for known drug interactions
6. Produces a structured discharge summary draft with full source citations
7. Logs every agent decision to a readable trace file

The system never fabricates clinical information. Every extracted fact carries its source 
document and page number. Missing or conflicting data is explicitly flagged for clinician 
review — never silently filled in.

---

## Agent Loop Design

The system uses a custom agent loop built from scratch — no LangChain, no LangGraph. 
Every architectural decision is explicit and auditable, which matters in a clinical setting.

while steps < MAX_STEPS and not complete:
action = planner.decide(state)      # LLM decides next action
result = execute(action)            # call the right tool
state.update(result)                # store what we learned
trace.log(action, result)           # write the trace

The planner returns one of four actions: `extract_field`, `run_drug_check`, 
`mark_field_missing`, or `mark_complete`. It cannot invent actions. The loop 
enforces a hard step cap of 40 iterations — the agent cannot run forever.

The planner tracks how many sections have been attempted per field. After 5 failed 
attempts on any field, that field is automatically marked missing rather than burning 
all remaining steps on one field. This rotation logic ensures the agent makes progress 
across all required fields rather than obsessing over one.

---

## No-Fabrication Guardrail

This is the core safety requirement and it is enforced at three levels:

**Level 1 — Prompt level:** Every extraction prompt contains explicit instructions:
"If you cannot find this information in the provided text, return found: false. 
Do NOT guess, infer, assume, or use medical knowledge to fill gaps."

**Level 2 — Data model level:** Every extracted value is stored as a `SourcedFact` 
object requiring `value`, `source_doc`, `page_number`, `confidence`, and `raw_context`. 
There is no code path that produces a clinical value without a source citation.

**Level 3 — Output level:** The formatter calls `memory.get_best_value_for_output()` 
for every field. This method returns either a sourced value with citation, a conflict 
block showing all disagreeing values, or `[MISSING — CLINICIAN REVIEW REQUIRED]`. 
A bare unsourced value cannot appear in the output.

The output document always carries a prominent draft banner and is explicitly marked 
as requiring clinician review before any clinical use.

---

## Handling Failures and Conflicts

**PDF extraction failures:** PyMuPDF renders pages as images regardless of whether 
content is typed, printed, scanned, or handwritten. Rendering failures are caught 
per-page, logged, and the agent continues with remaining pages.

**LLM extraction failures:** The section extractor retries up to 2 times with 
exponential backoff. On rate limit errors it waits 60 seconds before retrying. 
If all retries fail, the field is not marked missing immediately — the planner 
will attempt other sections before giving up.

**Conflict detection:** Conflicts are caught at two points. First, the section 
extractor checks each new value against existing values for the same field as it 
extracts. Second, a post-processing conflict sweep runs after all extractions 
complete, doing an N-way comparison across all sourced facts. Conflicting fields 
are never resolved by the agent — both values are preserved with their sources 
and the conflict is escalated for clinician review.

**Tool failures:** The drug checker simulates realistic API reliability including 
timeouts and service unavailability. If the drug check cannot be completed, this 
is escalated as a HIGH severity flag requiring manual pharmacist review. The agent 
never behaves as if a failed tool call succeeded.

---

## Technical Stack

- **PDF rendering:** PyMuPDF (fitz) — handles image-based and handwritten PDFs
- **Vision extraction:** Llama 4 Scout (`meta-llama/llama-4-scout-17b-16e-instruct`) via Groq API
- **Planning:** Llama 3.1 8B Instant (`llama-3.1-8b-instant`) via Groq API
- **Data validation:** Pydantic v2 — all state objects are typed and validated
- **Terminal output:** Rich — colored panels, tables, progress bars
- **No frameworks:** Custom agent loop, no LangChain or LangGraph

---

## Project Structure
```
patient_agent/
├── main.py                        # Entry point
├── requirements.txt
│
├── data/
│   └── patient_2.pdf              # Patient source documents
│
├── outputs/
│   ├── discharge_summary.md       # Generated discharge summary draft
│   └── agent_trace.md             # Step-by-step agent trace
│
├── agent/
│   ├── state.py                   # AgentState, SourcedFact, ClinicalFlag dataclasses
│   ├── memory.py                  # Query layer over state — what do we know/need?
│   ├── planner.py                 # LLM-based planner with rule-based fallback
│   └── loop.py                    # Main agent loop with step cap and rotation
│
├── tools/
│   ├── pdf_extractor.py           # PyMuPDF page rendering to base64 images
│   ├── section_extractor.py       # Vision LLM extraction with retry logic
│   ├── conflict_detector.py       # Post-processing N-way conflict sweep
│   ├── medication_reconciler.py   # Admission vs discharge medication diff
│   ├── drug_checker.py            # Mocked drug interaction checker
│   └── escalator.py               # Clinical flag creation and surfacing
│
├── prompts/
│   ├── extraction.py              # Per-field extraction prompts with no-fabrication rules
│   └── planning.py                # Planner prompt with action set and priority rules
│
└── formatter/
└── summary_formatter.py       # State → structured markdown discharge summary + trace
```
---

## How to Run

**Install dependencies:**
```bash
pip install groq pymupdf Pillow rich python-dotenv pydantic
```

**Set your Groq API key** (system environment variable):
```bash
# Windows
set GROQ_API_KEY=your_key_here

# Mac/Linux
export GROQ_API_KEY=your_key_here
```

**Place the patient PDF** at `data/patient_2.pdf`

**Run:**
```bash
python main.py
```

Outputs are written to `outputs/discharge_summary.md` and `outputs/agent_trace.md`.

---

## Output Format

The discharge summary contains:
- Patient demographics, admission and discharge dates
- Principal and secondary diagnoses with source citations
- Hospital course narrative
- Procedures performed
- Admission medications and discharge medications
- Medication reconciliation — every change flagged
- Pending results
- Follow-up instructions
- Discharge condition
- Clinical flags section at the top grouping all conflicts, missing fields, 
  medication changes, and drug interactions by severity

Every field either shows its value with `[Source: document, page]` or shows 
`[MISSING — CLINICIAN REVIEW REQUIRED]`. Conflicted fields show all values 
with their sources and a prominent conflict warning.

---

## Part 2 Status

Part 2 (learning from doctor edits) was not implemented within the time window.

The approach I would take: build a simulated reviewer as a second LLM call that 
applies a consistent hidden editing policy to agent drafts, producing (draft, edited) 
pairs. Use normalized edit distance between draft and corrected version as the reward 
signal — lower edit distance means fewer corrections needed. Accumulate corrections 
across runs and inject them as structured correction memory into future extraction 
prompts for sections where the agent consistently makes errors. 

The main risk is Goodhart's Law — optimizing edit distance can be gamed by producing 
vaguer output that requires fewer specific corrections while being less clinically 
useful. Mitigation: evaluate on held-out clinical accuracy metrics, not just edit 
distance, and ensure the safety guarantees from Part 1 are treated as hard constraints 
that the learning loop cannot override.

---

## Known Limitations

**Rate limits:** The Groq free tier limits `meta-llama/llama-4-scout-17b-16e-instruct` 
to 500,000 tokens per day. Each page image costs roughly 8,000-12,000 tokens. 
A 71-page PDF can exhaust the daily quota in a single run. In production this would 
require a paid tier or image caching between runs.

**Handwritten text quality:** Vision models read handwritten clinical notes less 
reliably than typed text. Confidence is lower on nursing notes and handwritten 
consultation sheets than on printed lab reports. The system handles this by flagging 
low-confidence extractions for clinician review rather than accepting them silently.

**Single patient:** The system is currently configured for one patient PDF. 
Extending to multiple patients requires adding a patient ID parameter to main.py 
and separating output directories per patient — straightforward to implement.

**No persistent state:** Each run starts fresh. There is no caching of rendered 
page images or previously extracted facts between runs. Adding a simple JSON cache 
keyed by PDF hash would eliminate the 2-minute rendering time on repeat runs.

---

## What I Would Do With More Time

- Implement persistent image caching so repeat runs don't re-render the PDF
- Add smarter section targeting — use the vision model to classify each page 
  before extraction so the planner knows which pages likely contain which document types
- Implement Part 2 learning loop with simulated reviewer
- Add confidence thresholds per field — require higher confidence for high-stakes 
  fields like principal diagnosis and discharge medications
- Add a structured JSON output option alongside the markdown for downstream system integration
- Support multiple patients with per-patient output directories

