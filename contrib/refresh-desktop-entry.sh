#!/usr/bin/env bash
set -uo pipefail
src=${WHEELPROXY_DESKTOP_SRC:-/usr/share/applications/google-chrome.desktop}
dst=${WHEELPROXY_DESKTOP_DST:-$HOME/.local/share/applications/google-chrome.desktop}
launcher=${WHEELPROXY_LAUNCHER:-$HOME/.local/bin/google-chrome-smooth}
stock=/usr/bin/google-chrome-stable

if [ ! -r "$src" ]; then
    echo "[desktop] refused reason=no_source src=$src" >&2
    exit 1
fi
if ! grep -q "^Exec=$stock" "$src"; then
    echo "[desktop] refused reason=no_exec_to_replace src=$src expected=$stock" >&2
    exit 1
fi

want=$(sed "s|^Exec=$stock|Exec=$launcher|" "$src"; echo x)
want=${want%x}
if [ -f "$dst" ] && [ "$(cat "$dst"; echo x)" = "${want}x" ]; then
    echo "[desktop] up_to_date dst=$dst" >&2
    exit 0
fi

reason=changed
[ -f "$dst" ] || reason=missing
mkdir -p "$(dirname "$dst")"
printf '%s' "$want" > "$dst.tmp" && mv "$dst.tmp" "$dst"
echo "[desktop] refreshed reason=$reason dst=$dst" >&2
if command -v kdialog >/dev/null; then
    timeout 10 kdialog --title wheelproxy --passivepopup "Chrome menu entry refreshed after an update, smooth wheel scrolling stays on" 5 >/dev/null 2>&1
fi
exit 0
