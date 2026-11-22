# fork-sync

This branch only holds the automation that keeps this fork current. It is the fork's
default branch so that GitHub runs its schedule; it never goes into a PR.

Full documentation (the record of truth for this setup) is the BookStack page
"Music Assistant forks: edge branch and ha-apps".

## edge

`edge` is a build output: upstream `dev` plus the branches in `features`, merged in list order.
Never commit or merge on `edge` by hand. A run refuses to replace a branch holding a commit found
nowhere else, but the supported way to change `edge` is always a feature branch.

- Every day at 05:30 UTC, the sync rebuilds `edge` on the newest upstream.
- Every hour at :47, it applies feature branch changes on the upstream commit `edge` already has.
- **Actions > Fork sync > Run workflow** does either now (tick `features_only` for the hourly kind).

A rebuild with the same files as `edge` pushes nothing. Otherwise it runs `test_command`, replaces
`edge`, and saves the old tip as `edge-last-good`. A conflict or failing test leaves `edge` alone
and opens an issue naming the feature.

To add or remove a feature, change `features` in `fork-sync.json`. To ship a fix, push its branch.
If a feature conflicts with upstream, merge upstream into that feature and resolve it there.
If two features conflict with each other, record the resolution in `rr-cache/`. Files under
`generated` are rebuilt by their command instead.

Local `edge` checkouts must follow with `git fetch fork && git reset --hard fork/edge`, never
`git pull`.

## stable

`stable` is upstream `stable` (the latest release plus any backports about to be released)
plus stable-based ports of features, merged in by hand. The same run syncs it the same way, as
a second target in `fork-sync.json`; **Run workflow** can pick one target. Features on `dev`/`main`
can't be merged into it: port them onto a `<feature>-stable` branch cut from upstream `stable`
first. Cut upstream backport PRs from upstream `stable`, never from this fork's `stable`.

Don't press GitHub's **Sync fork** on `stable`: it offers to discard the merged features. A
ruleset blocks force-pushes to it.

[jrhubott/ha-apps](https://github.com/jrhubott/ha-apps) builds `edge` of both forks into the
"Music Assistant Edge (jrhubott)" Home Assistant app, and `stable` into "Music Assistant Stable
(jrhubott)". It checks hourly and builds a channel when one of its branches has a new commit.

Needs a `SYNC_TOKEN` repository secret: a fine-grained token for this fork with
Contents and Workflows read/write.

Local clones: `git config branch.stable.rebase false`, so `git pull` on `stable` merges instead of
flattening merge commits. Upstream and the fork both have a
`stable` branch, so create the local one with `git switch -c stable --track fork/stable`.
