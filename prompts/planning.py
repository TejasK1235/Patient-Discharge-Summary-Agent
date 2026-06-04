# prompts/planning.py

AVAILABLE_ACTIONS = """
AVAILABLE ACTIONS — you must choose exactly one:

1. extract_field
   Use when: a required field has not been extracted yet AND there is
   an unprocessed document section that likely contains it.
   Required params: "field" (field name), "section" (document section name)

2. run_drug_check
   Use when: discharge medications have been extracted and the drug
   interaction check has not yet been run.
   Required params: none

3. mark_field_missing
   Use when: a required field cannot be found in ANY of the available
   document sections, and all relevant sections have been processed.
   Required params: "field" (field name)

4. mark_complete
   Use when: all required fields are either extracted or marked missing,
   all unprocessed sections have been processed, and the drug check
   has been run (or attempted).
   Required params: none
"""

PLANNING_RULES = """
RULES FOR PLANNING:
1. Prioritize fields in this order:
   - principal_diagnosis (highest priority — most critical)
   - admission_date, discharge_date
   - patient_demographics
   - hospital_course
   - discharge_medications, admission_medications
   - procedures_performed
   - allergies
   - discharge_condition
   - follow_up_instructions
   - pending_results
   - secondary_diagnoses (lowest priority)

2. Process unprocessed document sections before marking fields missing.
   A field is only truly missing if ALL relevant sections have been checked.

3. Run the drug check AFTER discharge_medications is extracted,
   not before.

4. Do not re-extract a field from the same section twice.
   If a section has already been processed for a field, move on.

5. If steps remaining is below 5, prioritize completing the most
   critical unextracted fields and then mark_complete.

6. You cannot add new actions. You cannot call tools not in the list above.

7. Return ONLY valid JSON. No explanation. No markdown fences.
"""

JSON_ACTION_SHAPE = """
Return exactly this JSON shape:
{
  "reasoning": "2-3 sentences explaining what you know, what is missing, and why you chose this action",
  "action": "extract_field | run_drug_check | mark_field_missing | mark_complete",
  "params": {
    "field": "field_name_if_applicable_or_null",
    "section": "document_section_name_if_applicable_or_null"
  },
  "next_decision_preview": "one sentence on what you expect to do after this action"
}
"""


def get_planning_prompt(state_summary: str, steps_taken: int, max_steps: int) -> str:
    """
    Returns the complete prompt for the planner LLM call.
    Called every iteration of the agent loop.
    """
    steps_remaining = max_steps - steps_taken
    urgency = ""
    if steps_remaining <= 5:
        urgency = (
            f"\n⚠️  ONLY {steps_remaining} STEPS REMAINING. "
            f"Prioritize critical fields. Move toward mark_complete.\n"
        )

    return f"""
You are the planning agent for a clinical discharge summary system.
Your job is to decide the next action to take based on the current state.

You are working toward producing a complete, safe discharge summary
for a patient. You must never fabricate clinical information.
Every field must come from the source documents or be marked missing.

CURRENT STATE:
---
{state_summary}
---

Steps taken: {steps_taken} / {max_steps}
{urgency}

{AVAILABLE_ACTIONS}

{PLANNING_RULES}

{JSON_ACTION_SHAPE}
"""


def get_conflict_resolution_prompt(
    field: str,
    conflicting_values: list[dict]
) -> str:
    """
    Prompt used when the conflict detector finds disagreement.
    The agent does NOT resolve conflicts — this prompt is used
    to generate a clear human-readable description of the conflict
    for the clinical flag.
    """
    values_text = "\n".join([
        f"  - Value: '{v['value']}' | Source: {v['source']} | Page: {v['page']}"
        for v in conflicting_values
    ])

    return f"""
You are analyzing conflicting clinical information found across multiple
source documents for the field: "{field}"

Conflicting values found:
{values_text}

Your job is NOT to resolve this conflict.
Your job is to write a clear, concise description of the conflict
that a clinician can quickly understand and act on.

Rules:
1. Do not pick a winner or suggest which value is correct.
2. Do not use medical judgment to resolve the conflict.
3. Be factual and specific about which document says what.
4. Keep it under 3 sentences.
5. Return ONLY the description string. No JSON. No preamble.

Write the conflict description now:
"""