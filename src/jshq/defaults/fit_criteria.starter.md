# Fit Criteria — starter

<!--
A neutral starting point. The onboarding wizard fills this in from your own
answers, or you can edit it by hand. The scoring prompt and the Tier 1 filter
parameters are generated/parsed from this file — change behavior here, not in
code. Everything outside the fenced `json` machine blocks (and outside HTML
comments like this one) is sent to the scorer verbatim as context.

  display_name  Your own name, or null to stay anonymous.
  domain_label  A few words for the kind of role you are searching for. It opens
                the scoring prompt, so keep it short.
-->

```json persona
{
  "display_name": null,
  "domain_label": "the roles you are searching for"
}
```

## Tier 1 — Binary filters (machine-checked, no AI)

These run in code before any AI scoring; a hard fail stops the pipeline. While
the values below are empty nothing is filtered out — no compensation floor, no
location restriction, no excluded sectors, no title band. Fill them in (the
onboarding wizard walks you through it) to start narrowing the field.

```json tier1_params
{
  "comp_floor": 0,
  "comp_target": 0,
  "location_allowlist": [],
  "company_location_overrides": {},
  "location_radius": null,
  "remote_regions": [],
  "excluded_sectors": [],
  "target_title_bands": [],
  "flag_title_bands": {}
}
```

### Title seniority bands

The seniority ladder used by `target_title_bands` and `flag_title_bands` above,
and offered as choices wherever you pick a band. A title is banded in code from
these phrases before any AI runs.

<!--
  bands      checked in order, FIRST HIT WINS, so the most senior (and any
             override, like an explicit "individual contributor") comes first.
             A band may appear twice: "junior" does, because program titles
             (intern, co-op) outrank the seniority words, while junior/jr/
             associate sit below them.
  phrases    whole-word phrases, not regexes. Words join loosely, so "head of"
             also matches "Head-of".
  fallback   the band a title matching nothing takes.

This ladder covers both the management track and the senior individual-
contributor engineering track (staff, senior staff, principal, distinguished).
Trim, reorder, or add bands to fit your own field; every name you use in
target_title_bands / flag_title_bands must be one this block can emit.
-->

```json level_bands
{
  "bands": [
    { "band": "ic", "label": "IC", "phrases": ["individual contributor"] },
    { "band": "junior", "label": "Junior", "phrases": ["intern", "internship", "co-op", "coop", "apprentice"] },
    { "band": "vp_plus", "label": "VP+", "phrases": ["vp", "vice president", "chief", "cdo"] },
    { "band": "distinguished", "label": "Distinguished", "phrases": ["distinguished"] },
    { "band": "principal", "label": "Principal", "phrases": ["principal"] },
    { "band": "senior_staff", "label": "Sr Staff", "phrases": ["senior staff", "sr staff", "sr. staff"] },
    { "band": "staff", "label": "Staff", "phrases": ["staff"] },
    { "band": "senior_director", "label": "Sr Director", "phrases": ["senior director", "sr director", "sr. director"] },
    { "band": "director", "label": "Director", "phrases": ["director", "head of"] },
    { "band": "senior_manager", "label": "Sr Manager", "phrases": ["senior manager", "sr manager", "sr. manager"] },
    { "band": "manager", "label": "Manager", "phrases": ["manager", "lead"] },
    { "band": "junior", "label": "Junior", "phrases": ["junior", "jr", "associate"] }
  ],
  "fallback": "ic"
}
```

## Tier 2 — Ranked criteria

Your stack-ranked wish list: what matters most in your next role, most important
first. Score each one individually on the -2 to +2 scale below; the fit score is
computed from those sub-scores in code, not chosen as a number. Until you add at
least one criterion here, AI scoring stays off and jobs are simply listed.

<!-- tier2:start -->

<!-- tier2:end -->

## Scoring rubric

**Do not produce a fit score.** Score each Tier 2 criterion individually; the
pipeline weights and totals them.

Every criterion gets one of six answers. The first five are integers; the sixth
is `null`.

- **+2** — a quotable phrase in the posting directly satisfies the criterion.
- **+1** — partial, adjacent, or inferred evidence that still rests on words the
  posting actually contains.
- **0** — evidence on both sides: a positive claim of balance, quoting both
  directions, not a way to hedge.
- **-1** — mild counter-evidence.
- **-2** — a quotable phrase directly contradicts the criterion.
- **`null`** — the posting does not address this criterion in either direction.

`null` and `0` are different answers: `0` means "I read this and found balance,"
`null` means "I looked and the posting is silent." A typical posting is `null`
on several criteria; resolving everything to seem decisive is a mistake.
