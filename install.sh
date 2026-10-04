#!/usr/bin/env bash
# Install lifdrop on ~/.local/bin and record PATH in ~/.zshrc and ~/.bashrc.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "${ROOT}/lifdrop.py"

BIN_DIR="${HOME}/.local/bin"
mkdir -p "${BIN_DIR}"
ln -sfn "${ROOT}/lifdrop.py" "${BIN_DIR}/lifdrop"

install_command() {
  local name="$1"
  local subcommand="$2"
  local dest="${BIN_DIR}/${name}"
  printf '%s\n' \
    '#!/bin/sh' \
    "exec \"${BIN_DIR}/lifdrop\" ${subcommand} \"\$@\"" \
    > "${dest}"
  chmod +x "${dest}"
}

install_command lift lift
install_command drop drop

MARK_BEGIN="# >>> lifdrop >>>"
MARK_END="# <<< lifdrop <<<"

append_path_block() {
  local rc="$1"
  if [[ -f "${rc}" ]] && grep -qF "${MARK_BEGIN}" "${rc}"; then
    echo "lifdrop: ${rc} already configured"
    return
  fi
  touch "${rc}"
  if [[ -s "${rc}" ]]; then
    printf '\n' >> "${rc}"
  fi
  printf '%s\n' \
    "${MARK_BEGIN}" \
    'export PATH="$HOME/.local/bin:$PATH"' \
    "${MARK_END}" >> "${rc}"
  echo "lifdrop: updated ${rc}"
}

append_path_block "${HOME}/.zshrc"
append_path_block "${HOME}/.bashrc"

echo "lifdrop: installed ${BIN_DIR}/lift and ${BIN_DIR}/drop"
echo "lifdrop: open a new shell, or run: export PATH=\"\$HOME/.local/bin:\$PATH\""
