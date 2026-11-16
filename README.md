# fork-sync

This branch only holds the automation that keeps this fork current. It is the fork's
default branch so that GitHub runs its schedule; it never goes into a PR.

Every day at 04:30 UTC (or from **Actions > Fork sync > Run workflow**) `fork_sync.py`:

1. Rebases each branch in `fork-sync.json` `features` onto upstream `dev`, runs
   `test_command`, and force-pushes with a lease, so a push you made meanwhile is never lost.
2. Builds `release/<tag>` for the newest server stable release and newest beta:
   the server tag plus the feature commits. Releases older than
   `requires_server_path` are skipped.
3. Keeps the newest 3 `release/*` branches.

A conflict or failing test pushes nothing and fails the run.

Needs a `SYNC_TOKEN` repository secret: a fine-grained token for this fork with
Contents and Workflows read/write.

Working on a feature branch locally: `git config pull.rebase true` and
`git config rebase.autoStash true`, then `git pull` before you start.
