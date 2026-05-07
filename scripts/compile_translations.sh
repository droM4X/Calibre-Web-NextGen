#!/bin/bash
# Compile every .po file under cps/translations to a sibling .mo file.
#
# Robust against per-locale failures: if one .po has a fatal msgfmt error,
# we log it and continue with the rest. A previous version used
# `find ... | while read ... exit 1` — that exit only exited the subshell,
# but stopped iteration there in practice because subsequent loop iterations
# never ran in a separate child. The first broken .po therefore silently
# dropped every locale that came after it in filesystem-traversal order.
# (See repo issue #71.)

set -uo pipefail

SCRIPT_DIR="$( cd -- "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
TRANSLATIONS_DIR="$ROOT_DIR/cps/translations"

if [ ! -d "$TRANSLATIONS_DIR" ]; then
  echo "[!] Translations directory not found: $TRANSLATIONS_DIR" >&2
  exit 1
fi

shopt -s nullglob
po_files=( "$TRANSLATIONS_DIR"/*/LC_MESSAGES/messages.po )
shopt -u nullglob

if [ "${#po_files[@]}" -eq 0 ]; then
  echo "[!] No messages.po files found under $TRANSLATIONS_DIR" >&2
  exit 1
fi

ok=0
failed_locales=()

for po_file in "${po_files[@]}"; do
  mo_file="${po_file%.po}.mo"
  locale_name="$(basename "$(dirname "$(dirname "$po_file")")")"
  if msgfmt "$po_file" -o "$mo_file" 2>/tmp/msgfmt-err.$$; then
    ok=$((ok + 1))
  else
    failed_locales+=( "$locale_name" )
    echo "[!] msgfmt failed for $po_file (locale=$locale_name):" >&2
    sed 's/^/    /' /tmp/msgfmt-err.$$ >&2
  fi
  rm -f /tmp/msgfmt-err.$$
done

total="${#po_files[@]}"
echo "[i] Compiled $ok/$total locales successfully."

if [ "${#failed_locales[@]}" -gt 0 ]; then
  echo "[!] Skipped locales (still have .po but no .mo): ${failed_locales[*]}" >&2
fi

# Don't fail the build over per-locale .po errors — losing one language
# shouldn't strand the rest. Only fail if literally nothing compiled.
if [ "$ok" -eq 0 ]; then
  echo "[X] No locales compiled. This is almost certainly a build problem." >&2
  exit 1
fi

exit 0
