# wheelproxy

Responsive, smooth mouse-wheel scrolling for Chromium-based browsers on Wayland.

Chromium animates every mouse-wheel notch with a fixed ease-in-out curve that lasts about 200 ms and starts
slowly: about 1.4 % of the distance is covered after one 60 Hz frame and 13 % after 50 ms. Scrolling feels
sluggish next to Firefox, especially on high refresh rate screens. wheelproxy is a small Wayland proxy that
sits between the browser and the compositor. It turns each wheel notch into a stream of small
high-resolution scroll steps that front-load the distance and then glide out. Chromium's own smooth scrolling
is switched off, so each step applies on the next frame.

No browser patching or rebuild is needed, and it does not use `LD_PRELOAD`. The browser runs unchanged and
connects to the proxy's socket instead of the compositor's.

## How it works

- The browser connects to `$XDG_RUNTIME_DIR/wayland-smooth`. The proxy opens a matching connection to the
  real compositor and forwards both directions byte for byte, file descriptors included.
- It tracks `wl_seat` bindings and `wl_pointer` objects. When the compositor sends a vertical
  `wl_pointer.axis_value120` wheel event, the proxy removes it and the matching `wl_pointer.axis` event from
  that frame.
- The notch, scaled by `--mult`, is added to a pending distance. Every millisecond the proxy moves a fixed
  fraction of what is left (time constant `--tau-ms`). It aims a few pixels past the target, so the motion
  is still slowing down when it lands instead of creeping at the end. Whole pixels are sent on as complete
  pointer frames: `axis_source`, `axis_value120`, `axis`, `frame`. Stepping much faster than the display
  refresh gives every frame an even share of the motion.
- Reversing the wheel direction drops whatever is still pending, and so does the pointer leaving the window.
- Touchpad scrolling (finger and continuous sources), horizontal wheel scrolling and all other traffic pass
  through unchanged.

Why this works on the browser side, for anyone curious (Chromium source, `ui/` and `cc/`):

- `ui/ozone/platform/wayland/host/wayland_pointer.cc` converts `axis_value120` one to one into pixels (120
  per notch). `wayland_event_source.cc` builds a wheel event from it without the precise-delta flag, so Blink
  animates it.
- `cc/animation/scroll_offset_animation_curve_factory.cc` uses the `kInverseDelta` duration for mouse
  wheels, which is 12 frames at 60 Hz (200 ms) for any step up to 120 px, with an ease-in-out curve.
- With `--disable-smooth-scrolling`, every wheel event applies directly. Feeding the browser many small
  steps then gives a curve the proxy controls.

## Status

Tested on KDE Plasma 6 (KWin, Wayland) with Google Chrome 155 and Chromium 154 on Arch Linux.

Other Chromium-based browsers (Brave, Vivaldi, Microsoft Edge, Opera and others) share Chromium's Wayland
input code and should behave the same, but they have not been tested. Other compositors should work if they
send `wl_pointer.axis_value120` (Wayland 1.21 and later), but only KWin has been tested.

## Requirements

- A Wayland session whose compositor supports `wl_pointer` version 8.
- The browser running as a native Wayland client. Google Chrome 155 and Chromium 154 did this by default in
  testing. If yours runs under XWayland, add `--ozone-platform=wayland`.
- A C compiler, `make`, and Python 3 for the tests and the launcher's proxy check.

## Install on Arch Linux

```sh
cd packaging/wheelproxy-git
makepkg -si
```

Before building, it lists the Chromium-based browsers installed on this system and asks which ones to route
through the proxy. Answer with their numbers separated by commas, for example `1,3`. The package installs the
proxy and launcher in `/usr/bin`, a user service enabled for every user, and a pacman hook.

To route every Chromium-based browser without being asked, including ones installed later, build
`packaging/wheelproxy-all-git` instead. The two packages conflict, so only one can be installed.

The choice is kept in `/etc/wheelproxy/browsers`, one pacman package name per line, or `all`. Edit it and run
`sudo wheelproxy-browsers apply` to change it later.

The hook runs whenever pacman installs or updates a desktop entry. If the entry belongs to a web browser whose
package contains a Chromium build, and that browser is selected, every `Exec=` line in the entry is prefixed
with `/usr/bin/wheelproxy-launch`. A browser counts as Chromium-based when its desktop entry declares it a web
browser and one of its package's executables knows Chromium's `disable-smooth-scrolling` switch, so Firefox
and Electron apps are left alone. Package updates replace the entry and the hook routes it again. Removing the
package restores the entries. Because the entries are edited in place, `pacman -Qkk` reports each routed
entry as an altered file of its browser package.

Start the proxy once with `systemctl --user start wheelproxy.service` (it starts by itself from the next
login), then quit the browser and open it from the application menu.

Remove an older `make install` before installing the package: it puts a copy of the browser's menu entry in
`~/.local/share/applications` that hides the system entry, and a service of the same name in
`~/.config/systemd/user` that takes precedence over the packaged one.

## Install for Google Chrome

```sh
./install.sh
```

It builds and runs the tests, installs the files and menu entries, enables the user service and starts
it. To update, pull and run it again: it restarts the service on the new build and checks that the new
process came up. A restart closes every browser window connected through the proxy, so while browsers are
connected it installs the files but leaves the running proxy alone and exits with status 3. Quit them and
run it again, or pass `--restart-now`. `ENTRIES` and `PREFIX` work as with make.

Then quit the browser completely and start it again from the application menu.

`make install` puts these files in place:

| file | purpose |
|------|---------|
| `~/.local/bin/wheelproxy` | the proxy |
| `~/.local/bin/wheelproxy-launch` | starts a browser through the proxy with `--disable-smooth-scrolling` |
| `~/.local/bin/wheelproxy-desktop` | writes menu entry overrides that start the browser through the launcher |
| `~/.config/systemd/user/wheelproxy.service` | runs the proxy for your graphical session |

`make install-desktop` also writes `~/.local/share/applications/google-chrome.desktop`. That is a copy of the
system entry with every `Exec=` line prefixed by `wheelproxy-launch`. It also adds an autostart entry that
runs `wheelproxy-desktop` at each login, so the copy is regenerated whenever a browser update changes the
original. A short notification appears when that happens.

If the proxy is not running, `wheelproxy-launch` starts the browser directly with its normal settings, so
the browser always opens.

## Other Chromium-based browsers

Any browser whose launcher accepts Chromium's command-line flags can be routed the same way. Only Google
Chrome and Chromium have been tested.

1. Try it once from a terminal, with the service running:

   ```sh
   wheelproxy-launch brave --new-window
   ```

   Use your browser's command, for example `chromium`, `brave`, `vivaldi-stable`, `microsoft-edge-stable`
   or `opera`. Quit any running instance first: a browser that is already running only opens a new window in
   the existing process, which is still connected directly. The launcher prints `[launcher] via=proxy` when
   the proxy is used.

2. To make it permanent, find the browser's desktop entry name:

   ```sh
   ls /usr/share/applications | grep -i brave
   ```

3. Install the override and the login check for every browser you use, separated by spaces:

   ```sh
   make install-desktop ENTRIES="google-chrome.desktop chromium.desktop brave-browser.desktop"
   ```

Entry names vary between distributions and packages, so use the names step 2 printed.

Flatpak and Snap builds are untested. The launcher passes `--disable-smooth-scrolling` to the first command
it is given, which for `flatpak run` is not the browser, and the sandbox may not expose the proxy socket.

## Tuning

The defaults are `--mult 1.65 --tau-ms 70`: about 198 px per notch, half of it covered in roughly 50 ms,
90 % in about 150 ms, and done after about 275 ms. To change them, edit `ExecStart=` in `~/.config/systemd/user/wheelproxy.service`.

| flag | default | meaning |
|------|---------|---------|
| `--listen` | `wayland-smooth` | socket name in `$XDG_RUNTIME_DIR`, or an absolute path |
| `--upstream` | `$WAYLAND_DISPLAY` | the compositor socket |
| `--mult` | `1.65` | distance multiplier, where one notch is 120 px at 1.0 |
| `--tau-ms` | `70` | decay time constant: lower is snappier, higher glides longer |
| `--tick-ms` | `1` | interval between steps while a scroll is running |
| `--verbose` | off | log every notch and the end of every scroll |

Then apply them:

```sh
systemctl --user daemon-reload
systemctl --user restart wheelproxy.service
```

Restarting the proxy closes every browser window connected through it, so quit the browser first.

## Troubleshooting

- `journalctl --user -u wheelproxy` shows connections opening and closing, with byte counts, queue sizes,
  age and the reason and errno for each close. A browser the proxy could not serve shows up as a
  `[conn] refused` line with the reason and errno.
- When the compositor is not accepting new connections, the proxy logs a `[conn] retry` line for each
  attempt and keeps trying for about 2 seconds, then refuses with `reason=upstream_connect errno=EAGAIN
  retries=11`. Browsers already connected are not held up while it waits.
- `systemctl --user kill -s USR1 wheelproxy.service` prints every open connection and tracked pointer as
  `[state]` lines without interrupting anything.
- With `--verbose` it also logs each seat and pointer it tracks, each notch, e.g.
  `[wheel] pointer=44 v120=120 remaining=198.0`, and the end of each scroll, e.g.
  `[wheel] settled pointer=44 steps=52 ms=208.0`. No `[wheel]` lines while you scroll means the browser is
  not connected through the proxy.
- To check a running browser, read its environment: `tr '\0' '\n' < /proc/<pid>/environ | grep WAYLAND`
  should show `WAYLAND_DISPLAY=wayland-smooth`.

## Uninstall

```sh
systemctl --user disable --now wheelproxy.service
make uninstall
rm ~/.local/share/applications/google-chrome.desktop
```

Remove the override of every other entry you installed too.

## Limitations

- If the proxy stops, every browser connected through it loses its display connection and closes.
- `--disable-smooth-scrolling` also turns off the animation for keyboard scrolling such as arrow keys and
  Page Down.
- Only vertical wheel scrolling is smoothed. Horizontal wheel and tilt-wheel scrolling pass through as-is.
- Mice in a high-resolution wheel mode send many small events per notch. Each one is added to the pending
  distance like a notch. This mode has not been tested.

## License

MIT, see [LICENSE](LICENSE).
