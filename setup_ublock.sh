#!/usr/bin/env bash
# setup_ublock.sh — Download and extract uBlock Origin for Playwright
#
# Playwright needs an unpacked extension directory (with manifest.json).
# This script grabs the latest uBlock Origin release from GitHub and extracts it.

set -euo pipefail

EXTENSION_DIR="./extensions/ublock-origin"

echo "🛡️  Setting up uBlock Origin for Playwright..."

# Clean old version
if [ -d "$EXTENSION_DIR" ]; then
    echo "Removing old version..."
    rm -rf "$EXTENSION_DIR"
fi

mkdir -p "$EXTENSION_DIR"

# Method 1: Download from GitHub releases (Chromium MV3 version)
echo "Downloading uBlock Origin from GitHub..."
RELEASE_URL=$(curl -s https://api.github.com/repos/gorhill/uBlock/releases/latest \
    | grep -o '"browser_download_url": "[^"]*chromium[^"]*"' \
    | head -1 \
    | cut -d'"' -f4)

if [ -z "$RELEASE_URL" ]; then
    echo "⚠️  Could not find Chromium build in latest release."
    echo ""
    echo "Manual setup instructions:"
    echo "1. Go to https://github.com/nickspaargaren/no-google or https://github.com/nickspaargaren/no-google"
    echo "2. OR install uBlock Origin in Chrome, then find the extension directory:"
    echo "   - Navigate to chrome://extensions"
    echo "   - Enable Developer mode"
    echo "   - Note the extension ID"
    echo "   - Copy from: ~/.config/google-chrome/Default/Extensions/<id>/<version>/"
    echo "   - Paste to: $EXTENSION_DIR/"
    echo ""
    echo "Alternative: Use your Chrome profile directly:"
    echo "   - Find your profile dir: ~/.config/google-chrome/Default"
    echo "   - Or on CachyOS: ~/.config/chromium/Default"
    echo "   - Set browser.user_data_dir in config.yaml to point there"
    echo "   - This will use ALL your existing extensions"
    exit 1
fi

echo "Downloading: $RELEASE_URL"
TMPFILE=$(mktemp /tmp/ublock-XXXXXX.zip)
curl -L -o "$TMPFILE" "$RELEASE_URL"

echo "Extracting to $EXTENSION_DIR..."
unzip -q -o "$TMPFILE" -d "$EXTENSION_DIR"
rm "$TMPFILE"

# Check if manifest.json ended up in a subdirectory
if [ ! -f "$EXTENSION_DIR/manifest.json" ]; then
    # Find it and move contents up
    SUBDIR=$(find "$EXTENSION_DIR" -name "manifest.json" -maxdepth 2 | head -1 | xargs dirname)
    if [ -n "$SUBDIR" ] && [ "$SUBDIR" != "$EXTENSION_DIR" ]; then
        mv "$SUBDIR"/* "$EXTENSION_DIR/"
        rm -rf "$SUBDIR"
    fi
fi

if [ -f "$EXTENSION_DIR/manifest.json" ]; then
    echo "✅ uBlock Origin extracted successfully!"
    echo "   Path: $EXTENSION_DIR"
    VERSION=$(grep '"version"' "$EXTENSION_DIR/manifest.json" | head -1 | grep -o '"[0-9.]*"' | tr -d '"')
    echo "   Version: ${VERSION:-unknown}"
else
    echo "❌ Something went wrong — manifest.json not found"
    echo "   Check $EXTENSION_DIR manually"
    exit 1
fi

echo ""
echo "💡 Alternative: Use your existing Chrome/Chromium profile instead."
echo "   Set browser.user_data_dir in config.yaml to:"
echo "   ~/.config/chromium/Default  (CachyOS/Arch)"
echo "   ~/.config/google-chrome/Default  (Chrome)"
echo "   This will use ALL your existing extensions including uBlock."
