# tools/drug_checker.py

import random
from rich.console import Console
from rich.table import Table

console = Console()

# Mocked drug interaction database.
# Keys are frozensets of drug name fragments (lowercase, partial match).
# Values are interaction details.
# These are real clinically relevant interactions.
KNOWN_INTERACTIONS = {
    frozenset(["insulin", "ondansetron"]): {
        "severity": "MEDIUM",
        "description": (
            "Ondansetron (Emeset) may mask nausea that could indicate "
            "hypoglycemia in insulin-dependent patients. Monitor blood "
            "glucose closely."
        ),
        "recommendation": "Monitor GRBS frequently. Educate patient on non-nausea hypoglycemia signs."
    },
    frozenset(["insulin", "paracetamol"]): {
        "severity": "LOW",
        "description": (
            "Paracetamol (Dolo) in high doses may cause false low readings "
            "on some continuous glucose monitors. "
        ),
        "recommendation": "Use fingerstick GRBS rather than CGM if high-dose paracetamol used."
    },
    frozenset(["meropenem", "valproate"]): {
        "severity": "HIGH",
        "description": (
            "Meropenem significantly reduces valproate serum levels, "
            "potentially leading to seizure breakthrough."
        ),
        "recommendation": "Avoid combination if possible. Monitor valproate levels closely if co-administered."
    },
    frozenset(["lantus", "actrapid"]): {
        "severity": "LOW",
        "description": (
            "Concurrent use of basal insulin (Lantus) and rapid-acting "
            "insulin (Actrapid) is standard in DKA management but requires "
            "careful glucose monitoring to avoid hypoglycemia."
        ),
        "recommendation": "Continue 2-hourly GRBS monitoring. Sliding scale review before discharge."
    },
    frozenset(["meropenem", "pantoprazole"]): {
        "severity": "LOW",
        "description": (
            "Pantoprazole (PAN) may slightly reduce meropenem efficacy "
            "in some organisms. Generally acceptable in clinical practice."
        ),
        "recommendation": "No dose adjustment required. Monitor clinical response."
    },
}

# Simulate realistic API behavior — sometimes it's slow or unavailable
def _simulate_api_reliability() -> str:
    """
    Simulates external API reliability.
    Returns 'ok', 'timeout', or 'unavailable'.
    In production this would be a real HTTP call.
    """
    roll = random.random()
    if roll < 0.85:
        return "ok"
    elif roll < 0.95:
        return "timeout"
    else:
        return "unavailable"


def check_interactions(medications: list[str]) -> dict:
    """
    Check a list of medication names for known interactions.
    Returns a structured result the agent can act on.

    medications: list of medication name strings
    e.g. ["Meropenem", "Inj. Lantus", "Tab. Dolo 650mg"]
    """
    console.print(f"\n[cyan]Drug checker:[/cyan] Checking {len(medications)} medications...")

    # Simulate API call reliability
    api_status = _simulate_api_reliability()

    if api_status == "timeout":
        console.print("[yellow]Drug checker: API timeout. Retrying once...[/yellow]")
        api_status = _simulate_api_reliability()
        if api_status != "ok":
            console.print("[red]Drug checker: Retry failed. Service unavailable.[/red]")
            return {
                "success": False,
                "error": "Drug interaction service unavailable after retry",
                "interactions": [],
                "checked": False,
                "message": (
                    "DRUG INTERACTION CHECK COULD NOT BE COMPLETED — "
                    "service unavailable. Manual review required."
                )
            }

    if api_status == "unavailable":
        console.print("[red]Drug checker: Service unavailable.[/red]")
        return {
            "success": False,
            "error": "Drug interaction service unavailable",
            "interactions": [],
            "checked": False,
            "message": (
                "DRUG INTERACTION CHECK COULD NOT BE COMPLETED — "
                "service unavailable. Manual review required."
            )
        }

    # Normalize medication names for matching
    # Strip dosing info, routes, prefixes — just get the drug name
    normalized = []
    for med in medications:
        name = med.lower()
        # Remove common prefixes
        for prefix in ["inj.", "inj ", "tab.", "tab ", "cap.", "cap ",
                        "inj-", "syp.", "syp "]:
            name = name.replace(prefix, "")
        # Remove dosing suffixes (numbers + units)
        import re
        name = re.sub(r'\d+\s*(mg|ml|mcg|iu|u|gm|g)\b', '', name)
        name = name.strip()
        normalized.append(name)

    console.print(f"[dim]Normalized names: {normalized}[/dim]")

    # Check all pairs
    interactions_found = []

    for i in range(len(normalized)):
        for j in range(i + 1, len(normalized)):
            drug_a = normalized[i]
            drug_b = normalized[j]

            for interaction_key, interaction_data in KNOWN_INTERACTIONS.items():
                # Check if both drugs partially match this interaction key
                key_drugs = list(interaction_key)
                match_a = any(kd in drug_a or drug_a in kd for kd in key_drugs)
                match_b = any(kd in drug_b or drug_b in kd for kd in key_drugs)

                if match_a and match_b:
                    interactions_found.append({
                        "drug_a": medications[i],
                        "drug_b": medications[j],
                        "severity": interaction_data["severity"],
                        "description": interaction_data["description"],
                        "recommendation": interaction_data["recommendation"]
                    })

    # Display results
    if interactions_found:
        table = Table(
            title="Drug Interactions Found",
            show_header=True,
            header_style="bold magenta"
        )
        table.add_column("Drug A", style="cyan")
        table.add_column("Drug B", style="cyan")
        table.add_column("Severity", style="bold")
        table.add_column("Description")

        for interaction in interactions_found:
            sev_color = {
                "HIGH": "red",
                "MEDIUM": "yellow",
                "LOW": "green"
            }.get(interaction["severity"], "white")

            table.add_row(
                interaction["drug_a"],
                interaction["drug_b"],
                f"[{sev_color}]{interaction['severity']}[/{sev_color}]",
                interaction["description"][:80] + "..."
                if len(interaction["description"]) > 80
                else interaction["description"]
            )

        console.print(table)
    else:
        console.print(
            "[green]Drug checker: No significant interactions found "
            "in database for this medication combination.[/green]"
        )

    return {
        "success": True,
        "checked": True,
        "medications_checked": medications,
        "interaction_count": len(interactions_found),
        "interactions": interactions_found,
        "message": (
            f"Drug interaction check complete. "
            f"{len(interactions_found)} interaction(s) found."
        )
    }