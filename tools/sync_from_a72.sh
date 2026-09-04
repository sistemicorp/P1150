#!/usr/bin/env bash
#
# Sync the pxxxx driver into pxxxx/ from an a72-PxDLL release -- both platforms,
# one version, in one step.
#
# WHY THE BINARIES STAY IN GIT HERE, unlike a53-P1150DLL and a73-PxxxxWASMGUI.
# This repository is PUBLIC and customer-facing: its README tells customers to
# clone it and add their own scripts. a72-PxDLL is PRIVATE, so a customer cannot
# fetch its release assets. Untracking pxxxx/ the way the other two consumers do
# would break every clone. Do not "finish the job" by gitignoring pxxxx/.
#
# The fault this fixes is not that the binaries are tracked -- it is that they
# drifted apart while being tracked. Measured 2026-09-03: pxxxx/libpxxxx.so was
# at a72 0.1-40 and pxxxx/pxxxx.dll at 0.1-41, in the same directory, ~20
# commits behind, and PXXXX.py differed from a72's by 7 lines. A P1150 user on
# Linux and one on Windows were running different drivers. So this script pulls
# BOTH legs from ONE release, refuses to write anything unless they agree, and
# leaves the result staged as a single commit. Mismatch becomes impossible
# rather than merely noticeable.
#
# PXXXX_VERSION is written from what actually landed, so which a72 release is
# shipped is legible from a checkout without running strings(1).
#
# LINE ENDINGS ARE WHY EACH FILE HAS A DESIGNATED LEG. The two legs' shared text
# files (PXXXX.py, __init__.py) are identical in content but not byte for byte:
# the signing box checks out with CRLF, so the windows-x64 leg ships CRLF and
# linux-x64 ships LF. Taking a text file from whichever leg was read last would
# flip 1067 lines of PXXXX.py back and forth on alternate syncs. Text comes from
# linux-x64; only pxxxx.dll comes from windows-x64. The check below still
# compares them after normalising line endings, so a *real* divergence -- two
# legs built from different source -- fails loudly instead of hiding behind the
# difference that is always there.
#
# CREDENTIALS. a72-PxDLL is private, so this needs `gh auth login` (or
# $PXXXX_RELEASE_TOKEN). There is deliberately no secret stored in this
# repository: it is public and has no CI, so there would be nothing to put one
# in and nowhere safe to put it. This script is run by a maintainer, by hand.
#
# Usage:
#   tools/sync_from_a72.sh [<a72-tag>]      # default: the tag in PXXXX_VERSION
#
set -euo pipefail

REPO='sistemicorp/a72-PxDLL'
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/pxxxx"
PIN_FILE="$ROOT/PXXXX_VERSION"

# file:leg -- which leg each installed file is taken from. See the line-endings
# note above for why the text files are pinned to linux-x64 rather than being
# taken from whichever leg happens to carry them.
INSTALL=(
  "libpxxxx.so:linux-x64"
  "pxxxx.dll:windows-x64"
  "PXXXX.py:linux-x64"
  "__init__.py:linux-x64"
)

# Shared between the legs, and required to agree once line endings are
# normalised. pxxxx.h is in here despite not being installed: it is the API
# contract, and two legs disagreeing about it is the §1.2 fault this whole
# arrangement exists to make impossible.
SHARED=(PXXXX.py __init__.py pxxxx.h pxxxx_labview.h)

die() { echo "error: $*" >&2; exit 1; }

case "${1:-}" in
  -h|--help) sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

for tool in curl tar unzip sha256sum python3 git; do
  command -v "$tool" >/dev/null 2>&1 \
    || die "$tool is not installed, and this script needs it."
done

if [ $# -ge 1 ] && [ -n "$1" ]; then
  WANT="$1"
elif [ -f "$PIN_FILE" ]; then
  WANT="$(head -n 1 "$PIN_FILE" | tr -d '[:space:]')"
  [ -n "$WANT" ] || die "$PIN_FILE is empty. Pass a tag: tools/sync_from_a72.sh 0.3"
else
  die "no tag given and no $PIN_FILE. Pass one: tools/sync_from_a72.sh 0.3"
fi

# ---- the credential --------------------------------------------------
if [ -n "${PXXXX_RELEASE_TOKEN:-}" ]; then
  TOKEN="$PXXXX_RELEASE_TOKEN"
  TOKEN_SOURCE='PXXXX_RELEASE_TOKEN'
else
  TOKEN_SOURCE='gh auth token'
  command -v gh >/dev/null 2>&1 || die \
"no credential for $REPO, which is private. Install the gh CLI and run:
       gh auth login"
  TOKEN="$(gh auth token 2>/dev/null || true)"
  [ -n "$TOKEN" ] || die "gh is installed but not authenticated. Run:  gh auth login"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== syncing pxxxx $WANT from $REPO"

code="$(curl --silent --show-error --location --retry 3 --retry-connrefused \
     --write-out '%{http_code}' --output "$TMP/release.json" \
     --header "Authorization: Bearer $TOKEN" \
     --header 'Accept: application/vnd.github+json' \
     --header 'X-GitHub-Api-Version: 2022-11-28' \
     --header 'User-Agent: p1150-sync-from-a72' \
     "https://api.github.com/repos/$REPO/releases/tags/$WANT")"

case "$code" in
  200) ;;
  401|403)
    die "$REPO rejected the credential from $TOKEN_SOURCE (HTTP $code). It is expired, revoked, or malformed." ;;
  404)
    die "no release for tag '$WANT' in $REPO (HTTP 404). Either the tag does not
       exist, or its release is still a DRAFT -- this endpoint does not see drafts." ;;
  *)
    die "could not reach $REPO for tag '$WANT' (HTTP $code)." ;;
esac

LEGS=(linux-x64 windows-x64)
declare -A ARCHIVE=( [linux-x64]="pxxxx-$WANT-linux-x64.tar.gz"
                     [windows-x64]="pxxxx-$WANT-windows-x64.zip" )

python3 - "$TMP/release.json" "${ARCHIVE[linux-x64]}" "${ARCHIVE[windows-x64]}" \
    > "$TMP/assets.sh" <<'PY'
import json, sys, shlex
rel = json.load(open(sys.argv[1]))
want = {sys.argv[2]: 'LINUX', sys.argv[3]: 'WINDOWS', 'SHA256SUMS': 'SUMS'}
found = {v: a for a in rel.get('assets', []) for k, v in want.items() if a['name'] == k}
missing = [k for k, v in want.items() if v not in found]
if missing:
    names = ', '.join(a['name'] for a in rel.get('assets', [])) or '(none)'
    sys.exit("release %s carries no %s. Present: %s"
             % (rel.get('tag_name'), ', '.join(missing), names))
for key, a in found.items():
    print("%s_URL=%s"  % (key, shlex.quote(a['url'])))
    print("%s_SIZE=%d" % (key, a['size']))
PY
# shellcheck disable=SC1090
. "$TMP/assets.sh"

get_asset() {  # get_asset <url> <out> <expected-size> <name>
  curl --silent --show-error --location --fail --retry 3 --retry-connrefused \
       --output "$2" \
       --header "Authorization: Bearer $TOKEN" \
       --header 'Accept: application/octet-stream' \
       --header 'User-Agent: p1150-sync-from-a72' \
       "$1" || die "could not download $4"
  local got
  got="$(stat -c %s "$2")"
  [ "$got" = "$3" ] || die "$4: got $got bytes, release says $3"
}

get_asset "$SUMS_URL" "$TMP/SHA256SUMS" "$SUMS_SIZE" 'SHA256SUMS'
get_asset "$LINUX_URL"   "$TMP/${ARCHIVE[linux-x64]}"   "$LINUX_SIZE"   "${ARCHIVE[linux-x64]}"
get_asset "$WINDOWS_URL" "$TMP/${ARCHIVE[windows-x64]}" "$WINDOWS_SIZE" "${ARCHIVE[windows-x64]}"

for leg in "${LEGS[@]}"; do
  a="${ARCHIVE[$leg]}"
  line="$(awk -v f="$a" '$2 == f || $2 == "*" f { print; exit }' "$TMP/SHA256SUMS")"
  [ -n "$line" ] || die "$a is not listed in the release's SHA256SUMS"
  ( cd "$TMP" && printf '%s\n' "$line" | sha256sum --check --status - ) \
    || die "$a does not match its sha256 in the release's SHA256SUMS"
done
echo "   sha256 ok (both legs)"

mkdir -p "$TMP/linux-x64" "$TMP/windows-x64"
tar -xzf "$TMP/${ARCHIVE[linux-x64]}"   -C "$TMP/linux-x64"
unzip -q "$TMP/${ARCHIVE[windows-x64]}" -d "$TMP/windows-x64"

# ---- the checks that make a mismatched pxxxx/ impossible --------------
python3 - "$TMP" "$WANT" "$DEST" "${INSTALL[@]}" -- "${SHARED[@]}" <<'PY'
import hashlib, json, os, sys

tmp, want, dest = sys.argv[1], sys.argv[2], sys.argv[3]
rest = sys.argv[4:]
sep = rest.index('--')
install = [x.split(':', 1) for x in rest[:sep]]
shared = rest[sep + 1:]
legs = ['linux-x64', 'windows-x64']

def sha(p):
    return hashlib.sha256(open(p, 'rb').read()).hexdigest()

man = {}
for leg in legs:
    mp = os.path.join(tmp, leg, 'manifest.json')
    if not os.path.exists(mp):
        sys.exit("the %s leg contains no manifest.json" % leg)
    man[leg] = json.load(open(mp))
    if man[leg].get('version') != want:
        sys.exit("the %s leg says version %r, asked for %r"
                 % (leg, man[leg].get('version'), want))
    if man[leg].get('leg') != leg:
        sys.exit("the %s archive says leg %r" % (leg, man[leg].get('leg')))

# ONE source revision. This is the check the whole script exists for: a .so and
# a .dll built four commits apart is exactly how this directory drifted before.
commits = {leg: man[leg].get('commit') for leg in legs}
if len(set(commits.values())) != 1:
    sys.exit("the legs were built from different commits: "
             + ', '.join("%s=%s" % (l, (c or '?')[:9]) for l, c in commits.items()))

# ONE firmware set, so the .so and the .dll flash the same images.
fw = {leg: sorted((f['file'], f['sha256']) for f in man[leg].get('firmware', []))
      for leg in legs}
if len({repr(v) for v in fw.values()}) != 1:
    sys.exit("the legs embed different firmware sets")

# The shared source files must agree once line endings are normalised. They are
# never byte-equal -- the signing box checks out CRLF -- so comparing raw bytes
# would be a check that always fails, and skipping the comparison would let a
# real divergence through. Normalise, then require equality.
for n in shared:
    seen = {}
    for leg in legs:
        p = os.path.join(tmp, leg, n)
        if not os.path.exists(p):
            sys.exit("the %s leg is missing %s" % (leg, n))
        seen[leg] = hashlib.sha256(open(p, 'rb').read().replace(b'\r\n', b'\n')).hexdigest()
    if len(set(seen.values())) != 1:
        sys.exit("%s differs between the legs beyond line endings -- they are not "
                 "the same source" % n)

# Each installed file against its own leg's manifest.
for name, leg in install:
    src = os.path.join(tmp, leg, name)
    if not os.path.exists(src):
        sys.exit("the %s leg carries no %s" % (leg, name))
    entry = next((a for a in man[leg].get('assets', []) if a['name'] == name), None)
    if entry is None:
        sys.exit("the %s manifest does not describe %s" % (leg, name))
    if sha(src) != entry['sha256']:
        sys.exit("%s (%s leg) does not match its manifest sha256" % (name, leg))

# ---- install ---------------------------------------------------------
os.makedirs(dest, exist_ok=True)
installed = []
for name, leg in install:
    data = open(os.path.join(tmp, leg, name), 'rb').read()
    open(os.path.join(dest, name), 'wb').write(data)
    installed.append({'name': name, 'leg': leg,
                      'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})

# One merged record, so "which driver and which firmware is this?" is a file in
# the repository rather than a strings(1) run -- which matters more here than
# anywhere else, because this is the copy customers clone.
ref = man['linux-x64']
json.dump({
    'version': ref['version'],
    'commit': ref['commit'],
    'source': 'https://github.com/sistemicorp/a72-PxDLL/releases/tag/' + ref['version'],
    'built_utc': {leg: man[leg].get('built_utc') for leg in legs},
    'signed': {leg: man[leg].get('signed') for leg in legs},
    'glibc_baseline': man['linux-x64'].get('glibc_baseline'),
    'firmware': ref.get('firmware', []),
    'firmware_sources': ref.get('firmware_sources', {}),
    'assets': installed,
}, open(os.path.join(dest, 'manifest.json'), 'w'), indent=2)

print("   one commit: %s" % ref['commit'][:9])
print("   one firmware set: a43 %s"
      % ((ref.get('firmware_sources') or {}).get('a43-P1150', {}).get('ref', '?')))
for a in installed:
    print("   %-16s %9d bytes  (%s leg)" % (a['name'], a['size'], a['leg']))
PY

printf '%s\n' "$WANT" > "$PIN_FILE"

# Staged, not committed: the maintainer reviews the diff and writes the message.
# One commit carrying both platforms is the point -- it is what makes a .so and
# a .dll from different releases impossible to land separately.
git -C "$ROOT" add pxxxx/ PXXXX_VERSION

echo
echo "pxxxx $WANT staged in $DEST. Review and commit:"
echo "   git -C $ROOT diff --cached --stat"
echo "   git -C $ROOT commit -m 'pxxxx driver $WANT'"
