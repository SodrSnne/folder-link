#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
APP="$ROOT/dist/Folder Link.app"
NATIVE_ARCH="$(uname -m)"
case "$NATIVE_ARCH" in arm64|x86_64) ;; *) echo "Unsupported architecture: $NATIVE_ARCH" >&2; exit 1 ;; esac
mkdir -p build/native dist
.venv/bin/python -m PyInstaller --noconfirm --clean --onedir --name folder-link-engine --paths "$ROOT" --distpath build/helper-dist --workpath build/helper-work --specpath build macos/backend.py
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
rsync -a --delete build/helper-dist/folder-link-engine/ "$APP/Contents/Resources/engine/"
swiftc -parse-as-library -swift-version 5 -O -target "${NATIVE_ARCH}-apple-macosx14.0" -framework AppKit -framework SwiftUI macos/FolderLink.swift -o "$APP/Contents/MacOS/FolderLink"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>FolderLink</string>
<key>CFBundleIdentifier</key><string>local.folderlink.mac</string>
<key>CFBundleName</key><string>Folder Link</string>
<key>CFBundleDisplayName</key><string>Folder Link</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.2.0</string>
<key>CFBundleVersion</key><string>3</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>NSHumanReadableCopyright</key><string>Folder Link · 本地双向文件同步</string>
</dict></plist>
PLIST
swift macos/Icon.swift "$ROOT/build/native/AppIcon.iconset"
iconutil -c icns build/native/AppIcon.iconset -o "$APP/Contents/Resources/AppIcon.icns"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
echo "已构建：$APP"
