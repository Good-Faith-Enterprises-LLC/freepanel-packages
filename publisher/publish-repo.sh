#!/bin/bash
# Adds new packages to the published repository's files and signs it again.
#
#   [FP_EL=10] publish-repo.sh CONF RPMDIR SITEDIR
#
# FP_EL=10 publishes RPMDIR's packages (built in an EL10 build root) into
# el10/ beside el9/, signed with the same key; unset, they go to el9/ as
# before. Each run checks and signs its own tree only, so EL9's files are
# left exactly as they are by an EL10 publish, and the other way round.
#
# SITEDIR is a checkout of what is served (the Pages branch). Packages
# already published are never replaced: one with the same file name is
# left out, so a server that has it keeps the same package. After the
# signing subkey is rotated, one run with FP_RESIGN=yes signs published
# packages again with the new one, but only those the previously published
# key verifies. (After a leaked subkey, check first that every commit on the
# Pages branch is a publish run's.) The rest are
# signed and the metadata rebuilt by build-repo.sh, with the key named by
# CONF's fingerprint. gpg needs only a signing subkey of it (the primary
# stays offline). Before anything is kept, the exported key must be the
# pinned one, and every file must fit GitHub Pages (under 100 MB).
# Committing and pushing SITEDIR is the caller's.
set -euo pipefail

CONF=${1:?usage: publish-repo.sh CONF RPMDIR SITEDIR}
IN=$(realpath "${2:?usage: publish-repo.sh CONF RPMDIR SITEDIR}")
SITE=$(realpath "${3:?usage: publish-repo.sh CONF RPMDIR SITEDIR}")
HERE=$(dirname "$(realpath "$0")")
EL=${FP_EL:-9}
case "$EL" in
    9 | 10) ;;
    *) echo "FP_EL must be 9 or 10, not $EL" >&2; exit 1 ;;
esac

fpr=$(sed -n 's/^fingerprint=//p' "$CONF" | tail -n 1 | tr 'a-f' 'A-F')
[ -n "$fpr" ] || { echo "repo.conf has no fingerprint: nothing can be signed yet" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/new"
n=0
for f in "$IN"/*.rpm; do
    [ -e "$f" ] || break
    case "$f" in *.src.rpm) continue ;; esac
    if compgen -G "$SITE/el$EL/*/Packages/$(basename "$f")" >/dev/null; then
        echo "already published, left as it is: $(basename "$f")"
        continue
    fi
    cp "$f" "$work/new/"
    n=$((n + 1))
done

# Built aside, then copied over, so a failure leaves SITEDIR as it was.
mkdir "$work/site"
tar -C "$SITE" --exclude=./.git -cf - . | tar -C "$work/site" -xf -

# After a rotation, published packages carry the retired subkey's
# signature. With FP_RESIGN=yes, one that the key published before (the
# pinned primary's, checked) still verifies against is signed again with
# the current key; anything else stops the publish.
primary() {
    gpg --batch --with-colons --import-options show-only --import "$1" 2>/dev/null \
        | awk -F: '$1=="pub"{p=1} $1=="fpr" && p {print $10; p=0}'
}
gpg --batch --armor --export "$fpr" > "$work/now.asc"
mkdir "$work/now.db" "$work/old.db"
rpmkeys --dbpath "$work/now.db" --import "$work/now.asc"
old=$work/site/RPM-GPG-KEY-freepanel
if [ -e "$old" ]; then
    [ "$(primary "$old")" = "$fpr" ] || { echo "the published key is not the pinned $fpr" >&2; exit 1; }
    rpmkeys --dbpath "$work/old.db" --import "$old"
fi
verifies() { rpmkeys --dbpath "$1" --checksig "$2" | grep -q ': digests signatures OK$'; }
again=0
for f in "$work"/site/el"$EL"/*/Packages/*.rpm; do
    [ -e "$f" ] || break
    verifies "$work/now.db" "$f" && continue
    if [ "${FP_RESIGN:-}" = yes ] && [ -e "$old" ] && verifies "$work/old.db" "$f"; then
        rpmsign --delsign "$f" >/dev/null
        mv "$f" "$work/new/"
        echo "signing again with the current key: $(basename "$f")"
        again=$((again + 1))
        continue
    fi
    if [ -e "$old" ] && verifies "$work/old.db" "$f"; then
        echo "$(basename "$f") is signed by a retired subkey: after a rotation, publish once with FP_RESIGN=yes" >&2
    else
        echo "$(basename "$f") is published but signed by neither the current nor the published key" >&2
    fi
    exit 1
done
if [ "$n" -eq 0 ] && [ "$again" -eq 0 ]; then
    echo "nothing new to publish"
    exit 0
fi
FP_SIGN_KEY=$fpr FP_EL=$EL "$HERE/build-repo.sh" "$work/new" "$work/site"

got=$(gpg --batch --with-colons --import-options show-only --import "$work/site/RPM-GPG-KEY-freepanel" 2>/dev/null \
    | awk -F: '$1=="pub"{p=1} $1=="fpr" && p {print $10; p=0}')
[ "$got" = "$fpr" ] || { echo "the exported key is $got, not the pinned $fpr" >&2; exit 1; }
big=$(find "$work/site" -type f -size +95M)
[ -z "$big" ] || { echo "too large for GitHub Pages: $big" >&2; exit 1; }
touch "$work/site/.nojekyll"

tar -C "$work/site" -cf - . | tar -C "$SITE" -xf -
echo "published $n new package(s) into $SITE, $again signed again"
