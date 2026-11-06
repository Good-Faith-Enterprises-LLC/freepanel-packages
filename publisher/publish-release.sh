#!/bin/bash
# Adds a release to the published repository's files and signs it.
#
#   publish-release.sh CONF RELDIR SITEDIR
#
# RELDIR holds what the release-build workflow kept, and nothing else:
# freepanel-VERSION-linux-amd64.tar.gz (the production tarball) and install
# (packaging/bootstrap/install.sh, the script customers pipe to a shell).
# SITEDIR is a checkout of what is served (the Pages branch) that already
# holds RPM-GPG-KEY-freepanel, so packages are published first. It gets:
#   releases/freepanel-VERSION-linux-amd64.tar.gz and its detached signature
#   releases/latest         one line, VERSION
#   install, install.asc    the script and its detached signature
# Both signatures are made with the key CONF's fingerprint names (gpg needs
# only a signing subkey of it) and checked against the published key in a
# keyring that holds nothing else, the way the installer checks. A tarball
# already published is never replaced and never moves "latest"; a newer
# script replaces the published one. The script must pin the same
# fingerprint, or no customer's install would verify. Committing and pushing
# SITEDIR is the caller's.
set -euo pipefail

CONF=${1:?usage: publish-release.sh CONF RELDIR SITEDIR}
IN=$(realpath "${2:?usage: publish-release.sh CONF RELDIR SITEDIR}")
SITE=$(realpath "${3:?usage: publish-release.sh CONF RELDIR SITEDIR}")

fpr=$(sed -n 's/^fingerprint=//p' "$CONF" | tail -n 1 | tr 'a-f' 'A-F')
[ -n "$fpr" ] || { echo "repo.conf has no fingerprint: nothing can be signed yet" >&2; exit 1; }
fail() { echo "$*" >&2; exit 1; }

# What was built: one tarball and the script, nothing else.
[ -d "$IN" ] || fail "$IN is not a folder"
[ "$(find "$IN" -mindepth 1 | wc -l)" -eq 2 ] || fail "the release folder must hold the tarball and install, nothing else"
[ -f "$IN/install" ] && [ ! -L "$IN/install" ] && [ -s "$IN/install" ] || fail "no install script"
tars=("$IN"/freepanel-*-linux-amd64.tar.gz)
[ "${#tars[@]}" -eq 1 ] && [ -f "${tars[0]}" ] && [ ! -L "${tars[0]}" ] || fail "no single freepanel-VERSION-linux-amd64.tar.gz"
tarball=$(basename "${tars[0]}")
version=${tarball#freepanel-}
version=${version%-linux-amd64.tar.gz}
# The names the installer accepts for a release.
[[ $version =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$ ]] || fail "the release name '$version' is not one the installer accepts"
grep -q "^PINNED_FPR=\"$fpr\"" "$IN/install" || fail "install does not pin $fpr: no customer's install would verify"

# The same checks the installer makes before it opens the tarball: every
# entry is in freepanel-VERSION/, none climbs out of it, and the installer
# it runs is there.
list=$(tar -tzf "$IN/$tarball") || fail "$tarball is not a readable archive"
bad=$(printf '%s\n' "$list" | awk -v p="freepanel-$version" '$0 != p && substr($0, 1, length(p) + 1) != p "/"')
[ -z "$bad" ] || fail "$tarball holds entries outside freepanel-$version/: $(printf '%s' "$bad" | head -n 3 | tr '\n' ' ')"
if printf '%s\n' "$list" | grep -qE '(^|/)\.\.(/|$)'; then fail "$tarball holds a .. entry"; fi
printf '%s\n' "$list" | grep -qx "freepanel-$version/installer/install.sh" || fail "$tarball has no installer/install.sh"
[ "$(stat -c %s "$IN/$tarball")" -lt $((95 * 1024 * 1024)) ] || fail "$tarball is too large for GitHub Pages (under 95 MB)"

work=$(mktemp -d)
home=$work/verify
cleanup() {
    gpgconf --homedir "$home" --kill all >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

# The key being published is the pinned one.
primary() {
    gpg --batch --with-colons --import-options show-only --import "$1" 2>/dev/null \
        | awk -F: '$1=="pub"{p=1} $1=="fpr" && p {print $10; p=0}'
}
[ -e "$SITE/RPM-GPG-KEY-freepanel" ] || fail "no RPM-GPG-KEY-freepanel in the site: publish the packages first"
[ "$(primary "$SITE/RPM-GPG-KEY-freepanel")" = "$fpr" ] || fail "the published key is not the pinned $fpr"

# A keyring with the published key and nothing else: a signature counts only
# if it verifies there and was made by the pinned key (or a subkey of it).
mkdir -m 0700 "$home"
gpg --batch --homedir "$home" --import "$SITE/RPM-GPG-KEY-freepanel" 2>/dev/null
verifies() {
    local status
    status=$(gpg --batch --homedir "$home" --status-fd 1 --verify "$1" "$2" 2>/dev/null) || return 1
    [ "$(awk '$2=="VALIDSIG"{print $NF}' <<<"$status")" = "$fpr" ]
}
sign() {
    gpg --batch --yes --local-user "$fpr" --armor --detach-sign --output "$1.asc" "$1"
    verifies "$1.asc" "$1" || fail "the signature of $(basename "$1") does not verify against the published key"
}

# Built aside, then copied over, so a failure leaves SITEDIR as it was.
mkdir "$work/site"
tar -C "$SITE" --exclude=./.git -cf - . | tar -C "$work/site" -xf -
mkdir -p "$work/site/releases"
news=0
if [ -e "$work/site/releases/$tarball" ]; then
    echo "already published, left as it is: $tarball"
else
    cp "$IN/$tarball" "$work/site/releases/$tarball"
    sign "$work/site/releases/$tarball"
    printf '%s\n' "$version" > "$work/site/releases/latest"
    echo "published $tarball, latest is now $version"
    news=$((news + 1))
fi

if [ -e "$work/site/install" ] && [ -e "$work/site/install.asc" ] && cmp -s "$IN/install" "$work/site/install" \
    && verifies "$work/site/install.asc" "$work/site/install"; then
    echo "install is as published"
else
    cp "$IN/install" "$work/site/install"
    rm -f "$work/site/install.asc"
    sign "$work/site/install"
    echo "published install"
    news=$((news + 1))
fi
if [ "$news" -eq 0 ]; then
    echo "nothing new to publish"
    exit 0
fi

# Each signature on the site, again, from the copy that will be written.
verifies "$work/site/install.asc" "$work/site/install" || fail "install.asc does not verify"
[ -s "$work/site/releases/latest" ] || fail "the site has no releases/latest"
latest=$(head -n 1 "$work/site/releases/latest")
verifies "$work/site/releases/freepanel-$latest-linux-amd64.tar.gz.asc" "$work/site/releases/freepanel-$latest-linux-amd64.tar.gz" \
    || fail "the signature of the latest release does not verify"
touch "$work/site/.nojekyll"

tar -C "$work/site" -cf - . | tar -C "$SITE" -xf -
echo "release step done in $SITE"
