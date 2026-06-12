import logging
from typing import List, Literal

import anthropic
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App + client
# ---------------------------------------------------------------------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)

try:
    client = anthropic.Anthropic()
    # Verify the key is present (Anthropic() resolves from env; raises if missing)
    if not client.api_key:
        raise ValueError("ANTHROPIC_API_KEY is empty")
    log.info("Anthropic client initialised successfully")
except anthropic.AuthenticationError as e:
    log.error("Invalid ANTHROPIC_API_KEY: %s", e)
    client = None
except Exception as e:
    log.error("Failed to initialise Anthropic client: %s", e)
    client = None

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
CATEGORIES = Literal["meals", "travel", "software", "office_supplies", "other"]

SYSTEM_PROMPT = (
    "You are a receipt parser. Extract every line item from the receipt text "
    "and return them as structured JSON. Each item must have:\n"
    "  - item (string): the item name\n"
    "  - amount (number): the price as a float\n"
    "  - category (string): exactly one of meals | travel | software | office_supplies | other\n"
    "Return only valid JSON matching the schema. No extra text."
)


class ReceiptItem(BaseModel):
    item: str
    amount: float
    category: CATEGORIES


class ParsedReceipt(BaseModel):
    # min_length=1 so an empty result (e.g. non-receipt input that Claude
    # answers with {"items": []}) fails validation and triggers the retry /
    # parse_failed path, rather than silently returning HTTP 200 with no items.
    items: List[ReceiptItem] = Field(min_length=1)


class ReceiptRequest(BaseModel):
    receipt_text: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_claude(messages: list, attempt: int) -> str:
    """Call Claude and return the raw text response. Logs request + response."""
    log.info(
        "Claude request (attempt %d) — %d message(s), last role=%s",
        attempt,
        len(messages),
        messages[-1]["role"],
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    raw = response.content[0].text
    log.info("Claude response (attempt %d) — %d chars: %.200s", attempt, len(raw), raw)
    return raw


def _validate(raw: str, attempt: int) -> ParsedReceipt:
    """Parse raw JSON into ParsedReceipt. Logs pass/fail."""
    import json
    import re

    # Strip markdown code fences if Claude wrapped the JSON
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())

    data = json.loads(cleaned)  # raises json.JSONDecodeError on bad JSON
    result = ParsedReceipt.model_validate(data)  # raises ValidationError on schema mismatch
    log.info("Validation PASSED (attempt %d) — %d item(s)", attempt, len(result.items))
    return result


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/parse")
def parse_receipt(request: ReceiptRequest):
    # Guard: client failed to initialise
    if client is None:
        log.error("Request rejected — Anthropic client not available")
        return JSONResponse(
            status_code=503,
            content={"error": "service_unavailable", "detail": "Anthropic client could not be initialised. Check ANTHROPIC_API_KEY."},
        )

    messages = [
        {
            "role": "user",
            "content": (
                "Parse this receipt and return JSON with an 'items' array.\n\n"
                f"Receipt:\n{request.receipt_text}"
            ),
        }
    ]

    raw = None
    last_error = None

    for attempt in range(1, 3):  # attempt 1, then retry attempt 2
        try:
            raw = _call_claude(messages, attempt)
        except anthropic.AuthenticationError as e:
            log.error("Authentication error on attempt %d: %s", attempt, e)
            return JSONResponse(
                status_code=401,
                content={"error": "auth_failed", "detail": "Invalid or missing ANTHROPIC_API_KEY."},
            )
        except anthropic.RateLimitError as e:
            log.warning("Rate limited on attempt %d: %s", attempt, e)
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limited", "detail": str(e)},
            )
        except anthropic.APIStatusError as e:
            log.error("API error on attempt %d (HTTP %d): %s", attempt, e.status_code, e.message)
            return JSONResponse(
                status_code=502,
                content={"error": "api_error", "detail": e.message},
            )

        try:
            return _validate(raw, attempt).model_dump()
        except Exception as e:
            last_error = e
            log.warning(
                "Validation FAILED (attempt %d): %s",
                attempt,
                str(e),
            )

            if attempt == 1:
                # Build retry message: show Claude its bad output and ask it to fix it
                log.info("Retrying — sending bad response back to Claude for correction")
                messages += [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "Your previous response failed validation.\n"
                            f"Reason: {e}\n\n"
                            "Please return corrected JSON with an 'items' array where every item "
                            "has item (string), amount (float), and category (one of: "
                            "meals | travel | software | office_supplies | other). "
                            "Return only the JSON, no extra text."
                        ),
                    },
                ]

    # Both attempts failed
    log.error("All attempts exhausted. Returning parse_failed error.")
    return JSONResponse(
        status_code=422,
        content={
            "error": "parse_failed",
            "detail": str(last_error),
            "raw_response": raw,
        },
    )
