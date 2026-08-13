#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
APP_PATH="${1:-${PROJECT_ROOT}/dist/Codex Pulse.app}"
PACKAGED_APP="${PROJECT_ROOT}/src-tauri/target/release/bundle/macos/Codex Pulse.app"

cd "${PROJECT_ROOT}"
if [[ ! -d node_modules ]]; then
    echo "Dependencies are missing. Run npm install first." >&2
    exit 1
fi
if ! command -v cargo >/dev/null 2>&1; then
    echo "Rust is missing from PATH. Install rustup and expose its bin directory first." >&2
    exit 1
fi

npm run package
if [[ ! -d "${PACKAGED_APP}" ]]; then
    echo "Packaged application was not found at ${PACKAGED_APP}" >&2
    exit 1
fi

mkdir -p "${APP_PATH:h}"
if [[ -e "${APP_PATH}" ]]; then
    rm -rf -- "${APP_PATH}"
fi
/usr/bin/ditto "${PACKAGED_APP}" "${APP_PATH}"

echo "Built ${APP_PATH}"
