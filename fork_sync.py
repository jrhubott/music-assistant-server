#!/usr/bin/env python3
"""Keep this fork's feature branches on top of upstream, and rebuild release branches.

Each run:
1. Rebases every feature branch onto the upstream base branch (dev/main), runs the
   tests, and force-pushes with a lease on the commit it fetched, so a push you made
   in the meantime is never overwritten.
2. Builds release/<tag> for the newest Music Assistant server stable release and the
   newest beta: the release tag plus the feature commits cherry-picked on top. The
   frontend fork uses the frontend version that server release pins.
3. Deletes old release/* branches, keeping the newest few.

Nothing is pushed for a step that conflicts or fails its tests. The run then exits
non-zero (GitHub mails you about failed scheduled runs) and, when the fork has issues
enabled, opens or updates an issue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
SERVER_REPO = "music-assistant/server"
ISSUE_TITLE = "Fork sync needs attention"


class Sync:
    def __init__(self, cfg: dict, args: argparse.Namespace) -> None:
        self.cfg = cfg
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

    def api(self, path: str, method: str = "GET", body: dict | None = None, raw: bool = False):
        req = urllib.request.Request(
            API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        req.add_header("Accept", "application/vnd.github.raw" if raw else "application/vnd.github+json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode()
        return data if raw else (json.loads(data) if data else None)

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

    # -- release targets ---------------------------------------------------------

    def server_has_path(self, tag: str, path: str) -> bool:
        try:
            self.api(f"/repos/{SERVER_REPO}/contents/{path}?ref={tag}")
            return True
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return False
            raise

    def release_targets(self) -> list[tuple[str, str]]:
        """Return (tag in this repo, description) for the newest stable and beta."""
        rels = [
            r
            for r in self.api(f"/repos/{SERVER_REPO}/releases?per_page=60")
            if not r["draft"] and ".dev" not in r["tag_name"]
        ]
        rels.sort(key=lambda r: r["published_at"], reverse=True)
        stable = next((r for r in rels if not r["prerelease"]), None)
        beta = next((r for r in rels if r["prerelease"]), None)
        if stable and beta and beta["published_at"] < stable["published_at"]:
            beta = None  # the beta line already shipped as stable
        rcfg = self.cfg["releases"]
        targets: list[tuple[str, str]] = []
        for rel in (stable, beta):
            if not rel:
                continue
            tag = rel["tag_name"]
            needs = rcfg.get("requires_server_path")
            if needs and not self.server_has_path(tag, needs):
                self.notes.append(f"Skipped server {tag}: it predates the code this feature builds on.")
                continue
            if rcfg["mode"] == "server-tag":
                targets.append((tag, f"server {tag}"))
            else:  # frontend-pin
                pyproject = self.api(f"/repos/{SERVER_REPO}/contents/pyproject.toml?ref={tag}", raw=True)
                match = re.search(r"music-assistant/frontend/releases/download/([^/\s]+)/", pyproject)
                if not match:
                    self.problems.append(f"Could not find the frontend pin in server {tag}.")
                    continue
                targets.append((match.group(1), f"frontend {match.group(1)}, pinned by server {tag}"))
        seen: set[str] = set()
        return [t for t in targets if not (t[0] in seen or seen.add(t[0]))]

    # -- steps -------------------------------------------------------------------

    def rebase_features(self, upstream_tip: str) -> dict[str, str]:
        """Rebase each feature branch; return the commit to build releases from."""
        use: dict[str, str] = {}
        for feature in self.cfg["features"]:
            old = self.rev(f"{self.fork}/{feature}")
            if not old:
                self.problems.append(f"`{feature}` does not exist on the fork.")
                continue
            use[feature] = old
            if self.ok("merge-base", "--is-ancestor", upstream_tip, old):
                self.notes.append(f"`{feature}` is already on top of upstream.")
                continue
            self.git("checkout", "-q", "-f", "--detach", old)
            if not self.ok("rebase", upstream_tip):
                files = self.git("diff", "--name-only", "--diff-filter=U", check=False)
                self.git("rebase", "--abort", check=False)
                self.problems.append(
                    f"`{feature}` conflicts with upstream `{self.cfg['base_branch']}`; not rebased. "
                    f"Conflicting files:\n" + "\n".join(f"    {f}" for f in files.splitlines())
                )
                continue
            new = self.git("rev-parse", "HEAD")
            if not self.run_tests(f"`{feature}` rebased on `{self.cfg['base_branch']}`"):
                continue
            self.push(f"--force-with-lease=refs/heads/{feature}:{old}", self.fork, f"{new}:refs/heads/{feature}")
            self.notes.append(f"Rebased `{feature}` onto upstream `{self.cfg['base_branch']}` ({new[:9]}).")
            use[feature] = new
        return use

    def build_releases(self, upstream_tip: str, use: dict[str, str]) -> list[str]:
        built: list[str] = []
        for tag, desc in self.release_targets():
            branch = f"release/{tag}"
            built.append(branch)
            self.git("fetch", "-q", "--no-tags", self.up, f"refs/tags/{tag}:refs/tags/{tag}")
            self.git("checkout", "-q", "-f", "--detach", f"refs/tags/{tag}")
            conflict = False
            for feature, tip in use.items():
                base = self.git("merge-base", tip, upstream_tip)
                if not self.ok("cherry-pick", "--empty=drop", f"{base}..{tip}"):
                    files = self.git("diff", "--name-only", "--diff-filter=U", check=False)
                    self.git("cherry-pick", "--abort", check=False)
                    self.problems.append(
                        f"`{feature}` does not apply to {desc}; `{branch}` not updated. "
                        f"Conflicting files:\n" + "\n".join(f"    {f}" for f in files.splitlines())
                    )
                    conflict = True
                    break
            if conflict:
                continue
            new = self.git("rev-parse", "HEAD")
            remote = self.rev(f"{self.fork}/{branch}")
            if (
                remote
                and self.git("rev-parse", f"{remote}^{{tree}}") == self.git("rev-parse", f"{new}^{{tree}}")
                and self.ok("merge-base", "--is-ancestor", f"refs/tags/{tag}", remote)
            ):
                self.notes.append(f"`{branch}` is unchanged.")
                continue
            if not self.run_tests(f"`{branch}` ({desc})"):
                continue
            self.push("--force", self.fork, f"{new}:refs/heads/{branch}")
            self.notes.append(f"Built `{branch}` from {desc}.")
        return built

    def prune_releases(self, keep_now: list[str]) -> None:
        if self.problems:
            return  # keep the older branches around while something is broken
        refs = self.git(
            "for-each-ref", "--sort=-committerdate", "--format=%(refname:lstrip=3)",
            f"refs/remotes/{self.fork}/release/",
        ).splitlines()
        keep = set(keep_now) | set(refs[: self.cfg.get("keep_releases", 3)])
        for branch in refs:
            if branch not in keep:
                self.push(self.fork, "--delete", branch)
                self.notes.append(f"Deleted old `{branch}`.")

    # -- reporting ---------------------------------------------------------------

    def report(self) -> None:
        lines = ["## Fork sync", ""]
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
            issue = next((i for i in issues if i["title"] == ISSUE_TITLE and "pull_request" not in i), None)
            run_url = os.environ.get("RUN_URL", "")
            body = f"{text}\n\n{run_url}".strip()
            if self.problems and issue:
                self.api(f"/repos/{repo}/issues/{issue['number']}/comments", "POST", {"body": body})
            elif self.problems:
                self.api(f"/repos/{repo}/issues", "POST", {"title": ISSUE_TITLE, "body": body})
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
        base = self.cfg["base_branch"]
        self.git("fetch", "-q", "--prune", "--no-tags", self.fork)
        self.git("fetch", "-q", "--no-tags", self.up, base)
        upstream_tip = self.git("rev-parse", f"{self.up}/{base}")
        use = self.rebase_features(upstream_tip)
        built = self.build_releases(upstream_tip, use) if use else []
        self.prune_releases(built)
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
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    os.chdir(args.repo_dir)
    return Sync(cfg, args).run()


if __name__ == "__main__":
    sys.exit(main())
