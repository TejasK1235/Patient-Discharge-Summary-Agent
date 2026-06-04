# agent/loop.py

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from agent.state import AgentState, FlagType, FlagSeverity
from agent.memory import AgentMemory
from agent import planner
from tools.section_extractor import extract_field_from_section
from tools.conflict_detector import run_conflict_detection
from tools.medication_reconciler import run_medication_reconciliation
from tools.drug_checker import check_interactions
from tools.escalator import escalate_missing, escalate

console = Console()

MAX_STEPS = 40

# Sections the agent must never extract patient facts from
REFERENCE_ONLY_SECTIONS = {"Sample Discharge Summary"}

# Maximum attempts per field before marking it missing
MAX_FIELD_ATTEMPTS = 5


def run(state: AgentState) -> AgentState:
    """
    Main agent loop with field rotation.

    Key changes from v1:
    - field_attempt_counts tracks how many sections have been
      tried per field. After MAX_FIELD_ATTEMPTS, field is marked
      missing and agent moves on.
    - Rotation: planner receives attempt counts and is instructed
      to rotate between fields rather than exhausting one.
    - Rate limit handling: graceful wait and retry.
    """
    memory = AgentMemory(state)

    # (field, section) pairs already attempted
    attempted_extractions: set = set()

    # How many sections have been tried per field
    field_attempt_counts: dict = {}

    drug_check_done: bool = False

    console.print(Rule("[bold cyan]Agent Loop Starting[/bold cyan]"))
    console.print(
        f"[dim]Patient: {state.patient_id} | "
        f"Documents: {len(state.documents)} | "
        f"Step cap: {MAX_STEPS} | "
        f"Max attempts per field: {MAX_FIELD_ATTEMPTS}[/dim]"
    )

    # Pre-block reference sections
    for ref_section in REFERENCE_ONLY_SECTIONS:
        if ref_section in state.documents:
            for field in _all_required_fields():
                attempted_extractions.add((field, ref_section))

    while state.steps < MAX_STEPS:
        state.steps += 1
        memory = AgentMemory(state)

        console.print(
            f"\n[bold]━━━ Step {state.steps}/{MAX_STEPS} ━━━[/bold]"
        )

        # Check completion
        if memory.is_complete() and drug_check_done:
            console.print(
                "[green]Complete. All fields covered.[/green]"
            )
            state.is_complete = True
            break

        # Auto-mark fields that have hit the attempt cap
        for field, count in list(field_attempt_counts.items()):
            if (count >= MAX_FIELD_ATTEMPTS
                    and field not in state.missing_fields
                    and field not in state.extracted_fields):
                console.print(
                    f"[yellow]Auto-marking '{field}' missing "
                    f"after {count} attempts.[/yellow]"
                )
                state.mark_missing(field)
                tried = {
                    sec for f, sec in attempted_extractions
                    if f == field
                }
                escalate_missing(
                    state=state,
                    field=field,
                    searched_in=list(tried)
                )

        # Ask planner
        action = planner.decide(
            state=state,
            attempted_extractions=attempted_extractions,
            drug_check_done=drug_check_done,
            max_steps=MAX_STEPS,
            field_attempt_counts=field_attempt_counts
        )

        action_name = action.get("action", "unknown")
        params = action.get("params", {}) or {}
        reasoning = action.get("reasoning", "")
        next_preview = action.get("next_decision_preview", "")
        result_message = ""

        # ── extract_field ─────────────────────────────────────────────
        if action_name == "extract_field":
            field = params.get("field", "")
            section = params.get("section", "")

            if not field or not section:
                result_message = "Missing field or section param."

            elif section not in state.documents:
                result_message = f"Section '{section}' not found."

            elif (field, section) in attempted_extractions:
                result_message = f"Already attempted ({field}, {section})."

            elif field in state.extracted_fields:
                result_message = (
                    f"'{field}' already extracted. Skipping."
                )

            elif field in state.missing_fields:
                result_message = (
                    f"'{field}' already marked missing. Skipping."
                )

            else:
                attempted_extractions.add((field, section))
                field_attempt_counts[field] = (
                    field_attempt_counts.get(field, 0) + 1
                )

                result = extract_field_from_section(
                    state=state,
                    field=field,
                    section_name=section,
                )
                result_message = result.get("message", "")

                if not result.get("success", False):
                    console.print(
                        f"[yellow]Extraction failed: "
                        f"{result.get('error', 'unknown')}[/yellow]"
                    )

        # ── run_drug_check ────────────────────────────────────────────
        elif action_name == "run_drug_check":
            if drug_check_done:
                result_message = "Drug check already done."
            else:
                med_names = []
                if state.medications_discharge:
                    med_names = [m.name for m in state.medications_discharge]
                else:
                    dis_fact = state.get_primary_fact(
                        "discharge_medications"
                    )
                    if dis_fact:
                        parts = [
                            p.strip()
                            for p in dis_fact.value.replace(
                                ";", "\n"
                            ).split("\n")
                            if p.strip()
                        ]
                        med_names = parts[:15]

                if not med_names:
                    result_message = "No medications for drug check."
                    drug_check_done = True
                else:
                    check_result = check_interactions(med_names)
                    drug_check_done = True

                    if not check_result.get("success", False):
                        escalate(
                            state=state,
                            flag_type=FlagType.DRUG_INTERACTION,
                            field="discharge_medications",
                            description=(
                                "Drug interaction check failed. "
                                "Manual pharmacist review required."
                            ),
                            severity=FlagSeverity.HIGH,
                            sources=["drug_checker (mocked)"]
                        )
                        result_message = "Drug check failed. Escalated."
                    else:
                        for interaction in check_result.get(
                            "interactions", []
                        ):
                            sev = interaction.get("severity", "MEDIUM")
                            flag_severity = {
                                "HIGH": FlagSeverity.HIGH,
                                "MEDIUM": FlagSeverity.MEDIUM,
                                "LOW": FlagSeverity.LOW,
                            }.get(sev, FlagSeverity.MEDIUM)

                            escalate(
                                state=state,
                                flag_type=FlagType.DRUG_INTERACTION,
                                field="discharge_medications",
                                description=(
                                    f"Interaction: "
                                    f"{interaction['drug_a']} + "
                                    f"{interaction['drug_b']} — "
                                    f"{interaction['description']}"
                                ),
                                severity=flag_severity,
                                sources=["drug_checker (mocked)"]
                            )
                        result_message = check_result.get("message", "")

        # ── mark_field_missing ────────────────────────────────────────
        elif action_name == "mark_field_missing":
            field = params.get("field", "")
            if not field:
                result_message = "mark_field_missing called without field."
            elif field in state.missing_fields:
                result_message = f"'{field}' already missing. Skipping."
            elif field in state.extracted_fields:
                result_message = f"'{field}' already extracted. Skipping."
            else:
                state.mark_missing(field)
                tried = {
                    sec for f, sec in attempted_extractions
                    if f == field
                }
                escalate_missing(
                    state=state,
                    field=field,
                    searched_in=list(tried)
                )
                result_message = (
                    f"'{field}' marked missing. "
                    f"Searched: {list(tried)}"
                )
                console.print(
                    f"[yellow]Field '{field}' marked MISSING.[/yellow]"
                )

        # ── mark_complete ─────────────────────────────────────────────
        elif action_name == "mark_complete":
            console.print("[green]Planner signaled completion.[/green]")
            result_message = "Planner marked complete."
            state.is_complete = True
            _run_post_processing(state)
            break

        else:
            result_message = f"Unknown action '{action_name}'."
            console.print(f"[red]{result_message}[/red]")

        # Log trace
        state.log_trace(
            step=state.steps,
            reasoning=reasoning,
            action=action_name,
            inputs=params,
            result=result_message,
            next_decision=next_preview
        )

    # Step cap reached
    if state.steps >= MAX_STEPS and not state.is_complete:
        console.print(Panel(
            f"[red]Step cap of {MAX_STEPS} reached.[/red]\n"
            "Generating output with what was collected.",
            title="[red]Step Cap Reached[/red]",
            border_style="red"
        ))
        state.step_cap_reached = True

        memory = AgentMemory(state)
        for field in memory.get_missing_required_fields():
            if field not in state.missing_fields:
                state.mark_missing(field)

        _run_post_processing(state)

    console.print(Rule("[bold cyan]Agent Loop Complete[/bold cyan]"))
    _print_final_summary(state)
    return state


def _run_post_processing(state: AgentState) -> None:
    console.print(
        "\n[bold cyan]Running post-processing...[/bold cyan]"
    )

    has_meds = (
        bool(state.facts.get("admission_medications"))
        or bool(state.facts.get("discharge_medications"))
    )

    if has_meds:
        run_medication_reconciliation(state)
    else:
        console.print(
            "[yellow]No medication facts. "
            "Skipping reconciliation.[/yellow]"
        )

    run_conflict_detection(state)


def _all_required_fields() -> list[str]:
    from agent.memory import REQUIRED_FIELDS
    return REQUIRED_FIELDS


def _print_final_summary(state: AgentState) -> None:
    high = [f for f in state.flags if f.severity == FlagSeverity.HIGH]
    med = [f for f in state.flags if f.severity == FlagSeverity.MEDIUM]
    low = [f for f in state.flags if f.severity == FlagSeverity.LOW]

    console.print(Panel(
        f"[bold]Steps used:[/bold] {state.steps} / {MAX_STEPS}\n"
        f"[bold]Fields extracted:[/bold] "
        f"{len(state.extracted_fields)}\n"
        f"[bold]Fields missing:[/bold] "
        f"{len(state.missing_fields)}\n"
        f"[bold]Pending:[/bold] {len(state.pending_items)}\n"
        f"[bold]Flags:[/bold] "
        f"[red]{len(high)} HIGH[/red] | "
        f"[yellow]{len(med)} MEDIUM[/yellow] | "
        f"[blue]{len(low)} LOW[/blue]",
        title="[bold green]Agent Complete[/bold green]",
        border_style="green"
    ))