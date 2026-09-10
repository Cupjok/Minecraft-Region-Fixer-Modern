#!/usr/bin/env bash
#
# Build the native (Rust) scanner and drop the extension module next to
# regionfixer.py so that "import regionfixer_native" finds it.
#
# Requires a Rust toolchain: https://rustup.rs
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

# Run from native/ so that native/.cargo/config.toml applies. It carries the
# macOS link flags an extension module needs.
(cd native && cargo build --release)

case "$(uname -s)" in
    Darwin)  built="native/target/release/libregionfixer_native.dylib" ;;
    Linux)   built="native/target/release/libregionfixer_native.so" ;;
    MINGW*|MSYS*|CYGWIN*) built="native/target/release/regionfixer_native.dll" ;;
    *)       echo "Unsupported platform: $(uname -s)" >&2; exit 1 ;;
esac

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) out="regionfixer_native.pyd" ;;
    *)                    out="regionfixer_native.abi3.so" ;;
esac

cp "$built" "$out"
echo "Built $out"
python3 -c "import regionfixer_native; print('native scanner', regionfixer_native.__version__, 'ready,', regionfixer_native.available_threads(), 'threads')"
