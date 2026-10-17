#!/usr/bin/env bash
set -uo pipefail
chrome=${WHEELPROXY_CHROME:-/usr/bin/google-chrome-stable}
sock=${XDG_RUNTIME_DIR:-/run/user/$UID}/${WHEELPROXY_SOCKET:-wayland-smooth}

if [ -S "$sock" ] && timeout 2 python3 -I -c 'import socket, sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1])' "$sock" 2>/dev/null; then
    echo "[launcher] via=proxy socket=$sock" >&2
    WAYLAND_DISPLAY=$(basename "$sock") exec "$chrome" --disable-smooth-scrolling "$@"
fi
echo "[launcher] via=direct reason=no_proxy socket=$sock" >&2
exec "$chrome" "$@"
