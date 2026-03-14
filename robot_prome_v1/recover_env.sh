#!/usr/bin/env bash
set -euo pipefail

IS_SOURCED=0
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  IS_SOURCED=1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

deactivate 2>/dev/null || true
rm -rf .venv
sudo apt update
sudo apt install -y alsa-utils python3-venv python3-dev libportaudio2 portaudio19-dev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "Environment recovery done."
echo "Activating .venv..."
source .venv/bin/activate

if [[ "${IS_SOURCED}" -eq 1 ]]; then
  echo "Done. .venv is active in current shell."
else
  echo "Done. Starting a new interactive shell with .venv active."
  echo "Tip: next time run: source ~/robot_prome_v1/recover_env.sh"
  exec bash --noprofile --norc -i -c "cd \"${SCRIPT_DIR}\" && source .venv/bin/activate && exec bash -i"
fi
