PREFIX ?= /usr
DESTDIR ?=
PKGNAME = audi-converter
VERSION = 2.0.0

PYSITE = $(shell python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')

.PHONY: all install uninstall dist rpm clean run

all:
	@echo "Targets: install, uninstall, dist, rpm, run, clean"

run:
	python3 audi_converter.py

install:
	install -d $(DESTDIR)$(PYSITE)
	install -m 0644 audi_converter.py $(DESTDIR)$(PYSITE)/audi_converter.py
	install -d $(DESTDIR)$(PREFIX)/bin
	install -m 0755 bin/audi-converter $(DESTDIR)$(PREFIX)/bin/audi-converter
	install -d $(DESTDIR)$(PREFIX)/share/applications
	install -m 0644 audi-converter.desktop \
		$(DESTDIR)$(PREFIX)/share/applications/audi-converter.desktop

uninstall:
	rm -f $(DESTDIR)$(PYSITE)/audi_converter.py
	rm -f $(DESTDIR)$(PREFIX)/bin/audi-converter
	rm -f $(DESTDIR)$(PREFIX)/share/applications/audi-converter.desktop

dist:
	rm -rf dist $(PKGNAME)-$(VERSION)
	mkdir -p $(PKGNAME)-$(VERSION)/bin
	cp audi_converter.py audi-converter.desktop Makefile audi-converter.spec \
		$(PKGNAME)-$(VERSION)/
	cp bin/audi-converter $(PKGNAME)-$(VERSION)/bin/
	mkdir -p dist
	tar czf dist/$(PKGNAME)-$(VERSION).tar.gz $(PKGNAME)-$(VERSION)
	rm -rf $(PKGNAME)-$(VERSION)

rpm: dist
	mkdir -p $(HOME)/rpmbuild/SOURCES
	cp dist/$(PKGNAME)-$(VERSION).tar.gz $(HOME)/rpmbuild/SOURCES/
	rpmbuild -ba audi-converter.spec

clean:
	rm -rf dist $(PKGNAME)-$(VERSION) __pycache__
