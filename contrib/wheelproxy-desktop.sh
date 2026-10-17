#!/usr/bin/env bash
set -uo pipefail
src_dir=${WHEELPROXY_APPS_SRC:-/usr/share/applications}
dst_dir=${WHEELPROXY_APPS_DST:-$HOME/.local/share/applications}
launch=${WHEELPROXY_LAUNCH:-$HOME/.local/bin/wheelproxy-launch}

if [ $# -eq 0 ]; then
    echo "[desktop] refused reason=no_entries usage=wheelproxy-desktop NAME.desktop..." >&2
    exit 2
fi

refresh() {
    local name=$1 src=$src_dir/$1 dst=$dst_dir/$1 want reason
    if [ ! -r "$src" ]; then
        echo "[desktop] refused entry=$name reason=no_source src=$src" >&2
        return 1
    fi
    if ! grep -q '^Exec=' "$src"; then
        echo "[desktop] refused entry=$name reason=no_exec src=$src" >&2
        return 1
    fi
    want=$(sed "/^Exec=[^ ]*wheelproxy-launch /!s|^Exec=|Exec=$launch |" "$src"; echo x)
    want=${want%x}
    if [ -f "$dst" ] && [ "$(cat "$dst"; echo x)" = "${want}x" ]; then
        echo "[desktop] up_to_date entry=$name" >&2
        return 0
    fi
    reason=changed
    [ -f "$dst" ] || reason=missing
    mkdir -p "$dst_dir"
    printf '%s' "$want" > "$dst.tmp" && mv "$dst.tmp" "$dst"
    echo "[desktop] refreshed entry=$name reason=$reason dst=$dst" >&2
    refreshed+=("$name")
}

refreshed=()
status=0
for name in "$@"; do
    refresh "$name" || status=1
done
if [ ${#refreshed[@]} -gt 0 ] && command -v kdialog >/dev/null; then
    timeout 10 kdialog --title wheelproxy --passivepopup "Menu entry refreshed for smooth wheel scrolling: ${refreshed[*]}" 5 >/dev/null 2>&1
fi
exit $status
