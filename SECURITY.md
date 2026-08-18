# Security policy

Job Search HQ runs entirely on your own machine. It stores your data in a local
SQLite file, keeps your Anthropic API key in a local `.env`, and makes outbound
calls only to ATS job boards and the Anthropic API. There is no server to attack
and no account system.

## Reporting a vulnerability

If you find a security problem, please report it privately instead of opening a
public issue. Use GitHub's private vulnerability reporting on this repo (the
**Security** tab, "Report a vulnerability"). Include what you found, how to
reproduce it, and what an attacker could do with it.

I read these as soon as I can and will confirm receipt. This is a solo project, so
a fix can take time. I will keep you posted on where it stands.

## Scope

In scope: anything that lets a malicious careers page, ATS response, or job
posting read files, run code, or move data off the machine running `jshq`, and
anything that exposes the local `.env` or the SQLite database to another origin.

Out of scope: the server binding to 127.0.0.1 by design, and features that need an
API key you chose to add.
