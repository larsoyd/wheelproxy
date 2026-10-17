#!/usr/bin/env bash
set -uo pipefail
apps=${WHEELPROXY_APPS:-/usr/share/applications}
config=${WHEELPROXY_CONFIG:-/etc/wheelproxy/browsers}
launch=${WHEELPROXY_LAUNCH:-/usr/bin/wheelproxy-launch}
wrapped='^Exec=[^ ]*wheelproxy-launch '
declare -A kind=() selected=()
all=0
status=0

main_key() {
    awk -v k="$2" '/^\[/ { main = ($0 == "[Desktop Entry]") }
        main && index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }' "$1"
}

is_browser() {
    local cats mime
    cats=";$(main_key "$1" Categories);"
    mime=";$(main_key "$1" MimeType);"
    [[ $cats == *";WebBrowser;"* || $mime == *";x-scheme-handler/http;"* || $mime == *";x-scheme-handler/https;"* ]]
}

# a package with an ELF that knows the smooth scrolling switch is a chromium build
scan_package() {
    local pkg=$1 files f
    [ -n "${kind[$pkg]:-}" ] && return
    if ! files=$(pacman -Qlq "$pkg" 2>/dev/null); then
        kind[$pkg]=error
        return
    fi
    kind[$pkg]=other
    while IFS= read -r f; do
        [ -f "$f" ] && [ -x "$f" ] && [ "$(head -c4 "$f")" = $'\x7fELF' ] || continue
        if grep -qaF disable-smooth-scrolling "$f"; then
            kind[$pkg]=chromium
            return
        fi
    done <<< "$files"
}

rewrite() {
    local path=$1 tmp=$1.wheelproxy
    if sed "$2" "$path" > "$tmp" && chmod --reference="$path" "$tmp" && mv "$tmp" "$path"; then
        return 0
    fi
    rm -f "$tmp"
    echo "[browsers] refused entry=${path##*/} reason=write_failed path=$path" >&2
    status=1
    return 1
}

load_config() {
    local line
    if [ ! -r "$config" ]; then
        echo "[browsers] refused reason=no_config path=$config" >&2
        exit 1
    fi
    while IFS= read -r line; do
        line=${line%%#*}
        line=${line//[[:space:]]/}
        [ -z "$line" ] && continue
        if [ "$line" = all ]; then all=1; else selected[$line]=1; fi
    done < "$config"
}

sync_entry() {
    local path=$1 name=${1##*/} pkg
    is_browser "$path" || return 0
    if ! pkg=$(pacman -Qqo "$path" 2>/dev/null); then
        echo "[browsers] skipped entry=$name reason=unowned" >&2
        return 0
    fi
    scan_package "$pkg"
    case ${kind[$pkg]} in
        error)
            echo "[browsers] refused entry=$name browser=$pkg reason=package_files" >&2
            status=1
            return ;;
        other)
            echo "[browsers] skipped entry=$name browser=$pkg reason=not_chromium" >&2
            return ;;
    esac
    if [ "$all" -eq 1 ] || [ -n "${selected[$pkg]:-}" ]; then
        if ! grep -v "$wrapped" "$path" | grep -q '^Exec='; then
            echo "[browsers] up_to_date entry=$name browser=$pkg" >&2
        elif rewrite "$path" "/$wrapped/!s|^Exec=|Exec=$launch |"; then
            echo "[browsers] routed entry=$name browser=$pkg" >&2
        fi
    elif ! grep -q "$wrapped" "$path"; then
        echo "[browsers] skipped entry=$name browser=$pkg reason=not_selected" >&2
    elif rewrite "$path" "s|$wrapped|Exec=|"; then
        echo "[browsers] restored entry=$name browser=$pkg reason=not_selected" >&2
    fi
}

list() {
    local f pkg
    declare -A names=()
    for f in "$apps"/*.desktop; do
        [ -f "$f" ] && is_browser "$f" && pkg=$(pacman -Qqo "$f" 2>/dev/null) || continue
        scan_package "$pkg"
        if [ "${kind[$pkg]}" = error ]; then
            echo "[browsers] refused entry=${f##*/} browser=$pkg reason=package_files" >&2
            status=1
        elif [ "${kind[$pkg]}" = chromium ] && [ -z "${names[$pkg]:-}" ]; then
            names[$pkg]=$(main_key "$f" Name)
        fi
    done
    for pkg in "${!names[@]}"; do
        printf '%s\t%s\n' "$pkg" "${names[$pkg]}"
    done | sort
}

pick() {
    local tok i
    local -a toks
    local -A want=()
    IFS=, read -ra toks <<< "$1"
    for tok in "${toks[@]}"; do
        tok=${tok//[[:space:]]/}
        [ -z "$tok" ] && continue
        if [[ ! $tok =~ ^[0-9]+$ ]]; then
            echo "[browsers] invalid answer=$tok reason=not_a_number max=${#keys[@]}" >&2
            return 1
        fi
        if [ ${#tok} -gt 4 ] || [ $((10#$tok)) -lt 1 ] || [ $((10#$tok)) -gt ${#keys[@]} ]; then
            echo "[browsers] invalid answer=$tok reason=out_of_range max=${#keys[@]}" >&2
            return 1
        fi
        want[$((10#$tok))]=1
    done
    if [ ${#want[@]} -eq 0 ]; then
        echo "[browsers] invalid answer=(none) reason=empty max=${#keys[@]}" >&2
        return 1
    fi
    for i in "${!keys[@]}"; do
        [ -n "${want[$((i + 1))]:-}" ] && echo "${keys[i]}"
    done
    return 0
}

choose() {
    local i answer attempt
    local -a rows keys=()
    mapfile -t rows < <(list)
    if [ ${#rows[@]} -eq 0 ]; then
        echo "[browsers] refused reason=no_browsers apps=$apps" >&2
        exit 1
    fi
    echo "Chromium-based browsers on this system:" >&2
    for i in "${!rows[@]}"; do
        keys[i]=${rows[i]%%$'\t'*}
        printf '  %d) %s (%s)\n' $((i + 1)) "${rows[i]#*$'\t'}" "${keys[i]}" >&2
    done
    for attempt in 1 2 3; do
        printf 'Route which browsers through wheelproxy? Numbers separated by commas, attempt %d of 3: ' "$attempt" >&2
        if ! IFS= read -r answer; then
            echo >&2
            echo "[browsers] refused reason=no_answer" >&2
            exit 2
        fi
        pick "$answer" && exit 0
    done
    echo "[browsers] refused reason=no_valid_answer attempts=3" >&2
    exit 2
}

case "${1:-}" in
    list)
        list ;;
    choose)
        choose ;;
    apply)
        load_config
        for f in "$apps"/*.desktop; do
            [ -f "$f" ] && sync_entry "$f"
        done ;;
    hook)
        load_config
        while IFS= read -r target; do
            f=$apps/${target#usr/share/applications/}
            [ -f "$f" ] && sync_entry "$f"
        done ;;
    revert)
        for f in "$apps"/*.desktop; do
            [ -f "$f" ] && grep -q "$wrapped" "$f" || continue
            rewrite "$f" "s|$wrapped|Exec=|" && echo "[browsers] restored entry=${f##*/} reason=revert" >&2
        done ;;
    *)
        echo "[browsers] refused reason=unknown_command command=${1:-(none)} usage=list|choose|apply|hook|revert" >&2
        exit 2 ;;
esac
exit $status
