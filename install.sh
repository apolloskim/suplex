#!/bin/bash
# Install suplex — single binary, no dependencies.
# Usage: curl -fsSL https://raw.githubusercontent.com/apolloskim/suplex/main/install.sh | bash

set -e

REPO="apolloskim/suplex"
INSTALL_DIR="${SUPLEX_INSTALL_DIR:-/usr/local/bin}"
BINARY_NAME="suplex"

# Detect OS and architecture.
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

case "$OS" in
    darwin) PLATFORM="macos" ;;
    linux)  PLATFORM="linux" ;;
    *)      echo "Unsupported OS: $OS"; exit 1 ;;
esac

case "$ARCH" in
    x86_64|amd64)  ARCH="x64" ;;
    arm64|aarch64) ARCH="arm64" ;;
    *)             echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

ASSET_NAME="suplex-${PLATFORM}-${ARCH}"

echo "Installing suplex for ${PLATFORM}-${ARCH}..."

# Get latest release URL.
RELEASE_URL=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | grep "browser_download_url.*${ASSET_NAME}" \
    | head -1 \
    | cut -d '"' -f 4)

if [ -z "$RELEASE_URL" ]; then
    echo "Error: Could not find release for ${ASSET_NAME}"
    echo "Check https://github.com/${REPO}/releases for available downloads."
    exit 1
fi

# Download.
TMPFILE=$(mktemp)
echo "Downloading from ${RELEASE_URL}..."
curl -fsSL "$RELEASE_URL" -o "$TMPFILE"
chmod +x "$TMPFILE"

# Install.
if [ -w "$INSTALL_DIR" ]; then
    mv "$TMPFILE" "${INSTALL_DIR}/${BINARY_NAME}"
else
    echo "Installing to ${INSTALL_DIR} (requires sudo)..."
    sudo mv "$TMPFILE" "${INSTALL_DIR}/${BINARY_NAME}"
fi

echo ""
echo "suplex installed to ${INSTALL_DIR}/${BINARY_NAME}"
echo ""
echo "Get started:"
echo "  suplex setup"
echo ""
