# tools/section_extractor.py

import json
import time
import os
from groq import Groq
from dotenv import load_dotenv
from rich.console import Console

from agent.state import AgentState, SourcedFact, Confidence
from prompts.extraction import get_extraction_prompt
from tools.escalator import escalate_pending, escalate, escalate_conflict
from agent.state import FlagType, FlagSeverity

load_dotenv()
console = Console()

# Vision model for reading image-based and handwritten documents
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 3

# Maximum pages to send per vision call
# More pages = more context but more tokens
MAX_PAGES_PER_CALL = 1


def _init_groq_client() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError(
            "GROQ_API_KEY not found in environment."
        )
    return Groq(api_key=api_key)


def _parse_llm_response(response_text: str) -> dict | None:
    """
    Parse JSON from LLM response.
    Extracts first complete JSON object only.
    """
    # Strategy 1: direct parse
    try:
        return json.loads(response_text.strip())
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip markdown fences
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

    # Strategy 3: extract first complete JSON object by brace depth
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


def _map_confidence(confidence_str: str) -> Confidence:
    mapping = {
        "high": Confidence.HIGH,
        "medium": Confidence.MEDIUM,
        "low": Confidence.LOW,
        "uncertain": Confidence.UNCERTAIN,
    }
    return mapping.get(
        str(confidence_str).lower(),
        Confidence.UNCERTAIN
    )


def _build_vision_messages(
    field: str,
    section_name: str,
    base64_images: list[str]
) -> list[dict]:
    """
    Build the messages list for a vision API call.
    Sends page images with a targeted extraction prompt.
    """
    # Get the text prompt for this field
    field_prompt = get_extraction_prompt(field, "")

    # Build the user message content
    # First the images, then the text instruction
    content = []

    # Add each page image
    for i, b64 in enumerate(base64_images[:MAX_PAGES_PER_CALL]):
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{b64}"
            }
        })

    # Add the text instruction after images
    content.append({
        "type": "text",
        "text": (
            f"These are pages from a medical document section '{section_name}'.\n\n"
            f"{field_prompt}"
        )
    })

    return [
        {
            "role": "system",
            "content": (
                "You are a precise clinical data extraction assistant. "
                "You read medical documents including handwritten clinical notes, "
                "lab reports, drug charts, nursing notes, and admission records. "
                "You extract specific fields accurately from what you can see. "
                "You never fabricate information. "
                "You always return valid JSON exactly as instructed. "
                "If text is handwritten and unclear, note uncertainty in confidence."
            )
        },
        {
            "role": "user",
            "content": content
        }
    ]


def extract_field_from_section(
    state: AgentState,
    field: str,
    section_name: str,
) -> dict:
    """
    Extract a specific clinical field from a document section
    using a vision LLM that can read the page images directly.

    This handles image-based, scanned, and handwritten PDFs
    that text extraction tools cannot read.
    """
    # Check section exists
    if section_name not in state.documents:
        return {
            "success": False,
            "found": False,
            "field": field,
            "section": section_name,
            "error": f"Section '{section_name}' not found",
            "message": f"Section '{section_name}' not loaded"
        }

    # Get base64 images for this section from state
    # Images are stored in state.section_images (set in main.py)
    section_images = getattr(state, '_section_images', {})
    base64_images = section_images.get(section_name, [])

    if not base64_images:
        return {
            "success": False,
            "found": False,
            "field": field,
            "section": section_name,
            "error": "No images available for this section",
            "message": f"No page images found for '{section_name}'"
        }

    # Get page numbers for source attribution
    section_pages = []
    for page_num, page_data in state.pages.items():
        section = page_data.get("section", "")
        # Match both exact section names and page-range names
        if section == section_name or section_name.startswith("Pages"):
            # For page-range sections, check if page_num is in range
            section_data = section_images.get(
                f"_section_pages_{section_name}", []
            )
            if section_data:
                section_pages = section_data
                break
    # Fallback: parse page numbers from section name
    if not section_pages and section_name.startswith("Pages "):
        try:
            parts = section_name.replace("Pages ", "").split("-")
            if len(parts) == 2:
                start, end = int(parts[0]), int(parts[1])
                section_pages = list(range(start, end + 1))
        except (ValueError, IndexError):
            section_pages = []

    primary_page = section_pages[0] if section_pages else 0

    console.print(
        f"\n[cyan]Vision extraction:[/cyan] '{field}' from '{section_name}' "
        f"({len(base64_images)} page image(s))"
    )

    try:
        client = _init_groq_client()
    except ValueError as e:
        return {
            "success": False,
            "found": False,
            "field": field,
            "section": section_name,
            "error": str(e),
            "message": "Groq client initialization failed"
        }

    messages = _build_vision_messages(field, section_name, base64_images)
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                console.print(
                    f"[yellow]Retry {attempt}/{MAX_RETRIES}...[/yellow]"
                )
                time.sleep(RETRY_DELAY_SECONDS * attempt)

            response = client.chat.completions.create(
                model=VISION_MODEL,
                messages=messages,
                temperature=0.0,
                max_tokens=600,
            )

            response_text = response.choices[0].message.content
            parsed = _parse_llm_response(response_text)

            if parsed is None:
                last_error = f"JSON parse failed: {response_text[:150]}"
                continue

            if "found" not in parsed:
                last_error = "Response missing 'found' field"
                continue

            # Field not found in these pages
            if not parsed.get("found", False):
                if parsed.get("pending", False):
                    pending_item = (
                        parsed.get("notes")
                        or f"{field} result pending"
                    )
                    escalate_pending(
                        state=state,
                        item=pending_item,
                        source_doc=section_name,
                        page=primary_page
                    )
                    return {
                        "success": True,
                        "found": False,
                        "pending": True,
                        "field": field,
                        "section": section_name,
                        "message": f"'{field}' is pending per '{section_name}'"
                    }

                console.print(
                    f"[dim]  → '{field}' not found in '{section_name}'[/dim]"
                )
                return {
                    "success": True,
                    "found": False,
                    "field": field,
                    "section": section_name,
                    "message": f"'{field}' not present in '{section_name}'"
                }

            # Field found
            value = str(parsed.get("value", "")).strip()
            if not value or value.lower() in ("null", "none", ""):
                return {
                    "success": True,
                    "found": False,
                    "field": field,
                    "section": section_name,
                    "message": f"'{field}' returned empty from '{section_name}'"
                }

            confidence = _map_confidence(
                parsed.get("confidence", "uncertain")
            )
            raw_context = (
                parsed.get("raw_context") or "read from page image"
            )

            fact = SourcedFact(
                value=value,
                source_doc=section_name,
                page_number=primary_page,
                confidence=confidence,
                raw_context=raw_context
            )

            # Conflict check
            existing_facts = state.facts.get(field, [])
            conflict_found = False

            if existing_facts:
                for existing_fact in existing_facts:
                    existing_val = existing_fact.value.lower().strip()
                    new_val = value.lower().strip()

                    if existing_val == new_val:
                        continue
                    if existing_val in new_val or new_val in existing_val:
                        continue

                    words_existing = set(existing_val.split())
                    words_new = set(new_val.split())
                    shared = len(words_existing & words_new)
                    smaller = min(len(words_existing), len(words_new))
                    overlap_threshold = max(1, smaller // 2)

                    if (shared < overlap_threshold
                            and not state.has_conflict_flag(field)):
                        conflict_found = True
                        escalate_conflict(
                            state=state,
                            field=field,
                            values_and_sources=[
                                (existing_fact.value,
                                 existing_fact.source_doc,
                                 existing_fact.page_number),
                                (value, section_name, primary_page)
                            ]
                        )
                        break

            state.add_fact(field, fact)

            confidence_color = {
                Confidence.HIGH: "green",
                Confidence.MEDIUM: "yellow",
                Confidence.LOW: "orange3",
                Confidence.UNCERTAIN: "red"
            }.get(confidence, "white")

            console.print(
                f"  [{confidence_color}]✓[/{confidence_color}] "
                f"'{field}': "
                f"{value[:80]}{'...' if len(value) > 80 else ''} "
                f"[{confidence_color}]"
                f"({confidence.value} confidence)"
                f"[/{confidence_color}]"
            )

            if confidence in (Confidence.LOW, Confidence.UNCERTAIN):
                escalate(
                    state=state,
                    flag_type=FlagType.UNRESOLVED,
                    field=field,
                    description=(
                        f"Low confidence extraction of '{field}' from "
                        f"'{section_name}'. Value: '{value}'. "
                        f"Clinician should verify."
                    ),
                    severity=FlagSeverity.LOW,
                    sources=[section_name]
                )

            result_msg = (
                f"Extracted '{field}' from '{section_name}': "
                f"'{value[:60]}' ({confidence.value} confidence)"
            )
            if conflict_found:
                result_msg += " ⚠️ CONFLICT DETECTED"

            return {
                "success": True,
                "found": True,
                "field": field,
                "section": section_name,
                "value": value,
                "confidence": confidence.value,
                "conflict_detected": conflict_found,
                "message": result_msg
            }

        except Exception as e:
            last_error = str(e)
            console.print(
                f"[red]Vision extraction error attempt {attempt}: "
                f"{last_error[:200]}[/red]"
            )
            if "rate_limit" in str(e).lower() or "429" in str(e):
                console.print(
                    "[yellow]Rate limit hit. Waiting 60 seconds...[/yellow]"
                )
                time.sleep(60)
            elif attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    return {
        "success": False,
        "found": False,
        "field": field,
        "section": section_name,
        "error": last_error,
        "message": (
            f"Vision extraction failed after {MAX_RETRIES} attempts: "
            f"{last_error}"
        )
    }