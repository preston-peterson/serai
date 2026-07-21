# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report privately through GitHub's [private vulnerability
reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
— the **Security** tab → *Report a vulnerability*. That opens a channel visible
only to the maintainer.

Please include what you were doing, what happened, and how to reproduce it.
This is a personal project maintained in spare time: expect a first response
within a couple of weeks, and no guaranteed fix timeline.

## What serai is, in threat terms

serai is a web front end that can **open a shell on every host you can reach**.
Anyone who can talk to it, and authenticate, effectively has your shell access.
That framing drives everything below.

**Design constraints the project holds itself to:**

- **It stores no remote credentials.** No ssh keys, no remote passwords, no host
  database. Remote work goes through your ssh-agent, and every ssh call runs with
  `BatchMode=yes` so nothing can prompt. The only secret serai keeps is its *own*
  login: scrypt password hashes and a cookie-signing key under `~/.config/serai/`.
- **It binds to `127.0.0.1` by default** and serves HTTPS. The login and TLS are
  defence in depth, not a licence to expose it — put it behind a VPN, reverse
  proxy, or LAN bind.
- **Untrusted input never reaches a shell string.** Host aliases, session names,
  and paths are hostile input; every command is built as an argv list, and host
  aliases are validated against your ssh config before use. An unchecked alias
  would otherwise reach `ssh` as a positional argument — an option-injection
  vector that has produced unauthenticated RCE in comparable tools.
- **Every HTTP route and the websocket are gated** behind a session cookie.

## Scope

**In scope:** authentication bypass, command or argument injection, path
traversal in the file browser, session-cookie weaknesses, and anything that lets
one authenticated user reach a host or file they shouldn't.

**Out of scope / known by design:**

- The default certificate is **self-signed**, so browsers warn. Bring your own
  cert with `SERAI_CERT` / `SERAI_KEY`.
- `SERAI_AUTH=off` disables the login entirely. It exists for a trusted
  localhost; using it on a reachable interface is not a vulnerability.
- Exposing serai directly to the internet is out of scope — it is documented as
  unsupported.
- Session-state detection ("blocked", "working") is a heuristic and can be wrong.
  That is a correctness limitation, not a security boundary.
