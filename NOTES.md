# Submission Notes

## Core Problem

The scaffold trusted Claude's output completely — raw text was returned directly to the caller with no parsing, no schema enforcement, and no error handling. If Claude returned prose, a malformed string, or wrapped JSON in markdown fences, the API either crashed (500) or silently returned garbage. There was also no visibility into what was happening: no logs, no distinction between API failures and parse failures.

## What I Built

- **Pydantic schema** (`ReceiptItem`, `ParsedReceipt`) with a `Literal` type constraining `category` to exactly five allowed values, and `min_length=1` on the items list so an empty result is rejected rather than silently returned as a 200. `model_validate` enforces the contract at runtime.
- **JSON-forcing prompt** — explicit schema description in the system prompt; user message asks for a JSON object with an `items` array. Ambiguity about output format was the root cause of Claude returning prose or markdown-fenced code blocks.
- **Code-fence stripping** — `re.sub` removes ` ```json ` / ` ``` ` wrappers before `json.loads`. I chose this as a defense-in-depth measure rather than relying on the system prompt alone: even with an explicit "return only JSON" instruction, models occasionally wrap output in markdown, and stripping it locally is cheaper and faster than spending a retry round-trip on a deviation I can fix deterministically.
- **Retry loop** — on *validation* failure (bad JSON or schema mismatch), the bad response is sent back as an assistant turn with the exact error reason, asking Claude to correct it. Max 1 retry. **API errors bypass the retry loop entirely** — an `AuthenticationError`, `RateLimitError`, or `APIStatusError` on either attempt returns immediately, because retrying a 401 or 429 against the same key/limit would just fail again and waste a round-trip. Only validation errors are eligible for the second attempt.
- **Structured API error handling** — `AuthenticationError` → 401, `RateLimitError` → 429, `APIStatusError` → 502, missing client → 503. All return typed JSON bodies, never a raw exception traceback.
- **Logging** — every Claude request and response (truncated to 200 chars), validation pass/fail with item count or error reason, retry decisions, and exhaustion all logged with timestamps and attempt numbers via Python's `logging` module.
- **Model choice** — settled on Claude Haiku 4.5. Receipt parsing into a fixed five-value enum is a constrained classification task; Haiku handles every test case correctly on the first attempt at a fraction of Opus's cost. Opus would be overkill here.

## Deliberately Left Out

- **Rate-limit backoff / retry** — `RateLimitError` returns 429 immediately; exponential backoff with jitter would be the right production addition but adds complexity beyond the scope of this task.
- **Endpoint authentication** — no API key or auth middleware on `/parse`. Out of scope; noted here so it's not mistaken for an oversight.
- **Persistent log storage** — logs go to stdout only. A production system would ship them to a log aggregator.
- **Streaming** — synchronous `messages.create`; streaming would improve latency on large receipts but isn't needed for correctness here.
- **Input sanitization** — `receipt_text` is passed verbatim to Claude. Very large inputs could hit `max_tokens` limits; truncation or a size check would be a simple guard.

## Where It Would Still Break

- **`float` for currency** — the schema uses `float` for `amount` to match the spec, but float is the wrong type for money: `0.1 + 0.2 != 0.3` and rounding errors accumulate across sums. In production I'd use `Decimal` (or store integer cents) and serialize as a string. I kept `float` here only because the assignment specified it; this is the first thing I'd change.
- **Amounts with locale / non-dollar formatting** — a receipt printing `"34,20"` (comma decimal), `"$1,000.00"` (comma thousands), or `€`/`¥`/`£` symbols can trip up parsing: Claude may emit the amount as a string, strip the wrong separator, or drop the currency. There's no `currency` field, so everything is implicitly assumed USD. A pre-validation normalizer plus an explicit `currency` field would fix this.
- **Duplicate line items** — the same item on two lines (e.g. two coffees) is returned as two separate objects with no de-duplication or quantity roll-up. Arguably correct, but a caller expecting aggregated quantities would be surprised — a documented decision rather than a bug.
- **Fabricated items on ambiguous input** — with `min_length=1`, truly empty input (gibberish, blank) returns a clean `parse_failed` 422 (Claude returns `{"items": []}` → fails validation → retry → still empty → 422). But for input that contains *something* nameable yet isn't a receipt, Haiku sometimes invents a plausible line item (e.g. `{"item": "Random paragraph text", "amount": 0.0, "category": "other"}`) and returns 200. The schema can't tell a hallucinated item from a real one; a confidence threshold or an "is this actually a receipt?" pre-check would be the proper fix.
- **Nested or ambiguous receipts** — receipts with subtotals, taxes, tips, or multi-line item descriptions may cause Claude to return extra fields, split items oddly, or omit amounts. The schema silently ignores extra fields; a missing `amount` raises a `ValidationError` that triggers the retry.
- **`items` returned as a flat object instead of an array** — if Claude returns `{"item": "...", "amount": 5.0, "category": "meals"}` instead of `{"items": [...]}`, `model_validate` raises `ValidationError` (missing `items` field). This is caught and retried correctly, but worth noting as a failure mode.

## How I Used AI Tools

Used Claude Code (this tool) throughout. Specific interactions:

- **Initial schema design** — prompted for a Pydantic v2 model with a `Literal` category field; accepted the output, then manually verified that `model_validate` (not the deprecated `parse_obj`) was used.
- **Retry loop structure** — asked for a multi-turn retry that feeds the bad response back as an assistant message. Reviewed the message-list construction carefully — in particular confirmed that the assistant turn containing the bad raw text is appended *before* the correction request, which is required for the conversation to make sense to Claude.
- **Error handling coverage** — prompted for Anthropic SDK exception types; cross-checked `AuthenticationError`, `RateLimitError`, and `APIStatusError` against the SDK source to confirm they were real class names, not hallucinated.
- **Code-fence stripping regex** — accepted the `re.sub` pattern but tested it manually against ` ```json\n{...}\n``` ` and ` ```\n{...}\n``` ` to confirm both cases were stripped correctly.
- **One thing I caught and changed** — the original scaffold used `claude-sonnet-4-20250514` (a retired model ID) which 404'd. I caught this from the live API error response, moved to a current model, then deliberately stepped down to Haiku 4.5 after confirming it passes every test — the categorisation task doesn't need a frontier model.

## A note on the prompt log

`PROMPT_LOG.md` reconstructs my prompts. I worked through Claude Code's interactive session, which doesn't export a verbatim transcript, so the wording is my honest reconstruction of what I actually asked rather than a copy-paste. The sequence, intent, and the accept-vs-change decisions are accurate; if it's useful on a follow-up call I'm happy to walk through the real session live.
