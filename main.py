# main.py

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from tools.pdf_extractor import extract_pdf
from agent.state import AgentState
from agent.loop import run
from formatter.summary_formatter import save_outputs

load_dotenv()
console = Console()

PDF_PATH = "data/patient_2.pdf"
OUTPUT_DIR = "outputs"
PATIENT_ID = "patient_2"


def main():
    console.print(Rule("[bold blue]Discharge Summary Agent[/bold blue]"))
    console.print(Panel(
        "[bold]Dscribe — Agentic Discharge Summary System[/bold]\n"
        "Reads raw clinical source documents and produces a\n"
        "structured discharge summary draft for clinician review.\n\n"
        "[dim]This system never fabricates clinical information.\n"
        "All missing or conflicting data is flagged explicitly.[/dim]",
        border_style="blue"
    ))

    # ── Verify PDF ────────────────────────────────────────────────────
    pdf_path = Path(PDF_PATH)
    if not pdf_path.exists():
        console.print(Panel(
            f"[red]PDF not found at: {pdf_path.resolve()}[/red]\n"
            f"Place the PDF at: {pdf_path.resolve()}",
            title="[red]File Not Found[/red]",
            border_style="red"
        ))
        sys.exit(1)

    # ── Extract PDF (render to images) ────────────────────────────────
    console.print(Rule("[dim]Phase 1: PDF → Images[/dim]"))
    extraction_result = extract_pdf(str(pdf_path))

    if not extraction_result["sections"]:
        console.print(Panel(
            "[red]No sections produced from PDF.[/red]\n"
            f"Errors: {extraction_result.get('errors', [])}",
            title="[red]Extraction Failed[/red]",
            border_style="red"
        ))
        sys.exit(1)

    sections = extraction_result["sections"]
    page_images = extraction_result["page_images"]
    total_pages = extraction_result["total_pages"]
    errors = extraction_result.get("errors", [])

    console.print(
        f"\n[green]PDF rendered:[/green] "
        f"{total_pages} pages, "
        f"{len(sections)} sections."
    )
    if errors:
        console.print(
            f"[yellow]{len(errors)} warning(s).[/yellow]"
        )

    # ── Build state ───────────────────────────────────────────────────
    console.print(Rule("[dim]Phase 2: Building State[/dim]"))
    state = AgentState(patient_id=PATIENT_ID)

    # state.documents: {section_name: text_or_placeholder}
    # We store a placeholder since actual content is in images
    for section_name, section_data in sections.items():
        state.documents[section_name] = section_data.get("text", " ")

    # state.pages: store page image metadata
    # section_extractor reads from state._section_images
    for page_num, page_data in page_images.items():
        state.pages[page_num] = {
            "text": "",
            "section": f"Page {page_num}",
            "quality": "image"
        }

    # Attach section images directly to state as a private attribute
    # section_extractor accesses this via getattr(state, '_section_images')
    section_images_map = {}
    for section_name, section_data in sections.items():
        section_images_map[section_name] = section_data.get(
            "base64_images", []
        )

    # Use object.__setattr__ to bypass Pydantic validation
    object.__setattr__(state, '_section_images', section_images_map)

    console.print(
        f"[green]State built:[/green] "
        f"{len(state.documents)} sections loaded."
    )

    # Show sections
    console.print("\n[dim]Sections:[/dim]")
    for section_name, section_data in sections.items():
        is_ref = section_data.get("is_reference", False)
        img_count = len(section_data.get("base64_images", []))
        ref_tag = " [dim](reference)[/dim]" if is_ref else ""
        console.print(
            f"  [cyan]●[/cyan] {section_name} "
            f"({img_count} image(s)){ref_tag}"
        )

    # ── Agent loop ────────────────────────────────────────────────────
    console.print(Rule("[dim]Phase 3: Agent Loop[/dim]"))
    state = run(state)

    # ── Save outputs ──────────────────────────────────────────────────
    console.print(Rule("[dim]Phase 4: Saving Outputs[/dim]"))
    output_paths = save_outputs(state, output_dir=OUTPUT_DIR)

    console.print(Panel(
        "[bold green]Done.[/bold green]\n\n"
        f"[bold]Summary:[/bold] {output_paths['summary_path']}\n"
        f"[bold]Trace:[/bold] {output_paths['trace_path']}\n\n"
        "[yellow]⚠️  Draft — clinician review required.[/yellow]",
        title="[bold green]Complete[/bold green]",
        border_style="green"
    ))


if __name__ == "__main__":
    main()