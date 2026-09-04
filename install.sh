#!/bin/zsh
#
# install.sh — put `new-claude` and `remove-claude` on your PATH.
#
# Symlinks the scripts into ~/.local/bin (created if needed) and reminds you to
# ensure that dir is on PATH. Safe to re-run.

set -e

REPO_DIR="${0:A:h}"          # directory this script lives in
BIN_SRC="$REPO_DIR/bin"
BIN_DST="$HOME/.local/bin"

mkdir -p "$BIN_DST"

chmod +x "$BIN_SRC/new-claude.sh" "$BIN_SRC/remove-claude.sh" \
         "$BIN_SRC/new-chatgpt.sh" "$BIN_SRC/remove-chatgpt.sh"

ln -sf "$BIN_SRC/new-claude.sh"     "$BIN_DST/new-claude"
ln -sf "$BIN_SRC/remove-claude.sh"  "$BIN_DST/remove-claude"
ln -sf "$BIN_SRC/new-chatgpt.sh"    "$BIN_DST/new-chatgpt"
ln -sf "$BIN_SRC/remove-chatgpt.sh" "$BIN_DST/remove-chatgpt"

echo "Linked:"
echo "   $BIN_DST/new-claude     -> $BIN_SRC/new-claude.sh"
echo "   $BIN_DST/remove-claude  -> $BIN_SRC/remove-claude.sh"
echo "   $BIN_DST/new-chatgpt    -> $BIN_SRC/new-chatgpt.sh"
echo "   $BIN_DST/remove-chatgpt -> $BIN_SRC/remove-chatgpt.sh"
echo ""

case ":$PATH:" in
  *":$BIN_DST:"*)
    echo "$BIN_DST is already on your PATH. You're ready:"
    echo "   new-claude \"Claude MyCompany\""
    echo "   new-chatgpt \"ChatGPT MyCompany\"" ;;
  *)
    echo "Add $BIN_DST to your PATH by adding this line to ~/.zshrc:"
    echo ""
    echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    echo "Then run: source ~/.zshrc" ;;
esac
