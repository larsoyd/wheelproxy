#!/usr/bin/env bash
set -uo pipefail
if [ $# -eq 0 ]; then
    echo "[launcher] refused reason=no_browser usage=wheelproxy-launch BROWSER [ARGS]" >&2
    exit 2
fi
sock=${XDG_RUNTIME_DIR:-/run/user/$UID}/${WHEELPROXY_SOCKET:-wayland-smooth}

if [ -S "$sock" ] && timeout 2 python3 -I -c 'import socket, sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1])' "$sock" 2>/dev/null; then
    echo "[launcher] via=proxy socket=$sock browser=$1" >&2
    browser=$1
    shift
    WAYLAND_DISPLAY=$(basename "$sock") exec "$browser" --disable-smooth-scrolling "$@"
fi
echo "[launcher] via=direct reason=no_proxy socket=$sock browser=$1" >&2
exec "$@"
