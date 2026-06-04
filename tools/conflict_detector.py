# tools/conflict_detector.py

from agent.state import (
    AgentState, ClinicalFlag, FlagType, FlagSeverity, SourcedFact
)
from tools.escalator import escalate_conflict
from rich.console import Console
from rich.table import Table

console = Console()

# Fields where having multiple different values is expected and important
# These should ALWAYS be flagged if values differ across documents
HIGH_STAKES_FIELDS = {
    "principal_diagnosis",
    "discharge_medications",
    "allergies",
    "discharge_date",
    "admission_date",
}

# Known medical abbreviation equivalents
# If one value is an abbreviation of the other, not a conflict
KNOWN_EQUIVALENTS = [
    {"dka", "diabetic ketoacidosis"},
    {"t2dm", "type 2 diabetes mellitus", "type 2 dm", "t2 dm"},
    {"uti", "urinary tract infection"},
    {"afi", "acute febrile illness"},
    {"nkda", "no known drug allergies", "not known", "nil known"},
    {"bp", "blood pressure"},
    {"urti", "upper respiratory tract infection"},
    {"lrti", "lower respiratory tract infection"},
    {"ckd", "chronic kidney disease"},
    {"aki", "acute kidney injury"},
    {"b/l", "bilateral"},
]


def _are_equivalent(value_a: str, value_b: str) -> bool:
    """
    Check if two clinical values are medically equivalent
    and therefore NOT a genuine conflict.
    Returns True if equivalent, False if potentially conflicting.
    """
    a = value_a.lower().strip()
    b = value_b.lower().strip()

    # Exact match
    if a == b:
        return True

    # One contains the other — probably compatible
    if a in b or b in a:
        return True

    # Check known medical abbreviation equivalents
    for equiv_group in KNOWN_EQUIVALENTS:
        a_in_group = any(eq in a for eq in equiv_group)
        b_in_group = any(eq in b for eq in equiv_group)
        if a_in_group and b_in_group:
            return True

    # Word overlap ratio check
    words_a = set(a.split())
    words_b = set(b.split())

    # Remove very common words that don't carry meaning
    stopwords = {
        "the", "a", "an", "and", "or", "with", "of", "in",
        "on", "at", "to", "for", "is", "was", "has", "had",
        "been", "be", "are", "were", "no", "not"
    }
    words_a -= stopwords
    words_b -= stopwords

    if not words_a or not words_b:
        return True  # Can't compare empty sets — assume compatible

    intersection = words_a & words_b
    union = words_a | words_b
    overlap_ratio = len(intersection) / len(union)

    # More than 40% word overlap → probably compatible
    return overlap_ratio > 0.40


def _check_field_for_conflicts(
    state: AgentState,
    field: str,
    facts: list[SourcedFact]
) -> list[dict]:
    """
    Check a list of facts for the same field and return
    any genuine conflicts found.

    Returns list of conflict dicts, empty if no conflicts.
    """
    if len(facts) <= 1:
        return []

    conflicts = []

    # Compare every pair of facts
    for i in range(len(facts)):
        for j in range(i + 1, len(facts)):
            fact_a = facts[i]
            fact_b = facts[j]

            # Skip if from the same source document
            if fact_a.source_doc == fact_b.source_doc:
                continue

            # Check if values conflict
            if not _are_equivalent(fact_a.value, fact_b.value):
                # For high-stakes fields, flag even minor differences
                # For other fields, only flag clear differences
                conflicts.append({
                    "fact_a": fact_a,
                    "fact_b": fact_b,
                    "field": field,
                    "is_high_stakes": field in HIGH_STAKES_FIELDS
                })

    return conflicts


def run_conflict_detection(state: AgentState) -> dict:
    """
    Post-processing conflict sweep across all extracted facts.
    Runs after all document sections have been processed.

    Catches conflicts that the per-extraction check may have missed,
    particularly N-way conflicts across three or more documents.

    Returns a summary of conflicts found.
    """
    console.print("\n")
    console.print(
        "[bold cyan]Running post-processing conflict detection sweep...[/bold cyan]"
    )

    total_conflicts_found = 0
    fields_with_conflicts = []
    already_flagged = set()

    # Build set of fields already flagged for conflict
    # so we don't create duplicate flags
    for flag in state.flags:
        if flag.flag_type == FlagType.CONFLICT:
            already_flagged.add(flag.field)

    if not state.facts:
        console.print("[dim]No facts in state to check.[/dim]")
        return {
            "success": True,
            "conflicts_found": 0,
            "fields_checked": 0,
            "message": "No facts to check for conflicts"
        }

    fields_checked = 0

    for field, facts_list in state.facts.items():
        fields_checked += 1

        conflicts = _check_field_for_conflicts(state, field, facts_list)

        if not conflicts:
            continue

        for conflict in conflicts:
            fact_a = conflict["fact_a"]
            fact_b = conflict["fact_b"]

            # Skip if already flagged from per-extraction check
            # But for high-stakes fields, always surface even if already flagged
            if field in already_flagged and not conflict["is_high_stakes"]:
                continue

            escalate_conflict(
                state=state,
                field=field,
                values_and_sources=[
                    (fact_a.value, fact_a.source_doc, fact_a.page_number),
                    (fact_b.value, fact_b.source_doc, fact_b.page_number)
                ]
            )

            total_conflicts_found += 1
            if field not in fields_with_conflicts:
                fields_with_conflicts.append(field)

    # Display summary table
    if total_conflicts_found > 0:
        table = Table(
            title="Conflict Detection Summary",
            show_header=True,
            header_style="bold red"
        )
        table.add_column("Field", style="cyan")
        table.add_column("Values Found", style="white")
        table.add_column("Sources", style="yellow")

        for field in fields_with_conflicts:
            facts_list = state.facts.get(field, [])
            values_str = " | ".join(
                f"'{f.value[:40]}'" for f in facts_list
            )
            sources_str = ", ".join(
                f.source_doc for f in facts_list
            )
            table.add_row(field, values_str, sources_str)

        console.print(table)

    else:
        console.print(
            "[green]Conflict detection complete. "
            "No new conflicts found.[/green]"
        )

    console.print(
        f"[dim]Fields checked: {fields_checked} | "
        f"New conflicts flagged: {total_conflicts_found}[/dim]"
    )

    return {
        "success": True,
        "conflicts_found": total_conflicts_found,
        "fields_with_conflicts": fields_with_conflicts,
        "fields_checked": fields_checked,
        "message": (
            f"Conflict sweep complete. "
            f"{total_conflicts_found} conflict(s) found across "
            f"{len(fields_with_conflicts)} field(s)."
        )
    }