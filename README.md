# wheelproxy

A small Wayland proxy that gives Chromium-based browsers smooth, responsive mouse-wheel scrolling on Linux.

Chrome animates each wheel notch with a 200 ms ease-in-out curve, so a page barely moves during the first
50 ms. wheelproxy sits between the browser and the compositor. It absorbs each vertical wheel notch and
replays it as a stream of small high-resolution wheel steps that front-load the distance and then decay
exponentially. Run the browser with its own smooth scrolling turned off so these steps apply directly.

Everything else on the connection, file descriptors included, passes through byte for byte. Touchpad
(finger and continuous) scrolling and horizontal wheel scrolling are not changed.

## Build

    make
    make test
    make install            # binary and launcher to ~/.local/bin, user service to ~/.config/systemd/user
    systemctl --user enable --now wheelproxy.service

`google-chrome-smooth` starts Chrome through the proxy with smooth scrolling off. If the proxy is not
answering, it starts Chrome directly instead. `make install` also writes a copy of Chrome's desktop entry to
`~/.local/share/applications` with its Exec lines pointing at the launcher. An autostart entry runs
`wheelproxy-refresh-desktop` at each login to regenerate that copy when a google-chrome update changes the
original.

## Run

    wheelproxy --listen wayland-smooth --upstream "$WAYLAND_DISPLAY" --mult 1.33 --tau-ms 45
    WAYLAND_DISPLAY=wayland-smooth google-chrome-stable --ozone-platform=wayland --disable-smooth-scrolling

| flag          | default            | meaning                                                   |
|---------------|--------------------|-----------------------------------------------------------|
| `--listen`    | `wayland-smooth`   | socket name in `$XDG_RUNTIME_DIR`, or an absolute path     |
| `--upstream`  | `$WAYLAND_DISPLAY` | the real compositor socket                                 |
| `--mult`      | `1`                | distance multiplier per notch (one notch is 120 px × the compositor's scroll factor) |
| `--tau-ms`    | `45`               | decay time constant: lower is snappier, higher is smoother |
| `--tick-ms`   | `4`                | step interval while a scroll is animating                  |
| `--verbose`   | off                | log each notch and when each animation settles             |

If the proxy stops, every client connected through it loses its display connection.

## Requirements

A compositor that supports `wl_pointer` version 8 or later (high-resolution wheel events). The browser
must bind `wl_seat` at version 8 or later. Chrome does.
