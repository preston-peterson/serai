# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version lives in `serai/__init__.py` and is stamped on API responses, so the
running instance always reports what it is.

## [Unreleased]

## [2.25.1]

### Fixed

- **CI red on 2.25.0: committed the presence backend that `main.py` already
  references.** `serai/main.py` calls `auth.touch()` on every authenticated
  request (the admin-presence hook, item 12), and that line shipped inside the
  2.25.0 commit — but the `serai/auth.py` definitions it needs (`touch`,
  `seen_at`, the in-memory `_last_seen`, and the `seen` field on `/api/users`)
  were still sitting uncommitted. The committed tree therefore called a function
  that didn't exist, and six API tests that go through the auth middleware
  failed on every Python in CI. Committing the backend makes the state
  consistent and finishes item 12's backend; the UI is still not wired to show
  presence.

## [2.25.0]

### Added

- **Hermes is a first-class session kind, alongside Claude, Grok, and OpenCode.**
  Pick `hermes` from `+ New`; it flows through the rail, board, palette, edit
  dialog, restart, and resume exactly like the other agents. Same one-row shape
  as 2.24.0: the `_AGENTS` table carries its command (`hermes`), its `hm-`
  storage prefix, and its per-kind resume mapping, mirrored by a `KINDS` entry
  in the UI (glyph ■, teal `hm` chip). `hermes`'s `--resume` takes a session id
  (no interactive picker), so `resume` — like OpenCode — falls back to
  `--continue`; the args-still-win rule is unchanged. Busy/done markers read the
  TUI content like the other agents and are extendable via
  `SERAI_WAIT_MARKERS_HERMES`.

- **Tag a session from the `+ New` form.** A `tags` field (shown for every kind)
  sets the session's tags at creation. Like the owner claim, the tags ride in the
  *same* `tmux new -A` command as the create, so nothing ever observes the
  session untagged and re-attaching to an existing name (no tags passed) never
  clobbers what is already there. Input goes through `clean_tags`, the one parser
  for hostile tag strings.

### Changed

- **Copy and paste give feedback and never kill the agent.** A plain drag is
  tmux's selection (mouse mode is on, so it owns the on-screen highlight and the
  full-history scrollback), not xterm's — which is why "highlight, then
  Ctrl+Shift+C" read the empty xterm buffer and did nothing. Now:
  - A plain drag still copies on release via tmux's OSC 52 → browser clipboard,
    and confirms with a brief "copied N chars" toast, so it is visible that the
    copy already happened (you never have to press anything).
  - `Ctrl+Shift+C` / `Ctrl+Insert` no longer fall through as a raw key: with an
    xterm selection (e.g. after a Shift+drag) they copy and confirm; with no
    selection they are a safe no-op and hint once how to highlight — they can no
    longer send a stray byte to the PTY or, on the Ctrl+C path, a SIGINT that
    kills the agent. Plain `Ctrl+C` is untouched and still interrupts.
  - Paste is unchanged in behaviour (raw bytes, never a bracketed-paste wrapper;
    Ctrl+V dedup) and its failure path already surfaces a toast.

### Fixed

- **Sessions whose agent lives only in `~/.bashrc` died the instant they were
  created.** `run.sh` recovers the login shell's PATH so a serai-launched session
  can find its tool, but it probed *non-interactively* (`-lc`) — and the standard
  `~/.bashrc` returns early when non-interactive. PATH dirs added there
  (`~/.opencode/bin`, `~/.grok/bin`, ...) were invisible to the probe, so an
  opencode/grok session died on create with a `command not found` that tmux
  erased before you could read it. The probe now runs *interactively* (`-ilc`),
  so `~/.bashrc` is sourced in full and serai recovers the complete PATH every
  session will actually run under. (Same failure class as the `~/.local/bin` fix
  in 2.18.1; the interactive bit is what this needed. All kinds are covered —
  they resolve their binary the same way, so this is kind-agnostic.)

## [2.24.0]

### Added

- **Grok Build and OpenCode are first-class session kinds, alongside Claude.**
  Pick them from `+ New` (and they flow through the rail, board, palette, edit
  dialog, restart, and resume like Claude already did). Each kind is defined in
  one place — the `_AGENTS` table in `serai/sessions.py` (command, storage
  prefix, and per-kind resume-flag mapping) mirrored by a `KINDS` object in the
  UI — so a new agent is a one-row change plus a board chip.

  - **Own names and chips.** `grok-` and `oc-` prefixes (invariant #5 grows to
    four storage prefixes); the board/rail show a two-letter badge — `cc` / `gr`
    / `oc` / `sh` — colour-coded (blue / coral / purple / neutral), and the
    palette + tab bar carry a per-kind glyph (✦ ● ▲ ❯).
  - **Own command.** `attach_argv`/`restore_argv` run the kind's own binary
    (`grok` / `opencode`) in the project dir, exactly as they run `claude`.
  - **Own resume flags.** grok mirrors Claude (`--continue` / `--resume`, the
    latter an interactive picker); opencode has no picker, so `resume` falls
    back to `--continue` (its "pick up the last session") rather than silently
    starting fresh. The args-still-win rule now recognises `--session` too, so
    opencode's `--session <id>` suppresses a flag serai would otherwise add.
  - **State reads the content, like Claude.** All three agents route through the
    TUI/content path (a permission prompt = needs input, a busy marker =
    running), not the shell activity-age path. grok/opencode have no known busy
    string yet, so they read "done" while actively working; the per-kind wait
    markers are extendable via `SERAI_WAIT_MARKERS_GROK` /
    `SERAI_WAIT_MARKERS_OPENCODE`.
  - **Resume is on demand for every agent.** A `/exit`-ed grok or opencode
    session is offered the same way a Claude one is (`recently_exited` now
    covers all agent kinds, not just claude).

## [2.23.1]

### Fixed

- **Paste into grok (in a shell, from another machine) did nothing, then
  typing died.** Ctrl+V was still reaching the PTY as `^V` (quoted-insert),
  so grok waited on a control sequence and ate every later key. `term.paste()`
  also wrapped the clipboard in bracketed-paste (`CSI 200~/201~`) when the
  parent shell had that mode on; grok doesn't close that sequence, same freeze.
  And a TUI that doesn't drain stdin made `os.write` on the PTY block the
  whole attach loop. Ctrl+V is swallowed by xterm (the paste event carries
  the text), paste is raw bytes on the websocket, and PTY writes are
  non-blocking.

## [2.23.0]

### Changed

- **Saved session profiles, not an ever-growing restore inventory.** Creating a
  session still remembers how to start it (host, kind, label, dir, tags, args).
  `/exit` and a reboot keep that profile. ✕ — on a live row or on a saved
  row in the jump palette — forgets it, and it is not offered again.

- **Resume is on demand.** Jump-to-session lists saved-but-not-live profiles
  (marked *saved*); picking one reopens it with `claude --resume` when it's a
  Claude session. + New fills path/args from a matching profile and defaults
  the session picker to resume. The dimmed exited cards that led the board,
  and the sidebar "resume N sessions from before?" banner, are gone — they
  were a constant nag of the same list.

### Fixed

- **✕ on a session that had already `/exit`ed now forgets the profile.** The
  live kill path already dropped the snapshot; a name that was only saved
  was previously a no-op for ownership checks (no live tmux session), so
  anyone who guessed it could also have dropped someone else's. `/api/kill`
  now applies the owner rule to saved profiles too.

## [2.22.2]

### Fixed

- **A session attached from another machine went silent after a minute or
  two** — typing did nothing, with no reconnect message. `/ws/attach` never
  pinged, so once a TUI (grok, and anything else that bursts then waits for
  input) went idle, a NAT or browser idle timeout killed the socket. Both
  ends now send a tiny keepalive every 20s. Localhost never showed this; a
  second machine did.

## [2.22.1]

### Fixed

- **A normal user saw the admin-only "users" and "network & hostname" sections**
  in the account panel. `.acct-section` sets `display: flex`, which silently
  defeats the HTML `hidden` attribute the code was setting — so the hiding did
  nothing. Both sections now stay hidden, as intended.

  They were never usable: every user- and network-management endpoint already
  refused a non-admin with 403, and the fields showed placeholder text rather
  than the real listen address or hostnames. So this was misleading, not a
  disclosure.

  A test now walks every element that the markup toggles with `hidden` and fails
  if its class sets `display` without a matching `[hidden] { display: none }`.
  This trap had already shipped once, as an un-dismissable restore banner; the
  sweep also found `.mobile-only` one rule away from the same fate.

## [2.22.0]

### Added

- **An owner chip on board cards and rail rows**, so an admin can see at a
  glance who created each session. Shown to admins only — everyone else sees
  nothing but their own sessions, so a chip on each saying "you" would be noise.
  Sessions with no recorded owner get no chip rather than a placeholder.

### Changed

- **Board cards no longer print "local"** on every card. It was ~40px spent
  stating the default on every session, and without it a long name plus an owner
  chip fits where it otherwise truncated. Remote hosts are still labelled. The
  restore banner already worked this way.
- **Rail rows no longer repeat the tag chip.** The group header immediately above
  a row already names that tag, so it was the most redundant thing in a 250px
  row — and with the owner chip added, keeping it collapsed session names to
  `escap…`. Mobile already dropped it for the same reason.
- The account button's tooltip says "Account & users" only for admins; everyone
  else can just change their own password.

## [2.21.0]

### Added

- **Sessions belong to the user who created them.** A new serai account no
  longer opens onto everyone else's work: you see the sessions you made, and
  admins see all of them. Ownership is recorded on the session itself
  (`@serai_owner`), so it survives a restart and a post-reboot restore.

  Sessions with no recorded owner — anything made outside serai, or before this
  release — are visible to **admins only**, so an existing fleet stays with the
  operator rather than appearing on a new user's board.

  It isn't only the list that's filtered: attaching, killing, renaming, tagging,
  restarting, restoring, setting a dir or args, and broadcasting all refuse a
  session belonging to someone else, since a session name is easy to guess.

  **This is an organisational boundary, not a security one.** Every session runs
  as serai's own OS user, so any account that can open a shell can still reach
  every session — and the file browser is unchanged, so any user can still read
  what serai can read. Treat serai accounts as convenience separation between
  people who already trust each other, not as a sandbox.

## [2.20.2]

### Changed

- **The restore banner's args control is a labelled `ARGS` pill**, not a bare
  `⋯`. The glyph read as decoration and nobody found it; the pill says what it
  opens and still costs no session name.
- **No more disabled dropdown.** When a session's args already say how it comes
  back, the row used to show a greyed-out `args` select — which looks like
  something you could use. It now states the flag that will actually be applied
  (`--resume`, `--continue`) as plain text, because there is nothing to pick.

## [2.20.1]

### Fixed

- **The restore banner's args box was wiped mid-typing.** The session poll lands
  every 5 seconds and rebuilt the banner from scratch, so an args box you were
  typing into lost its focus and caret under your hands. The list is now left
  alone while focus is inside it, and every keystroke is kept, so nothing is lost
  even if something else forces a rebuild.

## [2.20.0]

### Added

- **Args on the restore banner.** Each Claude session in the post-reboot banner
  now has a `⋯` that opens its own args box, pre-filled with what that session
  had. A dot on `⋯` marks a session that carries args, and hovering shows them
  without opening anything. (The args already survived a reboot — this is about
  seeing and changing them as you bring everything back.)

### Fixed

- **`claude --continue --resume` on restore.** The banner's dropdown and the
  session's stored args were both appended, so a session with `--resume` in its
  args and "continue last" selected came back with two conflicting flags — and a
  deliberate choice written into the args was silently overridden. The args are
  the command line: serai now adds a resume flag only when they don't carry one,
  and the row's dropdown reads `args`, greyed, when they do.

### Changed

- **Session names in the restore banner have more room.** The resume dropdown is
  fixed-width — it used to resize to the selected option, so picking "continue
  last" squeezed the name beside it and names jumped as you changed it — and the
  host is shown only when it isn't `local`. Together those more than pay for the
  space the new `⋯` takes.

## [2.19.1]

### Changed

- **A restart now runs exactly what the args say**, instead of adding
  `--resume` of its own. The args are the whole command line, so `--resume`
  belongs in them — adding one silently duplicated it for anyone who already had
  it, and overrode a deliberate "start fresh". The confirmation shows the command
  line it's about to run, and points out `--resume` when the args don't mention
  it.

## [2.19.0]

### Added

- **Extra `claude` arguments per session.** An **args** field on a Claude
  session — in **+ New** and in the edit dialog — passes whatever you put there
  to `claude` when the session starts: `--chrome`, `--model opus`, and so on. It
  lives on the session (a `@serai_args` tmux option) and is kept in the restore
  snapshot, so a reboot brings it back with the session.

  Every token is `shlex`-split and re-quoted before it reaches the command
  string, so shell metacharacters arrive at `claude` as literal arguments and
  never as commands — invariant #3 holds. Unbalanced quotes, `::` and control
  characters are refused outright rather than mangled.

- **Restart a session** — `⟳` on a Claude session's row, or **save & restart**
  in the edit dialog. tmux `new -A` only runs its command when the session is
  *created*, so a changed start dir or set of args can't reach one that's already
  running; this kills and recreates it, coming back with `--resume`. It asks
  first, because anything running in the session is lost. Unlike `✕` it keeps the
  restore record.

## [2.18.3]

### Added

- **Set every restore dropdown at once.** The post-reboot banner now carries a
  "set all N Claude sessions to" control above the list, so you choose once
  instead of clicking through two dozen rows. Individual rows stay editable
  afterwards for the odd one out.

### Changed

- **Restoring a Claude session now opens its resume picker by default**
  (`claude --resume`) rather than silently continuing the most recent
  conversation. With "continue" you got *a* conversation without being told
  which one; the picker shows you what you're resuming into. "continue last"
  and "fresh" are still there in the dropdown.
- **Board cards no longer lift on hover.** They highlight instead. The 1px
  transform made a column of cards read as bouncing as the pointer ran down it.
  Rail rows are pinned to a fixed height for the same reason.

### Fixed

- **Sessions no longer inherit serai's own working directory.** The PTY child
  kept serai's cwd, so a session created with no start dir opened *inside
  serai's install tree* — the root of the wrong-directory bug in 2.18.2. It now
  starts in your home directory, and an explicit start dir is applied as before.
- **A stale browser tab could stamp the bad directory back.** `ws_attach` writes
  the client-supplied path onto the session as `@serai_dir`; a tab still holding
  a pre-fix session list echoed serai's own directory back and it stuck.
  `clean_dir` now normalises that away, so a trailing slash or `.` segment can't
  smuggle it through either.

## [2.18.2]

### Fixed

- **Restored Claude sessions all opened in serai's own install directory**
  instead of their project. Two faults, in a chain. First, a Claude session
  created with no start directory never ran `claude` at all — `attach_argv` fell
  through to a bare `tmux new` and the session came up as an ordinary *shell*,
  sitting in serai's working directory. Second, the session snapshot recorded
  that directory as the session's own: `store.upsert` took the live pane's cwd
  at face value, so one degraded session permanently overwrote a good project
  path — and every later restore then opened there, spreading the bad value into
  `@serai_dir` as well. Both are closed: a Claude session runs `claude` with or
  without a start dir, and the snapshot now prefers the configured start-in dir
  over the pane's cwd, refuses serai's own directory in either role, and never
  lets an empty value overwrite a stored one. Same rule as the sticky-tags fix
  in 2.17.1 — **a degraded live value must never destroy good stored state.**

## [2.18.1]

### Fixed

- **Claude sessions died the instant they were created, after a reboot.** A new
  or restored Claude session flashed and vanished — `[exited]`, `[session
  ended]` — while `claude` ran fine from an ordinary terminal. serai's PATH is
  the PATH every session it starts runs under, because tmux takes a new
  session's environment from the client that creates it. As a lingering user
  service serai starts at *boot*, before the desktop pushes `~/.profile`'s PATH
  into the systemd user manager, so it came up without `~/.local/bin` and
  `claude` wasn't on it. tmux then printed `claude: command not found`,
  destroyed the session, and cleared the screen on the way out — taking the
  explanation with it. `run.sh` now asks the login shell for its PATH at
  startup, which is immune to login ordering because `~/.profile` adds those
  directories itself. Set `SERAI_SKIP_PATH_PROBE=1` to opt out.

## [2.18.0]

### Added

- **Resume a Claude session after `/exit`.** When you exit a Claude Code
  session, it now lingers on the board as a dimmed **resume** card instead of
  just vanishing. One tap reopens it in its project directory running
  `claude --resume`, dropping you into the conversation picker — pick up where
  you left off. Dismiss the card with ✕ if you meant to close it. (Only recent
  exits are offered, and never a session you deliberately killed.)
- **Shift+Tab on the mobile key bar** — the key Claude Code uses to cycle its
  permission mode, which a soft keyboard can't send. It sits between `tab` and
  `^C`.

## [2.17.2]

### Fixed

- **Mobile scrolling did nothing in a Claude Code session.** A regression from
  2.16.0: that release started scrolling via tmux's own history, which is exact
  and smooth — but a full-screen app (Claude's TUI, vim, less) runs on the
  *alternate screen*, which by design has **no tmux scrollback at all**. The
  scroll command ran, entered copy mode, and moved nothing.

  serai now detects an alternate-screen pane and scrolls it the way a desktop
  wheel does — the app scrolls its own view. Plain shells keep the exact
  line-by-line tmux scrolling from 2.16.0, which is still the nicer feel where
  there's real scrollback to move.

## [2.17.1]

### Fixed

- **A code update could drop a session's tags — or, worse, the whole tmux
  fleet.** serai's first attach starts the tmux server inside serai's own
  systemd cgroup, and the unit didn't set `KillMode`, so a restart signalled the
  entire cgroup: a backend update or a manual `systemctl restart serai` could
  take down the tmux server and every session on it. The unit now uses
  `KillMode=process`, so only serai itself restarts and your sessions keep
  running (invariant: tmux is the persistence substrate). **Apply this update
  with `./install.sh`, not a bare `systemctl restart` — the installer reloads
  the unit before restarting, which a manual restart wouldn't.**
- Tags are now **self-healing**: if a session ever comes back without its tags,
  serai re-applies them from its restore snapshot and shows them straight away.
  The snapshot also stopped overwriting good tags with a transient empty value.

## [2.17.0]

### Added

- **One-click "Update now"** in the ⚙ panel. When a newer release exists, an
  admin sees a button that downloads it, verifies its sha256, installs it, and
  restarts — your terminals reconnect on their own because tmux keeps them.

  It's the same path as a manual upgrade: download → verify → `install.sh`, run
  server-side. Admin-only, and shown only when the install can restart itself (a
  systemd unit); a dev checkout or `./run.sh` still shows the release-notes link
  to update by hand. Nothing from the request reaches the command, and the
  download is refused on any checksum mismatch.

## [2.16.0]

### Changed

- **Drag-to-scroll on touch is now line-by-line.** It previously rode the mouse
  wheel, which is quantised at about four lines per notch, so the pane arrived in
  visible jumps. A drag now asks tmux to scroll an exact number of lines over the
  websocket that is already open, giving single-line steps — four times finer —
  while still tracking your finger about 1:1.

  Falls back to the wheel if the socket isn't up. Horizontal drags are still left
  alone, so selecting text works.

## [2.15.4]

### Fixed

- **Drag-to-scroll on touch was far too fast**, which made it feel chunky and
  hard to control: the pane moved roughly 4.5x the distance your finger did.
  Movement now tracks the finger about 1:1, so a drag lands where you expect.

## [2.15.3]

### Fixed

- **On a phone, an attached session couldn't be scrolled** — pane history above
  the fold was unreachable. A one-finger vertical drag over the terminal now
  scrolls tmux's history, and dragging back down returns to the live output.

  There was nothing for the browser to scroll: tmux owns the scrollback and
  repaints the visible pane, so the terminal viewport is exactly as tall as its
  content. The drag is now translated into the same wheel events a desktop
  scroll produces, which tmux already understands. As on desktop, this needs
  ⚙ → *mouse scrollback* on (the default); with it off, tmux ignores scrolling
  from the terminal either way.

  Horizontal drags are left alone, so text selection still works.

## [2.15.2]

### Fixed

- **Settings could be silently reverted by another open tab.** The UI mirrors
  its preferences to the server as one blob, and the server replaced its copy on
  every save. A tab that had been open since before a preference existed didn't
  know that key, so the next time it saved anything — a splitter drag was enough
  — it dropped the setting for every other tab. The server now **merges** what a
  tab sends. This affected any preference, not just the one it was reported
  through.
- **⚙ → updates → check didn't keep your choice.** Picking *daily* and returning
  later showed *weekly* again. Three causes: the above, a save/refetch race that
  could read back the pre-change value and overwrite it, and a picker that read
  the server's copy rather than your stored choice.

## [2.15.1]

### Fixed

- `HEAD /` and `HEAD /favicon.ico` returned **405 Method Not Allowed** while
  `GET` returned 200. Uptime monitors commonly probe with `HEAD` and would read
  a healthy serai as down.

### Added

- **Releases now carry a verifiable source tarball.** Each release attaches
  `serai_<version>.tar.gz` and `serai_<version>_checksums.txt`, so a download can
  be checked with `sha256sum -c serai_<version>_checksums.txt` before you run
  anything. The tarball is built from the tag and is reproducible — rebuilding
  the same tag yields identical bytes.

## [2.15.0]

### Added

- **Update notifications.** serai can check whether a newer release has been
  published and shows a dot on the ⚙ button when one has. Opening the panel
  gives the version and a link to its release notes.
- **An `updates` section in the ⚙ panel** to choose how often that check runs —
  **daily**, **weekly** (the default), **monthly**, or **never** — plus a
  **check now** button.

  The check runs on the server, once per instance, and the result is cached and
  persisted: every open tab polling GitHub independently would hit the
  unauthenticated rate limit on a tool people leave open for days. It fails
  quietly when offline, and `SERAI_UPDATE_CHECK=off` disables it install-wide,
  overriding the panel. Forks can point it at their own repo with
  `SERAI_UPDATE_REPO=owner/name`.

## [2.14.2]

### Changed

- The phone's bottom-nav button for the board is labelled **Board**, matching the
  desktop control it mirrors. Its previous label named nothing else in the UI and
  disagreed with every other reference to the board.

## [2.14.1]

Entries below summarise the 2.x line rather than reconstructing every point
release; the git history has the detail.

### Added

- **The board** — the landing view: one card per session, colour-coded by state,
  sorted so whatever needs you floats to the top, each carrying a live preview of
  the pane. Cards grow when few are on screen so the preview shows more.
- **Session states** — `working` / `blocked` / `done` / `idle`, detected per kind:
  Claude sessions by pane content, shells by activity age and foreground process.
- **Tags as workspaces** — a picker in the top bar and grouping in the rail, so a
  large fleet divides into projects. Tags live on the tmux session itself.
- **Start-in directory** per session, driving both the terminal and the file
  browser, and reused when restoring after a reboot.
- **Mobile layout** — single-column board, the rail as a drawer, a terminal key
  bar (`esc`, `tab`, `^C`, arrows), a bottom nav, and long-press file actions.
- **Per-session resume choice** in the post-reboot restore banner: continue the
  last conversation, open the resume picker, or start fresh.
- **Streamed cross-host folder relay** — a folder crosses hosts as a piped tar at
  a fixed memory cost instead of being buffered whole, and reports bytes moved.
- Pane tabs, a jump-to-session palette, and a settings panel carrying pane
  layout, fleet broadcast, and the running version.

### Changed

- The UI moved to the Slate palette and a board-first layout; the old
  Host/Tagged/All sidebar modes were retired in favour of tag grouping.
- Session naming now also recognises `<project>-claude` / `<project>-term`
  alongside serai's own `cc-` / `shell-` prefixes, so sessions created outside
  serai are picked up without renaming.

### Fixed

- Downloads on mobile: fetched in-page and wrapped so the filename survives,
  working around a browser refusing downloads from a self-signed origin.
- Card tails skip prompt furniture, so an idle Claude card shows its last real
  output rather than an empty prompt box.
- State dots agree across the pane bar, tab, and rail.
- Rail rows no longer collapse the session name to zero width.

[Unreleased]: https://github.com/preston-peterson/serai/compare/v2.14.1...HEAD
[2.14.1]: https://github.com/preston-peterson/serai/releases/tag/v2.14.1
