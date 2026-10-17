CFLAGS ?= -O2 -Wall -Wextra -Werror -std=c11
PREFIX ?= $(HOME)/.local

build/wheelproxy: wheelproxy.c
	mkdir -p build
	$(CC) $(CFLAGS) -o $@ $< -lm

test: build/wheelproxy
	timeout 300 python3 -I -m unittest discover -s tests -v

install: build/wheelproxy
	install -Dm755 build/wheelproxy $(PREFIX)/bin/wheelproxy
	install -Dm755 contrib/google-chrome-smooth.sh $(PREFIX)/bin/google-chrome-smooth
	install -Dm644 contrib/wheelproxy.service $(HOME)/.config/systemd/user/wheelproxy.service

clean:
	rm -rf build

.PHONY: test install clean
