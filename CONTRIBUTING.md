# Contributing to Job Search HQ

This is a solo side project in public beta. I keep the process light. The most
useful things you can send are clear bug reports and small, focused fixes.

## Ways to help

- **Report a bug.** Open an issue with your OS, Python version, what you did, and
  what you expected. A failing case from a real careers URL is worth a lot.
- **Suggest a feature.** Open an issue first so we can settle the shape before
  anyone writes code.
- **Send a fix.** For anything past a typo, open an issue before the PR. I'd
  rather agree on the approach than turn away work you already built.

## Development setup

Python 3.12 or newer.

```
python3.12 -m venv .venv
.venv/bin/pip install -e . --group dev
.venv/bin/pytest
```

Run the tests from the repo root. `jshq` serves the whole app, frontend and API,
at http://127.0.0.1:5747.

## Tests

The suite runs offline and without an API key. Keep it that way. ATS adapters
test against recorded fixtures and never hit live endpoints. Run the full suite
before you open a PR, and get it green.

## Principles the project holds to

A change that breaks one of these can't merge, however useful it is otherwise.

- **Zero phone-home.** No telemetry, analytics, crash reporting, or update
  checks, and no CDN assets. The only outbound calls are ATS job boards and the
  Anthropic API with the user's own key. A new network call has to be added to
  [PRIVACY.md](PRIVACY.md).
- **Localhost only.** The server binds 127.0.0.1. There are no accounts and no auth.
- **Works without an API key.** Every AI feature degrades to an actionable
  message and keeps running.
- **Scoring config lives in the criteria document.** Behavior changes go in its
  machine blocks.
- **No personal data.** No real names, employers, or locations. The example
  persona is fictional and sample domains use `.example`.

## Style

- The frontend is framework-free ES modules. Please don't add a build step or a
  framework.
- Two themes. Every color change lands in both the light and dark blocks in
  `tokens.css`, and color carries state or urgency, nothing else.
- Short, conventional commit messages (`fix:`, `feat:`, `docs:`). Keep each PR to
  one thing.

## License

Contributions are licensed under the project's AGPL-3.0 license.

## What to expect

I maintain this in my spare time. I read everything that comes in. Replies can be
slow, and I turn down changes that add scope or upkeep I can't carry. Say what you
need plainly and I will do the same.
