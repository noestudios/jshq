# Fit Criteria — Alex Rivera (example persona: design leadership)

> An example criteria document for a fictional design-leadership candidate.
> **The scoring prompt and the Tier 1 filter parameters are generated/parsed
> from this file — change behavior here, not in code.** Everything outside the
> fenced `json` machine blocks is sent verbatim to the scorer as context.
> Review and edit every section for your own search.

<!--
Persona (machine-read). Guidance lives in this HTML comment rather than in the
prose because everything outside the machine blocks is sent to the scorer, and
these notes are addressed to you, not to it.

  display_name  The only part of this document that names a person. It is sent
                to the model on every scoring, compose, and tailoring call. Set
                it to your own name, or to null to stay anonymous.
  domain_label  The short phrase describing the kind of role you are searching
                for. It opens the scoring prompt, so keep it to a few words.
-->

```json persona
{
  "display_name": "Alex Rivera",
  "domain_label": "design-leadership"
}
```

## Tier 1 — Binary filters (machine-checked, no AI)

These run in code before any AI scoring. A hard fail stops the pipeline; the
job is stored with `fit_score = 0` and the failed filter named in the notes.
"Unknown" is never a fail — it becomes a near-miss flag instead.

1. **Compensation.** Hard floor **$160,000** stated max (the wishlist target
   is $185K+ base). Stated max below $160k → fail. Stated max in $160–185k →
   pass with near-miss flag `comp_below_target`. Salary not stated →
   `unknown` + flag `comp_unknown`, never a fail.
2. **Location.**
   - **Hybrid/onsite:** the job location must match the allowlist of towns
     within ~30 minutes of Evanston, IL (below), **or** fall within the
     `location_radius` (a center + `radius_minutes` drive-time threshold that
     augments — never replaces — the allowlist; commute time is a measured
     per-town drive-time where known, otherwise an offline estimate from
     straight-line distance × `detour_factor` ÷ `avg_mph`), **or** match that
     company's entries in `company_location_overrides` (e.g. Toronto and
     London are acceptable for Meridian Loom). Anywhere else — Sydney,
     Bangalore, San Jose — → fail.
   - **Remote:** passes only when the posting is US-scoped or unscoped —
     the location names the US, any US state (by full name, USPS code, or
     common short form — recognised in `tier1.py`, gated on a US/broader
     marker being present here, so the 50 states aren't listed below), a
     `remote_regions` marker (anywhere, global, worldwide…), an allowlisted
     town, or no region at all (bare "Remote"). Region-restricted remote
     elsewhere ("Remote Spain", a Sydney-based remote role) → fail.
   - No location string → `unknown` + flag `location_unknown`.
3. **Sector.** No gambling, no surveillance. Inherited from the company record
   (`companies.sector_flags`); a company flagged with an excluded sector fails
   every job it posts.
4. **Title band.** Target: Director / Head of Design / Senior Director, plus
   VP at a small org. Manager / Sr Manager / IC titles → flag `below_band`,
   not fail. VP/CDO titles → flag `scope_gap`, not fail — org size isn't
   machine-knowable, so the scorer judges from the JD whether it's
   "VP at small org" (in band) or "VP at large org" (scope gap).
   Intern/junior/associate titles carry their own `junior` band —
   flagged `below_band` and hard-capped in the score-caps table, tighter
   than the IC cap.

Two wishlist Tier 1 items are **not** machine-checkable and are handled by the
scorer instead: the values screen (helps rather than harms people, environment,
democracy) and "real design org with budget."

### Machine-readable parameters

Parsed by `backend/app/scoring/criteria.py`. Edit values freely; keep the keys
and JSON valid. The allowlist is a seed list — review and edit for your own
search.

```json tier1_params
{
  "comp_floor": 160000,
  "comp_target": 185000,
  "location_allowlist": [
    "evanston",
    "chicago",
    "skokie",
    "wilmette",
    "glenview",
    "morton grove",
    "niles",
    "park ridge",
    "des plaines",
    "northbrook",
    "highland park",
    "oak park",
    "lincolnwood",
    "winnetka",
    "kenilworth",
    "glencoe",
    "rogers park",
    "cook county"
  ],
  "company_location_overrides": {
    "Meridian Loom": [
      "toronto",
      "london"
    ],
    "Saltmarsh Labs": [
      "boston"
    ]
  },
  "location_radius": {
    "center": {
      "lat": 42.0451,
      "lng": -87.6877,
      "label": "Evanston, IL"
    },
    "radius_minutes": 45,
    "estimate": {
      "detour_factor": 1.4,
      "avg_mph": 33
    }
  },
  "remote_regions": [
    "united states",
    "us",
    "usa",
    "anywhere",
    "global",
    "worldwide",
    "americas",
    "north america"
  ],
  "excluded_sectors": [
    "gambling",
    "surveillance"
  ],
  "target_title_bands": [
    "director",
    "senior_director",
    "senior_manager"
  ],
  "flag_title_bands": {
    "manager": "below_band",
    "ic": "below_band",
    "junior": "below_band",
    "vp_plus": "scope_gap"
  }
}
```

<!--
Level bands (machine-read). The seniority ladder, matched against the job TITLE
before any AI runs. The band names above in target_title_bands and
flag_title_bands must be ones this block can produce; a name nothing emits is a
filter that silently matches nothing, so the loader refuses it.

  bands      checked in order, FIRST HIT WINS, so the most senior (and any
             override, like an explicit "individual contributor" designation)
             comes first. A band may appear twice: "junior" does, because
             program titles (intern, co-op) outrank the seniority words while
             junior/jr/associate sit below them, which is what keeps
             "Associate Creative Director" a director.
  phrases    whole-word phrases, not regexes. Words are joined loosely, so
             "head of" also matches "Head-of".
  fallback   the band a title matching nothing takes.

Editing this re-bands ATS jobs on the next refresh; manual jobs re-band when
you next edit them.
-->

```json level_bands
{
  "bands": [
    {
      "band": "ic",
      "label": "IC",
      "phrases": [
        "individual contributor"
      ]
    },
    {
      "band": "junior",
      "label": "Junior",
      "phrases": [
        "intern",
        "internship",
        "co-op",
        "coop",
        "apprentice"
      ]
    },
    {
      "band": "vp_plus",
      "label": "VP+",
      "phrases": [
        "vp",
        "vice president",
        "chief",
        "cdo"
      ]
    },
    {
      "band": "senior_director",
      "label": "Sr Director",
      "phrases": [
        "senior director",
        "sr director",
        "sr. director"
      ]
    },
    {
      "band": "director",
      "label": "Director",
      "phrases": [
        "director",
        "head of"
      ]
    },
    {
      "band": "senior_manager",
      "label": "Sr Manager",
      "phrases": [
        "senior manager",
        "sr manager",
        "sr. manager"
      ]
    },
    {
      "band": "manager",
      "label": "Manager",
      "phrases": [
        "manager",
        "lead"
      ]
    },
    {
      "band": "junior",
      "label": "Junior",
      "phrases": [
        "junior",
        "jr",
        "associate"
      ]
    }
  ],
  "fallback": "ic"
}
```

### Score adjustments (machine-read)

Named near-miss flags below deduct points from the aggregated score **in code**:
final = aggregated score − Σ deductions. The model never sees the point values.
Keep the table short; Tier-1-gated concerns (comp, location, below_band) never
belong here — they are already handled upstream — and `role_mismatch` is
excluded because learned rules already instruct the scorer to score those low.

**A zero keeps the flag, drops the points.** The scorer learns its flag
vocabulary from this table's *keys*, so setting a value to `0` disables the
deduction while the flag keeps appearing in the UI. Deleting the key would stop
the model emitting the flag at all.

Three entries are zeroed because the per-criterion sub-scores now
measure the same thing: `pace_unclear` is criterion 6 resolving to unevidenced,
and `craft_balance_unclear` / `convert_sell_undertone` are the `[craft]` criterion scoring
at or below zero. Deducting again would count the same evidence twice and
re-compress the top of the range — the exact defect the sub-scores exist to fix.
`scope_gap` survives at full value: "VP at a large org" is orthogonal to all
eleven criteria, so nothing else measures it.

```json score_adjustments
{
  "scope_gap": 10,
  "convert_sell_undertone": 0,
  "pace_unclear": 0,
  "craft_balance_unclear": 0
}
```

### Score caps (machine-read)

The job's final management_type — after the categorical IC-designation rule
below, which code enforces from the title/band — caps the score **in code**:
final = min(aggregated score, cap) − deductions. The cap lands after the
sub-scores are totalled, and the model never sees the cap values. Only `ic`
and `unclear` may carry a
management-type cap; people-leader roles are never capped. Two further keys
cap on the function check's `leads_discipline` read: `wrong_function` (the
role leads PMs/engineers/program/ops/content, not designers) and
`function_unclear` (the discipline read is `unclear` — flagged for manual
review, never a pass). A `junior` key caps on the deterministic title band
(intern/junior/associate titles). When several caps apply, the
lowest wins.

```json score_caps
{
  "ic": 50,
  "junior": 20,
  "wrong_function": 15,
  "function_unclear": 50
}
```

## Function check — which discipline does the role lead?

Before any ranked criterion is weighed, decide which discipline the role
actually leads and commit that read as `leads_discipline`. A director-band
title plus people leadership plus warm design language does not settle it —
a Director of Product whose reports are product managers is a
product-management role no matter how admiringly the posting speaks of
design. Three signals decide it, strongest first:

1. **Who reports to the role.** "Team of product managers" → product; "team
   of designers/researchers" → design; **"team of content designers" →
   content, not design.** Content designers are designers, which is exactly
   why this case must be called out by name — otherwise signal 1 and the
   exclusion below collide. Researchers are deliberately in band: a
   research team under a design leader still reads design.
2. **The discipline the experience requirement names.** "8+ years of
   product management experience" → product; "design leadership
   experience" → design.
3. **Where Design lives on the org chart.** Design as the role's own
   organization → design. Design as a neighboring partner ("partner with
   Design leadership", "collaborate closely with Design") → the role leads
   something else.

When the role leads PMs, engineers, program/delivery/ops, or content
instead of designers/researchers, that is the **wrong function**: code
hard-caps the score (the score-caps table) and flags it `wrong_function`,
so it surfaces the same way a comp or location failure does. A genuinely
`unclear` read is flagged `function_unclear` and capped for manual review —
unclear is never a pass.

An individual-contributor posting leads nothing, so read
`leads_discipline` as the discipline the seat itself sits in (an IC product
designer reads design; an IC product manager reads product, and is still
wrong function). The IC cap already governs IC seats; this check exists to
catch leadership roles aimed at the wrong discipline.

**Vocabulary rule:** design-flavored JD language — "design judgment,"
"intuitive experiences," "partner with design" — counts as craft-fit
evidence only where design reports into the role. If `leads_discipline` is
anything but `design`, those phrases must not raise `craft_lean` and must
not feed criteria 1, 5, 8, or 10.

The titles that need this check most: "Director of Product," "Head of
Product," "Product Lead," "Director of Program/Delivery." A clearly
design-titled role ("Director of Design/UX," "Head of Design") will
usually read `design` on sight — go straight to the criteria.

<!--
Taxonomy (machine-read). The vocabulary the scorer classifies a posting with.
Editing this is how you point the whole pipeline at a different field of work.

  disciplines          token -> gloss for `leads_discipline`, the discipline a
                       role LEADS. The glosses are rendered into the prompt, so
                       this is where you teach the scorer the distinctions that
                       matter to you. Must include "unclear".
  in_band_disciplines  the discipline(s) you are searching for. Anything else
                       derives wrong_function and is capped by the score-caps
                       table. This is the single most important line here.
  functions            token -> gloss for `function`, the sub-discipline within
                       your field. A gloss may be empty.
  quadrant_labels      display labels for the fulfillment matrix, and
  tension_labels       display labels for the craft/convert axis. The KEYS of
                       both are stored in the database and parsed back out, so
                       they are fixed; only the labels are yours to change.
-->

```json taxonomy
{
  "disciplines": {
    "design": "roles leading product/UX designers or researchers. For an individual-contributor posting (no reports), read the discipline the role itself sits in: a product/UX designer IC is 'design'",
    "product": "product MANAGEMENT, a role leading PMs. NOT product design",
    "engineering": "roles leading engineers",
    "content": "a role leading CONTENT designers / UX writers. Content designers are designers, so this case must be called by name: 'Director/Head of Content Design' is 'content' however much design craft the JD describes",
    "other": "program/delivery/ops leadership, or anything else",
    "unclear": "the evidence is genuinely thin. This flags the job for manual review, it does not pass"
  },
  "in_band_disciplines": [
    "design"
  ],
  "functions": {
    "product": "product/UX design",
    "content": "content design/UX writing",
    "research": "",
    "service": "service design",
    "platform": "design systems/internal tooling as the role's center",
    "other": ""
  },
  "quadrant_labels": {
    "energizing_strength": "energizing · strength",
    "energizing_growth": "energizing · growth",
    "draining_growth": "draining · growth",
    "draining_strength": "draining · strength"
  },
  "tension_labels": {
    "teach_craft": "teach craft",
    "convert_sell": "convert / sell",
    "mixed": "mixed"
  }
}
```

## Tier 2 — Ranked criteria

Stack-ranked. **Score every criterion on its own** against the −2..+2 scale
defined in the scoring rubric below — the fit score is computed from those
sub-scores in code, never chosen as a number.

A bracketed weight marks how much a criterion counts: `[w: 2]` is twice a
normal criterion, `[w: 0.5]` half, and an unmarked criterion is normal weight
(1.0). Weights are the dial for *importance*; what a criterion's **silence**
costs is its own dial (the `score_scale` block), so moving one never silently
moves the other.

<!-- tier2:start -->
1. **Design already has a seat** — the role reports to someone who speaks design: a CPO or founder who asks "how," never "why." Budget and headcount for the practice exist before the interview, not after a pitch. [w: 2]
2. **An existing team, not a hiring mandate** — reports on day one. Anchors: +2 a functioning team named in the posting, with growth headroom; +1 an existing team of unstated size; 0 part inherited, part to hire (a positive claim of both — quote it); −1 no team yet and building one is the mandate; −2 no reports and none planned (quote the phrase). [w: 1.75]
3. **Mission that survives scrutiny** — work that leaves people, the environment, and civic life better off. Asymmetric: a company that harms nobody scores 0, not negative — save negatives for actively adverse work (surveillance adtech, gambling, predatory lending) and positives for genuine alignment. [w: 1.5]
4. **Public craft, lightly held** — room to write and speak about the practice with a real point of view, without feeding a personal brand on a content schedule. [w: 0.5]
5. **Craft over conversion** — the seat exists to raise the work of people who already believe, never to argue design into the building. Negative signals in JD verbs: "evangelize," "champion," "drive adoption," "prove design's value," "win buy-in." Positive: mentoring, coaching, critique, tending the design system. This is the central tension made scoreable, so it is never unevidenced — every posting has responsibility verbs, and a 0 here is a positive claim that the responsibilities genuinely carry both threads, not a hedge. [craft] [w: 2.5]
6. **Pace that holds for years** — a rhythm compatible with doing this work well for a decade. Negative signals in JD: "extreme ownership," "high-velocity," "move fast," "wear many hats," "scrappy," "hustle," "whatever it takes." Positive: flexible hours, async-first, explicit work-life language. Aggressive tone anywhere in the posting counts against it, not only in a culture section. Score down only when intensity language clusters: one phrase is 0 (noted, not scored down), two phrases are −1, and three or more — or intensity as the posting's dominant register — is −2. Score up on concrete commitments, never on vibes: one explicit flexibility signal (flexible hours, async-first, a stated work-life policy) is +1; work-life balance as a named, repeated commitment — several concrete policies, or a benefits/culture section built around it — is +2. [w: 1.25]
7. **Scope that lives in the seat** — authority the org chart already grants; no re-making the case for the team between projects.
8. **Mentoring is chartered work** — developing designers appears in the responsibilities, not between the lines. Anchors: +2 named as a core responsibility (quote it); +1 present among other duties; 0 stated but explicitly subordinate to another center of gravity (a positive claim — quote both); −1 peer-level mentoring or "help others grow" asides — development without owning it; −2 the role's shape rules it out (quote the phrase).
9. **The third release, not the first pitch** — one product domain held long enough to ship a first release and still own its third; no new industry every engagement. [w: 0.75]
10. **Time at the bench** — making something most weeks — prototypes, design-system pieces — because it keeps the leadership honest. [w: 0.75]
11. **AI direction for the practice** — shaping how the design org adopts and uses AI: the practice layer, never the model-building layer. Positive signals in JD: "AI," "design systems," "emerging tools," "shape how the team works." Score up when explicit; neutral when absent, never negative — this criterion has no negative values. [bonus] [w: 0.5]
<!-- tier2:end -->

**Individual-contributor roles are categorically out of band.** Developing
designers (criterion 8) and team shape (criterion 2) assume people
leadership — the whole strategy is inheriting and growing a team. A posting
designated individual contributor (an IC-band title, or "individual
contributor" in the title or JD) reads management_type `ic` no matter how
much mentoring, critique, or "help with hiring" language it carries — those
are peer activities, not people leadership. management_type `ic` carries a
hard score cap applied in code (the score-caps table): an IC seat can never
outrank people-leader roles, however strong its craft, values, or pace
signals. State the management_type read and its evidence in the notes.

## Fulfillment matrix — quadrants

Classify the job's center of gravity by where its responsibility verbs land.

### energizing_strength — keep doing this (best fit)
- Developing designers one-on-one, including the harder half of it: growth
  feedback honest enough to stick.
- Leading both sides of a design system — the team that builds it and the
  teams that live in it.
- Design and research that lands in real people's lives, done with a
  healthy team.
- Making alongside the team — prototypes and system pieces treated as
  leadership work, not leftovers.
- Working a piece of craft with a designer until they can say why it works.

**Signal verbs:** mentor, coach, develop, grow, critique, raise the bar,
make, tend the design system.

### energizing_growth — where new-role energy comes from (strong fit)
- Staying with one product for years and watching the improvements compound.
- Setting the design org's AI direction — the practice layer, not the
  model-building layer.
- Sitting on a product company's leadership team beside a design-fluent CPO
  or founder.
- Practicing design leadership somewhere calm enough to do it well.
- Taking the craft-teaching work public — writing, talks, a stated point of
  view.

**Signal verbs:** evolve, shape, define (the practice), formalize, compound
small improvements, refine release over release.

### draining_growth — leave these alone (poor fit)
- The internals of AI/ML — model building belongs to another discipline.
- Research that is only numbers.
- People management with no craft left in it.
- Sales or business development as the actual job; feeding a personal brand
  on a content schedule.
- Writing production code as the main deliverable.
- Being narrowed into a single design specialty.

**Signal verbs:** build/train models, quantify, engineer, ship code, sell
(as the primary function).

### draining_strength — the trap: skilled at it, done with it (poor fit)
- Pitch and proposal cycles for clients whose work should not win.
- The evangelism treadmill — selling design to rooms that don't want it and
  watching the work die on its way to production.
- Winning over executives as the actual job — well practiced, and finished
  with it.

**Signal verbs:** evangelize, champion, advocate, influence stakeholders,
prove design's value, drive adoption, win buy-in, align leadership.

## The central tension test

> **Grow the craft of people who already believe — never talk people into
> believing.**

The craft-teaching thread shows up five ways in the energizing-strength
quadrant; the convert-and-persuade thread shows up three ways in
draining-strength. So the question to put to any job is not "does the
company have a design team" — it is **"would this role spend itself selling
design, or get to grow it?"**

**This axis is the `[craft]` criterion.** Score it there and nowhere else; `craft_lean` is
derived from that sub-score in code rather than committed separately, so the two
can never contradict each other. What each point on it means:

- **+2** — craft leadership is the center: grow, mentor, develop, raise the
  bar, scale a practice for people who already value it. Persuasion work is
  absent or incidental.
- **+1** — craft leads, with real adoption or influence work riding
  alongside it.
- **0** — genuine balance: the responsibilities carry both threads. Still a
  positive claim, not a shrug — name the strongest verb on each side (one
  quote can cover the pair). If the posting reads all one way, pick that
  side and score it.
- **−1** — influence and adoption work set the agenda; craft is the side
  dish.
- **−2** — persuasion all the way down: evangelize, champion, advocate,
  influence stakeholders, prove design's value, win buy-in.

Persuasion as the center of gravity is the draining-strength trap, and the
arithmetic already prices it in: the `[craft]` criterion is the heaviest item
on the board, so a −2 there costs more than any other single judgement.

## Moving away / moving toward (context)

**Away from:** Pitch-cycle design work. Re-selling the design function to
each new executive. A new industry every engagement. Handing off work before
it ships. Justifying the team's existence between projects. Travel that
appears without warning.

**Toward:** A role to stay in at an established product company. One domain
held long enough to see the third release. An existing team of 3–6 with room
to grow. Scope that comes with the seat instead of being re-earned.
Developing designers as the core of the work. Craft kept honest by regular
making. A pace that leaves room for a life.

**Calibration note:** if a posting sets off "this would look impressive"
thinking, treat that as evidence it conflicts with the criteria above —
never as a point in its favor.

## Scoring rubric

**Do not produce a fit score.** Score each Tier 2 criterion on its own; the
pipeline weights and totals the sub-scores, then applies caps and
deductions. Answering many small questions is the entire point — one
overall number invites a habitual value, and that habit is the defect this
rubric exists to remove.

### The scale

Every criterion receives one of six answers. Five are integers; the sixth is
`null`.

- **+2** — a quotable phrase in the posting directly satisfies the criterion.
- **+1** — the evidence is partial, adjacent, or inferred rather than stated.
  Even a +1 must stand on words the posting actually contains: a case built
  from absence — "no intensity language," "not explicitly stated," a lack of
  counter-evidence — is `null`, not +1.
- **0** — evidence on **both** sides. A positive claim of balance that
  requires quotes from both directions; never a way to hedge.
- **−1** — mild counter-evidence.
- **−2** — a quotable phrase directly contradicts the criterion.
- **`null`** — the posting does not speak to this criterion in either direction.

**`null` and 0 are different answers, and the difference carries real
weight.** `0` says "I read this and found balance"; `null` says "I looked
and the posting is silent." What silence costs is decided downstream and
differs per criterion — reporting it honestly matters more than resolving it.

Expect `null` wherever a posting simply doesn't address something. **A
typical posting is `null` on three to five of them.** Forcing everything to
±1 to look decisive is as wrong as calling everything 0.

Some criteria carry their own rules, stated on the criteria themselves:
the `[craft]` criterion is never `null`; `[bonus]` criteria are never negative; criterion 6 scores
down only when intensity language clusters; criteria 2, 6 and 8 define their
own anchor ladders (a ladder on each end keeps every step reachable).

### Evidence

Quote the posting's exact words for every **±2** and for the **`[craft]` criterion always**
— those judgements move the total most. A ±1 needs no quote; spend the quote
budget where the arithmetic is sensitive.

### Bands — what the computed score will mean

These describe the output; they are **not** a menu to choose from. They are
here so you know what the sub-scores add up to.

- **80–100 — strong fit.** Center of gravity in an energizing quadrant, the
  craft thread clearly leading, the top-half ranked criteria visibly
  satisfied.
- **60–79 — worth a look.** Mostly energizing, with gaps or unknowns on
  ranked criteria.
- **40–59 — real tension.** Energizing and draining signals genuinely in
  contention, or an energizing read undercut by scope, level, or pace
  questions. A posting that says very little lands here.
- **0–39 — poor fit.** Center of gravity in a draining quadrant, the `[craft]` criterion
  clearly negative, or the role is principally one of the "leave these alone"
  items (AI/ML internals, numbers-only research, craftless people management,
  sales/BD, production engineering).

Caps and deductions are applied in code after the total. Score the posting's
merits and commit honest `management_type` and `leads_discipline` reads —
never pre-cap, and never lower a sub-score for a concern you are already
naming with a near-miss flag.

### Score scale (machine-read)

`slope` and `intercept` map the weighted sub-score total onto 0–100:
`score = round(slope × Σ wᵢvᵢ + intercept)`, clamped to 1–100. `silence` gives
the value substituted when a criterion comes back `null`; omitted criteria
default to 0.

**Never convert silence into a negative sub-score yourself.** If the posting
does not address a criterion, the answer is `null` — the pipeline prices it.
Scoring −1 for "the JD never mentions this" applies the penalty twice and
overstates it.

Silence values are keyed on **expected disclosure**, not on rank. A JD is a
recruiting document — it advertises what its authors think makes the role
attractive — so an org that runs on craft development says so, and silence
about it is weak evidence in a way that silence about, say, scope-by-role is
not: postings never state that; interviews do. Mission stays at 0
deliberately: most companies harm no one, and penalising that would hand a
systematic advantage to purpose-flavoured marketing copy — the
"would look impressive" failure the calibration note warns about.

**The keys are criterion positions.** Reordering the Tier 2 list re-points them,
so re-check this block after any reorder. (The Vocabulary rule above already
refers to criteria by number, so the doc has this coupling either way.)

```json score_scale
{
  "slope": 1.6,
  "intercept": 55,
  "silence": {
    "1": -0.5,
    "2": -0.5,
    "6": -1,
    "8": -1,
    "9": -1,
    "10": -0.5
  }
}
```

Near-miss flags from the scorer should name the soft criterion a job fails
when it is otherwise strong, using the canonical tokens (`scope_gap`,
`convert_sell_undertone`, `pace_unclear`, `craft_balance_unclear`,
`mission_unclear`) so the UI can surface it rather than hiding it. Most no
longer carry points — the sub-scores price those concerns now — but the flags
are still how a concern gets *named*, and the UI surfaces every one.
