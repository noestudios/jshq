Job Search HQ is your private command center for the job hunt. It watches the companies you care about, pulls in their new openings, scores each one against what you actually want, and keeps your applications, contacts, reminders, and tailored resumes in one place. It runs only on your own network, and it never sends anything for you. Every email and application goes out only when you send it.

New here? The first launch opens a short **setup wizard**: your API key, your field, hard limits, two reflection exercises, and the first company to watch. Everything is skippable except that one company. You can leave any time with **Exit setup**: a **Setup N/6** pill stays in the top bar and reopens a welcome-back hub with per-step jump buttons until all six steps are done. After setup, start on the **Today** tab, click any job to open its details, and come back to **Help** (this page) whenever something isn't clear.

# The tabs

Everything in the app lives in the row of tabs across the top:

- **Today**: your home base. What's new, what's due, and any alerts. Start here.
- **Jobs**: every opening pulled from your tracked companies (see “What gets pulled in”), with fit scores and filters.
- **Applications**: jobs you're actively pursuing, plus resume and cover-letter tailoring.
- **Companies**: the organizations you track and their open roles.
- **Contacts**: people you know, and how you met them.
- **Calendar**: your reminders on a month grid, plus a feed you can subscribe to.
- **Help**: this manual.
- **Settings** (the gear): what counts as a good fit, and how the app is doing.

# A typical visit

1. Open **Today**. Skim **New jobs** (found today) and **Maybe** (close calls worth a second look).
2. Click any job to open it. If it's promising, hit **Start application**. If it's not, hit **Dismiss** and pick a reason. That reason quietly teaches the scorer what to skip next time.
3. Clear anything **due** (follow-ups, thank-you notes, interview prep) with **Done** or **Snooze**.
4. Glance at the top of Today for **alerts** (see "Staying current" below).

You don't have to fetch anything by hand once `jshq refresh` is scheduled (the README shows how). Each run checks every tracked board; the **Refresh** control in Settings → System does the same on demand.

# What gets pulled in

The app fetches each tracked company's **entire board**, then keeps only the postings that pass your **sourcing rules** (Settings → Sourcing). Two consequences worth knowing:

- **No include rules means no gate**: everything a board posts is kept, and your scoring does the sorting. The wizard's field answer writes your first rule (only titles containing your words are kept); edit or delete it any time.
- **Workday boards are the exception.** Workday's API only answers searches, so those boards fetch nothing until at least one include rule (or title keyword) exists. If a Workday company shows zero jobs and a healthy connection, that's why; the refresh log says so too.

**Exclude** rules always win over includes, and dismissing jobs teaches the app to propose new excludes for your approval.

# Reading a job

Every job gets a **fit score from 0 to 100**: the app's read on how well it matches what you want. The colored chip next to the title is the quick version:

- **70 and up**: a strong fit, worth pursuing.
- **60–69**: a mixed fit; worth a closer look.
- **Under 60**: a weak fit.
- **0**: a **rejection**. The job broke one of your hard rules (wrong location, below your pay floor, or an off-limits industry). These jobs are disqualified and hidden by default.
- **A dash (–)**: not scored yet.
- **"elevated"**: you marked this one a positive fit by hand (see below).

A small **"maybe"** tag means a near-miss: the job is strong but falls just short on one thing, and scored under 70. Once a job reaches 70 it counts as a real candidate, so the "maybe" tag drops off.

If a job under 70 deserves a place anyway, open it and click **Elevate to positive fit**. That pins it with an "elevated" chip and keeps it out of the low-fit pile. It's a manual override; the underlying score doesn't change.

The rest of a job row gives you the basics at a glance: **remote / hybrid / onsite**, a **seniority level**, and the **salary range** (or "comp unknown" when the posting doesn't say).

# What counts as a good fit

The scoring rules live in **Settings → Scoring**, and they work in two layers.

**Hard gates: the deal-breakers.** A job that fails any single one of these scores 0 and drops out:

- **Pay**: a floor (below it, rejected) and a target (below the target, kept but flagged).
- **Commute**: how far you'll travel, measured in **minutes** of drive time. The app estimates the drive to each town and counts nearby ones as acceptable. **US towns only** for now: a home base outside the bundled US place table can't be resolved, so unresolved towns pass the gate.
- **Location list**: specific towns that always pass, plus exceptions for individual companies.
- **Off-limits industries**: sectors to skip entirely.
- **Title level**: the seniority you're aiming for.

One nuance worth knowing: **"unknown" never rejects.** A posting with no salary or no location still clears the gate; it gets flagged as uncertain.

**Ranked preferences: what makes a job great.** Below the hard gates is an ordered list of the things you care about, like mission or team size. Each one has an **importance dial** (from 0.25 to 4, where 1 is normal) telling the scorer how much to weigh it. Turn it up toward 2 to count something about double, or down to 0.5 to count it half.

**Rules in plain language.** Instead of editing keyword lists, you can write rules like "always include" or "never include" by job title or location. The app also proposes its own rules after you dismiss jobs (for example, "this keeps showing up but it's really a sales role"). You approve or ignore each.

> One thing to remember: changing a scoring rule does not re-score the jobs you already have. To apply your changes to the existing list, open Settings → System and run Rescore. The app tells you first how many jobs it will re-check. New jobs always use your latest rules automatically.

# Companies and contacts

**Companies** are where every job comes from. On each one you'll find:

- **Open roles**: a live count, kept up to date automatically.
- **Connection status**: whether the app can still read that company's openings. Watch for **"stale"**: it looks connected, but the list hasn't updated in a while or came back empty, so it's worth checking by hand. **"no ATS"** means the app couldn't read that company automatically. Fixing or adding its careers URL gets it re-checked on save (there's also a **Check again** button in Company settings), and until it connects you'll need to check its careers page yourself.
- **LinkedIn role checks**: one-click searches for the titles you're watching there. Add a title and its search link appears.
- **Top jobs**: that company's most promising openings, listed right on its page.
- **Your notes**: a priority from 1 to 5 and a values-fit read.

**+ Add job** lets you enter a posting the app didn't find on its own: paste the URL, hit **Fetch**, and it fills in what it can for you to review and save. Jobs you add this way never expire.

**Contacts** are people, each tied to a company and tagged by how you met them (referral, LinkedIn, an event, and so on; the list is yours to edit in Settings → Sourcing). Every contact keeps a running activity log, and you can add one straight from a company's page.

# Applying and tailoring

When a job is worth pursuing, open it and click **Start application**. It moves into **Applications**, where you track its stage (drafting → applied → screening → interview → offer) and your next step.

The headline feature here is **tailoring**: the app can draft a resume and cover letter aimed at one specific posting:

1. **Generate**: it reads the job description and proposes specific resume edits plus a cover-letter draft. (Give it a few seconds. Needs your API key; see "AI features and your own key".)
2. **Review**: every proposed edit is a checkbox showing the old text, the new text, and why. Check the ones you want; the rest are left out.
3. **Refine** (optional): ask for changes in plain language, like "make the summary less salesy," and it rewrites the draft.
4. **Apply**: it produces downloadable PDFs of the resume and cover letter.

**The app never sends anything.** It only creates the files; you download them and send them yourself. The same goes for any message it helps you compose: it drafts, you send.

## Resume content file

Tailoring rewrites **your master resume**, which lives as `resume/content.json` in the app's data folder. A starter file with placeholder text is created on first run. Replace its placeholders with your real details before tailoring anything, or the drafts will be built on "Your Name" and "A skill".

The file is JSON with a `version` (always `1`), your `name`, `title`, and `contact` (email required; phone, linkedin, website, location optional), followed by `sections`. Every section has a unique `id`, a `heading`, and one of five types:

- `paragraph`: a `text` block (your summary).
- `columns`: an `items` list laid out in columns (skills).
- `keyvalue`: `rows` of `{label, text}` pairs (education, certifications).
- `bullets`: a flat `bullets` list of `{id, text}`.
- `roles`: your `roles`, each with an `id`, `title`, optional `dates`, and its own `bullets`.

Text supports `**bold**`, `*italic*`, and `[links](https://…)`. If the file has a problem, the tailoring buttons say exactly what and where. Fix the file and retry; the app never edits it (tailored versions exist only in the generated PDFs).

# AI features and your own key

Fit scoring, resume and cover-letter tailoring, message drafting, and URL parsing for **+ Add job** all run on Anthropic's API **with your own key**. The app has no server and no account of its own. Add a key in **Settings → System** (there's a test button); usage is billed to your key, and the System tab tracks what each feature has spent.

**Without a key, nothing breaks**: jobs still pull in and everything manual works, but nothing gets a fit score (rows show a dash) and the drafting features explain what's missing instead of running. Add a key later and run **Rescore** to score what accumulated.

# Reminders and the calendar

The app **suggests** follow-ups (after you apply, after you meet someone), but it never creates a reminder on its own. Each suggestion is a card you **Accept** or **Ignore**. You can also add your own reminder anywhere, and **Snooze** any reminder to tomorrow or next week.

The **Calendar** lays everything out on a month grid, with two options at the top:

- **Copy feed URL**: a live feed for a calendar app **on this computer** (Apple Calendar: File ▸ New Calendar Subscription). The feed is served by the app on your machine, so web calendars like Google (which fetch from their own servers) can never reach it; use Download for those.
- **Download**: saves a one-time snapshot file (`.ics`) you can import into any calendar. It won't update on its own; re-download to refresh it.

# Finding things

Every list has **filter pills** at the top. Click one to narrow by fit, salary, status, and so on, and they remember your choices. In the Jobs list a **hide 0-fit jobs** switch (on by default) keeps the rejected jobs out of view, and the **Sort** pill orders by fit, newest, or salary.

The **search box** matches a job's title, company, or location, with two shortcuts:

- A **comma** means either word: `director, engineering` finds jobs with one or the other.
- A **plus** means both: `director + remote` requires both.

# Staying current

- **Refresh.** Job boards are re-checked on every `jshq refresh` run. Schedule it twice a day (the README shows how) and the app stays current on its own. While a pull is running, a green **"Refreshing job boards…"** bar appears at the top of Today, followed by a short summary when it finishes.
- **Stale alert (amber).** The listings haven't updated in a while; the app re-checks on its own, so usually there's nothing to do.
- **Offline alert (red).** The app couldn't reach the job boards at all, often because the computer was asleep or offline. Click **Retry now** once you're back online.
- **Other alerts.** A company whose jobs stopped loading, or a backup that looks old, each shows its own banner with a link to where you can look. Backups run nightly once you've scheduled `jshq backup` (the README shows how); the banner tells you when the last one succeeded.

You can dismiss any of these banners; it stays hidden until your next browser session.

# How scoring works, in detail

To see the exact rubric the scorer follows (in plain English, with your current settings), open **Jobs**, click the **Fit** filter, and choose **How scoring works**. That viewer is read-only; to change anything, use **Settings → Scoring** as described above.
