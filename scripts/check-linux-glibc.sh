#!/usr/bin/env bash
# Checks that no ELF file under a folder needs a newer glibc than the Linux baseline (default 2.28: Debian 10,
# RHEL 8, Ubuntu 20.04+): the highest GLIBC_x.y symbol version each binary references must not exceed it.
# Usage: scripts/check-linux-glibc.sh <folder> [max glibc version]
set -euo pipefail
DIR="$1"
MAX="${2:-2.28}"
command -v objdump >/dev/null || { echo "objdump (binutils) is needed" >&2; exit 1; }
worst="0.0"
worst_file=""
count=0
while IFS= read -r -d '' f; do
  [ "$(head -c 4 "$f" | od -An -tx1 | tr -d ' \n')" = 7f454c46 ] || continue
  count=$((count + 1))
  v="$(objdump -T "$f" 2>/dev/null | grep -o 'GLIBC_[0-9][0-9.]*' | sed 's/GLIBC_//' | sort -V | tail -n 1 || true)"
  [ -n "$v" ] || continue
  if [ "$(printf '%s\n%s\n' "$v" "$worst" | sort -V | tail -n 1)" = "$v" ] && [ "$v" != "$worst" ]; then worst="$v"; worst_file="$f"; fi
done < <(find "$DIR" -type f -print0)
echo "checked $count ELF files under $DIR: newest glibc needed $worst (${worst_file:-none}); baseline $MAX"
[ "$(printf '%s\n%s\n' "$worst" "$MAX" | sort -V | tail -n 1)" = "$MAX" ] || { echo "$worst_file needs glibc $worst > $MAX" >&2; exit 1; }
