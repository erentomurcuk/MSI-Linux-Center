# MSI Linux Control — convenience targets.
#
# The real work is three commands (test, smoke, verify); this file just names
# them so nobody has to remember the QT_QPA_PLATFORM incantation.  Nothing
# here needs root except install/uninstall/verify, which say so.

PY      ?= python3
SRC      = src
TESTS    = tests
SHOTS    = $(CURDIR)/$(TESTS)/shots

.DEFAULT_GOAL := help

.PHONY: help test smoke shots shots-light lint compile check verify install uninstall clean

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

verify:              ## Live hardware checks — root, safe + reversible
	sudo ./verify.sh

install:             ## Install driver + programs (root, re-execs via pkexec)
	./install.sh

uninstall:           ## Remove everything (--purge also drops profiles+config)
	./uninstall.sh

clean:               ## Remove build/bytecode caches and regenerated shots
	find $(SRC) $(TESTS) tools -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf $(TESTS)/shots/_xdg_config $(TESTS)/shots/light/_xdg_config
