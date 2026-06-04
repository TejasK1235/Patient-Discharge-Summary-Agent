# agent/planner.py

import os
import json
import time
from groq import Groq
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from agent.state import AgentState
from agent.memory import AgentMemory
from prompts.planning import get_planning_prompt

load_dotenv()
console = Console()

# Use 8b model for planning to save tokens
# It has sufficient reasoning for structured JSON planning
PLANNING_MODEL = "llama-3.1-8b-instant"
MAX_RETRIES = 2
RETRY_DELAY = 3

VALID_ACTIONS = {
    "extract_field",
    "run_drug_check",
    "mark_field_missing",
    "mark_complete",
}


def _init_groq_client() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not found.")
    return Groq(api_key=api_key)


def _parse_planner_response(response_text: str) -> dict | None:
    try:
        return json.loads(response_text.strip())
    except json.JSONDecodeError:
        pass

    text = response_text.strip()
    if "```" in text:
        start_fence = text.find("```")
        end_fence = text.rfind("```")
        if start_fence != end_fence:
            inner = text[start_fence + 3:end_fence]
            if inner.startswith("json"):
                inner = inner[4:]
            try:
                return json.loads(inner.strip())
            except json.JSONDecodeError:
                pass

    # First complete JSON object by brace depth
    start = response_text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i, char in enumerate(response_text[start:], start=start):
        if escape_next:
            escape_next = False
            continue
        if char == "\\" and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = response_text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None

    return None


def _validate_action(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    if "action" not in parsed:
        return False
    if parsed["action"] not in VALID_ACTIONS:
        return False
    if "params" not in parsed or parsed["params"] is None:
        parsed["params"] = {}
    return True


def decide(
    state: AgentState,
    attempted_extractions: set,
    drug_check_done: bool,
    max_steps: int,
    field_attempt_counts: dict
) -> dict:
    """
    Decide next action. Accepts field_attempt_counts to enforce
    rotation — no field gets more than MAX_FIELD_ATTEMPTS sections
    before the planner moves to the next field.
    """
    MAX_FIELD_ATTEMPTS = 5

    memory = AgentMemory(state)
    base_summary = memory.summarize_for_planner()

    # Build attempt summary for planner
    attempt_lines = ["\nAttempts per field (max 5 before moving on):"]
    for field, count in field_attempt_counts.items():
        attempt_lines.append(f"  - {field}: {count} attempt(s)")

    # Fields that have hit the attempt cap — planner must move on
    capped_fields = [
        f for f, c in field_attempt_counts.items()
        if c >= MAX_FIELD_ATTEMPTS
    ]
    if capped_fields:
        attempt_lines.append(
            f"\nFIELDS AT ATTEMPT CAP (mark as missing or skip): "
            f"{', '.join(capped_fields)}"
        )

    drug_line = (
        "\nDrug check: DONE"
        if drug_check_done
        else "\nDrug check: NOT YET RUN"
    )

    reference_note = (
        "\nNOTE: 'Sample Discharge Summary' is reference only. "
        "Do NOT extract from it."
    )

    full_summary = (
        base_summary
        + "\n".join(attempt_lines)
        + drug_line
        + reference_note
    )

    prompt = get_planning_prompt(
        state_summary=full_summary,
        steps_taken=state.steps,
        max_steps=max_steps
    )

    try:
        client = _init_groq_client()
    except ValueError as e:
        console.print(f"[red]Planner: {e}[/red]")
        return _fallback_action(
            state, memory, attempted_extractions,
            field_attempt_counts, MAX_FIELD_ATTEMPTS
        )

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                console.print(
                    f"[yellow]Planner retry {attempt}...[/yellow]"
                )
                time.sleep(RETRY_DELAY * attempt)

            response = client.chat.completions.create(
                model=PLANNING_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a planning agent for a clinical AI system. "
                            "You decide the next action to extract discharge "
                            "summary fields from patient documents. "
                            "CRITICAL RULE: If a field has 5 or more attempts, "
                            "you MUST either mark it missing or move to a "
                            "different field. Never keep retrying a capped field. "
                            "Rotate between fields — do not try all sections for "
                            "one field before moving to the next. "
                            "Return valid JSON only."
                        )
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.0,
                max_tokens=300,
            )

            response_text = response.choices[0].message.content
            parsed = _parse_planner_response(response_text)

            if parsed is None:
                last_error = f"JSON parse failed: {response_text[:150]}"
                continue

            if not _validate_action(parsed):
                last_error = f"Invalid action: {parsed.get('action')}"
                continue

            # Validate extract_field params
            if parsed["action"] == "extract_field":
                field = parsed["params"].get("field")
                section = parsed["params"].get("section")

                if not field or not section:
                    last_error = "extract_field missing field or section"
                    continue

                # Reject if this combination already attempted
                if (field, section) in attempted_extractions:
                    last_error = (
                        f"Already attempted ({field}, {section})"
                    )
                    break

                # Reject if field is at attempt cap
                if field_attempt_counts.get(field, 0) >= MAX_FIELD_ATTEMPTS:
                    last_error = (
                        f"Field '{field}' at attempt cap. Must rotate."
                    )
                    break

                # Fix section name if it doesn't exist
                if section not in state.documents:
                    close = _find_closest_section(
                        section, state.documents
                    )
                    if close:
                        parsed["params"]["section"] = close
                    else:
                        last_error = f"Section '{section}' not found"
                        continue

            _display_plan(parsed, state.steps)
            return parsed

        except Exception as e:
            last_error = str(e)
            if "rate_limit" in str(e).lower() or "429" in str(e):
                console.print(
                    "[yellow]Rate limit on planner. "
                    "Waiting 30s then using fallback.[/yellow]"
                )
                time.sleep(30)
                break
            console.print(
                f"[red]Planner error attempt {attempt}: "
                f"{last_error[:150]}[/red]"
            )

    console.print(
        f"[yellow]Planner failed ({last_error[:100]}). "
        f"Using fallback.[/yellow]"
    )
    return _fallback_action(
        state, memory, attempted_extractions,
        field_attempt_counts, MAX_FIELD_ATTEMPTS
    )


def _find_closest_section(requested: str, available: dict) -> str | None:
    requested_lower = requested.lower()
    for section_name in available.keys():
        if (requested_lower in section_name.lower()
                or section_name.lower() in requested_lower):
            return section_name
    return None


def _fallback_action(
    state: AgentState,
    memory: AgentMemory,
    attempted_extractions: set,
    field_attempt_counts: dict,
    max_field_attempts: int
) -> dict:
    """
    Rule-based fallback with rotation logic.
    Cycles through fields rather than exhausting sections for one field.
    """
    from agent.memory import REQUIRED_FIELDS

    missing_required = memory.get_missing_required_fields()

    if not missing_required:
        return {
            "reasoning": "Fallback: all fields covered.",
            "action": "mark_complete",
            "params": {},
            "next_decision_preview": "Generating output."
        }

    # Priority sections — most likely to contain clinical facts
    priority_sections = [
        "Pages 45-48",  # Admission record area
        "Pages 3-6",    # ER chart area
        "Pages 49-52",  # Consultation sheets area
        "Pages 53-56",
        "Pages 41-44",
        "Pages 37-40",
    ]

    # Add all available sections not in priority list
    all_sections = [
        s for s in state.documents.keys()
        if s != "Sample Discharge Summary"
    ]
    for s in all_sections:
        if s not in priority_sections:
            priority_sections.append(s)

    # Rotate: pick the field with fewest attempts that still has
    # untried sections
    best_field = None
    best_section = None
    min_attempts = float('inf')

    for field in missing_required:
        attempt_count = field_attempt_counts.get(field, 0)
        if attempt_count >= max_field_attempts:
            continue
        if attempt_count < min_attempts:
            # Find untried section for this field
            for section in priority_sections:
                if (field, section) in attempted_extractions:
                    continue
                if section not in state.documents:
                    continue
                min_attempts = attempt_count
                best_field = field
                best_section = section
                break

    if best_field and best_section:
        return {
            "reasoning": (
                f"Fallback rotation: trying '{best_field}' "
                f"({field_attempt_counts.get(best_field, 0)} attempts) "
                f"from '{best_section}'."
            ),
            "action": "extract_field",
            "params": {
                "field": best_field,
                "section": best_section
            },
            "next_decision_preview": "Continue rotating through fields."
        }

    # Mark the field with most attempts as missing
    capped = [
        f for f in missing_required
        if field_attempt_counts.get(f, 0) >= max_field_attempts
    ]
    if capped:
        return {
            "reasoning": f"Fallback: marking '{capped[0]}' missing.",
            "action": "mark_field_missing",
            "params": {"field": capped[0]},
            "next_decision_preview": "Continue with other fields."
        }

    return {
        "reasoning": "Fallback: marking complete.",
        "action": "mark_complete",
        "params": {},
        "next_decision_preview": "Output generation."
    }


def _display_plan(action: dict, step_num: int) -> None:
    action_colors = {
        "extract_field": "cyan",
        "run_drug_check": "magenta",
        "mark_field_missing": "yellow",
        "mark_complete": "green",
    }
    color = action_colors.get(action["action"], "white")
    params = action.get("params", {})
    param_str = (
        f"field='{params.get('field', '')}', "
        f"section='{params.get('section', '')}'"
        if params else "none"
    )
    console.print(Panel(
        f"[bold]Reasoning:[/bold] "
        f"{action.get('reasoning', 'N/A')}\n"
        f"[bold]Action:[/bold] "
        f"[{color}]{action['action']}[/{color}]\n"
        f"[bold]Params:[/bold] {param_str}\n"
        f"[bold]Next:[/bold] "
        f"{action.get('next_decision_preview', 'N/A')}",
        title=f"[bold]Planner — Step {step_num}[/bold]",
        border_style=color
    ))