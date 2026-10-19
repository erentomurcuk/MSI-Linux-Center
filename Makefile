# MSI Linux Control: convenience targets.
#
# The real work is three commands (test, smoke, verify); this file just names
# them so nobody has to remember the QT_QPA_PLATFORM incantation.  Nothing
# here needs root except install/uninstall/verify, which say so.

PY      ?= python3
SRC      = src
TESTS    = tests
SHOTS    = $(CURDIR)/$(TESTS)/shots

# The version lives in src/msi_control.py and nowhere else: the CLI prints
# it with --version, the GUI puts it in its title bar, the rpm spec reads
# it out of the tarball.  Pull it straight out of the source so a release
# can never carry a number the program does not say about itself.
VERSION  = $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' $(SRC)/msi_control.py)

DIST     = dist
TARBALL  = $(DIST)/msi-control-$(VERSION).tar.gz

.DEFAULT_GOAL := help

.PHONY: help test smoke shots shots-light lint compile check verify install uninstall clean release tarball rpm

help:                ## Show this help
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) \
		| awk -F'##' '{ split($$1, a, ":"); \
			printf "  \033[1m%-11s\033[0m %s\n", a[1], $$2 }'

test:                ## Offline suite (fake sysfs + RAM-backed EC, no root)
	$(PY) $(TESTS)/test_msi_control.py

smoke:               ## Headless GUI build + screenshots (needs PySide6)
	QT_QPA_PLATFORM=offscreen $(PY) $(TESTS)/gui_smoke.py $(SHOTS)

shots: smoke         ## Alias for `smoke`

shots-light:         ## Headless GUI build + light-theme screenshots
	MSI_THEME=light QT_QPA_PLATFORM=offscreen $(PY) $(TESTS)/gui_smoke.py \
		$(SHOTS)/light

lint:                ## pyflakes over src/ and tests/ (if installed)
	-$(PY) -m pyflakes $(SRC) $(TESTS)

compile:             ## Byte-compile every source file
	$(PY) -m py_compile $(SRC)/msi_control.py $(SRC)/msi_controld.py \
		$(SRC)/msi-control $(SRC)/msi-control-gui $(TESTS)/test_msi_control.py \
		$(TESTS)/gui_smoke.py
	@bash -n install.sh uninstall.sh verify.sh
	@echo "syntax OK"

check: compile test smoke  ## Everything that runs without root or hardware

verify:              ## Live hardware checks: root, safe + reversible
	sudo ./verify.sh

install:             ## Install driver + programs (root, re-execs via pkexec)
	./install.sh

uninstall:           ## Remove everything (--purge also drops profiles+config)
	./uninstall.sh

# --- release ----------------------------------------------------------------
#
# make release: the tar.gz and the rpm, both named for the version above.
# Everything lands in dist/, which .gitignore already covers, so a build
# can never be committed: the version is in src/msi_control.py, and these
# artefacts must never be the thing anyone diffs against the source.

tarball:             ## Build dist/msi-control-<version>.tar.gz
	@mkdir -p $(DIST)
	@test -n "$(VERSION)" || { echo "cannot read VERSION from $(SRC)/msi_control.py"; exit 1; }
	@echo "version: $(VERSION)"
	# Pack the WORKING TREE, not HEAD: a half-finished polish pass must
	# never go out as a "released" version while git archive happily
	# ships the last commit instead.
	#
	# Git decides what is ignorable, not tar: --exclude-vcs-ignores
	# reimplements a partial .gitignore reader (directory-only patterns
	# like `dist/` and `__pycache__/` slip through), so the ignored
	# paths (and nothing else) come out of `git ls-files --others
	# --ignored --directory` instead.  Anchoring each pattern with ./
	# keeps `dist` from also excluding a same-named file elsewhere in
	# the tree.
	$(eval IGNORED = $(shell git ls-files --others --ignored --exclude-standard --directory | sed 's:/*$$::'))
	tar -czf $(TARBALL) \
		--transform 's,^\.,msi-control-$(VERSION),x' \
		--exclude-vcs \
		$(foreach f,$(IGNORED),--exclude=./$(f)) \
		.
	@tar -tzf $(TARBALL) | wc -l | xargs -I{} echo "{} files packed"

rpm: tarball         ## Build dist/*.rpm from the tarball + the rpm spec
	@test -n "$(VERSION)" || { echo "cannot read VERSION from $(SRC)/msi_control.py"; exit 1; }
	rm -rf $(DIST)/rpmbuild
	@mkdir -p $(DIST)/rpmbuild/SOURCES $(DIST)/rpmbuild/SPECS
	cp $(TARBALL) $(DIST)/rpmbuild/SOURCES/
	@# The spec's @VERSION@ - and nothing else in it - comes from the
	@# program's own VERSION above.
	sed 's|@VERSION@|$(VERSION)|' packaging/rpm/msi-control.spec \
		> $(DIST)/rpmbuild/SPECS/msi-control.spec
	rpmbuild -ba \
		--define "_topdir $(abspath $(DIST)/rpmbuild)" \
		--define "_source_filedigest_algorithm sha256" \
		$(DIST)/rpmbuild/SPECS/msi-control.spec
	cp $(DIST)/rpmbuild/RPMS/noarch/*.rpm $(DIST)/
	cp $(DIST)/rpmbuild/SRPMS/*.src.rpm $(DIST)/
	@ls -l $(DIST)/

release: rpm          ## Build the tarball AND the rpms into dist/

clean:               ## Remove build/bytecode caches and regenerated shots
	find $(SRC) $(TESTS) tools -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf $(TESTS)/shots/_xdg_config $(TESTS)/shots/light/_xdg_config
	rm -rf $(DIST)
