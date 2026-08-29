-- Job Search HQ schema. This file is the source of truth for the DB shape.
-- Idempotent: everything IF NOT EXISTS; safe to run at every startup.

CREATE TABLE IF NOT EXISTS companies (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT NOT NULL,
    location                TEXT,
    priority                INTEGER,            -- 1-5
    status                  TEXT,               -- prospect/closed/...
    values_fit              TEXT,               -- high/medium/low
    website                 TEXT,
    careers_url             TEXT,
    ats_type                TEXT,               -- filled in Phase 3
    ats_slug                TEXT,
    ats_last_checked        TEXT,               -- last refresh attempt (UTC)
    ats_last_status         TEXT,               -- 'ok: N matched' | 'error: ...'
    sector_flags            TEXT,               -- JSON array, e.g. ["healthcare"]; Tier 1 exclusion
    notes                   TEXT,
    linkedin_company_ids    TEXT,               -- JSON array of strings
    linkedin_title_searches TEXT,               -- JSON array of strings
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contacts (
    id                 INTEGER PRIMARY KEY,
    company_id         INTEGER REFERENCES companies(id),
    name               TEXT NOT NULL,
    role               TEXT,
    linkedin_url       TEXT,
    email              TEXT,
    source             TEXT,                    -- vocabulary: the contact_sources setting
    relationship_notes TEXT,
    last_contact_date  TEXT,                    -- free text from v2 export; not parsed
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY,
    company_id       INTEGER NOT NULL REFERENCES companies(id),
    external_id      TEXT,                      -- ATS req id
    title            TEXT NOT NULL,
    url              TEXT,
    location         TEXT,
    remote_type      TEXT,                      -- remote/hybrid/onsite/unknown
    level_band       TEXT,
    salary_min       INTEGER,
    salary_max       INTEGER,
    salary_stated    INTEGER,                   -- bool 0/1
    description_text TEXT,
    first_seen       TEXT,
    last_seen        TEXT,
    status           TEXT,                      -- active/closed/applied/dismissed
    fit_score        INTEGER,
    fit_quadrant     TEXT,
    tier1_results    TEXT,                      -- JSON: per-filter pass/fail
    near_miss_flags  TEXT,                      -- JSON array
    scoring_notes    TEXT,
    score_detail     TEXT,                      -- JSON: {model_score, subscores{}, subscore_quotes{}, evidenced_count, deductions{}, craft_lean, confidence, management_type, function, leads_discipline} + optional cap/function_cap/band_cap/model_management_type; NULL = tier1 fail or pre-redesign row
    miss_count       INTEGER NOT NULL DEFAULT 0, -- consecutive refreshes absent. ACTIVE rows flip to
                                                 -- 'closed' at 2; APPLIED rows keep their status and
                                                 -- carry the count past 2 as the "delisted" signal
    manually_elevated INTEGER NOT NULL DEFAULT 0, -- user override: keep a maybe/below-fit job in positive fit (orthogonal to fit_score; survives rescore)
    source           TEXT NOT NULL DEFAULT 'ats', -- 'ats' (pulled by the refresh pipeline) | 'manual' (hand-entered for a no-ATS company; excluded from decay)
    manually_edited  INTEGER NOT NULL DEFAULT 0, -- user corrected location/remote_type/salary; refresh preserves those fields instead of overwriting from the ATS payload
    dedupe_key       TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe_key ON jobs(dedupe_key);

CREATE TABLE IF NOT EXISTS applications (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER NOT NULL REFERENCES jobs(id),
    applied_date   TEXT,
    status         TEXT,                        -- drafting/applied/screen/interview/offer/rejected/withdrawn
    resume_version TEXT,
    cover_note     TEXT,
    next_step      TEXT,                        -- DORMANT since v10: superseded by next_steps rows
    next_step_date TEXT,                        -- (backfill nulls values; column kept — DROP COLUMN isn't worth a table rebuild)
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One application per job; the UI links job <-> application 1:1.
CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_job_id ON applications(job_id);

-- Unified notes table. Everything feeds AI context.
CREATE TABLE IF NOT EXISTS activities (
    id          INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,                  -- company/contact/job/application/general
    entity_id   INTEGER,
    date        TEXT,
    type        TEXT,                           -- note/meeting/linkedin/email/call/post
    content     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    type        TEXT,                           -- followup_contact/followup_application/thank_you/interview/linkedin_post/meeting/custom
    entity_type TEXT,
    entity_id   INTEGER,
    due_date    TEXT,
    due_time    TEXT,
    done        INTEGER NOT NULL DEFAULT 0,
    ics_uid     TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),  -- UTC; ICS DTSTAMP
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))   -- UTC; ICS SEQUENCE/LAST-MODIFIED
);

-- Application next steps (v10): first-class rows promoted from the old
-- applications.next_step field pair. Multiple pending steps per application
-- are allowed; done/dismissed rows are kept as history (they stay visible on
-- the in-app calendar with status styling, and drop out of the .ics feed).
CREATE TABLE IF NOT EXISTS next_steps (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    title          TEXT NOT NULL,
    due_date       TEXT,                             -- ISO date; NULL = undated (off calendar/ics)
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending/done/dismissed
    ics_uid        TEXT,                             -- immutable; backfilled rows keep app-nextstep-{app_id}@jobsearchhq
    resolved_at    TEXT,                             -- UTC; set when status leaves pending
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),  -- UTC
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))   -- UTC; ICS DTSTAMP/SEQUENCE
);

CREATE INDEX IF NOT EXISTS idx_next_steps_application ON next_steps(application_id);

-- Tailoring runs (Phase 7e): one AI pass per application producing
-- a resume change plan + cover letter draft. change_plan is JSON
-- [{id, old, new, rationale, approved}] addressed by content.json node ids;
-- chat refinement (7f) rewrites it in place. Discarded rows are kept — they
-- are taste signal and future context, like regenerated-away compose drafts.
CREATE TABLE IF NOT EXISTS tailorings (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending/applied/discarded
    analysis       TEXT,                             -- shared JD analysis
    change_plan    TEXT NOT NULL,                    -- JSON array
    cover_letter   TEXT NOT NULL,
    model          TEXT,
    version        INTEGER,                          -- set at apply; names the PDFs
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at     TEXT
);

-- At most one pending tailoring per application, enforced at the DB layer.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tailorings_pending
    ON tailorings(application_id) WHERE status = 'pending';

-- Tailoring chat (Phase 7f): refinement thread per tailoring run.
-- Rows survive apply/discard (taste signal / future context); the UI only
-- shows the thread while the tailoring is pending. Assistant rows keep both
-- the display reply (content) and the raw structured contract (payload) so
-- merges can be audited later — payload is write-only for now, not dead weight.
CREATE TABLE IF NOT EXISTS tailoring_messages (
    id           INTEGER PRIMARY KEY,
    tailoring_id INTEGER NOT NULL REFERENCES tailorings(id),
    role         TEXT NOT NULL,        -- 'user' / 'assistant'
    content      TEXT NOT NULL,        -- user text, or assistant display reply
    payload      TEXT,                 -- assistant rows: parsed contract JSON
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tailoring_messages_tailoring
    ON tailoring_messages(tailoring_id);

-- Performance indexes (single-user hardening, 2026-06-22). Each matches a hot
-- read path; the correctness/UNIQUE indexes sit next to their tables above.
--   jobs(company_id, status) — the per-company active-job COUNT subquery run for
--     every row on each company-list load, plus the refresh decay scan.
--   jobs(status)            — scoring's global "WHERE status='active'" scan per refresh.
--   activities/reminders(entity_type, entity_id) — detail-pane feeds + delete cascade.
--   contacts(company_id)    — company-scoped contact lookups + delete cascade.
CREATE INDEX IF NOT EXISTS idx_jobs_company_status ON jobs(company_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_status         ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_activities_entity   ON activities(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_reminders_entity    ON reminders(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_contacts_company    ON contacts(company_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

INSERT OR IGNORE INTO settings (key, value) VALUES ('schema_version', '10');
UPDATE settings SET value = '10' WHERE key = 'schema_version' AND CAST(value AS INTEGER) < 10;

-- Ingestion title filter: compiled from Settings → Sourcing inclusion rules,
-- matched case-insensitively on word boundaries against job titles before
-- anything is stored. Ships EMPTY, and empty means NO GATE (everything
-- ingests; excludes still apply) — the wizard's field step writes the first
-- rule. Workday boards are the exception: they fetch nothing without terms
-- (see workday_search_terms below).
-- workday_search_terms is a FETCH override, not a scoring filter: Workday's
-- API requires a searchText, and the terms derive from the live title_keywords
-- include list at refresh time (ats/refresh._fetch_config). Set this key (no
-- seed; PUT /api/settings/workday_search_terms) only to scope Workday boards
-- differently from the ingestion gate.
-- Seeded onto each newly created company to build its LinkedIn people searches.
-- Ships empty: role titles are field-specific, and the empty state asks for
-- them ("No tracked titles yet"). Populate it to give every new company a
-- starting set.
INSERT OR IGNORE INTO settings (key, value) VALUES ('linkedin_title_defaults', '[]');
INSERT OR IGNORE INTO settings (key, value) VALUES ('title_keywords', '[]');

-- Dismissal feedback loop. dismiss_reasons seeds the dialog
-- dropdown; title_exclude_keywords is fed by accepted suggestions and applied
-- by the adapters' title filter at ingestion; suggestions_ignored stops a
-- declined suggestion from resurfacing. All editable via /api/settings.
INSERT OR IGNORE INTO settings (key, value) VALUES ('dismiss_reasons', '["not my focus area", "wrong level", "comp too low", "location", "company concerns", "other"]');
-- How-you-met vocabulary for the contacts source dropdown. Plain TEXT column;
-- edit the list to match your own networking channels.
INSERT OR IGNORE INTO settings (key, value) VALUES ('contact_sources', '["linkedin", "referral", "event", "other"]');
INSERT OR IGNORE INTO settings (key, value) VALUES ('title_exclude_keywords', '[]');
INSERT OR IGNORE INTO settings (key, value) VALUES ('suggestions_ignored', '[]');

-- Reminder auto-suggestions: keys of declined/accepted event
-- suggestions so they never resurface.
INSERT OR IGNORE INTO settings (key, value) VALUES ('reminder_suggestions_ignored', '[]');

-- Human-readable inclusion rules (Phase 7i, decision C): the rules are the
-- source of truth and compile down to title_keywords / title_exclude_keywords +
-- location_allowlist. Written lazily by PUT /api/inclusion-rules (the rules array
-- lives under the inclusion_rules key); served only there, never via
-- /api/settings. Provenance is derived (manual = live array - rule-emitted), so
-- there is no separate manual store and nothing to seed — the already-seeded
-- title_keywords / location_allowlist simply surface as manual chips until the
-- user authors rules.
