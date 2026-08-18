# Privacy

Job Search HQ is local-first and phones home to nobody. This file is the
complete inventory of every network request the installed app makes, when,
and why. If you find the app making a request this document doesn't cover,
that's a bug; report it. (Developer-only scripts in the repo, which are
never shipped with the app, disclose their own network calls in their
docstrings.)

## What stays on your machine

Everything. Your companies, jobs, applications, contacts, reminders, notes,
scores, criteria document, voice guide, resume content, uploaded documents,
and usage ledger live in a single data directory you own
(`~/Library/Application Support/jshq` on macOS, `%LOCALAPPDATA%\jshq` on
Windows, `~/.local/share/jshq` on Linux, or wherever `JSHQ_DATA_DIR`
points). The database is a SQLite file. Your Anthropic API key, if you add
one, is stored in a `.env` file in that directory. Backups made by
`jshq backup` are written to `backups/` inside the same directory. Nothing
is synced, uploaded, or mirrored anywhere.

There are no accounts, no telemetry, no crash reporting, no analytics, no
update checks, and no install beacons. The frontend loads no CDN assets;
fonts ship with the app, and a test fails the build if any page resource
would leave the origin. Town lookups for the location filter use a bundled
offline table, so no geocoding service is contacted.

## Every network request the app makes

**1. The job boards you track.** The point of the app: it pulls openings
for each company you add from that company's applicant-tracking-system
board (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, and similar
hosts). A scheduled `jshq refresh` does this at most twice a day, plus any
manual "Retry now". Requests carry an honest User-Agent and respect
robots.txt. The board's host sees what any visitor to that careers page
would send: your IP address and the request itself.

**2. Finding a company's board (when you add or re-check a company).** The
app fetches the website or careers URL you typed and scans that page for
ATS signatures. If nothing identifies the board, it falls back to probing:
up to four slugs derived from the company's *name* are tried against the
public APIs of Greenhouse, Lever, Ashby, and SmartRecruiters to see if a
non-empty board answers. This means name-derived strings can be sent to
those four vendors even when the company uses none of them. This happens
when you add a company, when you change its website or careers URL, and
when you press "Check again" on its page. It never runs on a schedule. The
same lookup also runs *before* a company is saved, to offer a careers link
as you type: the first-run wizard and the company page run it when you
leave the website field. The hosts contacted are identical to the add-time
check; it just happens one step earlier.

**3. Logo lookup (per company).** For the avatar next to each company, the
app tries the company's own site first (its apple-touch-icon or declared
icon), then falls back to DuckDuckGo's public icon service
(`icons.duckduckgo.com`), sending only the company's domain. This runs when
you add a company and when you press the logo refresh control on a
company's page. It never runs on a schedule. A miss is invisible: the UI
draws an initials monogram instead. This is the one third-party service in
the app that isn't a job board or Anthropic; if that trade bothers you,
delete the company's website field and the lookup has nothing to resolve.

**4. Anthropic (only if you add your own API key).** The optional AI
features (scoring, compose, tailoring, the refine pass) call
`api.anthropic.com` with your key. What's sent is the content the feature
needs: your criteria document, the job posting being scored or tailored
against, your persona name (or "the candidate" if you set none), your
voice guide, and your resume content when tailoring. Spend is recorded
locally so Settings can show it. Without a key, none of these calls exist
and the app runs fully.

That's the whole list.

## The trade-offs, stated plainly

- Job-board hosts can see that some IP address polls their board twice a
  day. That's you, watching companies you chose.
- The detection probe sends company-name guesses to four ATS vendors; the
  logo fallback sends company domains to DuckDuckGo. Neither carries your
  name, your notes, or anything about you, but they are third-party
  requests, and they're listed here so you can judge them yourself.
- Anthropic's handling of API traffic is governed by their own terms; the
  app sends nothing there unless you bring a key.
