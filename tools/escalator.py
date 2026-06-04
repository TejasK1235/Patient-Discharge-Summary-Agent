# tools/escalator.py

from agent.state import AgentState, ClinicalFlag, FlagType, FlagSeverity
from rich.console import Console
from rich.panel import Panel

console = Console()


def escalate(
    state: AgentState,
    flag_type: FlagType,
    field: str,
    description: str,
    severity: FlagSeverity,
    sources: list[str] = None
) -> dict:
    """
    Surface a clinical concern that requires clinician attention.
    Creates a ClinicalFlag and adds it to state.
    Never resolves the concern — only flags it.

    Returns a result dict the agent loop uses for trace logging.
    """
    if sources is None:
        sources = []

    flag = ClinicalFlag(
        flag_type=flag_type,
        field=field,
        description=description,
        severity=severity,
        sources=sources
    )

    state.add_flag(flag)

    # Color and icon based on severity
    severity_style = {
        FlagSeverity.HIGH: ("red", "🚨"),
        FlagSeverity.MEDIUM: ("yellow", "⚠️"),
        FlagSeverity.LOW: ("blue", "ℹ️"),
    }

    color, icon = severity_style.get(severity, ("white", "•"))

    # Make this visible in the terminal — this is a key demo moment
    console.print(Panel(
        f"[{color}]{icon}  {flag_type.value}[/{color}]\n"
        f"[bold]Field:[/bold] {field}\n"
        f"[bold]Issue:[/bold] {description}\n"
        f"[bold]Sources:[/bold] {', '.join(sources) if sources else 'N/A'}\n"
        f"[dim]→ Flagged for clinician review. Agent will not resolve this.[/dim]",
        title=f"[{color}]Clinical Flag — {severity.value} Severity[/{color}]",
        border_style=color
    ))

    return {
        "success": True,
        "flag_created": True,
        "flag_type": flag_type.value,
        "field": field,
        "severity": severity.value,
        "description": description,
        "message": (
            f"Flag created: [{severity.value}] {flag_type.value} "
            f"on field '{field}'"
        )
    }


def escalate_conflict(
    state: AgentState,
    field: str,
    values_and_sources: list[tuple[str, str, int]]
) -> dict:
    """
    Convenience method for the most common escalation case:
    two or more documents disagree on the same field.

    values_and_sources: list of (value, doc_name, page_number) tuples
    """
    parts = [
        f"'{v}' (from {doc}, p.{pg})"
        for v, doc, pg in values_and_sources
    ]
    description = (
        f"Conflicting values found across documents: "
        f"{' vs '.join(parts)}. "
        f"Clinician must determine correct value."
    )
    sources = [doc for _, doc, _ in values_and_sources]

    return escalate(
        state=state,
        flag_type=FlagType.CONFLICT,
        field=field,
        description=description,
        severity=FlagSeverity.HIGH,
        sources=sources
    )


def escalate_missing(
    state: AgentState,
    field: str,
    searched_in: list[str] = None
) -> dict:
    """
    Convenience method for when a required field could not be
    found in any source document.
    """
    searched_str = (
        f"Searched in: {', '.join(searched_in)}"
        if searched_in
        else "Searched all available documents"
    )
    description = (
        f"Required field '{field}' not found in any source document. "
        f"{searched_str}. "
        f"Clinician must supply this value."
    )

    return escalate(
        state=state,
        flag_type=FlagType.MISSING,
        field=field,
        description=description,
        severity=FlagSeverity.MEDIUM,
        sources=searched_in or []
    )


def escalate_medication_change(
    state: AgentState,
    medication_name: str,
    change_type: str,
    details: str,
    source_doc: str
) -> dict:
    """
    Convenience method for medication changes with no documented reason.
    change_type: 'added', 'stopped', 'dose_changed', 'route_changed'
    """
    description = (
        f"Medication '{medication_name}' was {change_type} "
        f"with no documented reason found in source notes. "
        f"Details: {details}. "
        f"Clinician must verify this change is intentional."
    )

    return escalate(
        state=state,
        flag_type=FlagType.MEDICATION_CHANGE,
        field="discharge_medications",
        description=description,
        severity=FlagSeverity.HIGH,
        sources=[source_doc]
    )


def escalate_pending(
    state: AgentState,
    item: str,
    source_doc: str,
    page: int
) -> dict:
    """
    Convenience method for results explicitly documented as pending.
    """
    state.add_pending(item)

    description = (
        f"'{item}' is documented as pending/awaited in source notes. "
        f"Results not yet available at time of discharge. "
        f"Follow-up required."
    )

    return escalate(
        state=state,
        flag_type=FlagType.PENDING,
        field="pending_results",
        description=description,
        severity=FlagSeverity.MEDIUM,
        sources=[f"{source_doc} p.{page}"]
    )