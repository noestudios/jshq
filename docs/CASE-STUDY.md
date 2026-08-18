# Job Search HQ: a case study

Job Search HQ (jshq) is a local-first job-search dashboard: you pick the
companies you care about, it pulls their openings straight from the
applicant-tracking systems that run their careers pages, and it scores every
posting against fit criteria **you** write. No accounts, no server, no
tracking: one process on `127.0.0.1`, your data in a SQLite file you own.

I built it, and I'm writing this, as a UX leader making the case for a Director
or Head of UX role. The decisions worth a hiring manager's time here are not
features. They live in behavior, in the setup flow, and in code comments, where
a fast reader misses them: how a setup wizard earns its shape, a token
architecture that catches its own gaps, a scoring model that is really a
document you author, and a color system measured for color-blind safety. This
document brings those forward, then names the things I deliberately chose
**not** to build.

---

## Walkthrough: the product in three shots

> _Captured from a live instance. See the [README](../README.md) for how to run
> your own._

### 1. Setup is the application

<!-- SHOT: welcome/wizard -->
![First-run wizard: the welcome step](images/walkthrough-wizard.png)

A fresh install boots straight into a wizard that **is** the whole app: while
first-run, every route resolves to the welcome flow and the normal nav, stats,
and tracker are hidden. There is no empty dashboard to bounce off of. The very
first screen leads with the privacy promise, because the target user is often
someone protective and tired, and the only thing that will keep them in the
flow is knowing their data stays theirs.

### 2. The scoring reader: the one surface nothing else does

<!-- SHOT: scored posting + why -->
![A scored posting with its per-criterion reasoning](images/walkthrough-scoring.png)

This is the differentiated surface. Every posting is scored against the
wish-list criteria you ranked during setup, and the score is **shown its
work**: each of your criteria is mapped to evidence from the actual posting.
The reasoning is visible end to end. If you disagree, you can see exactly which
criterion drove the call and rewrite it.

### 3. The tracker never lies about progress

<!-- SHOT: onboarding tracker pill -->
![The persistent Setup N/N completeness tracker](images/walkthrough-tracker.png)

Setup is optional and resumable, so a persistent "Setup N of N" pill tracks
real completeness, fed verbatim by the backend's own count so a client-side
recount can't drift it. There is no fake progress bar, no "you're 80% done!"
that means nothing. When a step is done, it is done; when a board can't be
reached, the app says so plainly instead of spinning.

---

## How the setup flow got its shape

Three decisions shaped the wizard, and none of them show up as a feature. They
are the design work a screener can't see from the running app.

**Setup is the whole app, on purpose.** During first run, every route resolves
to the welcome flow and the normal nav, stats, and tracker stay hidden. The
alternative, a normal dashboard with an onboarding banner on top, hands a
first-time user an empty shell to bounce off of before they've added anything.
A protective, tired job-seeker who lands on an empty dashboard leaves. So the
first run has exactly one job, and the app refuses to show anything else until
it has enough to be useful.

**Finishing requires one real company.** Setup is optional and resumable, but
it cannot complete on nothing. Scoring needs at least one company with real
postings, or the one differentiated surface has nothing to render. The choice
was between letting someone "finish" into an empty app, tidy completion and a
useless result, and holding the gate until there is something to score. I held
the gate. The reflection exercises come first so the criteria exist, then at
least one company so there is something to apply them to.

**Continue confirms with the backend before it advances.** The completeness
pill has to tell the truth, which means the client can't advance a step on
optimism and let its own count drift from what the backend actually recorded.
Continue waits for the backend to confirm the step landed, then advances
against the backend's count, so the pill and the flow read from the same
authoritative number. "Setup 3 of 5" is never a client-side guess. It's a
correctness contract, and the pill's honesty rides on it.

---

## Decision 1: Token + theme architecture that catches its own gaps

**The problem.** A two-theme app (dark and light) rots one color at a time.
Someone adds a status hue in dark, forgets the light override, and the
regression only surfaces when a light-mode user files a bug months later.

**The decision.** [`tokens.css`](../src/jshq/frontend/css/tokens.css) is the
single source of truth: every color, font, scale, and radius lives there, and
`app.css` only ever references `var(--t-…)`. The file is authored **dark-first**:
`:root` holds the dark theme (design decisions target dark first), and
`html[data-theme="light"]` at the bottom overrides every color literal **in the
same section order**. That ordering is the whole trick: a side-by-side diff of
the two blocks makes a missing override visually obvious. The structure does the
QA that a human reviewer would otherwise have to do by hand, every time.

There are two tiers: primitives (raw scales) and semantic aliases (what a thing
*is*: `--t-radius-control`, `--t-fit-high-bg`). Diverging one family is a
one-line change to its alias. Tokens defined as `var()` aliases re-resolve per
theme for free, though the comments record the two cases where a re-resolved
value failed a contrast measurement and had to pick up a light-specific literal
anyway. The system has escape hatches, and they are documented where they are
used.

**Why it's a leadership signal.** The method encodes a maintenance discipline
into the file's structure so the discipline survives the author. That is a
systems decision.

---

## Decision 2: The wish list is the weighting

**The problem.** Most job tools hand you filters and a set of importance dials,
then trust that a slider labeled "salary: important" means something. It
doesn't. A weighting UI invites people to mark everything important, which
weights nothing, and it splits the act of deciding what matters from the act of
scoring against it.

**The decision.** Onboarding doesn't hand you a form. It runs two reflection
exercises adapted from career-coaching work by
[Kristin Chen](https://www.kristinmchen.com): a ranked wish list and a
fulfillment matrix. You rank what you want from a role in strict order, and that
rank order *is* the weight the scorer uses. There is no separate importance dial
to contradict the ranking, because forcing a strict order is what makes people
actually choose. The exercises produce the criteria document Decision 3 reads
and executes, so the design-thinking work and the systems work meet in the same
artifact.

**Why it's a leadership signal.** The most original move in the product is a
design-thinking one, borrowed on purpose and credited in the open. Turning a
reflection exercise into the app's scoring weights, rather than shipping a
settings screen and hoping, is a decision made before any code about what the
product asks of a person and what it does with the answer.

---

## Decision 3: Scoring behavior is user-authored config

**The problem.** "Relevance" scoring that lives in code is a black box. The user
can't see why a job scored the way it did, can't tune it, and has to trust a
model they can't inspect.

**The decision.** The single source of truth for scoring is a Markdown document
the user writes. A fresh install seeds a neutral starter; the live copy the app
reads and writes is `DATA_DIR/fit_criteria.md`. This
[worked example](../src/jshq/defaults/fit_criteria.md) shows a fully authored
one. Hard filters (comp floor, location radius, excluded sectors, seniority) and
weighted, ranked criteria in the user's own words live in that doc, inside fenced
machine blocks. The code reads them: the AI request schema is **built from the
doc** (`build_schema(criteria)` in
[`scoring/haiku.py`](../src/jshq/scoring/haiku.py)), and the taxonomy,
level bands, score scale, and persona are all declared in the doc as well.

The invariant, enforced by the project's own rules and a golden-prompt snapshot
test: **behavior changes go in the criteria doc, and code constants never encode
scoring policy.** A doc that declares nothing falls back to sensible defaults; a
malformed block fails loudly rather than silently mis-scoring. The score scale
itself is *derived* from the number and weight of the criteria the user wrote, so
the number means the same thing whether someone ranks five criteria or fifteen.

**Why it's a leadership signal.** It reframes the product's core from "our
scoring model" to "your criteria, executed faithfully." That is an
extensibility and trust decision made at the architecture level.

---

## Decision 4: A color-blind-safe state model, measured

**The problem.** The app uses color to encode state: job fit, application
status, urgency. Status dots are 4×4px. At that size, hue barely registers for
anyone, and for a deuteranope (red-green color blindness, ~8% of men) two hues
on the red-green axis collapse to the *same* grey. A status system that
separates only by hue is, for those users, no system at all.

**The decision.** Every state family sits on a monotonic **lightness ladder**,
so hue never has to carry the distinction. The status dots run
`closed 34 · prospect 48 · applied 56 · outreach 62 · interviewing 68 · offer
74 · targeting 80` in CIE L\*, and every adjacent pair is required to clear two
gates: **ΔE2000 ≥ 3 under simulated deuteranopia** and **ΔL\* ≥ 3**. The fit-score
bands run their own ladder, ascending in dark and descending in light.
*Direction is semantic*, because contrast against the page has to rise with the
band in both themes.

A script enforces this.
[`scripts/simulate_palette.py`](../scripts/simulate_palette.py) runs the
deuteranopia simulation and reports the worst-case ΔE and ΔL\* across the
palette. The comments in `tokens.css` carry the actual measured numbers next to
each value, and note the CIEDE2000 subtlety that near-white pairs need *more*
lightness travel for the same ΔE. Re-simulate if any value moves.

**Why it's a leadership signal.** Accessibility here is a measured engineering
constraint with a tool that enforces it. The color policy is also a restraint
decision: **color encodes state or urgency only.** Categorical metadata (remote
type, source, monograms) all shares one neutral treatment, so color never gets
diluted into decoration.

---

## What I chose not to build

Craft leadership often shows in what's *absent*. These were deliberate
refusals:

- **Framework-free by choice.** The frontend is plain ES modules
  ([no build step, no framework](../src/jshq/frontend/js/)), un-hashed and
  served with `Cache-Control: no-cache` on purpose. For a single-user
  localhost app, a framework would add a toolchain and a dependency surface to
  maintain in exchange for nothing the app needs.
- **No phone-home, ever.** No telemetry, crash reporting, update checks, or CDN
  assets. The *only* permitted outbound calls are ATS job boards (with an
  honest User-Agent, respecting robots.txt), a per-company logo lookup, and the
  Anthropic API with the user's own key. [`PRIVACY.md`](../PRIVACY.md) is the
  complete inventory, and adding any new call means updating it.

---

## The through-line

Local-first, honest about its limits, and built so its own rules are hard to
break by accident. A wizard that refuses to fake progress, a wish list whose
rank order is the scoring weight, a token file whose structure surfaces its own
theme gaps, and a color system with a simulator guarding it. The product is a
job-search tool. The case study is about building software that keeps its
promises after the author has moved on.
