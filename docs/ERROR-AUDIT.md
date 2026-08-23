# Error-message audit — human-readable errors + error codes

Audit-only deliverable for the "Human-readable error
messages" work item (2026-08-22). **No code changed.** Three
sweeps: every backend error exit, every frontend error surface, and the
constraint map (test pins, prose-parsing sites, conventions). Line references
drift with edits; the message texts are the durable identifiers.

## The shape of the problem

- **101 backend HTTP error exits** (97 `raise HTTPException` in `main.py` + 4
  via the `_resume_content_error` helper). Verdicts: **47 GOOD** (plain
  language), **25 MIXED** (readable but carrying an internal token/id),
  **25 TECH** (machine-shaped). Status spread: 404×39, 422×21, 409×12, 400×11,
  502×9, 503×2, 500×2, 413×1.
- **32 of 101 details embed a raw exception** (`str(exc)` / `f"…: {exc}"`) —
  the user can see Python reprs, JSON-decoder positions, absolute server
  paths (`render.py:321`), full board URLs, and exception class names.
- **The frontend has no translation layer.** `ApiError` sets
  `message = detail`, so the ubiquitous `error.detail || error.message` idiom
  is a raw pass-through with a dead second operand: **62 toast sites**, the
  whole-pane `renderLoadError` (8 mount handlers), 5 inline settings states,
  and the wizard's 6 `errMsg()` sites all render server text verbatim.
- **No `RequestValidationError` handler exists.** All ~40 body-taking routes
  fall through to FastAPI's default 422 array, which `api.js` flattens to
  `body.tier1_params.comp_floor: Input should be a valid integer` — Pydantic
  paths and Pydantic English in a toast (one route even toasts a regex, from
  `due_time`'s `pattern=`).
- **Three error-class corpora dominate the TECH count**: `CriteriaError`
  (~70 distinct texts, ~63 block/JSON/fence jargon — reachable from 12 HTTP
  sites plus the persisted scoring-skip line), `SynthesisError` (26 texts;
  the paste-back validator's 24 are all JSON-shape jargon), `ResumeError`
  (25 texts; 22 TECH). The AI 502 family wraps these into **triple-nested**
  toasts: `synthesis failed: unusable model output after retry: reply is not
  valid JSON: Expecting ',' delimiter: line 4 column 9 (char 87)`.

### Two structural bright spots (reuse, don't reinvent)

- **Friendly copy already wins wherever prose is computed from *state* rather
  than a thrown exception**: Today's `banners()`, Companies' ATS categories +
  `failReason()`, the wizard's inline `s.errors` machinery, Settings'
  `parseCriteriaError` field mapping, the API-key test verdicts. The gap
  aligns almost perfectly with the `try/catch` boundary.
- **Structured details already exist and flow**: the add-company /
  add-job 409s ship `{message, …ids}` through `error.info` into real recovery
  flows. A `code` field rides the same rails for free.

## The error-code proposal

The owner's ask: messages become human-readable, with a stable code appended
so any report targets the exact site — the *shape* `"…helpful sentence…
[error code: 018]"`.

**Recommended scheme** (proposal only — confirm before building):

1. **Format `[JSHQ-###]`**, appended to the end of the human sentence:
   `"Couldn't save that — keep the role under 120 characters. [JSHQ-312]"`.
   Short, greppable, unambiguous in a screenshot or a friend's paraphrase.
2. **One registry module** (`src/jshq/errors.py`): every code declared once —
   `code, default_message, note` — and raised through a tiny helper
   (`http_error(412 → HTTPException)`), so numbers can never collide or
   drift. Codes are **append-only**: a retired message keeps its number
   reserved. The registry doubles as the doc source (a future user-manual
   appendix can be generated from it).
3. **Block allocation** by area so a bare number hints at the subsystem:
   `0xx` transport/generic (frontend-authored) · `1xx` companies/jobs/apps ·
   `2xx` settings/key/uploads · `3xx` criteria & scoring · `4xx` synthesis ·
   `5xx` tailor/resume/render · `6xx` ATS detect/refresh · `7xx` backup ·
   `8xx` wizard/onboarding · `9xx` reserved.
4. **Frontend contract**: a shared `humanizeApiError(error, fallback)` in
   `lib/` extracts the trailing `[JSHQ-###]` and can (a) look up an even
   friendlier local mapping by code, (b) keep the code visible in the toast
   (small, at the end), and (c) let inline mappers like `parseCriteriaError`
   match on **code instead of prose** — killing the fragile
   `text.includes("comp_floor")` class of parsers (sites P1/P2 below).
5. **Tests migrate to codes**: `assert "[JSHQ-118]" in detail` replaces
   substring-pinning of prose. This *unfreezes* every pinned message: today
   31 detail assertions + 57 `pytest.raises(match=…)` sites pin exact prose,
   so wording can't improve without test churn. Codes decouple identity from
   wording permanently.
6. **What codes do NOT go on**: the `ats_last_status` column keeps its
   existing `ok:/none:/error:` prefix scheme (it is already a de-facto code,
   parsed at 7 frontend sites + 2 backend sites); frontend-authored toasts
   ("Name can't be empty") don't need codes — they name the control already.
   Persisted report strings (backup detail, scoring skip) SHOULD carry codes:
   they surface in banners long after the fact, when a code is most useful.
7. **Length/format constraints** (from the sweep): toasts are plain
   `textContent`, error-toast dwell is 6s — keep sentences short; macOS
   notifications truncate at 200 chars, `ats_last_status` at 300,
   `failReason()` at 117 — a trailing code would be truncated there, so for
   truncating channels the code goes FIRST or the truncation must preserve
   the tail. No i18n exists (all literals inline, `lang="en"`); a registry
   is also the natural seam if translation ever matters.

## Findings, prioritized

### F1 — The seven AI 502s leak raw SDK/exception text (TECH, high traffic)

`draft generation failed: {exc}` · `refine failed: {exc}` · `rule proposal
failed: {exc}` · `title suggestion failed: {exc}` · `synthesis failed: {exc}`
· `tailoring generation failed: {exc}` · `chat turn failed: {exc}` — all
catch bare `Exception` (anthropic is lazily imported), so an
`APIStatusError` repr with a JSON body lands in a toast. **Cheapest big win:**
`api.js` already has good generic 502/503 copy that is suppressed *only
because* these routes supply a detail; replacing `{exc}` with a stable
sentence + code (keep the raw text server-side in the log) activates it.
Note `tests/test_compose.py:287` and `test_synthesis_api.py:75` pin the
prefixes — migrate those pins to codes.

### F2 — No validation-error handler (TECH, every form)

One `@app.exception_handler(RequestValidationError)` converts the single
largest TECH surface (all ~40 body routes) in one place: translate
`loc`/`msg` into "field label: plain sentence [code]" using a loc→label map
(Settings' `CRIT_LABELS` is the seed). Also fixes the `models.py`
validator messages that currently arrive wrapped in `body.…` paths, and the
`due_time` regex-in-a-toast.

### F3 — `CriteriaError` corpus (~70 texts, 12 HTTP sites + persisted)

The block/JSON/fence jargon class (`no ```json tier1_params``` block…`,
`taxonomy[key] must be snake_case…`, `persona['domain_label'] must be…`).
Two-tier fix when picked up: (a) the ~10 messages reachable from normal UI
flows (tier1 saves, persona, discipline, rules) get human rewrites + codes;
(b) the long tail (hand-edited-doc validation) keeps precise technical
detail — the audience IS someone hand-editing the doc — but gains codes and
loses raw `{exc}` nesting. The `criteria error: {exc}` prefix (4 sites) is a
log line, not a sentence. Note: `main.py:626` puts an **absolute server
path** in a toast; `render.py:321` likewise.

### F4 — The prose-parsing sites (where codes pay for themselves first)

- **P1** `settings.js parseCriteriaError` — `text.includes(<tier1 key>)` + four
  regexes over `criteria.py:645/647` prose. Rewording those messages today
  silently degrades the inline field mapping. Code-match instead.
- **P2** `settings.js parseRulesError` — regex over a Pydantic-array-flattened
  message from `models.py:214`.
- **P4/P5** `_CONNECTIVITY_MARKERS` — a hand-duplicated Python/JS pair of
  substring lists over adapter-error text ("If you edit one list, edit
  both" — no test guards the match). A structured `offline` reason token (it
  already exists in `last_refresh_error.reason`) or a shared code would
  retire the duplication.
- **P6/P7** the `"error: "[7:]` slice and seven `startsWith("error:")` sites —
  fine as-is (the prefix IS the code); document it as such.

### F5 — Frontend surfaces to route through one humanizer

Replace the 62 raw toast sites + `renderLoadError` + `errMsg()` with a shared
`humanizeApiError(error, fallback)` (status-aware defaults for 0/401/403/404/
409/422/429/5xx; code extraction; `failReason()` is the in-repo model).
Specific bugs found in the sweep, fixable in the same pass:
- `applications.js:1188/1201` — upload/delete failures toast **without**
  `{error: true}`: failures render success-styled.
- The upload path's parallel error constructor ignores array/object details →
  `"422 Unprocessable Entity"` toasts; no timeout either.
- A 200 with a non-JSON body throws a raw `SyntaxError` whose engine text
  reaches the toast.
- `today.js:583` — the backup banner appends `backup.detail` raw
  (`integrity_check failed`, `row count mismatch: jobs, companies`).
- `settings.js:1008` — the synthesis paste-back inline error is fully raw
  (the 24-message JSON-jargon corpus lands here).

### F6 — Silent failures that hide actionable errors (9 + 3 pollers)

Documents/Tailoring/Tailor-chat sections stuck on "Loading…" forever
(`applications.js:551/565/578`); the debounced cover-letter autosave
`.catch(() => {})` (`:1257` — possible silent data loss); the onboarding
tracker's dropped dismiss PUT; the wizard's two dropped
`linkedin_title_defaults` PUTs; jobs' dismiss-reasons collapse to
`["other"]`; the careers probe mapping a *network* failure to "No job board
found automatically"; and the three refresh/rescore pollers that
self-terminate on any error, leaving a stale progress bar that looks hung.
These need a surface (inline retry line or tracked-state banner à la
`criteriaLoadFailed`), not just better prose.

### F7 — Assorted MIXED details worth a rewrite when touched

DB ids in toasts (`application 7 already exists…`, `tailoring 12 is already
pending…`, `company 3 does not exist`); `{entity_type} {entity_id} does not
exist`; byte counts (`max 200000 bytes`); `!r` Python-repr quoting in the
geocode 404; `"(stale?)"` developer hedges; `"integrity_check failed"`;
`"ids must be comma-separated integers"`; the upload filename rule
(`"filename must be a plain name ending in …"` — most jargon-heavy
user-reachable string); "JD"/"adapter"/"connectable ATS" insider vocabulary;
the two API-key inconsistencies (`main.py:1326` says `"no API key
configured"` where its sibling uses the actionable `MISSING_MESSAGE`;
`apikey.py` ValueErrors say "single token").

### F8 — What's already GOOD (leave alone, pattern-match against)

`MISSING_MESSAGE` and `NO_CRITERIA_MESSAGE` (the two centralized constants);
"no website or careers URL to check — add one first"; the 409 duplicate
flows; "file is in use — close it in your viewer and retry"; the synthesis
stale-draft 409; jobparse's `JobParseError` (docstring: "A user-facing
reason" — the only class *written as* user copy); the `_resume_content_error`
wrapper's pointer at Help (its embedded `{exc}` is the TECH half).

## Constraints for the fix pass (measured, not guessed)

- **Blast radius of rewording**: 31 HTTP-detail test assertions + 57
  `pytest.raises(match=…)` sites + the `ats_last_status` exact-equality pins
  (`test_companies.py`, `test_refresh.py`) + backup-detail pins
  (`test_backup_verify.py`) + notification-text pins (`test_notify.py`,
  `test_refresh.py:676`). The audit's per-message table lives in the agent
  sweep; the registry migration should move these to code-matching as each
  message is touched.
- **Do not reword blind**: P1/P2 (above) parse prose; the connectivity
  markers (P4/P5) parse adapter text; `refresh.py` slices the `error: `
  prefix.
- **`user-manual.md`** has no troubleshooting section yet and its docs are
  under the em-dash lint (`test_docs_no_ai_tells.py`) — an error-code
  appendix must be written em-dash-free even though the runtime strings
  themselves use em dashes freely.
- **No custom exception handlers exist today** — adding the validation
  handler (F2) is greenfield, nothing to migrate.

## Suggested fix waves

1. **Wave 1 — SHIPPED (2026-08-22):** the registry +
   `[JSHQ-###]` helper (`src/jshq/errors.py`); F1 (seven 502s stop leaking
   `{exc}`; the two PDF-render 502s deliberately survive as F3/Wave-2 scope,
   pinned shrink-only in `tests/test_errors.py`); F2 (validation handler —
   value_error prose passes through so P2 keeps matching); frontend
   `humanizeApiError`/`errorCode` in `js/lib/errors.js` + the two wrong-tone
   toasts and the upload error constructor (which also gained the 300s
   timeout and structured-detail parsing; the 200-non-JSON SyntaxError leak
   fixed in the same pass). Kills the worst leaks and establishes the scheme.
2. **Wave 2 — SHIPPED (2026-08-22):** F4 both parsers retired —
   P1 via structured `{message, field, kind}` on the criteria PUT
   (CriteriaError carries field/kind from the tier1 checks), P2 via the
   validator message becoming the coded human sentence itself ([JSHQ-202];
   the 422 handler never stacks [JSHQ-001] on a coded sentence, and
   `errorCodes()` in lib/errors.js reads codes anywhere in a detail).
   Rewritten + coded: criteria/persona/discipline 422 boundaries
   (302/303/304, "criteria error:" prefixes dropped), company/job/entity
   gone (101/102/104), structured application-exists / tailoring-pending
   409s (103/505), job-no-description minus "JD" (506), geocode 404 minus
   the `!r` repr (305), upload filename (203), voice-guide cap in KB (204),
   the "(stale?)" hedges (306/307), and the key-test 503 now uses
   `apikey.MISSING_MESSAGE`. Pins for all of those migrated to codes.
3. **Wave 3 — SHIPPED (2026-08-22):** the manual
   appendix is generated from the registry (`errors.manual_appendix()` +
   `scripts/gen_error_appendix.py`, shipped section drift-pinned by test;
   notes are the copy, so it stays em-dash-clean under the docs lint). F6:
   the three refresh/rescore pollers (plus app.js's load-time watcher)
   tolerate two failed ticks and surface their give-up; the Applications
   Documents/Tailoring/chat sections render an inline retry line instead of
   eternal "Loading…"; the cover-letter autosave toasts once per failure
   streak; the tracker ✕ reverts and explains a failed persist; the wizard's
   title-seed writes and careers probe say when they fail (probe failure is
   its own retryable state, not "no board found"); the jobs dismiss-reasons
   fallback stops caching itself and says why. F7 leftovers: the backup
   banner translates the persisted verify detail. Deliberately skipped: the
   ids-param 400 on the ICS batch route (not reachable from the UI).
