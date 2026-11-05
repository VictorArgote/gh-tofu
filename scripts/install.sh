#!/bin/sh
# Installs the gh-tofu binary from a GitHub release. Nothing under INSTALL_DIR is
# written until the download matches sha256sums.txt.
set -eu

REPOSITORY="${GH_TOFU_REPOSITORY:-vmvarela/gh-tofu}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
# Overrides exist so tests can point the installer at local fixtures.
API_URL="${GH_TOFU_API_URL:-https://api.github.com/repos/${REPOSITORY}}"
RELEASE_URL="${GH_TOFU_RELEASE_URL:-https://github.com/${REPOSITORY}/releases/download}"

fail() {
  printf 'gh-tofu installer: %s\n' "$*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

download() {
  url=$1
  output=$2

  if command -v curl >/dev/null 2>&1; then
    curl --fail --location --silent --show-error "$url" --output "$output"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$output"
  else
    fail "curl or wget is required"
  fi
}

latest_version() {
  tmp=$1
  download "$API_URL/releases/latest" "$tmp"

  version=$(sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\([^"]*\)".*/\1/p' "$tmp" | head -n 1)
  [ -n "$version" ] || fail "could not determine the latest stable release"
  printf '%s\n' "$version"
}

checksum() {
  file=$1

  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$file" | awk '{print $NF}'
  else
    fail "sha256sum, shasum, or openssl is required"
  fi
}

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=darwin ;;
  *) fail "unsupported operating system: $(uname -s)" ;;
esac

case "$(uname -m)" in
  x86_64|amd64) arch=amd64 ;;
  arm64|aarch64) arch=arm64 ;;
  *) fail "unsupported architecture: $(uname -m)" ;;
esac

need awk
need sed

tmpdir=$(mktemp -d 2>/dev/null || mktemp -d -t gh-tofu)
stage=""
trap 'rm -rf "$tmpdir"; if [ -n "$stage" ]; then rm -f "$stage"; fi' EXIT HUP INT TERM

version=${VERSION:-}
if [ -z "$version" ]; then
  version=$(latest_version "$tmpdir/latest.json")
fi
version=${version#v}

case "$version" in
  ''|*[!0-9.]*|.*|*.|*..*) fail "VERSION must be a stable semantic version: $version" ;;
esac
major=${version%%.*}
rest=${version#*.}
minor=${rest%%.*}
patch=${rest#*.}
[ "$version" = "$major.$minor.$patch" ] ||
  fail "VERSION must be a stable semantic version: $version"
case "$major$minor$patch" in
  ''|*[!0-9]*) fail "VERSION must be a stable semantic version: $version" ;;
esac

# Asset names are the contract with release.yml and with GitHub CLI extension
# discovery: gh-tofu-<GOOS>-<GOARCH>, as raw executables.
tag="v$version"
asset="gh-tofu-${os}-${arch}"
asset_path="$tmpdir/$asset"
sums_path="$tmpdir/sha256sums.txt"

printf 'Installing gh-tofu %s for %s/%s\n' "$version" "$os" "$arch"

download "$RELEASE_URL/$tag/$asset" "$asset_path"
download "$RELEASE_URL/$tag/sha256sums.txt" "$sums_path"

# Duplicate entries are ambiguous, so they fail closed like missing ones.
count=$(awk -v file="$asset" '$2 == file || $2 == "*" file { n++ } END { print n + 0 }' "$sums_path")
[ "$count" -eq 1 ] || fail "sha256sums.txt must list $asset exactly once (found $count)"
expected=$(awk -v file="$asset" '$2 == file || $2 == "*" file { print $1 }' "$sums_path")
case "$expected" in
  ''|*[!0-9a-f]*) fail "invalid checksum for $asset in sha256sums.txt" ;;
esac
[ "${#expected}" -eq 64 ] || fail "invalid checksum for $asset in sha256sums.txt"
actual=$(checksum "$asset_path")
[ "$actual" = "$expected" ] || fail "checksum mismatch for $asset"

mkdir -p "$INSTALL_DIR" 2>/dev/null || fail "cannot create $INSTALL_DIR"
dest="$INSTALL_DIR/gh-tofu"
# Symlinks and non-regular files are never replaced: a verified binary only
# swaps in for an existing regular file (an upgrade).
if [ -L "$dest" ] || { [ -e "$dest" ] && [ ! -f "$dest" ]; }; then
  fail "refusing to replace $dest: it is not a regular file"
fi

stage=$(mktemp "$INSTALL_DIR/.gh-tofu.XXXXXX") ||
  fail "cannot write to $INSTALL_DIR (set INSTALL_DIR to a writable directory)"
if ! cp "$asset_path" "$stage" || ! chmod 0755 "$stage" || ! mv -f "$stage" "$dest"; then
  fail "cannot install to $dest (set INSTALL_DIR to a writable directory)"
fi
stage=""

printf 'Installed %s\n' "$dest"
