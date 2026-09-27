# Ground Truth: breaking-change-demo branch

## Seeded change
File: booking_system_backend/server.py
Function: create_hold (POST /quotes/{quote_id}/holds)
Change: replaced `except httpx.HTTPError: return {"error": ...}` (always HTTP 200)
        with `raise HTTPException(status_code=..., detail=...)` (real status codes)

## Consumers that should be flagged

| # | File:Line | Consumer | Test coverage | Expected verdict | Why |
|---|-----------|----------|---------------|-------------------|-----|
| 1 | e2e/test_holds.py:81-90 | `test_hold_on_unknown_quote_returns_error` | Covered — will fail loudly | BREAKS | Asserts `"error" in body`; new body uses FastAPI's default `"detail"` key instead |
| 2 | booking_system_frontend/src/services/api.ts:161 | `assertNotProxyError()`, used by `createHold()` | **Uncovered — no frontend tests exist** | LATENT / DEFEATED SAFETY NET | `createHold` still throws on failure — Axios rejects non-2xx before `assertNotProxyError` is ever reached. But the thrown value is now a plain `{"detail": "..."}` object (from the interceptor at line 27) instead of the `Error` instance that `assertNotProxyError` would have constructed. The current catch site in `BookingModal.tsx:191` is unaffected today because its `toast.error(...)` message is hardcoded. However, `assertNotProxyError`'s guard is now dead code specifically for the `createHold` path: the 200-OK error shape it was designed to detect no longer occurs. Any future code added to that catch block that reads `err.message` will silently receive `undefined` because the caught value has no Error prototype. |
| 3 | booking_system_backend/server.py:336 (`FastApiMCP(app)`) | Auto-generated MCP tool for `create_hold`, used by any AI agent (including Bob) | Uncovered — no MCP-layer tests | INVESTIGATE | Error now surfaces as a raised exception instead of a normal-looking dict; needs checking whether fastapi-mcp translates this into a proper tool error or something worse |

## Expected tool output
A correct detector run against this diff should flag exactly these three call sites,
correctly separating #1 (loud/CI-caught) from #2 and #3 (silent/uncaught) — 
that separation is the actual value proposition of the tool.