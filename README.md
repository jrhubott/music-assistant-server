# fork-sync

This branch only holds the automation that keeps this fork current. It is the fork's
default branch so that GitHub runs its schedule; it never goes into a PR.

Full documentation (the record of truth for this setup) is the BookStack page
"Music Assistant forks: edge branch and ha-apps".

## edge

`edge` is upstream `dev` plus every feature branch merged into it by hand. It is never
rebased or force-pushed. Every day at 05:30 UTC (or from **Actions > Fork sync > Run workflow**)
`fork_sync.py` merges upstream `dev` into `edge`, runs `test_command`, and pushes
(fast-forward only). A conflict or failing test pushes nothing and fails the run. Merge
upstream into the feature branch that conflicts, resolve it there, then merge that branch into
`edge`. Only conflicts between two of your own features are resolved on `edge` itself.

To add a feature: add it to `features` for the target in `fork-sync.json`, then
`git checkout edge && git pull && git merge feature/x && git push fork edge`.

Each run checks the branch against `features`. A listed branch that is gone, or a commit that is
on neither upstream nor a listed feature, fails the run. A feature with unmerged commits, or one
that no longer merges cleanly with upstream, is only reported in the run summary.

## stable

`stable` is upstream `stable` (the latest release plus any backports about to be released)
plus stable-based ports of features, merged in by hand. The same run syncs it the same way, as
a second target in `fork-sync.json`; **Run workflow** can pick one target. Features on `dev`/`main`
can't be merged into it: port them onto a `<feature>-stable` branch cut from upstream `stable`
first. Cut upstream backport PRs from upstream `stable`, never from this fork's `stable`.

Don't press GitHub's **Sync fork** on `stable`: it offers to discard the merged features. A
ruleset blocks force-pushes to it.

Branches listed in `rebase_features` are rebased onto upstream instead; keep a branch out of
that list once it is merged into `edge`.

[jrhubott/ha-apps](https://github.com/jrhubott/ha-apps) builds `edge` of both forks into the
"Music Assistant Edge (jrhubott)" Home Assistant app, and `stable` into "Music Assistant Stable
(jrhubott)". It checks hourly and builds a channel when one of its branches has a new commit.

Needs a `SYNC_TOKEN` repository secret: a fine-grained token for this fork with
Contents and Workflows read/write.

Local clones: `git config branch.edge.rebase false` and `git config branch.stable.rebase false`,
so `git pull` merges instead of flattening merge commits. Upstream and the fork both have a
`stable` branch, so create the local one with `git switch -c stable --track fork/stable`.
