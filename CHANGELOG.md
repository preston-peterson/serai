# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version lives in `serai/__init__.py` and is stamped on API responses, so the
running instance always reports what it is.

## [Unreleased]

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
