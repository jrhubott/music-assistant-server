# fork-sync

This branch only holds the automation that keeps this fork current. It is the fork's
default branch so that GitHub runs its schedule; it never goes into a PR.

Every day at 05:30 UTC (or from **Actions > Fork sync > Run workflow**) `fork_sync.py`:

1. Rebases each branch in `fork-sync.json` `features` onto upstream `dev`, runs
   `test_command`, and force-pushes with a lease, so a push you made meanwhile is never lost.
2. Builds `release/<tag>` for the newest server stable release and newest beta:
   the server tag plus the feature commits. Releases older than
   `requires_server_path` are skipped.
3. Keeps the newest 3 `release/*` branches.

Then the `image` job builds an installable image for each current `release/*` branch:
`ghcr.io/jrhubott/music-assistant-server:<tag>`, also tagged `stable` or `beta`. It
starts from the official image of that release and lays over it only the package files the
feature changes, plus the light-follow frontend wheel the frontend fork publishes as
`lights-<version>` (that fork syncs an hour earlier). Dependencies, ffmpeg and the bundled
app credentials stay as the official image ships them. The image drops
`/app/build_info.json`, so the server logs that it is not an official build. An image is
rebuilt only when the branch or the frontend wheel changed. A feature that changes files
outside `music_assistant/` (a new dependency) fails the job: that needs a real build.

Run it like the official image, for example:

    docker run -d --network host -v ./data:/data ghcr.io/jrhubott/music-assistant-server:beta

The job's build files are in `image/`.

A conflict or failing test pushes nothing and fails the run.

Needs a `SYNC_TOKEN` repository secret: a fine-grained token for this fork with
Contents and Workflows read/write.

Working on a feature branch locally: `git config pull.rebase true` and
`git config rebase.autoStash true`, then `git pull` before you start.
