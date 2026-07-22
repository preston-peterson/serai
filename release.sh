#!/usr/bin/env bash
# =============================================================================
# serai — Release Script
# =============================================================================
#
# Usage:
#   ./release.sh                 # build + publish assets for the current version
#   ./release.sh --dry-run       # build and verify locally, upload nothing
#
# Builds a source tarball for the version in serai/__init__.py, writes a
# sha256 checksums file next to it, and attaches both to the matching GitHub
# release.
#
# Why a built tarball rather than GitHub's auto-generated one: GitHub
# regenerates `archive/refs/tags/*.tar.gz` on demand, and its bytes have
# changed before (the compression settings were changed in 2023, breaking
# every checksum anyone had published). An uploaded release asset is stored
# once and never rewritten, so a checksum published against it stays true.
# An installer that verifies a hash needs that guarantee to mean anything.
#
# The archive is built reproducibly: `git archive` from the tag (so it holds
# exactly the tracked tree, no .git, no venv, no untracked scratch) piped
# through `gzip -n` (which omits the timestamp and original filename that
# would otherwise make two builds of the same commit differ).
#
# The version lives in serai/__init__.py and nowhere else -- there is
# deliberately no VERSION file to drift from it.
# =============================================================================

set -euo pipefail

GREEN='\033[32m\033[1m'
RED='\033[31m\033[1m'
CYAN='\033[36m\033[1m'
YELLOW='\033[33m\033[1m'
RESET='\033[0m'

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SOURCE_DIR"

DRY=false
while [ $# -gt 0 ]; do
    case "$1" in
        -n|--dry-run) DRY=true; shift ;;
        -h|--help)
            sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo -e "${RED}Unknown option:${RESET} $1" >&2; exit 1 ;;
    esac
done

say()  { echo -e "${CYAN}==>${RESET} $*"; }
ok()   { echo -e "  ${GREEN}✓${RESET} $*"; }
fail() { echo -e "${RED}Error:${RESET} $*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------

command -v git >/dev/null       || fail "git is required"
command -v sha256sum >/dev/null || fail "sha256sum is required (coreutils)"
$DRY || command -v gh >/dev/null || fail "the GitHub CLI (gh) is required to upload"

VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' serai/__init__.py)"
[ -n "$VERSION" ] || fail "couldn't read __version__ from serai/__init__.py"
TAG="v${VERSION}"

say "serai ${VERSION}  (tag ${TAG})"

git rev-parse "$TAG" >/dev/null 2>&1 \
    || fail "tag ${TAG} does not exist. Tag the release first:
  git tag -a ${TAG} -m \"serai ${VERSION}\" && git push origin ${TAG}"

# The tarball is built from the TAG, not the working tree, so a dirty tree
# can't leak into a published artifact -- but warn, because it usually means
# the tag isn't what you think it is.
if [ -n "$(git status --porcelain)" ]; then
    echo -e "  ${YELLOW}note:${RESET} working tree is dirty; building from ${TAG}, not the tree"
fi

TAG_VERSION="$(git show "${TAG}:serai/__init__.py" | sed -n 's/^__version__ = "\(.*\)"$/\1/p')"
[ "$TAG_VERSION" = "$VERSION" ] \
    || fail "tag ${TAG} carries version ${TAG_VERSION}, but the tree says ${VERSION}"
ok "tag ${TAG} carries version ${VERSION}"

# --- build -------------------------------------------------------------------

DIST="${SOURCE_DIR}/dist"
TARBALL="serai_${VERSION}.tar.gz"
SUMS="serai_${VERSION}_checksums.txt"

mkdir -p "$DIST"
say "building ${TARBALL}"
# --format=tar + gzip -n keeps the bytes reproducible: gzip would otherwise
# stamp the current time into the header, so rebuilding the same tag would
# produce a different sha256 and make the published checksum unverifiable.
git archive --format=tar --prefix="serai-${VERSION}/" "$TAG" \
    | gzip -n -9 > "${DIST}/${TARBALL}"
ok "$(du -h "${DIST}/${TARBALL}" | cut -f1) — $(tar -tzf "${DIST}/${TARBALL}" | wc -l) entries"

# sanity: the archive must actually contain the app, not just docs.
# List once into a variable and match with a here-string: `tar | grep -q` would
# hand tar a SIGPIPE the moment grep found its match, and `set -o pipefail`
# reports that as a failed pipeline.
LISTING="$(tar -tzf "${DIST}/${TARBALL}")"
for required in "serai-${VERSION}/serai/main.py" "serai-${VERSION}/install.sh" \
                "serai-${VERSION}/pyproject.toml"; do
    grep -qx "$required" <<<"$LISTING" \
        || fail "archive is missing ${required} — refusing to publish it"
done
ok "archive contains the app, the installer, and packaging metadata"

say "writing ${SUMS}"
( cd "$DIST" && sha256sum "$TARBALL" > "$SUMS" )
ok "$(cut -d' ' -f1 "${DIST}/${SUMS}")"

# Verify the way an installer would, before anyone else has to trust it.
say "verifying the checksum file against the tarball"
( cd "$DIST" && sha256sum -c "$SUMS" >/dev/null ) \
    || fail "checksum verification failed locally — do not publish this"
ok "sha256sum -c passes"

# Reproducibility check: build it again and confirm the hash is identical.
say "rebuilding to confirm the archive is reproducible"
git archive --format=tar --prefix="serai-${VERSION}/" "$TAG" \
    | gzip -n -9 > "${DIST}/.repro.tar.gz"
A="$(sha256sum "${DIST}/${TARBALL}" | cut -d' ' -f1)"
B="$(sha256sum "${DIST}/.repro.tar.gz" | cut -d' ' -f1)"
rm -f "${DIST}/.repro.tar.gz"
[ "$A" = "$B" ] || fail "archive is not reproducible ($A != $B) — the checksum would be a lie"
ok "two builds of ${TAG} produce identical bytes"

# --- publish -----------------------------------------------------------------

if $DRY; then
    echo
    echo -e "  ${YELLOW}(dry-run)${RESET} would upload to release ${TAG}:"
    echo -e "  ${YELLOW}(dry-run)${RESET}   ${DIST}/${TARBALL}"
    echo -e "  ${YELLOW}(dry-run)${RESET}   ${DIST}/${SUMS}"
    echo -e "  ${YELLOW}(dry-run)${RESET} nothing was uploaded."
    exit 0
fi

gh release view "$TAG" >/dev/null 2>&1 \
    || fail "no GitHub release for ${TAG}. Create it first:
  gh release create ${TAG} --title \"serai ${VERSION}\" --notes-file <notes>"

say "uploading assets to release ${TAG}"
gh release upload "$TAG" "${DIST}/${TARBALL}" "${DIST}/${SUMS}" --clobber
ok "uploaded"

echo
echo -e "${GREEN}Released:${RESET} $(gh release view "$TAG" --json url --jq .url)"
echo "  verify a download with:"
echo "    sha256sum -c ${SUMS}"
