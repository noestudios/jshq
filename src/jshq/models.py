"""Request bodies for the CRUD endpoints.

status/contact source stay free text (UI constrains them via <select>, and
contact source's vocabulary is the contact_sources setting) so adding a value
never requires an API change; values_fit is a closed set.
"""

import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .scoring.criteria import (
    DEFAULT_TIER2_WEIGHT,
    MAX_TIER2_WEIGHT,
    MIN_TIER2_WEIGHT,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CompanyIn(BaseModel):
    name: NonEmptyStr
    location: str | None = None
    priority: int | None = Field(None, ge=1, le=5)
    status: str | None = None
    values_fit: Literal["high", "medium", "low", "unknown"] | None = None
    website: str | None = None
    careers_url: str | None = None
    # Accepted so a GET payload round-trips through PUT; Phase 3 owns the values.
    ats_type: str | None = None
    ats_slug: str | None = None
    notes: str | None = None
    linkedin_company_ids: list[str] = []
    linkedin_title_searches: list[str] = []


class CareersPreviewIn(BaseModel):
    # A no-write ATS probe run BEFORE a company exists (the wizard) or before an
    # edit is saved — just enough to derive a careers/board URL from a site.
    # name feeds the blind slug-probe fallback; blank is fine (yields no guesses).
    name: str | None = None
    website: str | None = None
    careers_url: str | None = None


class JobStatusIn(BaseModel):
    status: Literal["active", "applied", "dismissed", "closed"]
    # Dismissal feedback loop: reason + optional note are stored
    # as an activities row and feed the scoring digest + exclude suggestions.
    reason: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _reason_only_with_dismissed(self):
        if (self.reason or self.note) and self.status != "dismissed":
            raise ValueError("reason/note are only valid with status='dismissed'")
        return self


class RefreshIn(BaseModel):
    # POST /api/refresh body. "failed" retries only the boards whose last pull
    # errored (the bulk retry-failed button); "all" (or no body at all — the
    # cron/backstop callers send none) is the full-estate run.
    scope: Literal["all", "failed"] = "all"


class JobElevateIn(BaseModel):
    # Manual override (QA): keep a maybe/below-fit job in the positive-fit
    # category. Orthogonal to fit_score (which stays model-judged) and persists
    # across rescores. True elevates, False clears.
    elevated: bool


class JobDetailsIn(BaseModel):
    # Manual correction of an ATS job's facts: a wrong/missing location, the
    # remote type, or a salary range learned from a recruiter. salary_stated is
    # derived server-side; the edit sets jobs.manually_edited so the next refresh
    # preserves these fields instead of overwriting them from the board.
    location: str | None = None
    remote_type: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)


class JobParseUrlIn(BaseModel):
    # A pasted posting URL to fetch + parse for the Add-job prefill. Validation
    # of scheme/host (and the LinkedIn refusal) lives in jobparse.parse_job_url.
    url: NonEmptyStr


class JobCreateIn(BaseModel):
    # Hand-entered job for a company with no connectable ATS (the user found it
    # via the LinkedIn role links / careers page). Stored with source='manual',
    # scored like an ingested job, exempt from decay. salary_stated is derived
    # server-side from whether a figure was given.
    company_id: int
    title: NonEmptyStr
    url: str | None = None
    location: str | None = None
    remote_type: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    description_text: str | None = None


class SettingIn(BaseModel):
    value: list | dict | str | int | float | bool | None


class ApiKeyIn(BaseModel):
    # The Anthropic key, on its way to DATA_DIR/.env via apikey.write_key (which
    # revalidates: no whitespace, no control chars). NonEmptyStr strips and
    # rejects blank here so the empty-field case 422s before touching the file.
    key: NonEmptyStr


class Tier2Item(BaseModel):
    # One ranked Tier 2 criterion (Phase 8). text = the markdown line; weight = a
    # 1.0-centered importance multiplier the scorer reads as emphasis (default 1.0
    # = normal, rendered with no `[w:]` suffix so the doc stays byte-stable).
    # craft / bonus_only are the `[craft]` and `[bonus]` markers (Phase 2). They
    # round-trip through the editor rather than being editable in it: a PUT that
    # dropped them would silently move the craft axis, and craft_lean (with the
    # tension label derived from it) would change on every job thereafter.
    text: NonEmptyStr
    weight: float = Field(DEFAULT_TIER2_WEIGHT, ge=MIN_TIER2_WEIGHT, le=MAX_TIER2_WEIGHT)
    craft: bool = False
    bonus_only: bool = False


class VoiceGuideIn(BaseModel):
    # The user's voice guide (Phase 3). Prose the compose/tailor/refine prompts
    # carry verbatim, not machine config — an empty guide is legal (it degrades
    # to the base framing). The endpoint caps its byte size; nothing else.
    markdown: str


class PersonaIn(BaseModel):
    # Who the AI prompts are written for (Phase 3). display_name may be blank or
    # null to name nobody ("the candidate"); domain_label opens the scoring prompt
    # so it stays a short non-empty phrase. write_persona revalidates both against
    # the doc (length, no embedded quote/newline) before the live doc is touched.
    display_name: str | None = None
    domain_label: NonEmptyStr


class CriteriaIn(BaseModel):
    # File-first fit-criteria edit (Phase 7h; Tier 2 weights Phase 8). The shapes
    # are validated against DATA_DIR/fit_criteria.md by scoring.criteria.write_criteria
    # (which reuses load_criteria), so a bad payload is rejected before the doc is
    # touched. Out-of-range weights are 422'd here by the Tier2Item bounds.
    tier1_params: dict
    tier2_criteria: list[Tier2Item]


class DisciplineIn(BaseModel):
    # The onboarding wizard's one field question (Phase 4). `field` is a free-text
    # label ("product management", "data engineering"); write_field turns it into
    # the in-band discipline so scoring targets that field, not the design default.
    field: NonEmptyStr


class OnboardingIn(BaseModel):
    # PUT /api/onboarding — record that the user skipped (dismissed) or finished
    # (completed) the wizard. Both optional; neither is a gate, they only stop the
    # first-run redirect and let the tracker reflect the choice.
    dismissed: bool | None = None
    completed: bool | None = None


class SynthesisReplyIn(BaseModel):
    """A pasted model reply for the keyless synthesis path. Validation proper
    happens in synthesis.validate_synthesis — this just refuses blanks."""

    reply: NonEmptyStr


class SynthesisApplyIn(BaseModel):
    """apply_tier2 opts the parked draft's ranked-list refinements in; the
    reflection prose always applies (it is the point of the feature)."""

    apply_tier2: bool = False


class RoadmapIn(BaseModel):
    # PUT /api/onboarding/roadmap — the user's RAW exercise inputs, stored verbatim
    # for a later synthesis pass. Free-form: wishlist/matrix are named for
    # documentation, but any extra keys are kept (extra='allow') so the exercise
    # shape can evolve without an API change. The endpoint caps its size.
    model_config = ConfigDict(extra="allow")
    wishlist: list | None = None
    matrix: dict | None = None


# Human-readable inclusion rules (Phase 7i, decision C). Rules are the source of
# truth; the backend compiles them down to title_keywords / title_exclude_keywords
# + location_allowlist. See scoring/rules.py.
RuleVerb = Literal["include", "exclude"]
RuleTarget = Literal["title", "location"]


class InclusionRule(BaseModel):
    id: NonEmptyStr  # client-generated stable id
    verb: RuleVerb
    target: RuleTarget
    terms: list[NonEmptyStr] = Field(min_length=1)

    @model_validator(mode="after")
    def _location_exclude_invalid(self):
        # There is no location exclusion list — a town allowlist (include) is the
        # only location mechanism. Hard-gate it here as well as in the UI.
        if self.target == "location" and self.verb == "exclude":
            raise ValueError(
                "a location rule cannot be 'exclude' — there is no location "
                "exclusion list; use a town allowlist (include) instead"
            )
        return self


class InclusionManual(BaseModel):
    """One-off keywords added directly in the compiled 'Advanced' view (not from
    a rule). Kept so they survive a recompile."""

    title_keywords: list[NonEmptyStr] = []
    title_exclude_keywords: list[NonEmptyStr] = []
    location_allowlist: list[NonEmptyStr] = []


class InclusionRulesIn(BaseModel):
    rules: list[InclusionRule] = []
    manual: InclusionManual = Field(default_factory=InclusionManual)


class SuggestionActionIn(BaseModel):
    keyword: NonEmptyStr
    action: Literal["accept", "ignore"]


class ScoringRuleActionIn(BaseModel):
    # Accept/ignore a semantic JD/role-mismatch proposal (Phase 7i). Identified
    # by the proposal's id; accept promotes it to scoring_rules, ignore drops it.
    id: NonEmptyStr
    action: Literal["accept", "ignore"]


class ReminderIn(BaseModel):
    title: NonEmptyStr
    type: Literal[
        "followup_contact", "followup_application", "thank_you",
        "interview", "linkedin_post", "meeting", "custom",
    ] = "custom"
    entity_type: Literal["job", "contact", "company", "application"] | None = None
    entity_id: int | None = None
    due_date: datetime.date
    due_time: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    notes: str | None = None

    @model_validator(mode="after")
    def _entity_pair(self):
        if (self.entity_type is None) != (self.entity_id is None):
            raise ValueError("entity_type and entity_id must be given together")
        return self


class ReminderPatch(BaseModel):
    """Done-toggle and snooze. Only fields explicitly sent are applied."""

    done: bool | None = None
    due_date: datetime.date | None = None
    due_time: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @model_validator(mode="after")
    def _at_least_one(self):
        if not self.model_fields_set:
            raise ValueError("nothing to update")
        # None doubles as the not-sent sentinel; an EXPLICIT null must be
        # rejected here for done (int(None) is a 500) and due_date (every
        # reminder has one — writing SQL NULL makes build_event raise on all
        # three ICS endpoints, poisoning a subscribed feed until the row is
        # fixed). due_time may be explicitly nulled: time-less reminders are
        # legitimate and the ICS builder guards for it.
        if "done" in self.model_fields_set and self.done is None:
            raise ValueError("done cannot be null")
        if "due_date" in self.model_fields_set and self.due_date is None:
            raise ValueError("due_date cannot be null")
        return self


class ActivityIn(BaseModel):
    entity_type: Literal["job", "contact", "company", "application", "general"]
    entity_id: int | None = None
    date: datetime.date | None = None  # server defaults to today (local)
    type: Literal["meeting", "interview", "call", "note"]
    content: str | None = None

    @model_validator(mode="after")
    def _entity_pair(self):
        if self.entity_type == "general":
            if self.entity_id is not None:
                raise ValueError("entity_id is not valid with entity_type='general'")
        elif self.entity_id is None:
            raise ValueError("entity_id required unless entity_type='general'")
        return self


class ComposeIn(BaseModel):
    intent: Literal[
        "thank_you", "follow_up", "linkedin_comment", "connection_note",
        "reconnect", "outreach", "application_answer",
    ]
    entity_type: Literal["job", "contact"]
    entity_id: int
    instructions: str | None = None  # optional steering, free text
    question: str | None = None  # the application question text

    @model_validator(mode="after")
    def _question_pairing(self):
        if self.intent == "application_answer" and not (self.question or "").strip():
            raise ValueError("question is required for intent='application_answer'")
        if self.intent != "application_answer" and self.question:
            raise ValueError("question is only valid with intent='application_answer'")
        return self


class ReminderSuggestionActionIn(BaseModel):
    key: NonEmptyStr
    action: Literal["accept", "ignore"]


ApplicationStatus = Literal[
    "drafting", "applied", "screen", "interview", "offer", "rejected", "withdrawn"
]


class ApplicationIn(BaseModel):
    job_id: int
    status: ApplicationStatus = "drafting"
    applied_date: datetime.date | None = None
    resume_version: str | None = None
    cover_note: str | None = None
    next_step: str | None = None
    next_step_date: datetime.date | None = None


class ApplicationUpdate(BaseModel):
    """PUT body — job_id is immutable, set at creation only."""

    status: ApplicationStatus
    applied_date: datetime.date | None = None
    resume_version: str | None = None
    cover_note: str | None = None
    next_step: str | None = None
    next_step_date: datetime.date | None = None


class TailorIn(BaseModel):
    """POST /api/applications/{id}/tailor — optional steering, free text."""

    instructions: str | None = None


class TailoringChangePatch(BaseModel):
    id: NonEmptyStr
    approved: bool
    new: str | None = None  # accepted now for 7f; the 7e UI sends approved only


class TailoringPatch(BaseModel):
    """PATCH /api/tailorings/{id} — approval flags and/or the edited letter."""

    changes: list[TailoringChangePatch] | None = None
    cover_letter: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self):
        if not self.model_fields_set:
            raise ValueError("nothing to update")
        return self


class CoverRerenderIn(BaseModel):
    """POST /api/tailorings/{id}/rerender — re-render the cover PDF from a
    hand-edited letter as a new version, with no model call."""

    cover_letter: NonEmptyStr


class RefineTellsIn(BaseModel):
    """POST /api/refine-tells — one text blob to scrub of AI tells."""

    text: NonEmptyStr


class TailoringChatIn(BaseModel):
    """POST /api/tailorings/{id}/chat — one refinement message from the user."""

    message: NonEmptyStr


class ContactIn(BaseModel):
    name: NonEmptyStr
    company_id: int | None = None
    role: str | None = None
    linkedin_url: str | None = None
    email: str | None = None
    source: str | None = None  # vocabulary lives in the contact_sources setting
    relationship_notes: str | None = None
    last_contact_date: str | None = None  # ISO date since 2026-08-04 (UI is a date input; legacy free text migrated)
