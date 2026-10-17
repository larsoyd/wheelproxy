#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")" || exit 1
unit=wheelproxy.service
sock=${XDG_RUNTIME_DIR:-/run/user/$UID}/wayland-smooth
force=0
case "${1:-}" in
    "") ;;
    --restart-now) force=1 ;;
    *)
        echo "[install] refused reason=unknown_flag flag=$1 usage=./install.sh_[--restart-now]" >&2
        exit 2
        ;;
esac

step() {
    local name=$1 secs=$2 start=$SECONDS status
    shift 2
    timeout "$secs" "$@"
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "[install] refused reason=$name status=$status" >&2
        exit 1
    fi
    echo "[install] $name ok s=$((SECONDS - start))" >&2
}

clients() {
    timeout 10 ss -x -H | awk -v s="$sock" '$2 == "ESTAB" && $5 == s' | wc -l
}

main_pid() {
    timeout 10 systemctl --user show -p MainPID --value "$unit"
}

step test 600 make test
step install 120 make install-desktop
step daemon_reload 30 systemctl --user daemon-reload
step enable 30 systemctl --user enable "$unit"

old=$(main_pid)
dropped=0
if timeout 10 systemctl --user is-active --quiet "$unit"; then
    dropped=$(clients)
    if [ "$dropped" -gt 0 ] && [ "$force" -eq 0 ]; then
        echo "[install] deferred reason=clients_connected clients=$dropped pid=$old next=quit_browsers_and_rerun" >&2
        exit 3
    fi
    step restart 30 systemctl --user restart "$unit"
else
    step start 30 systemctl --user start "$unit"
fi

pid=0
for _ in $(seq 50); do
    pid=$(main_pid)
    if [ "${pid:-0}" != 0 ] && [ "$pid" != "$old" ] &&
        timeout 10 journalctl --user -u "$unit" "_PID=$pid" -o cat --no-pager | grep -q "start pid=$pid "; then
        echo "[install] ok pid=$pid old_pid=$old clients_dropped=$dropped commit=$(git rev-parse --short HEAD)" >&2
        exit 0
    fi
    sleep 0.1
done
state=$(timeout 10 systemctl --user is-active "$unit")
echo "[install] refused reason=not_running pid=${pid:-0} old_pid=$old state=$state" >&2
exit 1
