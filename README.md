# fork-sync

This branch only holds the automation that keeps this fork current. It is the fork's
default branch so that GitHub runs its schedule; it never goes into a PR.

Full documentation (the record of truth for this setup) is the BookStack page
"Music Assistant forks: edge branch and ha-apps".

## edge

`edge` is upstream `dev` plus every feature branch merged into it by hand. It is never
rebased or force-pushed. Every day at 05:30 UTC (or from **Actions > Fork sync > Run workflow**)
`fork_sync.py` merges upstream `dev` into `edge`, runs `test_command`, and pushes
(fast-forward only). A conflict or failing test pushes nothing and fails the run; merge
upstream into `edge` locally, resolve, and push.

To add a feature: `git checkout edge && git pull && git merge feature/x && git push fork edge`.

Branches listed in `rebase_features` are rebased onto upstream instead; keep a branch out of
that list once it is merged into `edge`.

[jrhubott/ha-apps](https://github.com/jrhubott/ha-apps) builds `edge` of both forks into the
"Music Assistant (light follow) Edge" Home Assistant app every day at 06:30 UTC.

Needs a `SYNC_TOKEN` repository secret: a fine-grained token for this fork with
Contents and Workflows read/write.

Local clones: `git config branch.edge.rebase false`, so `git pull` on `edge` merges
instead of flattening its merge commits.
