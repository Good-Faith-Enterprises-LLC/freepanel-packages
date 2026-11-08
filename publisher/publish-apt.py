#!/usr/bin/python3 -I
"""Adds new .deb packages to the published apt repository and signs it again.

    publish-apt.py CONF DEBDIR SITEDIR

The apt twin of publish-repo.sh, for Ubuntu 24.04 (noble). SITEDIR is a
checkout of what is served (the Pages branch); the apt repository is its
apt/ folder:

    apt/pool/main/<package>/<file>.deb
    apt/dists/noble/main/binary-amd64/Packages, Packages.gz
    apt/dists/noble/Release, Release.gpg, InRelease

Packages already published are never replaced: a .deb with a file name that
is already in the pool is left out, so a server that has it keeps the same
package. Packages, Release and its signatures are then written again from
everything in the pool. Architecture "all" packages are listed in
binary-amd64 with the rest. Release is signed (InRelease clear-signed and
Release.gpg detached) with the key named by CONF's fingerprint; gpg needs only
a signing subkey of it, the primary stays offline. Servers trust the release
file only by that key (installer/keys, signed-by in their sources), and it
lists the SHA-256 of each index, which lists the SHA-256 of each package.

Before anything is kept: the published key (SITEDIR/RPM-GPG-KEY-freepanel,
or the one gpg exports if there is none yet) must be the pinned one, both
signatures must verify against it with gpgv and nothing else, and every file
must fit GitHub Pages (under 95 MB). Works on any machine with python3, gpg,
gpgv and the decompressor of the packages' control member (zstd, xz or gzip):
it reads the .deb files itself and needs no dpkg. Committing and pushing
SITEDIR is the caller's.
"""

import datetime
import gzip
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

SUITE = "noble"
COMPONENT = "main"
ARCH = "amd64"
MAX = 95 * 1024 * 1024
# The fields of a package's control file that go into Packages, in apt's
# usual order; anything else the control file has follows them.
ORDER = ["Package", "Source", "Version", "Architecture", "Maintainer", "Installed-Size",
         "Pre-Depends", "Depends", "Recommends", "Suggests", "Conflicts", "Breaks",
         "Replaces", "Provides", "Section", "Priority", "Multi-Arch", "Homepage"]
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*_[A-Za-z0-9.+~:-]+_(amd64|all)\.deb$")


def die(msg):
    print("publish-apt: " + msg, file=sys.stderr)
    sys.exit(1)


def fingerprint(conf):
    fpr = ""
    with open(conf) as f:
        for line in f:
            if line.startswith("fingerprint="):
                fpr = line.split("=", 1)[1].strip().upper()
    if not re.fullmatch(r"[0-9A-F]{40}", fpr):
        die("repo.conf has no full fingerprint: nothing can be signed yet")
    return fpr


def primary_of(keyfile):
    out = subprocess.run(["gpg", "--batch", "--with-colons", "--import-options", "show-only",
                          "--import", keyfile], capture_output=True, text=True).stdout
    fprs, want = [], False
    for line in out.splitlines():
        f = line.split(":")
        if f[0] == "pub":
            want = True
        elif f[0] == "fpr" and want:
            fprs.append(f[9])
            want = False
    return fprs


def ar_members(data):
    """The members of an ar archive (a .deb), as {name: bytes}."""
    if data[:8] != b"!<arch>\n":
        raise ValueError("not an ar archive")
    pos, out = 8, {}
    while pos + 60 <= len(data):
        hdr = data[pos:pos + 60]
        name = hdr[:16].decode().strip().rstrip("/")
        size = int(hdr[48:58].decode().strip())
        pos += 60
        out[name] = data[pos:pos + size]
        pos += size + (size & 1)
    return out


def decompress(name, blob):
    if name.endswith(".gz"):
        return gzip.decompress(blob)
    if name.endswith(".xz"):
        import lzma
        return lzma.decompress(blob)
    if name.endswith(".zst"):
        return subprocess.run(["zstd", "-dc"], input=blob, capture_output=True, check=True).stdout
    if name.endswith(".tar"):
        return blob
    raise ValueError("unknown compression: " + name)


def control_of(path):
    """The control file of a .deb, as an ordered list of (field, value)."""
    with open(path, "rb") as f:
        members = ar_members(f.read())
    if members.get("debian-binary", b"").strip() != b"2.0":
        raise ValueError("not a version 2.0 .deb")
    names = [n for n in members if n.startswith("control.tar")]
    if len(names) != 1:
        raise ValueError("no single control member")
    tar = tarfile.open(fileobj=io.BytesIO(decompress(names[0], members[names[0]])))
    for m in tar.getmembers():
        if m.name in ("./control", "control"):
            text = tar.extractfile(m).read().decode()
            break
    else:
        raise ValueError("no control file")
    fields, cur = [], None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t":
            if cur is None:
                raise ValueError("continuation line first")
            fields[-1] = (fields[-1][0], fields[-1][1] + "\n" + line)
            continue
        k, _, v = line.partition(":")
        cur = k.strip()
        fields.append((cur, v.strip()))
    return fields


def digest(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stanza(apt, path):
    fields = dict(control_of(path))
    for k in ("Package", "Version", "Architecture"):
        if not fields.get(k):
            raise ValueError("no %s field" % k)
    if fields["Architecture"] not in (ARCH, "all"):
        raise ValueError("architecture %s" % fields["Architecture"])
    want = "%s_%s_%s.deb" % (fields["Package"], fields["Version"].split(":")[-1], fields["Architecture"])
    if os.path.basename(path) != want:
        raise ValueError("file name is not %s" % want)
    lines = []
    for k in ORDER:
        if k in fields:
            lines.append("%s: %s" % (k, fields[k]))
    for k, v in fields.items():
        if k not in ORDER and k != "Description":
            lines.append("%s: %s" % (k, v))
    lines += [
        "Filename: " + os.path.relpath(path, apt),
        "Size: %d" % os.path.getsize(path),
        "MD5sum: " + digest(path, "md5"),
        "SHA256: " + digest(path, "sha256"),
    ]
    if "Description" in fields:
        lines.append("Description: " + fields["Description"])
    return "\n".join(lines) + "\n"


def main():
    if len(sys.argv) != 4:
        die("usage: publish-apt.py CONF DEBDIR SITEDIR")
    conf, src, site = sys.argv[1], os.path.realpath(sys.argv[2]), os.path.realpath(sys.argv[3])
    fpr = fingerprint(conf)
    work = tempfile.mkdtemp()
    try:
        # Built aside, then copied over, so a failure leaves SITEDIR as it was.
        stage = os.path.join(work, "site")
        shutil.copytree(site, stage, ignore=shutil.ignore_patterns(".git"), symlinks=False)
        apt = os.path.join(stage, "apt")
        pool = os.path.join(apt, "pool", COMPONENT)
        os.makedirs(pool, exist_ok=True)
        published = {f for _, _, fs in os.walk(pool) for f in fs if f.endswith(".deb")}

        n = 0
        for name in sorted(os.listdir(src)):
            if not name.endswith(".deb"):
                continue
            if not NAME_RE.match(name):
                die("not a package file name this repository takes: " + name)
            if name in published:
                print("already published, left as it is: " + name)
                continue
            try:
                pkg = dict(control_of(os.path.join(src, name)))["Package"]
            except (ValueError, KeyError, OSError, tarfile.TarError, subprocess.CalledProcessError) as e:
                die("%s: %s" % (name, e))
            dest = os.path.join(pool, pkg)
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(os.path.join(src, name), dest)
            n += 1
        if n == 0 and os.path.exists(os.path.join(apt, "dists", SUITE, "InRelease")):
            print("nothing new to publish")
            return

        # Packages, from everything in the pool.
        debs = sorted(os.path.join(d, f) for d, _, fs in os.walk(pool) for f in fs if f.endswith(".deb"))
        if not debs:
            die("no packages to publish")
        stanzas = []
        for p in debs:
            try:
                stanzas.append(stanza(apt, p))
            except (ValueError, OSError, tarfile.TarError, subprocess.CalledProcessError) as e:
                die("%s: %s" % (os.path.relpath(p, apt), e))
        idx_rel = os.path.join(COMPONENT, "binary-" + ARCH)
        dist = os.path.join(apt, "dists", SUITE)
        idx = os.path.join(dist, idx_rel)
        os.makedirs(idx, exist_ok=True)
        body = "\n".join(stanzas).encode()
        with open(os.path.join(idx, "Packages"), "wb") as f:
            f.write(body)
        with open(os.path.join(idx, "Packages.gz"), "wb") as f:
            # mtime 0: the same packages give the same file.
            with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
                g.write(body)
        with open(os.path.join(idx, "Release"), "w") as f:
            f.write("Archive: %s\nComponent: %s\nOrigin: freePanel\nLabel: freePanel\nArchitecture: %s\n"
                    % (SUITE, COMPONENT, ARCH))

        files = ["Packages", "Packages.gz", "Release"]
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC")
        rel = ["Origin: freePanel", "Label: freePanel", "Suite: " + SUITE, "Codename: " + SUITE,
               "Date: " + now, "Architectures: " + ARCH, "Components: " + COMPONENT,
               "Description: freePanel packages for Ubuntu 24.04", "Acquire-By-Hash: no", "SHA256:"]
        for name in files:
            p = os.path.join(idx, name)
            rel.append(" %s %d %s" % (digest(p, "sha256"), os.path.getsize(p), os.path.join(idx_rel, name)))
        release = os.path.join(dist, "Release")
        with open(release, "w") as f:
            f.write("\n".join(rel) + "\n")
        for p in ("InRelease", "Release.gpg"):
            if os.path.exists(os.path.join(dist, p)):
                os.remove(os.path.join(dist, p))
        sign = ["gpg", "--batch", "--yes", "--quiet", "--digest-algo", "SHA512", "--local-user", fpr]
        subprocess.run(sign + ["--clearsign", "--output", os.path.join(dist, "InRelease"), release], check=True)
        subprocess.run(sign + ["--armor", "--detach-sign", "--output", os.path.join(dist, "Release.gpg"), release],
                       check=True)

        # The published key must be the pinned one, and both signatures must
        # verify against it and nothing else.
        keyfile = os.path.join(stage, "RPM-GPG-KEY-freepanel")
        if not os.path.exists(keyfile):
            with open(keyfile, "wb") as f:
                f.write(subprocess.run(["gpg", "--batch", "--armor", "--export", fpr],
                                       capture_output=True, check=True).stdout)
        if primary_of(keyfile) != [fpr]:
            die("the published key is not the pinned %s alone" % fpr)
        ring = os.path.join(work, "pinned.gpg")
        with open(ring, "wb") as f:
            f.write(subprocess.run(["gpg", "--batch", "--dearmor"], input=open(keyfile, "rb").read(),
                                   capture_output=True, check=True).stdout)
        for args in (["--keyring", ring, os.path.join(dist, "InRelease")],
                     ["--keyring", ring, os.path.join(dist, "Release.gpg"), release]):
            r = subprocess.run(["gpgv", "--status-fd", "1"] + args, capture_output=True, text=True)
            if r.returncode != 0 or not re.search(r"^\[GNUPG:\] VALIDSIG \S+ .* %s$" % fpr, r.stdout, re.M):
                die("the signature on %s does not verify against the pinned key" % args[2])

        big = [os.path.join(d, f) for d, _, fs in os.walk(stage) for f in fs
               if os.path.getsize(os.path.join(d, f)) > MAX]
        if big:
            die("too large for GitHub Pages: " + " ".join(big))
        open(os.path.join(stage, ".nojekyll"), "a").close()

        for d, _, fs in os.walk(stage):
            out = os.path.join(site, os.path.relpath(d, stage))
            os.makedirs(out, exist_ok=True)
            for f in fs:
                shutil.copy2(os.path.join(d, f), os.path.join(out, f))
        print("published %d new .deb package(s) into %s (%d in %s)" % (n, site, len(debs), SUITE))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
