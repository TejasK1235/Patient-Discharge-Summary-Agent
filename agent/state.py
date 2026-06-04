# agent/state.py

from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNCERTAIN = "uncertain"


class FlagType(str, Enum):
    CONFLICT = "CONFLICT"
    MISSING = "MISSING"
    PENDING = "PENDING"
    MEDICATION_CHANGE = "MEDICATION_CHANGE"
    DRUG_INTERACTION = "DRUG_INTERACTION"
    UNRESOLVED = "UNRESOLVED"


class FlagSeverity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SourcedFact(BaseModel):
    """
    A single clinical fact extracted from a document.
    Every fact must carry its source — no sourceless facts allowed.
    """
    value: str
    source_doc: str          # e.g. "ER Observation Chart"
    page_number: int         # page in the original PDF
    confidence: Confidence
    raw_context: str         # the exact sentence/phrase this was pulled from
                             # so the clinician can verify


class ClinicalFlag(BaseModel):
    """
    A problem the agent found that requires clinician attention.
    The agent never resolves these — it only surfaces them.
    """
    flag_type: FlagType
    field: str               # which discharge summary field this relates to
    description: str         # human-readable explanation of the problem
    severity: FlagSeverity
    sources: list[str] = Field(default_factory=list)  # which docs are involved


class MedicationEntry(BaseModel):
    """
    A single medication record from either admission or discharge.
    """
    name: str
    dose: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    source_doc: str
    page_number: int
    notes: Optional[str] = None  # e.g. "started in ICU", "dose changed"


class AgentState(BaseModel):
    """
    The complete state of the agent at any point in time.
    Passed into every tool and updated after every step.
    This is the single source of truth.
    """

    patient_id: str
    steps: int = 0
    is_complete: bool = False
    step_cap_reached: bool = False

    # Raw text extracted from the PDF, keyed by document section name
    # e.g. {"ER Observation Chart": "...", "Nursing Notes": "..."}
    documents: dict[str, str] = Field(default_factory=dict)

    # All page texts extracted, keyed by page number
    # Used for source attribution and re-extraction if needed
    pages: dict[int, str] = Field(default_factory=dict)

    # Core facts — each field maps to a list of sourced extractions
    # List because multiple documents may state the same field differently
    facts: dict[str, list[SourcedFact]] = Field(default_factory=dict)

    # Problems found — conflicts, missing data, drug issues
    flags: list[ClinicalFlag] = Field(default_factory=list)

    # Fields the agent searched for but could not find in any document
    missing_fields: list[str] = Field(default_factory=list)

    # Items explicitly documented as pending/awaited in the source notes
    pending_items: list[str] = Field(default_factory=list)

    # Medications — kept separate for reconciliation
    medications_admission: list[MedicationEntry] = Field(default_factory=list)
    medications_discharge: list[MedicationEntry] = Field(default_factory=list)

    # Track which document sections have been processed
    # So the planner knows what's been done
    processed_sections: list[str] = Field(default_factory=list)

    # Track which fields have been extracted
    extracted_fields: list[str] = Field(default_factory=list)

    # The final discharge summary text once formatted
    final_summary: Optional[str] = None

    # Agent trace log — every step recorded here
    trace: list[dict] = Field(default_factory=list)

    def add_fact(self, field: str, fact: SourcedFact) -> None:
        """Add a sourced fact to the state."""
        if field not in self.facts:
            self.facts[field] = []
        self.facts[field].append(fact)
        if field not in self.extracted_fields:
            self.extracted_fields.append(field)

    def add_flag(self, flag: ClinicalFlag) -> None:
        """Add a clinical flag to the state."""
        self.flags.append(flag)

    def mark_missing(self, field: str) -> None:
        """Mark a field as not found in any document."""
        if field not in self.missing_fields:
            self.missing_fields.append(field)

    def add_pending(self, item: str) -> None:
        """Mark a clinical item as explicitly pending."""
        if item not in self.pending_items:
            self.pending_items.append(item)

    def get_primary_fact(self, field: str) -> Optional[SourcedFact]:
        """
        Get the highest-confidence fact for a field.
        Returns None if no fact exists.
        Note: if multiple facts exist with conflicts, the conflict
        detector will have already flagged this — do not use this
        method to silently resolve conflicts.
        """
        if field not in self.facts or not self.facts[field]:
            return None
        # Return highest confidence fact
        priority = {
            Confidence.HIGH: 3,
            Confidence.MEDIUM: 2,
            Confidence.LOW: 1,
            Confidence.UNCERTAIN: 0
        }
        return max(
            self.facts[field],
            key=lambda f: priority[f.confidence]
        )

    def has_conflict_flag(self, field: str) -> bool:
        """Check if a field already has a conflict flag."""
        return any(
            f.field == field and f.flag_type == FlagType.CONFLICT
            for f in self.flags
        )

    def log_trace(self, step: int, reasoning: str, action: str,
                  inputs: dict, result: str, next_decision: str) -> None:
        """Record a step in the agent trace."""
        self.trace.append({
            "step": step,
            "reasoning": reasoning,
            "action": action,
            "inputs": inputs,
            "result": result,
            "next_decision": next_decision
        })