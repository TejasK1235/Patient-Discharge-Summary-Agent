# agent/memory.py

from agent.state import AgentState, FlagType


# These are all the fields a complete discharge summary requires.
# The agent will keep working until all are either extracted or marked missing.
REQUIRED_FIELDS = [
    "patient_demographics",
    "admission_date",
    "discharge_date",
    "principal_diagnosis",
    "secondary_diagnoses",
    "hospital_course",
    "procedures_performed",
    "discharge_condition",
    "allergies",
    "follow_up_instructions",
    "pending_results",
    "discharge_medications",
    "admission_medications",
]

# Document sections we expect to find and process from the PDF.
# The pdf_extractor will identify these from the content.
EXPECTED_SECTIONS = [
    "Discharge Summary",
    "ER Observation Chart",
    "Nursing Documentation",
    "Investigation Results",
    "Drug Chart",
    "ICU Chart",
    "Consultation Sheet",
    "Procedure Chart",
    "Monitoring Chart",
    "Admission Record",
    "Nursing Assessment",
    "Nurses Notes",
]


class AgentMemory:
    """
    The query layer on top of AgentState.
    Answers questions about what the agent knows and what it still needs.
    Does not modify state — only reads it.
    """

    def __init__(self, state: AgentState):
        self.state = state

    def get_missing_required_fields(self) -> list[str]:
        """
        Returns fields that are required but not yet extracted or marked missing.
        These are the fields the agent still needs to find.
        """
        covered = set(self.state.extracted_fields) | set(self.state.missing_fields)
        return [f for f in REQUIRED_FIELDS if f not in covered]

    def get_unprocessed_sections(self) -> list[str]:
        """
        Returns document sections that have been loaded into state.documents
        but not yet processed for field extraction.
        """
        return [
            section for section in self.state.documents.keys()
            if section not in self.state.processed_sections
        ]

    def is_complete(self) -> bool:
        """
        The agent is complete when every required field is either
        extracted or explicitly marked as missing/pending.
        Missing is fine — fabricating is not.
        """
        missing_required = self.get_missing_required_fields()
        unprocessed = self.get_unprocessed_sections()
        return len(missing_required) == 0 and len(unprocessed) == 0

    def get_conflicted_fields(self) -> list[str]:
        """Returns all fields that have at least one conflict flag."""
        return list({
            f.field for f in self.state.flags
            if f.flag_type == FlagType.CONFLICT
        })

    def get_flags_by_severity(self, severity: str) -> list:
        """Returns all flags of a given severity level."""
        return [f for f in self.state.flags if f.severity.value == severity]

    def get_facts_summary(self) -> dict:
        """
        Returns a compact summary of all extracted facts.
        Each field maps to its primary value + how many sources found it.
        Used for the planner prompt.
        """
        summary = {}
        for field, facts_list in self.state.facts.items():
            if facts_list:
                primary = self.state.get_primary_fact(field)
                summary[field] = {
                    "value": primary.value if primary else "unknown",
                    "source_count": len(facts_list),
                    "has_conflict": self.state.has_conflict_flag(field),
                    "confidence": primary.confidence.value if primary else "unknown"
                }
        return summary

    def summarize_for_planner(self) -> str:
        """
        Produces a compact plain-text summary of current agent state
        to inject into the planner's LLM prompt.
        Keeps it concise — the planner doesn't need raw document text.
        """
        lines = []

        lines.append(f"Patient ID: {self.state.patient_id}")
        lines.append(f"Steps taken: {self.state.steps}")
        lines.append("")

        # What documents we have
        lines.append(f"Documents loaded ({len(self.state.documents)}):")
        for section in self.state.documents.keys():
            status = "processed" if section in self.state.processed_sections else "NOT YET PROCESSED"
            lines.append(f"  - {section}: {status}")
        lines.append("")

        # What fields we've extracted
        missing_required = self.get_missing_required_fields()
        lines.append(f"Required fields still needed ({len(missing_required)}):")
        for f in missing_required:
            lines.append(f"  - {f}")
        lines.append("")

        lines.append(f"Fields already extracted ({len(self.state.extracted_fields)}):")
        for f in self.state.extracted_fields:
            primary = self.state.get_primary_fact(f)
            val = primary.value[:60] if primary else "see missing"
            conflict = " ⚠️ CONFLICT" if self.state.has_conflict_flag(f) else ""
            lines.append(f"  - {f}: {val}{conflict}")
        lines.append("")

        # Flags
        lines.append(f"Clinical flags raised ({len(self.state.flags)}):")
        for flag in self.state.flags:
            lines.append(f"  - [{flag.severity.value}] {flag.flag_type.value}: {flag.field} — {flag.description[:80]}")
        lines.append("")

        # Missing and pending
        if self.state.missing_fields:
            lines.append(f"Fields marked missing: {', '.join(self.state.missing_fields)}")
        if self.state.pending_items:
            lines.append(f"Pending items: {', '.join(self.state.pending_items)}")

        return "\n".join(lines)

    def get_best_value_for_output(self, field: str) -> str:
        """
        Returns the best displayable value for a field for the final output.
        If conflicted: returns all values with conflict warning.
        If missing: returns the missing marker.
        If single source: returns value with source citation.
        Never returns a bare unsourced value.
        """
        # Check if missing
        if field in self.state.missing_fields:
            return "[MISSING — CLINICIAN REVIEW REQUIRED]"

        facts_list = self.state.facts.get(field, [])

        if not facts_list:
            return "[MISSING — CLINICIAN REVIEW REQUIRED]"

        # Check if conflicted
        if self.state.has_conflict_flag(field):
            parts = []
            for fact in facts_list:
                parts.append(
                    f"{fact.value} "
                    f"(Source: {fact.source_doc}, p.{fact.page_number})"
                )
            conflict_str = " | ".join(parts)
            return (
                f"⚠️ CONFLICT — CLINICIAN REVIEW REQUIRED\n"
                f"  Values found: {conflict_str}"
            )

        # Single clean value
        primary = self.state.get_primary_fact(field)
        if primary:
            return (
                f"{primary.value} "
                f"[Source: {primary.source_doc}, p.{primary.page_number}]"
            )

        return "[MISSING — CLINICIAN REVIEW REQUIRED]"