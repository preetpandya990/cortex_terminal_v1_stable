#!/usr/bin/env bash
set -euo pipefail

# ----------------------------------------
# CONFIG
# ----------------------------------------

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="$ROOT_DIR/code_dump_chunks"

MAX_LINES=1200
MAX_FILE_SIZE=$((1024 * 1024 * 2)) # 2MB hard cap per file

CHUNK_INDEX=1
CURRENT_LINES=0

DEBUG=${DEBUG:-false}

IGNORE_FILES_REGEX="\
\.log$|\.md$|README|\.gitignore$|\
package-lock\.json$|yarn\.lock$|pnpm-lock\.yaml$|\
\.sql$|\.svg$|\.png$|\.jpg$|\.jpeg$|\.gif$|\.pdf$|\
\.woff2?$|\.ttf$|\.env$|\.env\..*|\
poetry\.lock$|uv\.lock$"

# ----------------------------------------
# SETUP
# ----------------------------------------

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

MANIFEST="$OUTPUT_DIR/manifest.txt"
> "$MANIFEST"

echo "### PROJECT TREE" > "$OUTPUT_DIR/00_tree.txt"
tree -I ".git|node_modules|.next|dist|build|.turbo|.venv|venv|env|__pycache__|.mypy_cache|.pytest_cache|.ruff_cache|htmlcov|.axon|wsl.localhost" "$ROOT_DIR" >> "$OUTPUT_DIR/00_tree.txt"

CURRENT_FILE="$OUTPUT_DIR/chunk_${CHUNK_INDEX}.txt"
> "$CURRENT_FILE"

# ----------------------------------------
# FUNCTIONS
# ----------------------------------------

new_chunk() {
  ((CHUNK_INDEX++))
  CURRENT_FILE="$OUTPUT_DIR/chunk_${CHUNK_INDEX}.txt"
  CURRENT_LINES=0
  > "$CURRENT_FILE"
}

should_skip() {
  local filepath="$1"
  local base
  base="$(basename "$filepath")"
  [[ "$base" =~ $IGNORE_FILES_REGEX ]]
}

# ----------------------------------------
# MAIN
# ----------------------------------------

echo "Building chunks in: $OUTPUT_DIR"
echo "ROOT_DIR=$ROOT_DIR"

echo "Total files detected (pre-prune):"
find "$ROOT_DIR" -type f | wc -l

# ----------------------------------------
# STRICT PRUNED FIND (CRITICAL)
# ----------------------------------------

find -P "$ROOT_DIR" \
  -type d \( \
    -name ".git" \
    -o -name "node_modules" \
    -o -name ".next" \
    -o -name "dist" \
    -o -name "build" \
    -o -name ".turbo" \
    -o -name ".venv" \
    -o -name "venv" \
    -o -name "env" \
    -o -name "__pycache__" \
    -o -name ".mypy_cache" \
    -o -name ".pytest_cache" \
    -o -name ".ruff_cache" \
    -o -name "htmlcov" \
    -o -name ".axon" \
    -o -name "wsl.localhost" \
  \) -prune \
  -o -type f -print | sort | while IFS= read -r file; do

  rel_path="${file#$ROOT_DIR/}"

  # ----------------------------------------
  # DEFENSE-IN-DEPTH: FORCE SKIP .axon
  # ----------------------------------------
  if [[ "$rel_path" == .axon/* ]]; then
    [[ "$DEBUG" == true ]] && echo "FORCE SKIP AXON: $rel_path"
    continue
  fi

  # Skip ignored file types
  if should_skip "$rel_path"; then
    [[ "$DEBUG" == true ]] && echo "SKIP FILE: $rel_path"
    continue
  fi

  # Skip unreadable
  if [[ ! -r "$file" ]]; then
    [[ "$DEBUG" == true ]] && echo "UNREADABLE: $rel_path"
    continue
  fi

  # ----------------------------------------
  # HARD FILE SIZE LIMIT (CRITICAL)
  # ----------------------------------------
  FILE_SIZE=$(stat -c%s "$file" 2>/dev/null || echo 0)
  if (( FILE_SIZE > MAX_FILE_SIZE )); then
    [[ "$DEBUG" == true ]] && echo "SKIP LARGE FILE: $rel_path ($FILE_SIZE bytes)"
    continue
  fi

  # ----------------------------------------
  # ROBUST BINARY DETECTION
  # ----------------------------------------
  if ! grep -Iq . "$file"; then
    [[ "$DEBUG" == true ]] && echo "SKIP BINARY: $rel_path"
    continue
  fi

  FILE_LINES=$(wc -l < "$file" || echo 0)

  # Skip pathological files
  if (( FILE_LINES > MAX_LINES )); then
    [[ "$DEBUG" == true ]] && echo "SKIP HUGE FILE (LINES): $rel_path"
    continue
  fi

  # Rotate chunk if needed
  if (( CURRENT_LINES + FILE_LINES + 5 > MAX_LINES )); then
    new_chunk
  fi

  echo "" >> "$CURRENT_FILE"
  echo "--- FILE: $rel_path ---" >> "$CURRENT_FILE"
  echo "" >> "$CURRENT_FILE"

  cat "$file" >> "$CURRENT_FILE"

  CURRENT_LINES=$((CURRENT_LINES + FILE_LINES + 3))

  echo "chunk_${CHUNK_INDEX}.txt :: $rel_path" >> "$MANIFEST"

  [[ "$DEBUG" == true ]] && echo "ADDED: $rel_path"

done

# ----------------------------------------
# DONE
# ----------------------------------------

echo "✅ Dump complete"
echo "Output directory: $OUTPUT_DIR"
echo "Total chunks created: $CHUNK_INDEX"
echo "Manifest: $MANIFEST"