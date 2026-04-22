#!/bin/bash

# Check repository files for hardcoded machine-specific absolute paths.
# Intended for project-owned files; third-party sources are excluded.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "Scanning for hardcoded absolute paths in project-owned files..."

# Exclude third-party code because it may legitimately contain toolchain paths.
EXCLUDES=(
  --exclude-dir=.git
  --exclude-dir=.venv
  --exclude-dir=lib
  --exclude=check_portability_paths.sh
)

PATTERN='/(home|Users)/|[A-Za-z]:\\\\|file:///'

if grep -RInE "$PATTERN" "${EXCLUDES[@]}" .; then
  echo
  echo "FAIL: Found potential hardcoded absolute paths."
  exit 1
fi

echo "PASS: No hardcoded machine-specific absolute paths found in project-owned files."
