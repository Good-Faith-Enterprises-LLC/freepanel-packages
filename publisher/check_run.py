"""Checks that a GitHub Actions run may be published, and prints its commit.

    python3 -I check_run.py [--workflow NAME] REPO RUN_ID [CONF_OUT]

REPO is OWNER/NAME. The run must be of the workflow NAME of REPO (httpd-build,
the default; radicale-build; or release-build), run on main by a push or by
hand (release-build: by hand only), finished successfully, at a commit that
main contains. The commit is printed. With CONF_OUT, packaging/repo/repo.conf as
it is at that commit is written there, so the fingerprint a publisher signs
for is the one servers pin. GITHUB_TOKEN (read access to actions and
contents of REPO) and GITHUB_API_URL come from the environment; the
publisher runs in another repository and passes a read-only token for REPO.
"""

import json
import os
import re
import sys
import urllib.request

# The workflows whose runs may be published, and how each may have started.
WORKFLOWS = {
    "httpd-build": ("push", "workflow_dispatch"),
    "radicale-build": ("push", "workflow_dispatch"),
    # A release is made by hand, with a name for it.
    "release-build": ("workflow_dispatch",),
}
DEFAULT = "httpd-build"
CONF = "packaging/repo/repo.conf"
USAGE = "usage: check_run.py [--workflow %s] OWNER/NAME RUN_ID [CONF_OUT]" % "|".join(WORKFLOWS)


def fetch(api, path, token, accept="application/vnd.github+json"):
    req = urllib.request.Request(
        api + path,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(1 << 16)


def get(api, path, token):
    return json.loads(fetch(api, path, token))


def check(repo, run, compare, workflow=DEFAULT):
    """Returns why run may not be published, or "" when it may."""
    if workflow not in WORKFLOWS:
        return "unknown workflow %r" % workflow
    path = ".github/workflows/%s.yml" % workflow
    # GitHub names are not case-sensitive.
    if ((run.get("repository") or {}).get("full_name") or "").lower() != repo.lower():
        return "the run is not in " + repo
    if run.get("path") != path:
        return "the run is %r, not %s" % (run.get("path"), path)
    if run.get("event") not in WORKFLOWS[workflow]:
        return "the run was started by %r, not %s" % (run.get("event"), " or ".join(WORKFLOWS[workflow]))
    if run.get("head_branch") != "main":
        return "the run is on %r, not main" % run.get("head_branch")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return "the run did not succeed (%s, %s)" % (run.get("status"), run.get("conclusion"))
    if not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha") or ""):
        return "the run has no commit"
    # main is the commit itself or comes after it.
    if compare.get("status") not in ("identical", "behind"):
        return "commit %s is not on main (%s)" % (run["head_sha"], compare.get("status"))
    return ""


def main():
    args = sys.argv[1:]
    workflow = DEFAULT
    if args[:1] == ["--workflow"]:
        if len(args) < 2 or args[1] not in WORKFLOWS:
            sys.exit(USAGE)
        workflow, args = args[1], args[2:]
    if len(args) not in (2, 3):
        sys.exit(USAGE)
    repo, run_id = args[0], args[1]
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repo) or not re.fullmatch(r"[0-9]+", run_id):
        sys.exit(USAGE)
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    token = os.environ["GITHUB_TOKEN"]
    run = get(api, "/repos/%s/actions/runs/%s" % (repo, run_id), token)
    sha = run.get("head_sha") or ""
    compare = {}
    if re.fullmatch(r"[0-9a-f]{40}", sha):
        # main's own commit, read from the branch (heads/main: a tag can have
        # the name too), so no name decides which commit "main" is.
        # BASE...HEAD: "behind" means BASE, main's commit, holds every commit
        # of HEAD, the run's.
        tip = (get(api, "/repos/%s/git/ref/heads/main" % repo, token).get("object") or {}).get("sha") or ""
        if re.fullmatch(r"[0-9a-f]{40}", tip):
            compare = get(api, "/repos/%s/compare/%s...%s" % (repo, tip, sha), token)
    why = check(repo, run, compare, workflow)
    if why:
        sys.exit("not publishing run %s: %s" % (run_id, why))
    if len(args) == 3:
        # The commit is checked above (40 hex digits), so the path is fixed.
        conf = fetch(api, "/repos/%s/contents/%s?ref=%s" % (repo, CONF, sha), token, "application/vnd.github.raw+json")
        with open(args[2], "wb") as f:
            f.write(conf)
    print(sha)


if __name__ == "__main__":
    main()
