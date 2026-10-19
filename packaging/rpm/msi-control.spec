# RPM spec for MSI Linux Control.
#
# The Makefile's `rpm` target substitutes @VERSION@ here from VERSION in
# src/msi_control.py, so that file stays the single source of the version
# (the same trick the vendored dkms.conf's literal @VERSION@ placeholder
# already plays, which install.sh rewrites).  Build with `make rpm`; never
# build this spec by rpmbuild copy without that substitution, or the
# package you get is "msi-control-@VERSION@" and exactly nothing about the
# program is wrong, which is the worst possible outcome.
#
# Expected source name: msi-control-<version>.tar.gz, the tarball `make
# release` produces.  The main package and both subpackages are noarch:
# every program is a script, every register is data.
#
# Path decisions, before you "fix" them:
#   * /usr, never /usr/local.  RPM owns /usr; install.sh keeps /usr/local
#     for a root-run checkout.  Both CLI and GUI wrappers already try
#     /usr/lib/msi-control as a fallback, so no source change was needed.
#   * The systemd unit and the sleep hook are sed'd out of the shipped
#     copy below because they hard-code /usr/local, which is right for
#     install.sh and wrong here.
#   * The EC driver ships as an msi-ec-dkms subpackage only (source +
#     DKMS hooks).  RPM cannot prebuild a kernel module for every kernel;
#     DKMS builds it against the running one at install time, the same
#     contract install.sh implements by hand.  Its %post is deliberately
#     non-fatal: a machine without kernel headers comes back on a later
#     boot (DKMS), not with a failed dnf transaction.
#
# A machine where the daemon was installed with install.sh already owns
# /etc/systemd/system/msi-controld.service, which shadows this package's
# unit under /usr/lib.  The documented flow is ./uninstall.sh first; see
# README.md, "Install from a package".

%define driver_ver 0.13
# driver_ver matches DRIVER_VER in install.sh: they install the same
# vendored source into the same /usr/src/msi-ec-<version> directory DKMS
# reads, so let them drift and one of the two upgrades breaks the other.
%define driver_name msi-ec

Name:           msi-control
# Version is real (the make target substitutes it).  The Release field is
# the package's own counter for successor builds of one upstream version;
# bump it if you rebuild a tarball without moving the program's VERSION.
Version:        @VERSION@
Release:        1%{?dist}
Summary:        Linux control centre for MSI laptops (battery, fans, performance modes)
License:        GPL-2.0-or-later
# No URL yet: the repository is not public.  Add it here (and in README.md)
# in the same change that publishes it, so a package never points users at
# a place that does not exist.
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

# The %systemd_* scripts below expand out of these macros; without this the
# build dies with "Failed to expand" on a minimal build root.
BuildRequires:  systemd-rpm-macros

Requires:       python3
Requires:       systemd

%description
A Linux control centre for MSI laptops: the things MSI's Dragon Center does
on Windows, done on Linux without it.  Battery charge thresholds,
performance (shift) modes, fan modes and Cooler Boost, custom fan curves,
named profiles, a graphics-mode (MUX) switch, and live temperature and
fan-RPM monitoring.

The daemon (msi-controld) runs as root and is the ONLY writer to the
embedded controller: it validates every value, serialises writes behind a
lock, and reads every byte back, so it can never report a write that did
not stick.  The CLI (msi-control) and the GUI (msi-control-gui, in the
msi-control-gui subpackage) talk to it over a unix socket and degrade to
read-only when it is down.

Laptop-specific EC registers live in a model registry shipped under
/usr/lib/msi-control/models.  A laptop with no model file still controls
everything the msi-ec kernel driver exposes by itself; it only refuses
what would require guessed registers (fan curves, RPM, the MUX switch).

Run `./uninstall.sh` first if the same programs were installed from a
checkout with install.sh, or the manually installed unit under
/etc/systemd/system will shadow this one.

# ============================================================ driver (dkms) ==
%package        driver-dkms
Summary:        DKMS source and build hooks for the msi-ec embedded-controller driver
Requires:       dkms
Requires:       make
Requires:       gcc
Requires:       /usr/lib/modules

%description    driver-dkms
The out-of-tree msi-ec driver (%{driver_name} %{driver_ver}, vendored from
the teackot/msi-ec tree) that exposes MSI's embedded controller through
sysfs: performance and fan modes, Cooler Boost, temperatures, the charge
threshold, and the debug interface the model registry work needs
(debug=1, set by msi-control's shipped modprobe.d config).

DKMS builds the module against the running kernel at install time and on
every kernel update it is present for.  The module will not load with
Secure Boot enabled; disable it in firmware setup or sign the module
yourself.

%post           driver-dkms
# Non-fatal on purpose: DKMS refuses when kernel-devel for the *running*
# kernel is not installed yet, and a python3-pyside6-less GUI is fine
# while a failed transaction on a shared library is not.  DKMS retries
# the build later (on boot), so exiting 0 can only defer, never break.
if [ ! -d /lib/modules/$(uname -r)/build ]; then
    echo "msi-ec: kernel headers for $(uname -r) are missing;" \
         "install kernel-devel-$(uname -r) and DKMS will build and" \
         "load the driver on the next boot"
    exit 0
fi
dkms add  -m %{driver_name} -v %{driver_ver} 2>/dev/null || true
if ! dkms build -m %{driver_name} -v %{driver_ver} -k "$(uname -r)"; then
    echo "msi-ec: DKMS build failed; the module will be missing until" \
         "dkms build -m %{driver_name} -v %{driver_ver} succeeds" >&2
    exit 0
fi
dkms install -m %{driver_name} -v %{driver_ver} -k "$(uname -r)" || true

%postun         driver-dkms
# Two cases: upgrade (keep the module, rebuild stays possible) and erase
# (remove it).  $1 is >= 1 for the upgrade case.
if [ "$1" -ge 1 ] && [ -r /usr/src/%{driver_name}-%{driver_ver}/dkms.conf ]; then
    # upgrade: source returned, re-register if DKMS lost it
    dkms add -m %{driver_name} -v %{driver_ver} 2>/dev/null || true
else
    dkms remove -m %{driver_name} -v %{driver_ver} --all 2>/dev/null || true
fi

# Every comment that sits between a scriptlet body and the next
# %-section becomes part of that scriptlet, and every comment inside
# %changelog becomes part of the newest entry's text, so the gui helper
# comment lives here, above what would have swallowed it.
%package        gui
Summary:        PySide6 GUI for msi-control
Requires:       %{name} = %{version}-%{release}
Requires:       python3-pyside6

%description    gui
The graphical front end of msi-control, built with PySide6: six sections
(dashboard, performance, fans with a curve editor, battery, profiles,
settings), a system-tray icon with the live temperature, and the same
model registry awareness as the CLI.  It talks to the daemon over the
same unix socket and never writes to the EC itself.

# ====================================================================== ==
%prep
%autosetup -n %{name}-%{version}

%build
# Nothing to build: everything is a script or data.  What *is* done
# here are the two path rewrites install.sh does not need because it
# installs under /usr/local and this package under /usr.
sed 's|/usr/local/lib/msi-control|/usr/lib/msi-control|g; s|/usr/local/share/doc|/usr/share/doc|g' \
    systemd/msi-controld.service > msi-controld.service
sed 's|/usr/local/bin/msi-control|/usr/bin/msi-control|g' \
    systemd/msi-control-sleep > msi-control-sleep

%install
# ---- programs -------------------------------------------------------------
install -d -m 755 %{buildroot}%{_bindir}
install -m 755 src/msi-control      %{buildroot}%{_bindir}/msi-control
install -m 755 src/msi-control-gui  %{buildroot}%{_bindir}/msi-control-gui

install -d -m 755 %{buildroot}%{_prefix}/lib/%{name}
install -m 644 src/msi_control.py  %{buildroot}%{_prefix}/lib/%{name}/msi_control.py
install -m 644 src/msi_controld.py %{buildroot}%{_prefix}/lib/%{name}/msi_controld.py

# The model registry ships as a tree: a model directory may carry a README
# beside its model.json, and unknown files there would be silently
# dropped by an explicit list.
cp -a models %{buildroot}%{_prefix}/lib/%{name}/models

# ---- systemd ---------------------------------------------------------------
install -d -m 755 %{buildroot}%{_unitdir}
install -m 644 msi-controld.service %{buildroot}%{_unitdir}/msi-controld.service

install -d -m 755 %{buildroot}%{_prefix}/lib/systemd/system-sleep
install -m 755 msi-control-sleep \
    %{buildroot}%{_prefix}/lib/systemd/system-sleep/msi-control

# ---- config ----------------------------------------------------------------
install -d -m 755 %{buildroot}%{_sysconfdir}/%{name}
install -m 644 etc/config.json \
    %{buildroot}%{_sysconfdir}/%{name}/config.json

install -d -m 755 %{buildroot}%{_sysconfdir}/modprobe.d \
                  %{buildroot}%{_sysconfdir}/modules-load.d
install -m 644 modprobe/msi-ec.conf \
    %{buildroot}%{_sysconfdir}/modprobe.d/%{driver_name}.conf
echo "%{driver_name}" > \
    %{buildroot}%{_sysconfdir}/modules-load.d/%{driver_name}.conf

# ---- desktop ---------------------------------------------------------------
install -d -m 755 %{buildroot}%{_datadir}/applications \
                  %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
install -m 644 desktop/msi-control.desktop %{buildroot}%{_datadir}/applications/
install -m 644 icons/msi-control.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg

# ---- driver source for the dkms subpackage ---------------------------------
install -d -m 755 %{buildroot}%{_usrsrc}/%{driver_name}-%{driver_ver}
cp -a third_party/%{driver_name}/. \
    %{buildroot}%{_usrsrc}/%{driver_name}-%{driver_ver}/
# dkms.conf ships the literal placeholder install.sh also rewrites.
sed -i "s|@VERSION@|%{driver_ver}|g" \
    %{buildroot}%{_usrsrc}/%{driver_name}-%{driver_ver}/dkms.conf

# ---- docs (licence with its package) ---------------------------------------
install -d -m 755 %{buildroot}%{_docdir}/%{name}
install -m 644 README.md CHANGELOG.md %{buildroot}%{_docdir}/%{name}/

%files
%license LICENSE
%{_docdir}/%{name}/README.md
%{_docdir}/%{name}/CHANGELOG.md
%{_bindir}/msi-control
%dir %{_prefix}/lib/%{name}
%{_prefix}/lib/%{name}/msi_control.py
%{_prefix}/lib/%{name}/msi_controld.py
%{_prefix}/lib/%{name}/models
%{_unitdir}/msi-controld.service
%{_prefix}/lib/systemd/system-sleep/msi-control
%{_sysconfdir}/modprobe.d/%{driver_name}.conf
%{_sysconfdir}/modules-load.d/%{driver_name}.conf
%{_datadir}/applications/msi-control.desktop
%{_datadir}/icons/hicolor/scalable/apps/msi-control.svg
%config(noreplace) %{_sysconfdir}/%{name}/config.json

%files gui
%{_bindir}/msi-control-gui

%files driver-dkms
%{_usrsrc}/%{driver_name}-%{driver_ver}

# ====================================================================== ==
%post
%systemd_post msi-controld.service
if [ -x /usr/bin/gtk-update-icon-cache ]; then
    gtk-update-icon-cache -f -t %{_datadir}/icons/hicolor >/dev/null 2>&1 || true
fi

%preun
%systemd_preun msi-controld.service

%postun
%systemd_postun msi-controld.service
if [ -x /usr/bin/gtk-update-icon-cache ]; then
    gtk-update-icon-cache -f -t %{_datadir}/icons/hicolor >/dev/null 2>&1 || true
fi

# The changelog below is maintained by release tooling and by hand; the
# program's user-facing history is CHANGELOG.md.  One entry per VERSION
# bump; rebuilds of the same VERSION bump the Release field instead.
%changelog
* Mon Oct 19 2026 the MSI Linux Control maintainers - @VERSION@-1
- First public release: daemon, CLI, GUI, model registry, msi-ec DKMS driver.

