#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"

# Codex currently applies per-skill enablement only from user/session layers,
# so keep these exclusions scoped to sessions launched through this script.
SKILL_CONFIG='skills.config=[{name="imagegen",enabled=false},{name="plugin-creator",enabled=false},{name="skill-creator",enabled=false},{name="skill-installer",enabled=false},{name="browser:control-in-app-browser",enabled=false},{name="chrome:control-chrome",enabled=false},{name="documents:documents",enabled=false},{name="html-ppt",enabled=false},{name="pdf:pdf",enabled=false},{name="presentations:Presentations",enabled=false},{name="spreadsheets:Spreadsheets",enabled=false},{name="spreadsheets:excel-live-control",enabled=false},{name="template-creator:template-creator",enabled=false},{name="visualize:visualize",enabled=false}]'

exec codex -C "${PROJECT_ROOT}" -c "${SKILL_CONFIG}" "$@"
