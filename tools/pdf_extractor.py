# tools/pdf_extractor.py

import base64
import io
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.progress import track

try:
    import fitz  # PyMuPDF
except ImportError:
    raise ImportError(
        "PyMuPDF not installed. Run: pip install pymupdf"
    )

try:
    from PIL import Image
except ImportError:
    raise ImportError(
        "Pillow not installed. Run: pip install Pillow"
    )

console = Console()

# Pages 1-2 are the sample discharge summary — reference only
SAMPLE_SUMMARY_PAGES = {1, 2}

# DPI for page rasterization
# 150 DPI is sufficient to read both printed and handwritten text
# Higher DPI = better quality but more tokens
RENDER_DPI = 150


def page_to_base64(page, dpi: int = RENDER_DPI) -> str:
    """
    Render a PyMuPDF page to a JPEG base64 string.
    """
    # Scale factor from 72 DPI (PDF default) to target DPI
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)

    # Render page to pixmap (RGB)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)

    # Convert pixmap to PIL Image
    img_bytes = pixmap.tobytes("jpeg")
    img = Image.open(io.BytesIO(img_bytes))

    # Convert to base64
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=85)
    buffer.seek(0)
    b64 = base64.b64encode(buffer.read()).decode("utf-8")

    return b64


def extract_pdf(pdf_path: str) -> dict:
    """
    Convert PDF pages to base64 images for vision-based extraction.

    Instead of attempting text extraction (which fails on image-based
    and handwritten PDFs), we render each page as an image that a
    vision LLM can read directly.

    Returns:
        {
            "page_images": {
                1: {"base64": "...", "is_reference": True},
                2: {"base64": "...", "is_reference": True},
                3: {"base64": "...", "is_reference": False},
                ...
            },
            "sections": {
                "Sample Discharge Summary": {
                    "pages": [1, 2],
                    "base64_images": ["...", "..."],
                    "is_reference": True
                },
                "Pages 3-5": {
                    "pages": [3, 4, 5],
                    "base64_images": ["...", "...", "..."],
                    "is_reference": False
                },
                ...
            },
            "total_pages": 71,
            "errors": []
        }
    """
    path = Path(pdf_path)

    if not path.exists():
        console.print(f"[red]PDF not found: {pdf_path}[/red]")
        return {
            "page_images": {},
            "sections": {},
            "total_pages": 0,
            "errors": [f"File not found: {pdf_path}"]
        }

    console.print(Panel(
        f"[cyan]Converting PDF to images:[/cyan] {path.name}\n"
        f"[dim]Using PyMuPDF at {RENDER_DPI} DPI[/dim]",
        title="PDF Extractor",
        border_style="blue"
    ))

    page_images = {}
    errors = []

    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)

        console.print(f"[dim]Total pages: {total_pages}[/dim]")

        for page_num in track(
            range(1, total_pages + 1),
            description="Rendering pages..."
        ):
            try:
                page = doc[page_num - 1]  # fitz is 0-indexed
                b64 = page_to_base64(page, dpi=RENDER_DPI)
                is_reference = page_num in SAMPLE_SUMMARY_PAGES

                page_images[page_num] = {
                    "base64": b64,
                    "is_reference": is_reference
                }

            except Exception as e:
                error_msg = f"Page {page_num}: render error — {str(e)}"
                errors.append(error_msg)
                console.print(f"[yellow]Warning: {error_msg}[/yellow]")

        doc.close()

    except Exception as e:
        error_msg = f"Failed to open PDF: {str(e)}"
        errors.append(error_msg)
        console.print(f"[red]Critical: {error_msg}[/red]")
        return {
            "page_images": {},
            "sections": {},
            "total_pages": 0,
            "errors": errors
        }

    # Build sections from page images
    # Group into logical sections:
    # - Pages 1-2: Sample Discharge Summary (reference only)
    # - Pages 3-71: patient data in groups of 4 pages
    sections = {}

    # Reference section
    ref_pages = [p for p in [1, 2] if p in page_images]
    if ref_pages:
        sections["Sample Discharge Summary"] = {
            "pages": ref_pages,
            "base64_images": [page_images[p]["base64"] for p in ref_pages],
            "is_reference": True,
            "text": ""  # kept for compatibility
        }

    # Patient data pages — group by 1
    patient_pages = sorted([
        p for p in page_images.keys()
        if p not in SAMPLE_SUMMARY_PAGES
    ])

    chunk_size = 1
    chunks = [
        patient_pages[i:i + chunk_size]
        for i in range(0, len(patient_pages), chunk_size)
    ]

    for chunk in chunks:
        if not chunk:
            continue
        section_name = f"Pages {chunk[0]}-{chunk[-1]}"
        sections[section_name] = {
            "pages": chunk,
            "base64_images": [
                page_images[p]["base64"] for p in chunk
                if p in page_images
            ],
            "is_reference": False,
            "text": ""  # kept for compatibility
        }

    console.print(f"\n[green]Conversion complete.[/green]")
    console.print(f"  Pages rendered: {len(page_images)}")
    console.print(f"  Sections created: {len(sections)}")
    if errors:
        console.print(f"  [yellow]Warnings: {len(errors)}[/yellow]")

    console.print("\n[dim]Sections:[/dim]")
    for section_name, section_data in sections.items():
        ref_tag = " [dim](reference only)[/dim]" if section_data["is_reference"] else ""
        console.print(
            f"  [cyan]●[/cyan] {section_name} "
            f"(pages {section_data['pages']}){ref_tag}"
        )

    return {
        "page_images": page_images,
        "sections": sections,
        "total_pages": total_pages,
        "errors": errors
    }