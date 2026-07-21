---
name: Bug report
about: Something in serai is broken
title: ''
labels: bug
assignees: ''
---

> ⚠ **Security-shaped bug?** (auth bypass, command injection, path escape in the
> file browser) — please **don't** file it here. See
> [SECURITY.md](../blob/main/SECURITY.md) for the private reporting path.

> ⚠ **Screenshots:** serai's UI shows session names, file paths, and live
> terminal output. Please check anything you attach for private data first.

## What I tried to do

<!-- The action, not just the symptom. -->

## What happened instead

<!-- The observed behaviour. A screenshot helps for anything UI-shaped. -->

## What I expected

## Where

- **serai version:** <!-- bottom of the ⚙ settings panel -->
- **Session:** <!-- local, or a remote host over ssh -->
- **Does it follow the session or stay with the host?**
- **Browser / platform:**
- **Layout:** <!-- desktop or mobile -->

## Logs

<details>
<summary><code>journalctl --user -u serai -n 200 --no-pager</code></summary>

```
paste here
```

</details>

## Anything else

<!-- If this is about a session reading as the wrong state (idle vs blocked),
     note what the pane looked like at the time — that detection is a
     documented heuristic, see the README's "Known rough edges". -->
