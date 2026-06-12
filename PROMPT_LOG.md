# Prompt Log

All prompts were sent to Claude Code (Sonnet 4.6) in an interactive session.

> **On verbatim accuracy:** these are my actual prompts as closely as I can reproduce them. Claude Code's interactive session doesn't export a clean copy-pasteable transcript, so a few are lightly reconstructed from memory rather than pulled character-for-character. The sequence, intent, and the accept-vs-change decisions are all accurate — and I'm happy to walk through the live session on a follow-up call if useful. Prompts kept in my original casing/typos where I had them, so they read a little rough on purpose.

---

## Round 1 — Structured JSON output with Pydantic validation

**Prompt (verbatim):**
> I have a Python FastAPI endpoint that sends a receipt to Claude API and returns raw text. I need to make it return Valid, structured JSON. Here's the current code: @scaffold.py — These are the only categories you should use: meals, travel, software, office_supplies, other — Each item must have: item (string), amount (float), category (one of the above) — Use Pydantic to validate the parsed response — If the response can't be parsed into that schema, return a clean error (not a 500) — Update the prompt to Claude to ask for JSON output in that exact schema

**What the tool gave back:**
- `ReceiptItem` and `ParsedReceipt` Pydantic v2 models with `Literal` for category
- System prompt instructing Claude to return JSON only
- `_validate()` helper using `json.loads` + `model_validate`
- 422 `JSONResponse` on parse failure

---

## Round 2 — Retry loop, API key error handling, and logging

**Prompt (verbatim):**
> Now add a retry loop: if the first Claude response fails validation, send a second request that includes the bad response from first attempt and asks Claude to fix it. If the retry also fails, it returns a structured error: { 'error': 'parse_failed', 'detail': '...', 'raw_response': '...' }. Max 1 retry. Wrap the API Key with proper error handling and Log all the Claude API requests, responses, Validation Pass or Fail, and any reasons for Failure or Errors.

**What the tool gave back:**
- `for attempt in range(1, 3)` loop
- On validation failure at attempt 1: appends `{"role": "assistant", "content": raw}` + a correction user message to `messages`, then loops
- `anthropic.AuthenticationError` → 401, `RateLimitError` → 429, `APIStatusError` → 502
- `client = None` sentinel at module init with 503 guard at the endpoint
- `logging.basicConfig` with timestamps; log lines for request, response (200-char truncated), validation pass/fail, retry decision, exhaustion

**Design decision — which errors retry:** only *validation* failures (bad JSON, schema mismatch) consume the second attempt. API errors (`AuthenticationError` 401, `RateLimitError` 429, `APIStatusError` 502) short-circuit and return immediately — they're raised *before* the validation block, so the `range(1, 3)` loop never reaches attempt 2 for them. Retrying a 401 or 429 against the same key/limit would just fail identically and waste a round-trip.

---

## Round 3 — Switch model from Opus to Haiku

**Prompt (verbatim):**
> isn't opus 4.8 over kill for this requirement of cateorizing response change it to haiku 4.5 and let me know how the tests perform with haiku 4.5. if needed enhance the prompt to be more clear for haiku 4.5 to understand the request

**What the tool gave back:**
- Updated `_call_claude()` to use `claude-haiku-4-5-20251001`
- Restarted the server and ran 5 test cases against the new model

**Test results with Haiku 4.5 — all passed on attempt 1, no retries needed:**

| Test | Input | Result |
|---|---|---|
| 3-item restaurant receipt | Steak, wine, dessert | ✅ 200 — 3 items, all `meals` |
| Mixed categories | Uber + GitHub Pro + coffee | ✅ 200 — `travel`, `software`, `meals` |
| 4-item office supplies | HDMI, notebook, whiteboard, paper | ✅ 200 — all `office_supplies` |
| All-category sweep | Flight + Slack + lunch + ink + taxi | ✅ 200 — all 5 categories correct |

No prompt changes were needed — the existing system prompt was already clear enough for Haiku. Conclusion: Haiku 4.5 is the right call for a fixed five-value enum classification task at a fraction of Opus's cost.

---

## Round 4 — Live test with real API key

**Prompt (verbatim):**
> i've added the API key in .env file, use it to test the FAST API's functionality

(Earlier in the session I'd also asked: *"run the server on local host 8000"* — the tool returned `uvicorn scaffold:app --host 0.0.0.0 --port 8000 --reload`. Folding it in here rather than logging a one-line launch command as its own round.)

**What the tool gave back:**
- Shell command to export the key: `export $(grep -v '^#' .env | tr -d ' ' | xargs)`
- A `curl` test against `/parse` with a sample receipt body
- Confirmed the endpoint returned structured JSON with items, amounts, and categories

---

## Round 5 — Testing

**Test 1 — Happy path, 3-item receipt**

Request:
```
POST /parse
{
  "receipt_text": "Dinner at The Grand Bistro\n\nSteak Frites $34.00\nGlass of Merlot $12.50\nCrème Brûlée $9.00"
}
```

Response (HTTP 200):
```json
{
  "items": [
    { "item": "Steak Frites",   "amount": 34.0,  "category": "meals" },
    { "item": "Glass of Merlot","amount": 12.5,  "category": "meals" },
    { "item": "Crème Brûlée",   "amount": 9.0,   "category": "meals" }
  ]
}
```

Logs:
```
2026-06-11 [INFO]  Claude request (attempt 1) — 1 message(s), last role=user
2026-06-11 [INFO]  Claude response (attempt 1) — 187 chars: {"items": [{"item": "Steak Frites"...
2026-06-11 [INFO]  Validation PASSED (attempt 1) — 3 item(s)
```

---

**Test 2 — Code fence wrapping (pre-fix behaviour)**

Claude returned attempt 1 wrapped in markdown fences, triggering the retry:

```
2026-06-11 [INFO]  Claude response (attempt 1) — 229 chars: ```json\n{"items": [...]}\n```
2026-06-11 [WARNING] Validation FAILED (attempt 1): Expecting value: line 1 column 1 (char 0)
2026-06-11 [INFO]  Retrying — sending bad response back to Claude for correction
2026-06-11 [INFO]  Claude request (attempt 2) — 3 message(s), last role=user
2026-06-11 [INFO]  Claude response (attempt 2) — 187 chars: {"items": [...]}
2026-06-11 [INFO]  Validation PASSED (attempt 2) — 3 item(s)
```

After adding the `re.sub` fence-stripping step in `_validate()`, the same receipt passed on attempt 1 with no retry needed.

**Why strip fences instead of just hardening the prompt:** I treat this as defense-in-depth. Even with an explicit "return only JSON, no extra text" system prompt, models still occasionally wrap output in ```` ```json ````. Stripping it locally is deterministic, instant, and free — versus a retry that costs a full extra round-trip and still isn't guaranteed to come back clean. The prompt does the steering; the regex is the cheap safety net.

---

**Test 3 — Mixed categories (travel + software + meals)**

Request:
```
POST /parse
{
  "receipt_text": "Expense Report\n\nUber to airport    $24.50\nGitHub Pro (annual) $4.00\nAirport coffee      $6.75"
}
```

Response (HTTP 200):
```json
{
  "items": [
    { "item": "Uber to airport",    "amount": 24.5,  "category": "travel" },
    { "item": "GitHub Pro (annual)","amount": 4.0,   "category": "software" },
    { "item": "Airport coffee",     "amount": 6.75,  "category": "meals" }
  ]
}
```

Logs:
```
2026-06-11 [INFO]  Claude request (attempt 1) — 1 message(s), last role=user
2026-06-11 [INFO]  Claude response (attempt 1) — 201 chars: {"items": [{"item": "Uber to airport"...
2026-06-11 [INFO]  Validation PASSED (attempt 1) — 3 item(s)
```

---

**Test 4 — Empty result rejected → parse_failed (non-receipt input)**

This test drove a real design change. Originally `items: List[ReceiptItem]` had no minimum length, so when Claude answered non-receipt input with `{"items": []}`, that *passed* validation and returned HTTP 200 with an empty list — inconsistent with the `parse_failed` path I wanted for garbage. I added `min_length=1` to the items list so an empty result now fails validation, retries once, and (if still empty) returns a clean 422. Behaviour is now consistent: no-item input always ends in `parse_failed`, never a silent empty 200.

Actual run with the updated schema:

Request:
```
POST /parse
{ "receipt_text": "asdfghjkl qwerty zxcvbnm" }
```

Response (HTTP 422):
```json
{
  "error": "parse_failed",
  "detail": "1 validation error for ParsedReceipt\nitems\n  List should have at least 1 item after validation, not 0 [type=too_short, input_value=[], input_type=list]",
  "raw_response": "```json\n{\"items\": []}\n```"
}
```

Logs:
```
2026-06-11 [INFO]  Claude request (attempt 1) — 1 message(s), last role=user
2026-06-11 [INFO]  Claude response (attempt 1): {"items": []}
2026-06-11 [WARNING] Validation FAILED (attempt 1): List should have at least 1 item after validation, not 0
2026-06-11 [INFO]  Retrying — sending bad response back to Claude for correction
2026-06-11 [INFO]  Claude request (attempt 2) — 3 message(s), last role=user
2026-06-11 [INFO]  Claude response (attempt 2): {"items": []}
2026-06-11 [WARNING] Validation FAILED (attempt 2): List should have at least 1 item after validation, not 0
2026-06-11 [ERROR]  All attempts exhausted. Returning parse_failed error.
```

**Known gap (documented, not fixed):** `min_length=1` cleanly handles *empty* results, but not *hallucinated* ones. For input that contains something nameable but isn't a receipt (e.g. `"This is not a receipt. Just some random paragraph text."`), Haiku sometimes invents a single plausible item — `{"item": "Random paragraph text", "amount": 0.0, "category": "other"}` — and returns HTTP 200. The schema can't distinguish a fabricated item from a real one. The proper fix is a separate "is this actually a receipt?" pre-check or a confidence signal; flagged in `NOTES.md` as a with-more-time item.

---

**Test 5 — API error: bad key (401)**

Request:
```
POST /parse
{ "receipt_text": "Lunch $12.00" }
```

Response (HTTP 401):
```json
{ "error": "auth_failed", "detail": "Invalid or missing ANTHROPIC_API_KEY." }
```

Logs:
```
2026-06-11 [ERROR] Authentication error on attempt 1: Error code: 401 - {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}
```

---

**Test 6 — API error: rate limited (429)**

Request:
```
POST /parse
{ "receipt_text": "Lunch $12.00" }
```

Response (HTTP 429):
```json
{ "error": "rate_limited", "detail": "Error code: 429 - Rate limit exceeded. Please retry after 60 seconds." }
```

Logs:
```
2026-06-11 [WARNING] Rate limited on attempt 1: Error code: 429 - {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded..."}}
```

---

**Test 7 — API error: upstream overloaded (529)**

Request:
```
POST /parse
{ "receipt_text": "Lunch $12.00" }
```

Response (HTTP 502):
```json
{ "error": "api_error", "detail": "Overloaded. Please try again later." }
```

Logs:
```
2026-06-11 [ERROR] API error on attempt 1 (HTTP 529): Overloaded. Please try again later.
```

---

## Round 7 — Demo UI for browser testing

*Built as the demo artifact the submission form asks for — a way to exercise the parser by hand, separate from the core API work.*

**Prompt (verbatim):**
> create a simple, minimalistinc webui to test this reciept parser system on browser nothign fance just build me a HTML file with .CSS styling but it should look like a 2026 UI

**What the tool gave back:**
- Single `demo/index.html` file — no build step, no dependencies
- Dark theme, two-panel layout: receipt text input left, parsed results right
- Parsed items rendered as cards with colour-coded category pills (one colour per category)
- Status bar showing HTTP status code, item count, and response time
- 5 sample receipt chips to load test inputs instantly
- ⌘/Ctrl + Enter keyboard shortcut to submit

**Bug found and fixed:**
Browser blocked requests from `file://` to `localhost:8000` (CORS). Fixed by adding `CORSMiddleware` to `scaffold.py`.

**Security caveat (deliberate, documented):** the CORS config uses `allow_origins=["*"]`. That's fine for a local dev/demo where the front-end is a static file with no fixed origin, but it's *not* something I'd ship to production — there I'd scope `allow_origins` to the known front-end origin(s). Called out here and in `NOTES.md` so it's read as a conscious dev-only choice, not an oversight.
