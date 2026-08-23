# Job Search HQ (jshq)

**Status: beta, a solo project under active development.** Issues and
feedback are welcome; there are no formal support or stability guarantees yet.

A personal, local-first job-search dashboard: track target companies, pull their
openings directly from applicant-tracking-system job boards, score every
posting against your own written fit criteria (optionally with AI, using your
own Anthropic API key), and manage applications, contacts, and reminders, all
on your own machine.

## Walkthrough

**Setup is the application.** A fresh install boots into a wizard that _is_ the
whole app (no empty dashboard to bounce off of) and leads with the privacy
promise.

![First-run wizard: the welcome step](docs/images/walkthrough-wizard.png)

**The scoring reader shows its work.** Every posting is scored against the
wish-list criteria you ranked, and each criterion is mapped to evidence from the
actual posting. Every score shows its reasoning.

![A scored posting with its per-criterion reasoning](docs/images/walkthrough-scoring.png)

**The tracker never fakes progress.** A persistent "Setup N of N" pill tracks
real completeness, fed verbatim by the backend's own count.

![The persistent Setup completeness tracker](docs/images/walkthrough-tracker.png)

> **Why these choices?** The senior decisions here are design and systems
> judgment carried into code: a setup flow designed as reflection rather than a
> form, a token architecture that catches its own theme gaps, scoring that
> lives in a document _you_ write, and a color-blind-safe state model with a
> simulator guarding it. The **[case study](docs/CASE-STUDY.md)** walks through
> each and names the things deliberately left unbuilt.

## Principles

- **Local only.** One process on localhost. Your data lives in a SQLite file
  you own. No accounts, no server, no sync.
- **Zero tracking.** No telemetry, no crash reporting, no update pings, no
  install beacons. The app talks to job-board services for the companies you
  add (finding and pulling their openings, plus a per-company logo lookup)
  and to the AI provider you configure: the Anthropic API with your own
  key, or any OpenAI-compatible endpoint you point it at, local runtimes
  like Ollama included (AI features are optional; the app works without
  either). [PRIVACY.md](PRIVACY.md) is the complete inventory of every
  request.
- **Cross-platform.** Python + FastAPI + a framework-free web frontend.
  Mac, Windows, and Linux.
- **Your criteria are the product.** Scoring is driven by a criteria document
  you author: hard filters (comp, location, sector, seniority) and weighted,
  ranked criteria in your own words. You can read and change exactly what drives
  every score.

## Install and run

```
pip install .
jshq
```

Then open http://127.0.0.1:5747. First run creates a data directory
(`~/Library/Application Support/jshq` on macOS, `%LOCALAPPDATA%\jshq` on
Windows, `~/.local/share/jshq` on Linux, or wherever `JSHQ_DATA_DIR`
points) seeded with editable copies of the example fit-criteria doc and the
voice guide.

Two commands keep an install current: `jshq refresh` runs one ATS ingestion
pass (meant to run twice a day), and `jshq backup` takes one verified backup
(a SQLite snapshot plus dated copies of your criteria doc, voice guide,
roadmap, resume content, and a mirror of uploaded application files) into
`backups/` inside the data directory, keeping the newest 30 of each. The
`.env` holding your API key is deliberately not included; copy it somewhere
safe yourself.

You don't have to schedule either by hand:

```
jshq schedule --install
```

writes the native scheduler entries for both (launchd on macOS, cron on
Linux, Task Scheduler on Windows) with refresh at 10:00 and 16:00 and backup
at 02:00. Change the times with repeatable flags
(`jshq schedule --install --refresh-time 08:00 --refresh-time 12:00
--refresh-time 18:00`) or from **Settings → System → Scheduling** in the app;
`--status` shows what's installed and `--uninstall` removes it. Re-running
`--install` replaces the entries, never duplicates them. On a system without
a supported scheduler the command prints manual instructions instead: point
your own scheduler at `jshq refresh` twice a day and `jshq backup` nightly
(for example `0 10,16 * * * jshq refresh` and `0 2 * * * jshq backup` in
cron), with `JSHQ_DATA_DIR` set if you use a custom data directory.

The optional AI features run on a provider you choose in **Settings →
System** (the setup wizard offers the same choice): Anthropic with your own
API key, saved to a `.env` in your data directory on this machine and sent
only to api.anthropic.com, or any OpenAI-compatible endpoint you run or
trust (Ollama, LM Studio, a hosted provider) with a free-text model id.
Switching providers keeps both configurations saved, and an Advanced control
can route analysis and writing work to different providers and models. The
same screen sets your persona (the name the AI writes as, or none) and your
voice guide. Without a key or endpoint the app runs fully; AI features
simply stay off with an in-app note.

PDF rendering (resume/cover letters) uses an installed Chrome, Chromium,
or Edge; set `JSHQ_CHROME` if yours lives somewhere unusual.

## Development

```
python3.12 -m venv .venv
.venv/bin/pip install -e . --group dev
.venv/bin/pytest
```

Copy `.env.example` to `.env` and set `JSHQ_DATA_DIR=./data` to keep dev
data inside the checkout.

## Credits

Built by [Chris Hays](https://noestudios.com). The repository is at
[github.com/noestudios/jshq](https://github.com/noestudios/jshq).

The onboarding's two reflection exercises (the ranked wish list and the
fulfillment matrix) are adapted from tier-list ranking and fulfillment-matrix
exercises by [Kristin Chen](https://www.kristinmchen.com). Visit
[www.kristinmchen.com](https://www.kristinmchen.com) for her career coaching,
startup advising, and fractional CPO services. Thank you, Kristin.

## License

AGPL-3.0. See [LICENSE](LICENSE).
