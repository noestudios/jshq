# AI-Tell Sweep: portable copy rubric

A reusable rubric for catching and rewriting "AI tells": the phrasing patterns
that make human-authored prose read as machine-generated. Illustrated
throughout with invented before/after pairs. Applies to any first-person
professional copy: cover letters, recruiter outreach, LinkedIn "About," resume
summaries, bios.

The goal is NOT blandness. It is to remove the cadence and vocabulary that signal
a model wrote it, while keeping specifics, numbers, and real voice. Over-
correction (stripping all rhythm and personality) is its own failure mode; see
"Don't over-correct."

## How to integrate (pick one or more)

1. **Generation constraint.** Paste "Hard rules" + "The patterns" (signals only)
   into the system prompt of whatever drafts the copy, as negative constraints.
   Cheapest; prevents most tells at the source.
2. **Review / scoring pass.** Run drafted text against "The checklist" as an
   LLM-judge rubric. Each hit lowers a "reads as human" score (0-10) and returns
   the offending span + a rewrite. Best for a tool that already scores content.
3. **Deterministic pre-lint.** A few patterns are regex-catchable (em dashes,
   "X, not Y", triads of identical sentence openers). Flag those mechanically,
   then send flagged passages to the model for the judgment call.

## Hard rules (binary, easy to lint)

- **No em dashes** (the long dash or the en dash). Use a comma, a colon, a
  period, or parentheses.
- **No "X, not Y" antithesis** ("abstracted, not censored"; "leadership, not
  management"). State X directly; drop the foil.
- **No consultant-speak / inflated abstraction.** Ban-list to flag: leverage,
  synergy, unlock, empower, deliver value, at scale, journey, passionate about,
  dynamic, robust, seamless, holistic, drive impact, structural/systemic [pain].

## The patterns

Each: **signal** -> why it reads as AI -> **fix**, with an invented before->after.

1. **Grandiose absolutes / superlative stakes.** Sweeping "only / always / more
   than any" claims a human wouldn't assert.
   - Before: "it's the only onboarding flow that has ever actually worked here"
   - After:  "it's the onboarding flow that has held up"
   - Before: "Renewing says more about the program's worth than any dashboard could."
   - After:  "The district choosing to renew for a third year is the outcome I'd point to first."

2. **The aphoristic fragment closer.** A short, punchy "X is the Y." fragment
   dropped in for drama. Models love these; they read as manufactured.
   - Before: "Documentation is the moat."  ->  After: "So I keep the runbooks current."
   - Before: "The handoff is the job."     ->  After: "Getting the night shift what they need is most of the job."

3. **Rule-of-three / anaphora.** Three parallel clauses, or repeated sentence
   openers, for cadence. One or two carries the meaning; the third is filler.
   - Before: "Reports that answer the question. Reports that end the meeting early. Reports that people actually open on a Monday morning."
   - After:  one concrete sentence naming the actual outcome.
   - Before: "its own spreadsheet, its own naming scheme, its own copy of the same"
   - After:  "spreadsheet and naming scheme, all colliding over the same"

4. **Vague universal payoff.** A closing line that gestures at meaning without
   adding fact ("the reason any of this matters," "that's always the goal").
   - Before: "In the end, that's what all of this is really about." -> After: (cut)

5. **Restated-thesis / self-praise tag.** A sentence that re-asserts the point
   already made, often appending "that's what I'm proudest of."
   - Before: "...the regional teams ran the audit themselves by March. That's what I'm proudest of." -> After: drop the tag.

6. **Inflated abstraction vocabulary.** Concrete nouns swapped for grand ones.
   - Before: "navigating the same systemic friction" -> After: "running into the same three approval bottlenecks"

7. **Dramatic verb pairs / intensifier stacking.** "hit fast and hit hard."
   - Before: "the launch had to hit fast and hit hard" -> After: "the launch had to come early in the quarter, and it had to work the first time"

8. **Cliche metaphor.** Stock figurative language (lever, seat at the table,
   take the next swing, learned the long way).
   - Before: "give them a seat at the table" -> After: "put them in the meetings where the decisions got made"
   - Before: "Three habits, learned in the trenches." -> After: "Three habits, picked up across nine years of closing the books."

9. **Staccato anaphora for "scene."** A run of short "Through... Into... Through"
   sentences to evoke atmosphere. Collapse into one flowing sentence with a
   colon and a real list.
   - Before: "Through the intake dock. Into the aisles where pickers assemble each order. Through the loading bays..."
   - After:  "...the whole fulfillment path: the intake dock, the aisles where pickers assemble each order, the packing stations behind them, and the loading bays where drivers stage the morning routes."

10. **Hedge-cliches.** "isn't necessarily the prettiest," "ready or not," "needless
    to say." Either cut or replace with a specific.
    - Before: "I've taken on support queues that needed fixing, ready or not."
    - After:  "I've taken over support queues mid-backlog." (then check #8)

## Don't over-correct

- Keep numbers, proper specifics, and first-person voice. A fixed line should be
  MORE concrete than the original, not blander.
- A single short sentence for emphasis is fine. The tell is the *patterned*
  fragment ("X is the Y."), not brevity itself.
- One vivid metaphor a person actually uses is fine; the tell is stock/stacked
  metaphor.

## The checklist (scoring form)

Score each draft 0-10 on "reads as written by a person." Deduct per hit; return
span + suggested rewrite. Flags:

- [ ] Em dash present
- [ ] "X, not Y" antithesis
- [ ] Consultant-speak / ban-list term
- [ ] Superlative absolute ("only," "always," "more than any")
- [ ] Aphoristic "X is the Y." fragment
- [ ] Triad / repeated sentence opener
- [ ] Closing line with no new fact ("why any of this matters")
- [ ] Restated thesis or self-praise tag
- [ ] Stock/stacked metaphor
- [ ] Hedge-cliche ("isn't necessarily," "ready or not")

Rewrite test for any flagged line: does the replacement add a concrete fact,
number, or named specific? If not, cut rather than reword.

---

## Appendix: LLM-judge prompt (Mode 2, drop-in)

System or task prompt for a scoring pass. Returns structured JSON.

> You are reviewing first-person professional copy for "AI tells": phrasing that
> makes human writing read as machine-generated. Using the rubric flags below,
> score the text 0-10 on "reads as written by a person" (10 = no tells). For each
> tell you find, return the exact span, the flag it matches, and a rewrite that
> is MORE concrete than the original (adds a fact, number, or named specific) or
> a deletion if no fact can be added. Do not flag brevity itself, real numbers,
> or a single genuine metaphor. Output JSON only:
>
> {
>   "score": <0-10 integer>,
>   "findings": [
>     { "span": "<verbatim text>", "flag": "<flag name>", "fix": "<rewrite or 'cut'>" }
>   ]
> }
>
> Flags: em-dash; x-not-y; consultant-speak; superlative-absolute;
> aphoristic-fragment; triad-anaphora; vague-payoff; restated-thesis;
> cliche-metaphor; hedge-cliche.
