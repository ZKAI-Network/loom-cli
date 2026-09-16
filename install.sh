#!/bin/sh
# Loom CLI installer.
#
#   curl -fsSL https://raw.githubusercontent.com/ZKAI-Network/loom-cli/main/install.sh | sh
#
# Downloads the right prebuilt `loom` binary for your OS/arch, verifies its
# checksum, and installs it to ~/.local/bin (override with LOOM_INSTALL_DIR).
# Pin a version with LOOM_VERSION=v1.2.3; otherwise the latest release is used.
set -eu

# The public repo that hosts the release binaries. Override for testing.
REPO="${LOOM_CLI_REPO:-ZKAI-Network/loom-cli}"
INSTALL_DIR="${LOOM_INSTALL_DIR:-$HOME/.local/bin}"

say()  { printf '\033[1;35m▌ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# 1. Detect platform → asset name (must match the release-workflow matrix).
os="$(uname -s)"; arch="$(uname -m)"
case "$os" in
  Darwin) os_tag="darwin" ;;
  Linux)  os_tag="linux" ;;
  *) die "Unsupported OS: $os (Windows is not yet packaged)." ;;
esac
case "$arch" in
  x86_64|amd64) arch_tag="x86_64" ;;
  arm64|aarch64) arch_tag="arm64" ;;
  *) die "Unsupported architecture: $arch." ;;
esac
asset="loom-${os_tag}-${arch_tag}"

# 2. Resolve the version (latest, or a pinned LOOM_VERSION).
if [ -n "${LOOM_VERSION:-}" ]; then
  tag="$LOOM_VERSION"
else
  say "Finding the latest release…"
  tag="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | grep '"tag_name"' | head -1 | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')"
  [ -n "$tag" ] || die "Could not determine the latest release. Set LOOM_VERSION=vX.Y.Z."
fi
base="https://github.com/${REPO}/releases/download/${tag}"

# 3. Download the binary + checksums to a temp dir.
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
say "Downloading loom ${tag} (${asset})…"
curl -fsSL "${base}/${asset}" -o "$tmp/loom" || die "Download failed: ${base}/${asset}"
curl -fsSL "${base}/SHA256SUMS" -o "$tmp/SHA256SUMS" || die "Could not fetch checksums."

# 4. Verify the checksum.
say "Verifying checksum…"
want="$(grep " ${asset}\$" "$tmp/SHA256SUMS" | awk '{print $1}')"
[ -n "$want" ] || die "No checksum for ${asset} in SHA256SUMS."
if command -v sha256sum >/dev/null 2>&1; then
  got="$(sha256sum "$tmp/loom" | awk '{print $1}')"
else
  got="$(shasum -a 256 "$tmp/loom" | awk '{print $1}')"
fi
[ "$want" = "$got" ] || die "Checksum mismatch — refusing to install (expected $want, got $got)."

# 5. Install.
mkdir -p "$INSTALL_DIR"
chmod +x "$tmp/loom"
mv "$tmp/loom" "$INSTALL_DIR/loom"
say "Installed loom ${tag} → $INSTALL_DIR/loom"

# 6. PATH hint.
case ":$PATH:" in
  *":$INSTALL_DIR:"*) : ;;
  *) printf '\n\033[1;33m%s\033[0m\n' "Add $INSTALL_DIR to your PATH:"
     printf '  export PATH="%s:$PATH"\n' "$INSTALL_DIR" ;;
esac

cat <<EOF

✓ Done. Get started:
  loom login                 # sign in (opens your browser)
  loom -p "your question"    # one-shot
  loom                       # interactive session
EOF
