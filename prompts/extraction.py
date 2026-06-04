# prompts/extraction.py

# Base instruction appended to every extraction prompt.
# This is the no-fabrication guardrail at the prompt level.
NO_FABRICATION_INSTRUCTION = """
CRITICAL RULES — YOU MUST FOLLOW THESE WITHOUT EXCEPTION:
1. Only extract information that is explicitly present in the provided text.
2. Do NOT guess, infer, assume, or use medical knowledge to fill gaps.
3. Do NOT paraphrase in a way that changes clinical meaning.
4. If the information is not present, return "found": false.
5. If the text says the result is pending, awaited, or not yet available,
   return "found": false and "pending": true.
6. The "raw_context" field must contain the EXACT phrase or sentence
   from the text that supports your extraction — copy it verbatim.
7. Return ONLY valid JSON. No preamble, no explanation, no markdown fences.
"""

JSON_SHAPE = """
Return exactly this JSON shape and nothing else:
{
  "found": true or false,
  "value": "extracted value or null if not found",
  "confidence": "high, medium, low, or uncertain",
  "raw_context": "exact phrase copied from the text, or null if not found",
  "pending": true or false,
  "notes": "any important caveats about this extraction, or null"
}
"""


def get_extraction_prompt(field: str, section_text: str) -> str:
    """
    Returns the complete prompt for extracting a specific field
    from a document section's text.
    """
    field_instructions = {

        "patient_demographics": f"""
You are extracting patient demographic information from a clinical document.

Look for: patient name, age, gender, weight, blood group, MRN/IP number,
date of birth. These may appear on admission forms, nursing assessment forms,
or ER charts.

Document text:
---
{section_text[:3000]}
---
{NO_FABRICATION_INSTRUCTION}

Extract all demographic details you find as a single combined string.
For example: "Male patient, weight 71kg, admitted 26/02/2026"
If only partial demographics are found, extract what is there.
{JSON_SHAPE}
""",

        "admission_date": f"""
You are extracting the hospital admission date from a clinical document.

Look for: date of admission, date patient arrived, ER arrival date,
date written at top of admission forms. Format: DD/MM/YYYY or similar.

Document text:
---
{section_text[:3000]}
---
{NO_FABRICATION_INSTRUCTION}

Return the admission date exactly as written in the document.
{JSON_SHAPE}
""",

        "discharge_date": f"""
You are extracting the hospital discharge date from a clinical document.

Look for: date of discharge, discharge date, date patient was sent home,
final date on monitoring charts, date on discharge checklist.

Document text:
---
{section_text[:3000]}
---
{NO_FABRICATION_INSTRUCTION}

Return the discharge date exactly as written in the document.
{JSON_SHAPE}
""",

        "principal_diagnosis": f"""
You are extracting the principal (primary/main) diagnosis from a clinical document.

Look for: "Diagnosis:", "Principal Diagnosis", "Final Diagnosis",
"Provisional Diagnosis", diagnosis written on admission record,
ER chart diagnosis field, ICU chart diagnosis field.

Document text:
---
{section_text[:3000]}
---
{NO_FABRICATION_INSTRUCTION}

IMPORTANT: If you find multiple different diagnoses in this document,
extract ALL of them as a semicolon-separated list and set confidence
to "uncertain" — do not choose one over another.
{JSON_SHAPE}
""",

        "secondary_diagnoses": f"""
You are extracting secondary (additional/comorbid) diagnoses from a clinical document.

Look for: secondary diagnosis, comorbidities, additional conditions,
past history conditions, known conditions, background diagnoses.
Examples: Type 2 Diabetes Mellitus, Hypertension, Hypothyroidism.

Document text:
---
{section_text[:3000]}
---
{NO_FABRICATION_INSTRUCTION}

Return all secondary diagnoses as a semicolon-separated list.
Do not include the principal diagnosis in this field.
{JSON_SHAPE}
""",

        "hospital_course": f"""
You are extracting the hospital course narrative from a clinical document.

Look for: "Course in the Hospital", "Hospital Course", progress notes,
ICU care plans, nursing documentation entries that describe the
patient's clinical progress, treatment given, and response to treatment.

Document text:
---
{section_text[:4000]}
---
{NO_FABRICATION_INSTRUCTION}

Summarize the hospital course using ONLY information stated in the text.
Include: presenting complaint, key investigations done, treatments given,
clinical events during stay, and clinical trajectory.
Do not add any medical interpretation not present in the text.
{JSON_SHAPE}
""",

        "procedures_performed": f"""
You are extracting procedures performed on the patient during their hospital stay.

Look for: procedure charts, "IV Cannulization", "Foley's Catheterisation",
"USG", "CT scan", "ECG", "ECHO", "Blood C/S", "Urine C/S",
"Bone marrow", any intervention documented.

Document text:
---
{section_text[:3000]}
---
{NO_FABRICATION_INSTRUCTION}

Return all procedures as a semicolon-separated list.
Include the date if mentioned in the text.
{JSON_SHAPE}
""",

        "discharge_condition": f"""
You are extracting the patient's condition at the time of discharge.

Look for: "Condition at Discharge", "Discharge Condition",
"hemodynamically stable", "stable", "improved", clinical status
on the last nursing note or consultation sheet before discharge.

Document text:
---
{section_text[:3000]}
---
{NO_FABRICATION_INSTRUCTION}

Return the discharge condition exactly as documented.
{JSON_SHAPE}
""",

        "allergies": f"""
You are extracting known drug allergies from a clinical document.

Look for: "Known Drug Allergies", "Allergic History", "Allergies",
"Drug Allergy", fields on nursing assessment or drug charts.
Common values: "Not Known", "NKDA", "No known drug allergies",
or specific allergen names.

Document text:
---
{section_text[:2000]}
---
{NO_FABRICATION_INSTRUCTION}

If the document states "Not Known" or "NKDA", return that exactly —
it is a valid clinical finding, not a missing value.
{JSON_SHAPE}
""",

        "follow_up_instructions": f"""
You are extracting follow-up instructions given to the patient at discharge.

Look for: "Follow-up Instructions", "Follow-up", "Review on",
"OPD follow-up", "Review immediately if", advice given at discharge,
any instructions about when to return or what to watch for.

Document text:
---
{section_text[:3000]}
---
{NO_FABRICATION_INSTRUCTION}

Return all follow-up instructions as written in the document.
Include specific dates, conditions for immediate review, and
any specialist follow-up mentioned.
{JSON_SHAPE}
""",

        "pending_results": f"""
You are identifying investigation results that were pending or awaited
at the time of discharge.

Look for: "report awaited", "pending", "sent to lab", "result awaited",
"culture sensitivity sent", "awaited", any lab or investigation
documented as sent but result not yet received.

Document text:
---
{section_text[:3000]}
---
{NO_FABRICATION_INSTRUCTION}

Return all pending items as a semicolon-separated list.
If nothing is documented as pending, return found: false.
{JSON_SHAPE}
""",

        "admission_medications": f"""
You are extracting medications the patient was on at the time of admission.

Look for: medications listed in admission/case records, past drug history,
"patient on regular medication", medications mentioned in the history
of present illness as ongoing treatment before this admission.

Document text:
---
{section_text[:3000]}
---
{NO_FABRICATION_INSTRUCTION}

Return all admission medications as a semicolon-separated list.
Include dose and frequency if documented.
{JSON_SHAPE}
""",

        "discharge_medications": f"""
You are extracting the complete list of medications prescribed at discharge.

Look for: "Advice on Discharge", "Discharge Medications",
drug charts with discharge dates, medications listed in the
final consultation sheet or discharge summary section.

Document text:
---
{section_text[:4000]}
---
{NO_FABRICATION_INSTRUCTION}

Return each medication on a new line with: name, dose, frequency, duration.
If dose or frequency is not documented for a medication, note it as
"dose not documented" rather than omitting it.
{JSON_SHAPE}
""",
    }

    # Return the field-specific prompt or a generic one if field unknown
    if field in field_instructions:
        return field_instructions[field]

    # Generic fallback for any field not in the list above
    return f"""
You are extracting "{field}" from a clinical document.

Document text:
---
{section_text[:3000]}
---
{NO_FABRICATION_INSTRUCTION}

Extract the value for "{field}" from the text above.
{JSON_SHAPE}
"""