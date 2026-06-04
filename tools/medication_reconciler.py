# tools/medication_reconciler.py

import re
from agent.state import AgentState, MedicationEntry, FlagType, FlagSeverity
from tools.escalator import escalate_medication_change, escalate
from rich.console import Console
from rich.table import Table

console = Console()

# Prefixes to strip when normalizing medication names for comparison
MED_PREFIXES = [
    "inj.", "inj ", "inj-",
    "tab.", "tab ", "tab-",
    "cap.", "cap ", "cap-",
    "syp.", "syp ", "syrup ",
    "drops ", "drop ",
    "inf.", "inf ",
    "ointment ", "cream ",
    "injection ", "tablet ", "capsule ",
]

# Keywords that suggest a medication is being continued from before admission
CONTINUATION_KEYWORDS = [
    "continue", "continuing", "on regular", "regular medication",
    "home medication", "prior medication", "pre-admission",
    "ayurvedic medication", "on treatment"
]


def _normalize_med_name(name: str) -> str:
    """
    Normalize a medication name for comparison.
    Strips prefixes, dose info, and extra whitespace.
    Returns lowercase clean name.
    """
    name = name.lower().strip()

    # Remove common prefixes
    for prefix in MED_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):].strip()

    # Remove dose and frequency info
    # Pattern: numbers followed by mg/ml/iu/u/mcg/g
    name = re.sub(r'\d+(\.\d+)?\s*(mg|ml|mcg|iu|iu/ml|u|gm|g|%)\b', '', name)

    # Remove frequency patterns like 1-0-1, BD, TDS, OD, SOS
    name = re.sub(r'\b\d-\d-\d\b', '', name)
    name = re.sub(
        r'\b(bd|tds|od|sos|qid|tid|stat|prn|hs|ac|pc|once|twice|thrice)\b',
        '', name
    )

    # Remove route info
    name = re.sub(r'\b(iv|sc|im|po|sl|pr|topical|s/c|i/v)\b', '', name)

    # Remove duration patterns like "x 5 days", "for 7 days"
    name = re.sub(r'(x|for)\s*\d+\s*(day|days|week|weeks)', '', name)

    # Clean up remaining whitespace and punctuation
    name = re.sub(r'[,;/]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()

    return name


def _parse_medications_string(
    raw_string: str,
    source_doc: str,
    page_number: int
) -> list[MedicationEntry]:
    """
    Parse a raw medication string (as returned by section_extractor)
    into a list of MedicationEntry objects.

    Handles both newline-separated and semicolon-separated formats.
    """
    if not raw_string or not raw_string.strip():
        return []

    entries = []

    # Split on newlines first, then semicolons
    lines = []
    for line in raw_string.split('\n'):
        parts = line.split(';')
        lines.extend(parts)

    for line in lines:
        line = line.strip()

        # Skip empty lines and header-like lines
        if not line:
            continue
        if len(line) < 3:
            continue
        # Skip lines that are just numbers (table row numbers)
        if re.match(r'^\d+\.?\s*$', line):
            continue

        # Try to extract dose and frequency from the line
        # Pattern: medication name [dose] [frequency] [duration]
        dose_match = re.search(
            r'(\d+(\.\d+)?\s*(mg|ml|mcg|iu|u|gm|g|%))',
            line, re.IGNORECASE
        )
        freq_match = re.search(
            r'\b(\d-\d-\d|bd|tds|od|sos|qid|stat|once daily|twice daily)\b',
            line, re.IGNORECASE
        )

        dose = dose_match.group(0) if dose_match else None
        frequency = freq_match.group(0) if freq_match else None

        # The medication name is whatever is left after removing dose/freq
        name = line
        if dose_match:
            name = name.replace(dose_match.group(0), '').strip()
        if freq_match:
            name = name.replace(freq_match.group(0), '').strip()

        # Clean up the name
        name = re.sub(r'^\d+[\.\)]\s*', '', name)  # Remove leading numbers
        name = name.strip(' ,;-')

        if not name or len(name) < 2:
            continue

        entries.append(MedicationEntry(
            name=name,
            dose=dose,
            frequency=frequency,
            source_doc=source_doc,
            page_number=page_number,
            notes=None
        ))

    return entries


def _meds_are_same(name_a: str, name_b: str) -> bool:
    """
    Check if two medication names refer to the same drug.
    Uses normalized comparison.
    """
    norm_a = _normalize_med_name(name_a)
    norm_b = _normalize_med_name(name_b)

    if not norm_a or not norm_b:
        return False

    # Exact match after normalization
    if norm_a == norm_b:
        return True

    # One contains the other (handles brand vs generic)
    if norm_a in norm_b or norm_b in norm_a:
        return True

    # Check first significant word match
    # e.g. "meropenem" matches "meropenem 1g"
    words_a = [w for w in norm_a.split() if len(w) > 3]
    words_b = [w for w in norm_b.split() if len(w) > 3]

    if words_a and words_b:
        if words_a[0] == words_b[0]:
            return True

    return False


def run_medication_reconciliation(state: AgentState) -> dict:
    """
    Main reconciliation function.

    1. Parses raw medication strings from state.facts into
       MedicationEntry objects stored in state.medications_admission
       and state.medications_discharge.

    2. Diffs the two lists.

    3. Flags all changes for clinician review.

    Returns a structured reconciliation result.
    """
    console.print("\n")
    console.print(
        "[bold cyan]Running medication reconciliation...[/bold cyan]"
    )

    # --- Step 1: Parse admission medications ---
    admission_facts = state.facts.get("admission_medications", [])
    admission_meds = []

    if admission_facts:
        primary = state.get_primary_fact("admission_medications")
        if primary:
            parsed = _parse_medications_string(
                raw_string=primary.value,
                source_doc=primary.source_doc,
                page_number=primary.page_number
            )
            admission_meds.extend(parsed)
            state.medications_admission = admission_meds
            console.print(
                f"[dim]Parsed {len(admission_meds)} admission medication(s)[/dim]"
            )
    else:
        console.print(
            "[yellow]No admission medications found in extracted facts.[/yellow]"
        )

    # --- Step 2: Parse discharge medications ---
    discharge_facts = state.facts.get("discharge_medications", [])
    discharge_meds = []

    if discharge_facts:
        # Collect from ALL sources — discharge meds may be across
        # multiple documents (drug chart + discharge summary)
        all_discharge_text_parts = []
        for fact in discharge_facts:
            all_discharge_text_parts.append(fact.value)
            source_doc = fact.source_doc
            page_num = fact.page_number

        combined_discharge_text = "\n".join(all_discharge_text_parts)
        parsed = _parse_medications_string(
            raw_string=combined_discharge_text,
            source_doc=source_doc,
            page_number=page_num
        )
        discharge_meds.extend(parsed)
        state.medications_discharge = discharge_meds
        console.print(
            f"[dim]Parsed {len(discharge_meds)} discharge medication(s)[/dim]"
        )
    else:
        console.print(
            "[yellow]No discharge medications found in extracted facts.[/yellow]"
        )
        escalate(
            state=state,
            flag_type=FlagType.MISSING,
            field="discharge_medications",
            description=(
                "Discharge medications could not be extracted from "
                "any source document. Clinician must supply complete "
                "discharge medication list."
            ),
            severity=FlagSeverity.HIGH,
            sources=[]
        )
        return {
            "success": False,
            "message": "Discharge medications not found — cannot reconcile",
            "admission_count": len(admission_meds),
            "discharge_count": 0,
            "added": [],
            "stopped": [],
            "continued": [],
            "changed": []
        }

    # --- Step 3: Diff the two lists ---
    added = []       # In discharge but not admission
    stopped = []     # In admission but not discharge
    continued = []   # In both, unchanged
    changed = []     # In both, but dose/frequency different

    discharge_matched = set()  # Track which discharge meds have been matched

    for adm_med in admission_meds:
        matched = False
        for i, dis_med in enumerate(discharge_meds):
            if i in discharge_matched:
                continue
            if _meds_are_same(adm_med.name, dis_med.name):
                matched = True
                discharge_matched.add(i)

                # Check if dose or frequency changed
                dose_changed = (
                    adm_med.dose and dis_med.dose and
                    _normalize_med_name(adm_med.dose) !=
                    _normalize_med_name(dis_med.dose)
                )
                freq_changed = (
                    adm_med.frequency and dis_med.frequency and
                    adm_med.frequency.lower().strip() !=
                    dis_med.frequency.lower().strip()
                )

                if dose_changed or freq_changed:
                    changed.append({
                        "medication": adm_med.name,
                        "admission": adm_med,
                        "discharge": dis_med,
                        "change_type": "dose_changed" if dose_changed else "frequency_changed"
                    })
                else:
                    continued.append(adm_med.name)
                break

        if not matched:
            stopped.append(adm_med)

    # Discharge meds not matched to any admission med = newly added
    for i, dis_med in enumerate(discharge_meds):
        if i not in discharge_matched:
            added.append(dis_med)

    # --- Step 4: Flag all changes ---
    for med in added:
        escalate_medication_change(
            state=state,
            medication_name=med.name,
            change_type="added at discharge",
            details=(
                f"Dose: {med.dose or 'not documented'}, "
                f"Frequency: {med.frequency or 'not documented'}"
            ),
            source_doc=med.source_doc
        )

    for med in stopped:
        escalate_medication_change(
            state=state,
            medication_name=med.name,
            change_type="stopped at discharge",
            details=(
                f"Was: Dose {med.dose or 'unknown'}, "
                f"Frequency {med.frequency or 'unknown'}"
            ),
            source_doc=med.source_doc
        )

    for change in changed:
        adm = change["admission"]
        dis = change["discharge"]
        escalate_medication_change(
            state=state,
            medication_name=change["medication"],
            change_type=change["change_type"],
            details=(
                f"Admission: {adm.dose or '?'} {adm.frequency or '?'} → "
                f"Discharge: {dis.dose or '?'} {dis.frequency or '?'}"
            ),
            source_doc=dis.source_doc
        )

    # --- Display reconciliation table ---
    table = Table(
        title="Medication Reconciliation",
        show_header=True,
        header_style="bold magenta"
    )
    table.add_column("Medication", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Adm. Dose/Freq", style="white")
    table.add_column("Dis. Dose/Freq", style="white")

    for name in continued:
        table.add_row(name, "[green]CONTINUED[/green]", "-", "-")

    for med in added:
        table.add_row(
            med.name,
            "[yellow]ADDED ⚠️[/yellow]",
            "—",
            f"{med.dose or '?'} {med.frequency or '?'}"
        )

    for med in stopped:
        table.add_row(
            med.name,
            "[red]STOPPED ⚠️[/red]",
            f"{med.dose or '?'} {med.frequency or '?'}",
            "—"
        )

    for change in changed:
        adm = change["admission"]
        dis = change["discharge"]
        table.add_row(
            change["medication"],
            "[orange3]CHANGED ⚠️[/orange3]",
            f"{adm.dose or '?'} {adm.frequency or '?'}",
            f"{dis.dose or '?'} {dis.frequency or '?'}"
        )

    console.print(table)

    summary_msg = (
        f"Reconciliation complete: "
        f"{len(continued)} continued, "
        f"{len(added)} added, "
        f"{len(stopped)} stopped, "
        f"{len(changed)} changed."
    )
    console.print(f"[dim]{summary_msg}[/dim]")

    return {
        "success": True,
        "message": summary_msg,
        "admission_count": len(admission_meds),
        "discharge_count": len(discharge_meds),
        "added": [m.name for m in added],
        "stopped": [m.name for m in stopped],
        "continued": continued,
        "changed": [c["medication"] for c in changed]
    }