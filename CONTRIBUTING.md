# Contributing to serai

serai is a one-person hobby project. The short version of how to work with it
from outside:

- **Bug reports:** welcome, best-effort response.
- **Feature suggestions:** welcome in issues, but the bar for "I'll build this"
  is "I want it for myself."
- **Pull requests:** disabled at the repository level.
- **Forks:** encouraged. The MIT license invites you to take serai somewhere
  different if it isn't going where you want.

The longer version is below.

## Why pull requests are disabled

serai is built and maintained by one person, and I keep direct merges to myself
so the codebase stays internally consistent — in formatting, in style, and in
which features earn their place versus adding complexity that isn't worth it.
Reviewing and merging outside code well takes real time, and for a project this
size I'd rather spend that time building.

That doesn't mean ideas aren't welcome. **They are — please file an issue.** I
want to know what people are running into and what's missing. I read every bug
report and feature suggestion, and the things that fit serai's direction land in
future releases. The "I want it for myself" filter applies, but a good idea well
explained genuinely moves the project.

The MIT license keeps the door open the other way too: fork freely, change
whatever, ship your own version. If serai doesn't do something you need and the
answer to "will the maintainer build this?" is no, your fork is a real option —
not a polite deflection.

## Reporting bugs

Please open an issue. The more of the following you can include, the faster I
can act on it:

- **What you tried to do** — the user action, not just the symptom.
- **What happened instead** — ideally with a screenshot if it's UI-shaped.
  Please check the screenshot for anything private first: serai's own UI shows
  session names, file paths, and live terminal output.
- **serai version** — shown at the bottom of the ⚙ settings panel.
- **Where the session lives** — local or a remote host over ssh, and whether the
  problem follows the session or stays with the host.
- **Browser and platform**, especially for anything layout- or terminal-shaped,
  and whether it's the desktop or mobile layout.
- **Relevant log output** — `journalctl --user -u serai -n 200 --no-pager`.

**Session state is a heuristic.** If a session reads as *idle* when it's
genuinely waiting on you (or vice versa), that's a known class of limitation
rather than a clear bug — see the README's *Known rough edges*. Reports are
still useful; include what the pane looked like at the time.

If the bug is **security-shaped** — an auth bypass, command injection, a path
escape in the file browser — please **don't** open a public issue. See
[SECURITY.md](SECURITY.md) for the private reporting path. serai can open a
shell on every host you can reach, so that category deserves care.

## Suggesting features

Feature requests are fine, with a few realities to set expectations:

- **The maintenance bar is high.** serai already has plenty of surface area, and
  I'm conservative about adding more — especially for use cases I won't
  personally exercise.
- **Roadmap is private.** I don't keep a public roadmap and won't commit to
  dates. If a request lands in my own backlog you may see it in a release; if it
  doesn't, it doesn't.
- **A "no" isn't an indictment of the idea.** It usually means the idea is good
  but not aligned with what I want this codebase to be — exactly the case where
  forking makes sense.

## If you're forking

Go ahead. The MIT license has you covered. A few pointers so the architecture
doesn't surprise you:

- **`serai/__init__.py` holds the version**, and it's the single source of
  truth: `pyproject.toml` reads it at build time, the API stamps it on
  responses, and the UI prompts a reload when it changes underneath an open tab.
  Bump it in anything that ships.
- **`./install.sh` copies the app out of your checkout** to a clean location and
  runs the service from there, so your working tree is only the source. It also
  restarts the service *only* when backend code changed — frontend-only updates
  deploy without dropping attached sessions.
- **The invariants are not incidental.** serai stores no remote credentials,
  binds to localhost by default, and never interpolates untrusted input into a
  shell string — host aliases and session names are hostile input, and every
  command is built as an argv list. That last one is the bug class that gave a
  comparable tool an unauthenticated RCE. If you change the command builders in
  `serai/sessions.py`, keep them argv-shaped.
- **tmux is the persistence substrate.** Every session is `tmux new -A -s <name>`,
  remote is the same wrapped in `ssh -t`. There's deliberately no parallel
  non-tmux session path.

Good luck, and have fun with it.
