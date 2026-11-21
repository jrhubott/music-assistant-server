#!/usr/bin/env python3
"""Keep this fork's long-lived branches current with upstream.

`fork-sync.json` lists the targets. Each one is a branch of the fork (`edge_branch`, such as
`edge` or `stable`) that is an upstream branch (`base_branch`, such as dev/main or stable) plus
every feature branch you merged into it by hand. It is never rebased by this script. Each run
handles one target (`--target`), or all of them in turn:

1. Fetches upstream. If `edge` already contains the upstream tip, there is nothing to do.
2. Otherwise merges upstream into `edge`, runs `test_command`, and pushes (fast-forward only),
   so a push you made in the meantime is never overwritten; the run just retries next time.
3. Optionally rebases the branches in `rebase_features` onto upstream (empty by default:
   a branch that is merged into `edge` should not also be rebased, or its commits come in twice).
4. Checks the branch against its `features` list. A listed branch that is gone, or a commit that
   is on the branch but on neither upstream nor a listed feature, fails the run. A feature with
   unmerged commits, or one that no longer merges cleanly with upstream, is only reported.

Nothing is pushed for a merge that conflicts or fails its tests. The run then exits non-zero
(GitHub mails you about failed scheduled runs) and, when the fork has issues enabled, opens or
updates an issue. Fix it by merging upstream into `edge` locally, resolving, and pushing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
ISSUE_TITLE = "Fork sync needs attention"


def load_targets(cfg: dict) -> list[dict]:
    """Return the targets of a config, each merged with the config's shared keys."""
    shared = {key: value for key, value in cfg.items() if key != "targets"}
    return [{**shared, **target} for target in cfg.get("targets", [{}])]


class Sync:
    def __init__(self, cfg: dict, args: argparse.Namespace) -> None:
        self.cfg = cfg
        self.base = cfg["base_branch"]
        self.edge = cfg["edge_branch"]
        # the edge target keeps the issue title it always had
        self.issue_title = ISSUE_TITLE if self.edge == "edge" else f"{ISSUE_TITLE} ({self.edge})"
        self.dry_run = args.dry_run
        self.skip_tests = args.skip_tests
        self.fork = args.fork_remote
        self.up = args.upstream_remote
        self.token = os.environ.get("SYNC_TOKEN", "")
        self.problems: list[str] = []
        self.notes: list[str] = []

    # -- helpers -----------------------------------------------------------------

    def git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(["git", *args], text=True, capture_output=True)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed:\n{proc.stdout}{proc.stderr}")
        return proc.stdout.strip()

    def ok(self, *args: str) -> bool:
        return subprocess.run(["git", *args], capture_output=True).returncode == 0

    def rev(self, ref: str) -> str | None:
        out = self.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        return out or None

    def api(self, path: str, method: str = "GET", body: dict | None = None):
        req = urllib.request.Request(
            API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        req.add_header("Accept", "application/vnd.github+json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode()
        return json.loads(data) if data else None

    def push(self, *args: str) -> None:
        if self.dry_run:
            self.notes.append(f"(dry run) would run: git push {' '.join(args)}")
            return
        self.git("push", *args)

    def run_tests(self, label: str) -> bool:
        if self.skip_tests:
            return True
        print(f"::group::Tests for {label}", flush=True)
        proc = subprocess.run(self.cfg["test_command"], shell=True)
        print("::endgroup::", flush=True)
        if proc.returncode != 0:
            self.problems.append(f"Tests failed for {label}; nothing pushed. See the run log.")
            return False
        return True

    def conflict_files(self) -> str:
        files = self.git("diff", "--name-only", "--diff-filter=U", check=False)
        return "\n".join(f"    {f}" for f in files.splitlines())

    # -- steps -------------------------------------------------------------------

    def merge_edge(self, upstream_tip: str) -> str | None:
        """Merge upstream into the branch and return the commit the branch is now at."""
        edge = self.cfg["edge_branch"]
        old = self.rev(f"{self.fork}/{edge}")
        if not old:
            self.problems.append(f"`{edge}` does not exist on the fork.")
            return None
        if self.ok("merge-base", "--is-ancestor", upstream_tip, old):
            self.notes.append(f"`{edge}` already contains upstream `{self.base}` ({upstream_tip[:9]}).")
            return old
        self.git("checkout", "-q", "-f", "--detach", old)
        message = f"Merge upstream {self.base} ({upstream_tip[:9]}) into {edge}"
        if not self.ok("merge", "--no-ff", "-m", message, upstream_tip):
            files = self.conflict_files()
            self.git("merge", "--abort", check=False)
            self.problems.append(
                f"Upstream `{self.base}` conflicts with `{edge}`; not merged. Merge it locally, "
                f"resolve, and push. Conflicting files:\n{files}"
            )
            return old
        new = self.git("rev-parse", "HEAD")
        if not self.run_tests(f"`{edge}` with upstream `{self.base}` merged in"):
            return old
        # a plain push only fast-forwards, so it can never drop commits pushed meanwhile
        self.push(self.fork, f"{new}:refs/heads/{edge}")
        self.notes.append(f"Merged upstream `{self.base}` into `{edge}` ({new[:9]}).")
        return new

    def rebase_features(self, upstream_tip: str) -> None:
        for feature in self.cfg.get("rebase_features", []):
            old = self.rev(f"{self.fork}/{feature}")
            if not old:
                self.problems.append(f"`{feature}` does not exist on the fork.")
                continue
            if self.ok("merge-base", "--is-ancestor", upstream_tip, old):
                self.notes.append(f"`{feature}` is already on top of upstream.")
                continue
            self.git("checkout", "-q", "-f", "--detach", old)
            if not self.ok("rebase", upstream_tip):
                files = self.conflict_files()
                self.git("rebase", "--abort", check=False)
                self.problems.append(
                    f"`{feature}` conflicts with upstream `{self.base}`; not rebased. "
                    f"Conflicting files:\n{files}"
                )
                continue
            new = self.git("rev-parse", "HEAD")
            if not self.run_tests(f"`{feature}` rebased on `{self.base}`"):
                continue
            self.push(f"--force-with-lease=refs/heads/{feature}:{old}", self.fork, f"{new}:refs/heads/{feature}")
            self.notes.append(f"Rebased `{feature}` onto upstream `{self.base}` ({new[:9]}).")

    def patch_ids(self, *log_args: str) -> dict[str, str]:
        """Return a map of patch id to commit for the commits `git log` selects."""
        log = subprocess.run(
            ["git", "log", "--no-merges", "-p", "--format=commit %H", *log_args],
            capture_output=True,
            check=True,
        )
        ids = subprocess.run(
            ["git", "patch-id", "--stable"], input=log.stdout, capture_output=True, check=True
        )
        return dict(line.split() for line in ids.stdout.decode().splitlines())

    def audit_features(self, tip: str, upstream_tip: str) -> None:
        """Compare the branch with its declared `features` list."""
        features = self.cfg.get("features")
        if features is None:
            return
        refs: list[str] = []
        for feature in features:
            ref = self.rev(f"{self.fork}/{feature}")
            if not ref:
                self.problems.append(f"`{feature}` is listed for `{self.edge}` but is not on the fork.")
                continue
            refs.append(ref)
            if not self.ok("merge-base", "--is-ancestor", ref, tip):
                count = self.git("rev-list", "--count", ref, f"^{tip}")
                self.notes.append(f"`{feature}` has {count} commit(s) not merged into `{self.edge}` yet.")
            if not self.ok("merge-tree", "--write-tree", "--no-messages", upstream_tip, ref):
                self.notes.append(
                    f"`{feature}` no longer merges cleanly with upstream `{self.base}`. "
                    "Merge upstream into the feature branch and resolve it there."
                )
        excluded = [f"^{ref}" for ref in refs]
        stray = self.git("rev-list", "--no-merges", tip, f"^{upstream_tip}", *excluded).split()
        if not stray:
            return
        # a rebuilt feature or a cherry-picked fix has a new SHA but the same patch
        known = self.patch_ids(*refs, f"^{upstream_tip}") if refs else {}
        for patch_id, commit in self.patch_ids("--no-walk", *stray).items():
            if patch_id in known or self.git("cherry", upstream_tip, commit, f"{commit}^").startswith("-"):
                continue
            subject = self.git("log", "-1", "--format=%s", commit)
            self.problems.append(
                f"`{commit[:9]}` ({subject}) is only on `{self.edge}`. Move it to a feature branch "
                "or add its branch to `features`."
            )

    # -- reporting ---------------------------------------------------------------

    def report(self) -> None:
        lines = [f"## Fork sync: `{self.edge}`", ""]
        if self.problems:
            lines += ["### Needs attention", ""] + [f"- {p}" for p in self.problems] + [""]
        lines += ["### Done", ""] + [f"- {n}" for n in self.notes or ["Nothing to do."]]
        text = "\n".join(lines)
        print(text)
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        self.update_issue(text)

    def update_issue(self, text: str) -> None:
        repo = self.cfg["fork"]
        if self.dry_run or not self.token:
            return
        try:
            if not self.api(f"/repos/{repo}")["has_issues"]:
                return
            issues = self.api(f"/repos/{repo}/issues?state=open&per_page=100")
            issue = next((i for i in issues if i["title"] == self.issue_title and "pull_request" not in i), None)
            run_url = os.environ.get("RUN_URL", "")
            body = f"{text}\n\n{run_url}".strip()
            if self.problems and issue:
                self.api(f"/repos/{repo}/issues/{issue['number']}/comments", "POST", {"body": body})
            elif self.problems:
                self.api(f"/repos/{repo}/issues", "POST", {"title": self.issue_title, "body": body})
            elif issue:
                self.api(f"/repos/{repo}/issues/{issue['number']}/comments", "POST", {"body": "Sync is clean again.\n\n" + run_url})
                self.api(f"/repos/{repo}/issues/{issue['number']}", "PATCH", {"state": "closed"})
        except urllib.error.HTTPError as err:
            print(f"Could not update the issue: {err}", file=sys.stderr)

    # -- main --------------------------------------------------------------------

    def run(self) -> int:
        if not self.git("config", "user.email", check=False):
            self.git("config", "user.name", "fork-sync")
            self.git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        self.git("fetch", "-q", "--prune", "--no-tags", self.fork)
        self.git("fetch", "-q", "--no-tags", self.up, self.base)
        upstream_tip = self.git("rev-parse", f"{self.up}/{self.base}")
        tip = self.merge_edge(upstream_tip)
        self.rebase_features(upstream_tip)
        if tip:
            self.audit_features(tip, upstream_tip)
        self.report()
        return 1 if self.problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--fork-remote", default="origin")
    parser.add_argument("--upstream-remote", default="upstream")
    parser.add_argument("--dry-run", action="store_true", help="push nothing, touch no issues")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--target", help="the edge_branch of the target to sync (default: all)")
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as fh:
        targets = load_targets(json.load(fh))
    if args.target:
        targets = [t for t in targets if t["edge_branch"] == args.target]
        if not targets:
            parser.error(f"no target with edge_branch {args.target!r} in {args.config}")
    os.chdir(args.repo_dir)
    # every target runs, so a failing one does not hold back the others
    return max(Sync(target, args).run() for target in targets)


if __name__ == "__main__":
    sys.exit(main())
