# freePanel packages

The signed package repository that servers running freePanel get their
packages from (today: its Apache build). GitHub Pages serves the `gh-pages`
branch; `publish` is the only thing that writes it.

Everything here is written by the freePanel repository's
`packaging/repo/publisher/export.sh`. Change it there, export again and
review the diff of `publisher/` and `.github/workflows/publish.yml` before
committing: they run with the signing key, so what is on `main` here is
what is trusted. `publisher/VENDORED` names the freePanel commit they came
from and their checksums.

- `.github/workflows/publish.yml`: by hand, with the IDs of successful runs
  on main in the freePanel repository: `httpd_run` (httpd-build),
  `radicale_run` (radicale-build) and `release_run` (release-build), any of
  them. It checks each run, signs the RPMs with the signing subkey and adds
  them to `gh-pages`; a release run adds `install`, `install.asc`,
  `releases/freepanel-VERSION-linux-amd64.tar.gz`, its `.asc` and
  `releases/latest`, signed by the same subkey. A package or tarball that is
  already published is never replaced. It needs the owner's approval on the
  `publish` environment every time.
- Do not edit `gh-pages` by hand. Each commit on it should be a `publish`
  run's; after a leaked key, check that before trusting it.

Setup (the owner, once): `packaging/repo/README.md` in the freePanel
repository.
