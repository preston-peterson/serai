# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version lives in `serai/__init__.py` and is stamped on API responses, so the
running instance always reports what it is.

## [Unreleased]

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
