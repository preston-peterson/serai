# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version lives in `serai/__init__.py` and is stamped on API responses, so the
running instance always reports what it is.

## [Unreleased]

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
