#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"

cd "${PROJECT_ROOT}"
npm test
