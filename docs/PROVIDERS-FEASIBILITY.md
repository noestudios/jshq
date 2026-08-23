# Feasibility: non-Anthropic AI providers + per-task model selection

Investigation deliverable for the "Investigate non-Anthropic AI providers"
work item. Written 2026-08-22 from a full touchpoint audit of the codebase.
Line references drift with edits.

> **Tier 1 shipped 2026-08-22** (same day, owner green-light): `jshq/aicfg.py`
> now owns the task→model map, the two axes ride one `ai_models` settings row
> through `GET/PUT /api/settings/ai-models`, and Settings → System has the two
> selects. Section 1's touchpoint map describes the PRE-Tier-1 code.
>
> **Tier 2 shipped 2026-08-22** (owner green-light, same day): the
> OpenAI-compatible adapter is `jshq/oaicompat.py` (httpx, no OpenAI SDK),
> the roster/config is `jshq/providers.py` (base URL in the `ai_providers`
> settings row, key in `.env` as `JSHQ_OPENAI_COMPAT_API_KEY`), and each
> axis binds (provider, model) via `aicfg.binding_for`. The owner decisions:
> per-axis provider+model, one generic compat entry (no vendor presets),
> free-text model ids with local/unpriced ledger labels, scoring allowed on
> any model behind the uncalibrated note. Tier 3 stays not-built as
> recommended below — Gemini rides the compat endpoint.

## Verdict up front

Feasible, in three cleanly separable tiers. The architecture is friendlier to
this than expected: every AI feature already takes an **injected client** and
never constructs one, so there is a single seam to swap at. The hard parts are
not the transport — they are (a) the five schema-forced call sites, (b) the
Anthropic-priced usage ledger, and (c) the calibration story for scoring.

Recommended path: **Tier 1 (per-task model selection, Anthropic-only) → Tier 2
(OpenAI-compatible base-URL endpoints, which covers Ollama/LM Studio/llama.cpp
AND OpenAI itself) → stop and reassess before Tier 3 (native Gemini).** Tier 2's
local-endpoint case is the best privacy fit: nothing leaves the machine, so the
zero-phone-home invariant barely moves.

---

## 1. What exists today (the touchpoint map)

### The seam

Every feature module documents and honors "the client is injected by the
caller; this module never creates one":

| Feature | Entry point | Client construction it relies on |
|---|---|---|
| Bulk job scoring | `haiku.score_job(client, …)` — `scoring/haiku.py:472` | `scoring/__init__.py:656` (`max_retries=6`) |
| Draft composer | `compose.generate(client, …)` — `compose.py:289` | `get_compose_client()` — `main.py:2071` (`max_retries=2`) |
| AI-tells scrub | `refine.refine(client, …)` — `refine.py:93` | same dependency |
| Resume/cover tailor + chat | `tailor.generate/chat(client, …)` — `tailor.py:498/505` | same dependency |
| Learned-rule proposal | `learned.propose_rule(client, …)` — `learned.py:101` | same dependency |
| Roadmap synthesis | `synthesis.propose(client, …)` — `synthesis.py:485` | same dependency |
| Job-URL prefill | `jobparse.parse_job_url(url, client=None)` — `jobparse.py:199` | `jobparse.py:164` (`max_retries=4`) |
| Key liveness ping | `main.py:1339` | `main.py:1330` (`max_retries=0`) |

Tests already fake this seam in 8 files with
`SimpleNamespace(messages=SimpleNamespace(create=…))` — meaning the **de facto
provider interface** is small and proven:

- `client.messages.create(model, max_tokens, system, messages, [temperature],
  [thinking], [output_config])`
- response: `resp.content` = blocks where `b.type == "text"` → `b.text`;
  `resp.usage` with `input_tokens` / `output_tokens` /
  `cache_read_input_tokens` / `cache_creation_input_tokens` (`usage.py:51-56`)

Emulate that shape and every feature module ports untouched.

### Model ids (scattered, two duplicated)

- `compose.py:42` `"claude-sonnet-5"` — aliased by `refine.py:15`,
  `tailor.py:31`, `synthesis.py:29`; **duplicated as a literal** in
  `learned.py:29`.
- `haiku.py:28` `"claude-haiku-4-5"` — **duplicated as a literal** in
  `jobparse.py:21`; `main.py:1343` reaches for `jobparse.MODEL` for the ping.
- Price table `usage.py:19-21` and UI spend labels `settings.js:1120-1122` are
  two more independent maps keyed on the same strings.

### Keyless degradation (the invariant, per feature)

Scoring skips with `MISSING_MESSAGE` and records the skip; the six
`get_compose_client` endpoints 503 with the same message; jobparse silently
falls back to its no-key JSON-LD path; synthesis has a first-class keyless
transport (copy-prompt / paste-reply through the same validator). Onboarding
treats "declined" as a completed state. The whole suite runs keyless
(`conftest.py` deletes the env var).

---

## 2. The hard parts

1. **Schema-forced output (5 sites).** `haiku.py:501`, `synthesis.py:497`,
   `learned.py:113`, `refine.py:107`, `jobparse.py:181` all pass Anthropic's
   `output_config={"format": {"type": "json_schema", …}}`. The scoring schema is
   **dynamic** — `build_schema(criteria)` generates enums from the user's own
   criteria doc (`haiku.py:117-157`). `tests/test_output_schemas.py` pins an
   Anthropic-specific constraint (structural JSON-Schema keywords only; range
   checks live in each module's validator). OpenAI's equivalent is
   `response_format={"type": "json_schema", …}` with its own strictness rules
   (`additionalProperties: false` required, different keyword support); Ollama
   takes a `format` JSON schema; Gemini's `response_schema` is an OpenAPI-style
   subset. Each provider needs its own schema translation + constraint audit.
   By contrast `compose` and `tailor` are plain text (tailor hand-rolls its
   JSON contract with a conversational retry) — they port for free.

2. **The usage ledger assumes Anthropic.** `usage.py` hardcodes cache-pricing
   multipliers (`:32-33`), a Sonnet-5 intro-pricing date branch (`:59-66`), and
   — the sharp edge — **an unknown model id records tokens at $0.00
   silently** (`:66-72`). Any second provider must either extend `PRICES` or
   the ledger must say "unpriced" out loud. Cost-per-job estimates
   (`main.py:1824-1828`) key on `haiku.MODEL` specifically.

3. **Calibration is provider-pinned by design.** `scripts/calibrate_scoring.py`
   is a model-drift sentinel keyed on `haiku.MODEL`; changing the scoring
   model or provider invalidates the baseline deliberately. A non-Anthropic
   scoring model needs its own blessed baseline (a paid live run) or an
   explicit "uncalibrated — results vary" state surfaced in Settings.

4. **Anthropic-shaped plumbing.** The `THINKING={"type":"disabled"}` Sonnet-5
   workaround rides 5 call sites (`compose.py:45-52`) and is meaningless
   elsewhere — it must become per-provider. `SCORE_CONCURRENCY = 2` is sized to
   Anthropic Tier-1 rate limits (`scoring/__init__.py:27-30`); a local endpoint
   wants a different number. Rate-limit detection duck-types the Anthropic SDK
   (`scoring/__init__.py:504-510`). All SDK imports are lazy (the app must run
   without the package installed) — a second SDK inherits that rule, which is
   an argument for reusing the OpenAI wire format over adding SDKs.

5. **Tests and docs that pin Anthropic literals.**
   `tests/test_settings_frontend.py` asserts `"api.anthropic.com"` and
   `"claude-sonnet-5"` appear in the JS; CLAUDE.md's zero-phone-home invariant
   and PRIVACY.md's outbound inventory name `api.anthropic.com` as the only
   AI destination. Stored provenance (`tailorings.model`, compose activity
   JSON, synthesis proposals) will carry old ids across any migration —
   display code must tolerate unknown ids (it already does: label maps fall
   back to the raw id).

---

## 3. The tiers

### Tier 1 — per-task model selection, Anthropic-only (low effort)

The "per-task model" sub-item, buildable without touching providers:

- New module (e.g. `jshq/aicfg.py`): resolves `{task → model}` for the two
  axes the owner named — **analysis** (scoring/synthesis/jobparse/learned) and
  **generative** (compose/tailor/refine) — from a `settings` row, falling back
  to today's constants. Replaces the 7 scattered `MODEL` constants (and fixes
  the two duplicated literals).
- Settings → System control: two selects (analysis model / writing model) over
  a curated Anthropic list; unknown-model guard in `usage.rate_for` becomes a
  visible "unpriced" instead of silent $0.00.
- Invariants untouched (still only `api.anthropic.com`). Calibration note in
  the UI when the scoring model differs from the blessed baseline's.
- Rough size: a focused session. No PRIVACY/CLAUDE.md changes.

### Tier 2 — OpenAI-compatible endpoints (medium effort)

One wire format buys OpenAI **and** every serious local runtime (Ollama,
LM Studio, llama.cpp server, vLLM):

- A thin client adapter implementing the de facto interface above
  (`messages.create` → chat-completions request; response → text blocks +
  usage fields). Prefer speaking the wire format over `httpx` (already a
  dependency) rather than adding the OpenAI SDK — keeps the lazy-import rule
  moot.
- Provider config UX: provider select + adaptive right-hand
  field (API key for hosted, base URL for local). Extends `apikey.py`'s
  storage pattern (`.env` writer, status/masking, per-provider env names) —
  the "graceful without a key" invariant becomes "graceful without a
  credential *or* endpoint," per task.
- Schema translation for the 5 structured sites (JSON-schema dialect audit per
  runtime; local models may need the tailor-style corrective-retry fallback
  when a runtime's schema support is weak).
- Ledger: per-provider pricing (local = $0 but *labeled* local, not silently
  unpriced); spend labels keyed by provider+model.
- **Policy edits ship in the same change**: CLAUDE.md invariant widens to
  "user-configured AI endpoint," PRIVACY.md documents exactly what rides an
  AI call (job text, criteria excerpt, resume content) so pointing it at a
  third party is informed. The local case sends nothing off-device.
- Scoring quality gate: run the calibration harness against the chosen model
  before enabling scoring on it, or surface the uncalibrated state.
- Rough size: several sessions; the schema audit is the long pole.

### Tier 3 — native Gemini (high effort, weakest case)

A third wire format, a third schema dialect (OpenAPI-subset), a third
credential shape, for a provider most of whose value Tier 2 already reaches
(Google exposes an OpenAI-compatible endpoint for Gemini). **Recommendation:
don't build natively; cover Gemini via Tier 2's compatible endpoint and
revisit only on demand.**

## 4. Decision checklist for the owner

- [ ] Green-light Tier 1 now? (self-contained, no policy changes)
- [ ] For Tier 2: comfortable widening the zero-phone-home invariant to
      user-configured endpoints (with PRIVACY.md carrying the full story)?
- [ ] Scoring on non-Anthropic models: require a blessed calibration baseline
      per model, or ship with an explicit "uncalibrated" banner?
- [ ] Accept "Gemini via its OpenAI-compatible endpoint" instead of a native
      adapter?
