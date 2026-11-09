#!/bin/bash
# Signs RPMs and builds a dnf repository from them.
#
#   FP_SIGN_KEY=FINGERPRINT [FP_EL=10] build-repo.sh RPMDIR OUTDIR
#
# RPMDIR holds the binary RPMs (source RPMs are left out). OUTDIR gets
# el9/x86_64/ (or whatever architectures the packages are) with every
# package signed, the repository metadata, and repomd.xml signed beside it
# as repomd.xml.asc, plus the public key as RPM-GPG-KEY-freepanel. The
# signing key must already be in gpg's keyring (GNUPGHOME) without a
# passphrase prompt; CI imports a signing subkey from a secret. Every
# package is checked against the exported public key before the metadata
# is written, so an unsigned or wrongly signed package never gets in.
# FP_EL=10 puts them in el10/ instead, the tree EL10 servers read (packages
# built in an EL10 build root); unset, it is el9/, as it always was. Each run
# touches its own tree only.
set -euo pipefail

IN=$(realpath "${1:?usage: build-repo.sh RPMDIR OUTDIR}")
OUT=${2:?usage: build-repo.sh RPMDIR OUTDIR}
KEY=${FP_SIGN_KEY:?set FP_SIGN_KEY to the fingerprint of the signing key}
case "$KEY" in
    *[!0-9A-Fa-f]*|"") echo "FP_SIGN_KEY must be a full hex fingerprint" >&2; exit 1 ;;
esac
[ "${#KEY}" -eq 40 ] || [ "${#KEY}" -eq 64 ] || { echo "FP_SIGN_KEY must be a full fingerprint, not a short id" >&2; exit 1; }
EL=${FP_EL:-9}
case "$EL" in
    9 | 10) ;;
    *) echo "FP_EL must be 9 or 10, not $EL" >&2; exit 1 ;;
esac
for tool in rpmsign rpmkeys createrepo_c gpg; do
    command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 1; }
done

mkdir -p "$OUT"
OUT=$(realpath "$OUT")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The public key, exported once: what servers will trust.
gpg --batch --armor --export "$KEY" > "$work/key.asc"
grep -q 'BEGIN PGP PUBLIC KEY BLOCK' "$work/key.asc" || { echo "no public key for $KEY" >&2; exit 1; }
# A throwaway rpm database that trusts only that key.
mkdir "$work/rpmdb"
rpmkeys --dbpath "$work/rpmdb" --import "$work/key.asc"

n=0
for f in "$IN"/*.rpm; do
    [ -e "$f" ] || { echo "no packages in $IN" >&2; exit 1; }
    case "$f" in *.src.rpm) continue ;; esac
    arch=$(rpm -qp --qf '%{ARCH}' "$f" 2>/dev/null)
    case "$arch" in noarch) arch=x86_64 ;; esac
    dest="$OUT/el$EL/$arch/Packages"
    mkdir -p "$dest"
    cp "$f" "$dest/"
    rpmsign --addsign --define "_gpg_name $KEY" --define "__gpg /usr/bin/gpg" "$dest/$(basename "$f")" >/dev/null
    rpmkeys --dbpath "$work/rpmdb" --checksig "$dest/$(basename "$f")" | grep -q ': digests signatures OK$' \
        || { echo "$(basename "$f") is not signed by $KEY" >&2; exit 1; }
    n=$((n + 1))
done
[ "$n" -gt 0 ] || { echo "no binary packages in $IN" >&2; exit 1; }

for repo in "$OUT"/el"$EL"/*/; do
    # Every package the metadata will list, not only the new ones, so a file
    # put there by other means never gets signed metadata.
    for f in "$repo"Packages/*.rpm; do
        rpmkeys --dbpath "$work/rpmdb" --checksig "$f" | grep -q ': digests signatures OK$' \
            || { echo "$f is not signed by $KEY" >&2; exit 1; }
    done
    createrepo_c --quiet --update "$repo"
    rm -f "$repo/repodata/repomd.xml.asc"
    gpg --batch --yes --local-user "$KEY" --armor --detach-sign --output "$repo/repodata/repomd.xml.asc" "$repo/repodata/repomd.xml"
    gpg --batch --verify "$repo/repodata/repomd.xml.asc" "$repo/repodata/repomd.xml" 2>/dev/null \
        || { echo "repomd.xml signature does not verify" >&2; exit 1; }
done
cp "$work/key.asc" "$OUT/RPM-GPG-KEY-freepanel"
chmod -R u=rwX,go=rX "$OUT"
echo "signed $n package(s) into $OUT"
