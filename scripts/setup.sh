#!/usr/bin/env bash
# Richtet das reMarkable-Toolkit auf einem frischen Checkout ein:
# lädt das passende rmapi-Binary, baut das venv, installiert Abhängigkeiten und
# rendert die /remarkable-Skill für DIESE Maschine.
# Unterstützt macOS (arm64/intel) und Linux (amd64/arm64).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RMAPI_VER="v0.0.34"
OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS/$ARCH" in
  Darwin/arm64)              ASSET="rmapi-macos-arm64.zip" ;;
  Darwin/x86_64)             ASSET="rmapi-macos-intel.zip" ;;
  Linux/x86_64|Linux/amd64)  ASSET="rmapi-linux-amd64.tar.gz" ;;
  Linux/aarch64|Linux/arm64) ASSET="rmapi-linux-arm64.tar.gz" ;;
  *) echo "FEHLER: nicht unterstützte Plattform $OS/$ARCH"; exit 1 ;;
esac

if [ ! -x bin/rmapi ]; then
  echo "→ lade rmapi $RMAPI_VER ($OS/$ARCH → $ASSET) …"
  mkdir -p bin
  curl -fsSL -o "bin/$ASSET" \
    "https://github.com/ddvk/rmapi/releases/download/${RMAPI_VER}/${ASSET}"
  case "$ASSET" in
    *.zip)    unzip -o "bin/$ASSET" -d bin >/dev/null ;;
    *.tar.gz) tar -xzf "bin/$ASSET" -C bin ;;
  esac
  rm -f "bin/$ASSET"
  chmod +x bin/rmapi
fi

if [ ! -d .venv ]; then
  echo "→ erstelle venv …"
  python3 -m venv .venv
fi
echo "→ installiere Python-Abhängigkeiten …"
.venv/bin/pip install -q --upgrade pip markdown pygments trafilatura rmc

# /remarkable-Skill für DIESE Maschine rendern (echter Pfad statt Platzhalter)
SKILL_DIR="$HOME/.claude/skills/remarkable"
if [ -f skill/SKILL.md.tmpl ]; then
  echo "→ installiere Skill nach $SKILL_DIR …"
  mkdir -p "$SKILL_DIR"
  sed "s|@@RM_HOME@@|$ROOT|g" skill/SKILL.md.tmpl > "$SKILL_DIR/SKILL.md"
fi

if [ ! -f .rmapi.conf ]; then
  echo "⚠  Noch nicht angemeldet. Code holen: https://my.remarkable.com/device/browser/connect"
  echo "   dann:  echo <CODE> | RMAPI_CONFIG=$ROOT/.rmapi.conf bin/rmapi ls"
fi
echo "✓ Setup fertig ($OS/$ARCH)."
