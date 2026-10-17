CFLAGS ?= -O2 -Wall -Wextra -Werror -std=c11
PREFIX ?= $(HOME)/.local
ENTRIES ?= google-chrome.desktop

build/wheelproxy: wheelproxy.c
	mkdir -p build
	$(CC) $(CFLAGS) -o $@ $< -lm

test: build/wheelproxy
	timeout 300 python3 -I -m unittest discover -s tests -v

install: build/wheelproxy
	install -Dm755 build/wheelproxy $(PREFIX)/bin/wheelproxy
	install -Dm755 contrib/wheelproxy-launch.sh $(PREFIX)/bin/wheelproxy-launch
	install -Dm755 contrib/wheelproxy-desktop.sh $(PREFIX)/bin/wheelproxy-desktop
	install -Dm644 contrib/wheelproxy.service $(HOME)/.config/systemd/user/wheelproxy.service

install-desktop: install
	mkdir -p $(HOME)/.config/autostart
	sed -e 's|@BINDIR@|$(PREFIX)/bin|' -e 's|@ENTRIES@|$(ENTRIES)|' contrib/wheelproxy-desktop.desktop \
		> $(HOME)/.config/autostart/wheelproxy-desktop.desktop
	WHEELPROXY_LAUNCH=$(PREFIX)/bin/wheelproxy-launch $(PREFIX)/bin/wheelproxy-desktop $(ENTRIES)

uninstall:
	rm -f $(PREFIX)/bin/wheelproxy $(PREFIX)/bin/wheelproxy-launch $(PREFIX)/bin/wheelproxy-desktop
	rm -f $(HOME)/.config/systemd/user/wheelproxy.service $(HOME)/.config/autostart/wheelproxy-desktop.desktop
	@echo "remove the overrides you created in $(HOME)/.local/share/applications to restore the stock menu entries"

clean:
	rm -rf build

.PHONY: test install install-desktop uninstall clean
