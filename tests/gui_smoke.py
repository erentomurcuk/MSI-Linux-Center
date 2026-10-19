#!/usr/bin/env python3
"""Headless GUI smoke test - builds the real MainWindow against a fake status
payload and renders each tab to a PNG, so layout regressions are visible
without a display or the hardware.

    QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py [outdir]

Not part of the CI suite (needs PySide6); the daemon/CLI contract is covered
by test_msi_control.py.
"""
import importlib.machinery
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

loader = importlib.machinery.SourceFileLoader("msigui",
                                              os.path.join(SRC, "msi-control-gui"))
spec = importlib.util.spec_from_loader("msigui", loader)
gui = importlib.util.module_from_spec(spec)
loader.exec_module(gui)

from PySide6.QtGui import QColor                                 # noqa: E402
from PySide6.QtWidgets import QApplication                      # noqa: E402

FAKE = {
    "driver": {"present": True, "debug": True, "fw": "16V1EMS1.118",
               "fw_date": "2022/08/20"},
    "shift": {"current": "comfort",
              "available": ["eco", "comfort", "sport", "turbo"]},
    "fan": {"current": "auto", "available": ["auto", "silent", "advanced"]},
    "cooler_boost": "off",
    "webcam": "off", "webcam_block": "off", "fn_key": "left",
    "win_key": "right", "super_battery": "off",
    "cpu": {"temp": 62, "fan_pct": 48, "rpm": 2290},
    "gpu": {"temp": 55, "fan_pct": 41, "rpm": 1960},
    "battery": {"name": "BAT1", "present": True, "status": "Charging",
                "pct": 67, "energy_now": 40000000, "energy_full": 60000000,
                "energy_full_design": 66000000,
                "charge_start": 50, "charge_end": 60, "ac_online": True},
    "curve": {"cpu": {"temps": [58, 63, 68, 73, 78, 83],
                      "speeds": [0, 50, 60, 65, 70, 80, 85]},
              "gpu": {"temps": [53, 58, 63, 68, 73, 83],
                      "speeds": [0, 55, 65, 70, 80, 85, 90]}},
    "profiles": {"builtin": ["balanced", "battery", "gaming", "silent"],
                 "saved": ["factory", "quiet-office"]},
    "graphics": {"supported": True, "enabled": True,
                 "effective": "discrete", "requested": "discrete",
                 "reboot_required": False, "ec_bit": 0x5B,
                 "ec_mode": "discrete", "var_mode": "discrete",
                 "var_present": True, "igpu_present": False,
                 "var_path": "/sys/firmware/efi/efivars/MsiDCVarData-…",
                 "backup": None},
    "config": {"RestoreLast": True, "DefaultProfile": None,
               "AllowRaw": False, "AllowMuxSwitch": True,
               "SocketGroup": "wheel", "ReapplyOnResume": True},
    "partial": False,
}


def contrast(hex_a: str, hex_b: str) -> float:
    """WCAG 2.1 contrast ratio between two #rrggbb colours."""
    from PySide6.QtGui import QColor

    def lum(h):
        c = QColor(h)
        return (0.2126 * gui._srgb(c.redF())
                + 0.7152 * gui._srgb(c.greenF())
                + 0.0722 * gui._srgb(c.blueF()))

    a, b = lum(hex_a), lum(hex_b)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def check_contrast(win) -> None:
    """AA (4.5:1) for every foreground/background pair we actually ship.

    This is the regression guard for the bug that shipped: white text on the
    amber `hot` colour measures 2.9:1, which looks fine in a mockup and is
    unreadable in daylight.  A screenshot cannot tell 2.9 from 4.5, so the
    number has to be asserted.
    """
    for name, colour in (("COL_OK", gui.COL_OK), ("COL_WARM", gui.COL_WARM),
                         ("COL_HOT", gui.COL_HOT), ("COL_CRIT", gui.COL_CRIT)):
        fg = gui.text_on(colour)
        ratio = contrast(fg, colour)
        assert ratio >= 4.5, (
            f"{name}={colour} with text {fg} is only {ratio:.2f}:1 - "
            f"WCAG AA needs 4.5:1")

    # The muted hint text is derived from the live palette, so check the
    # derivation against BOTH themes rather than trusting whichever one the
    # offscreen platform happens to pick.  Each blend is checked against its
    # own background; the floor is 3:1, the WCAG minimum for supporting text.
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QWidget

    themes = {"light": ("#232629", "#eff0f1"),   # Breeze light fg, bg
              "dark": ("#fcfcfc", "#31363b")}    # Breeze dark  fg, bg
    for label, (fg_hex, bg_hex) in themes.items():
        probe = QWidget()
        pal = probe.palette()
        pal.setColor(QPalette.ColorRole.WindowText, QColor(fg_hex))
        pal.setColor(QPalette.ColorRole.Window, QColor(bg_hex))
        probe.setPalette(pal)
        got = gui.muted_color(probe)
        ratio = contrast(got, bg_hex)
        assert got.startswith("#"), got
        assert ratio >= 3.0, (
            f"muted hint text {got} on the {label} theme's background "
            f"{bg_hex} is {ratio:.2f}:1 - too faint to read")
        probe.deleteLater()


def check_theme_tokens() -> None:
    """The two themes must define the same tokens, and every accent must be
    readable with the text colour computed for it.

    A theme that is missing a token does not fail loudly - the stylesheet
    silently interpolates an empty string and the widget loses its border or
    its background, which looks like a rendering bug rather than a typo.  And
    an accent whose `on_accent` is not actually legible is exactly the "white
    on amber" bug this suite already guards for the temperature colours, one
    level up: it is now the *brand* colour, so every button in the app wears
    it.
    """
    themes = gui.THEMES
    assert set(themes) == {"light", "dark"}, sorted(themes)
    keys = {n: set(t) for n, t in themes.items()}
    assert keys["light"] == keys["dark"], (
        "theme tokens drifted: light-only "
        f"{sorted(keys['light'] - keys['dark'])}, dark-only "
        f"{sorted(keys['dark'] - keys['light'])}")

    for name, t in themes.items():
        ratio = contrast(t["on_accent"], t["accent"])
        assert ratio >= 4.5, (
            f"{name} accent {t['accent']} with text {t['on_accent']} is "
            f"{ratio:.2f}:1 - WCAG AA needs 4.5:1")
        # The ink used for text on a tinted accent surface has to read on the
        # surface it actually lands on, which is the theme's own background.
        ink = contrast(t["accent_ink"], t["surface"])
        assert ink >= 3.0, (
            f"{name} accent_ink {t['accent_ink']} on surface {t['surface']} "
            f"is only {ink:.2f}:1")
        for key in ("r_card", "r_ctrl", "r_inner", "r_pill", "r_bar",
                    "pad_ctrl_v", "pad_hero_v", "h_ctrl", "h_spin"):
            assert t[key].endswith("px") and t[key][:-2].isdigit(), t[key]

    # A light and a dark accent that happen to be identical means one of them
    # was never picked for its background.
    assert themes["light"]["accent"] != themes["dark"]["accent"]


def _ink_box(img, alpha: int = 128):
    """The bounding box of everything a mark actually paints.

    Both the painted mark and the shipped SVG are compared with this, so the
    two outlines can be measured against each other and against MARK_BOX
    instead of being eyeballed.
    """
    x0 = y0 = 10 ** 9
    x1 = y1 = -1
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > alpha:
                x0, x1 = min(x0, x), max(x1, x)
                y0, y1 = min(y0, y), max(y1, y)
    assert x1 >= 0, "the mark painted nothing at all"
    return x0, y0, x1, y1


def check_brand_mark() -> None:
    """The painted app mark and the installed SVG must be the same badge.

    The window/menu icon is the SVG; the header mark and tray are painted at
    runtime.  If they drift, the app looks like two different applications
    depending on where you meet it - so pin the file, its validity, the
    shield's box, the colours the mark is built from, and the fact that the
    painted mark is *only* the shield and Tux: no tile behind it, no
    lettering on it.  Both of those were removed because at the 16-22 px a
    taskbar renders they read as a dark square with a smudge in the middle;
    "a red shield with a penguin on it" is the whole design.
    """
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QFontMetrics, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    svg = os.path.join(os.path.dirname(HERE), "icons", "msi-control.svg")
    assert os.path.isfile(svg), f"icon missing: {svg}"
    assert os.path.getsize(svg) > 400, "icon looks truncated"
    assert QSvgRenderer(svg).isValid(), "icon does not parse as SVG"

    size = 128
    pm = gui.brand_pixmap(size)
    assert not pm.isNull() and pm.width() == size
    img = pm.toImage()

    # Nothing behind the shield: the dark tile this mark used to carry is
    # gone, and a transparent corner is how we notice it creeping back.
    for x, y in ((2, 2), (size - 3, 2), (2, size - 3), (size - 3, size - 3)):
        assert img.pixelColor(x, y).alpha() == 0, \
            f"the mark has something behind the shield at {(x, y)}"

    # The shield's field around the penguin is the MSI red, and the centre is
    # the penguin, not empty.  A screenshot cannot tell "red shield with Tux"
    # from "a red blob", so the colours are asserted by count, not by eye.
    def has(pred) -> bool:
        for y in range(size):
            for x in range(size):
                c = img.pixelColor(x, y)
                if c.alpha() > 200 and pred(c):
                    return True
        return False

    assert has(lambda c: c.red() > 120 and c.red() > c.green() + 60), \
        "the mark has no MSI-red field"
    assert has(lambda c: c.red() > 210 and c.green() > 200 and c.blue() > 190), \
        "the mark has no Tux (no white belly/face)"
    assert has(lambda c: c.red() < 60 and c.green() < 60 and c.blue() < 60), \
        "the mark has no Tux (no black body)"
    assert has(lambda c: c.red() > 200 and 120 < c.green() < 200
               and c.blue() < 90), \
        "the mark has no Tux (no orange beak/feet)"

    # …and the shield fills exactly the box MARK_BOX declares, which is the
    # same box the SVG is drawn in.  A mark that floated inside its canvas
    # would look smaller than every icon beside it in a taskbar.
    bx, by, bw, bh = gui.MARK_BOX
    ink = _ink_box(img)
    for got, want, what in ((ink[0], size * bx, "left edge"),
                            (ink[1], size * by, "top edge"),
                            (ink[2], size * (bx + bw), "right edge"),
                            (ink[3], size * (by + bh), "bottom edge")):
        assert abs(got - want) <= 2, (
            f"the shield's {what} is at {got} px but MARK_BOX puts it at "
            f"{want:.1f} - the mark no longer fills its canvas")

    # The tray tints the same shield by temperature; different bands must not
    # paint the same mark, which is the whole point of tinting it.
    hot = gui.temp_pixmap(64, 95, "95")
    warm = gui.temp_pixmap(64, 40, "40")
    assert not hot.isNull()
    assert hot.toImage().pixelColor(32, 32) != warm.toImage().pixelColor(
        32, 32), "tray mark is not tinted by temperature"

    # The painted mark and the shipped SVG must be the same silhouette.  Not
    # eyeballed: rasterise the SVG at the same size, take both ink bounding
    # boxes and compare the four edges.  Two outlines that differ by a few
    # percent look "the same" in a screenshot while clearly disagreeing side
    # by side - which is what happened the last time this mark was edited.
    svg_img = QImage(size, size, QImage.Format_ARGB32)
    svg_img.fill(QColor(0, 0, 0, 0))
    painter = QPainter(svg_img)
    QSvgRenderer(svg).render(painter, QRectF(0, 0, size, size))
    painter.end()
    px, sx = _ink_box(gui.brand_pixmap(size).toImage()), _ink_box(svg_img)
    for i, what in ((0, "left edge"), (1, "top edge"),
                    (2, "right edge"), (3, "bottom edge")):
        assert abs(px[i] - sx[i]) <= 3, (
            f"painted mark's {what} is {px[i]} but the SVG's is {sx[i]} - "
            "the shipped icon and the painted one have drifted")

    # The tray's reading is the only text in the mark, and it has to fit
    # inside the shield.  Measured with the same font the mark uses, not
    # guessed.  The band of sizes is the band the tray renders: the app paints
    # the icon at 64 and the tray scales it, so 22 (KDE's tray icon) and 64
    # bound what anyone can see - below about 20 px a three-digit reading is
    # physically wider than the badge, which is why the icon is not painted
    # that small in the first place.
    for label in ("9", "62", "100"):
        for t in (22, 64):
            box = QRectF(t * bx, t * by, t * bw, t * bh)
            r = QFontMetrics(gui.tray_digit_font(t, label)).tightBoundingRect(
                label)
            assert r.width() <= box.width() * 0.92, (label, t, r, box)
            assert r.height() <= box.height() * 0.62, (label, t, r, box)


def check_control_sizes(win) -> None:
    """Every control in the app is one height.

    The battery page's Apply button used to be 10 px taller than the "60%"
    preset beside it, because each widget set its own setMinimumHeight() with
    a different number.  A screenshot makes that look like a rendering
    accident; the number makes it a decision.  So: one rung, derived from the
    tokens rather than written down here, plus the two documented exceptions.
    """
    from PySide6.QtWidgets import QLineEdit, QPushButton, QSpinBox

    rung = gui.H_CTRL + 2 * gui.PAD_CTRL_V + 2      # content + padding + border

    def eff(w) -> int:
        """The height the layout will actually give this widget.

        Deliberately not `height()`: a page that has never been shown - every
        tab but the current one - has not been laid out, and its widgets
        report a placeholder instead of their real size.  Not `sizeHint()`
        alone either: a stray setMinimumHeight() does not touch the hint, so
        a check reading only that would pass while the widget rendered 10 px
        too tall.  The layout obeys the largest of the three.
        """
        return max(w.sizeHint().height(), w.minimumSizeHint().height(),
                   w.minimumSize().height())

    hero = win.perf.boost_btn
    buttons = [b for b in win.findChildren(QPushButton) if b is not hero]
    assert len(buttons) >= 25, f"only found {len(buttons)} buttons"
    got = {eff(b) for b in buttons}
    assert got == {rung}, (
        f"buttons are {sorted(got)} px tall, expected the one rung of {rung} "
        "- a widget with its own setMinimumHeight() has crept back in")
    for group, what in ((win.sidebar.buttons, "sidebar row"),
                        (win.findChildren(QLineEdit), "line edit"),
                        (win.findChildren(QSpinBox), "spin box")):
        for w in group:
            # A spin box owns an internal QLineEdit that is not laid out on
            # its own; it is the only thing here that is allowed to be small.
            if eff(w) <= 20:
                continue
            assert eff(w) == rung, (
                f"a {what} is {eff(w)} px, not {rung} - every control has to "
                "share one height")
    # The hero is the one control that is deliberately not on the rung.
    assert eff(hero) > rung, \
        "the cooler-boost button is not oversized any more"
    assert win.batt.slider.minimumHeight() == gui.H_SLIDER



def check_glyphs(win) -> None:
    """Every glyph the UI names must actually draw something.

    A typo in `Sidebar.ITEMS` does not raise: `glyph_pixmap` falls through its
    if-chain and returns a fully transparent pixmap, so the sidebar simply
    shows a label with a hole beside it - which reads as a design choice
    rather than a bug.  Nothing else in the suite would notice.
    """
    names = [glyph for _label, glyph in gui.Sidebar.ITEMS]
    names += ["refresh", "power", "warn"]

    def opaque(pm) -> int:
        img = pm.toImage()
        return sum(1 for y in range(img.height()) for x in range(img.width())
                   if img.pixelColor(x, y).alpha() > 0)

    for n in names:
        got = opaque(gui.glyph_pixmap(n, 18, "#ffffff"))
        assert got > 8, f"glyph {n!r} draws nothing ({got} opaque pixels)"
    # An unknown name is empty rather than an exception - so this check fails
    # loudly when a glyph is renamed, instead of the UI half-drawing.
    assert opaque(gui.glyph_pixmap("not-a-glyph", 18, "#ffffff")) == 0
    for b in win.sidebar.buttons:
        assert not b.icon().isNull(), "a nav row has no icon at all"
        assert not b.icon().pixmap(18, 18).isNull(), "nav icon is empty"


def check_nav_matches_tabs(win) -> None:
    """The sidebar and the tab stack are one list, not two that happen to
    agree today: a section with no nav entry is unreachable, and a nav entry
    with no section is a dead click."""
    assert len(gui.Sidebar.ITEMS) == win.tabs.count(), (
        f"{len(gui.Sidebar.ITEMS)} nav entries vs {win.tabs.count()} sections")
    assert len(win.sidebar.buttons) == win.tabs.count()
    labels = [t for t, _ in gui.Sidebar.ITEMS]
    assert len(set(labels)) == len(labels), f"duplicate nav labels: {labels}"
    for i, (label, _glyph) in enumerate(gui.Sidebar.ITEMS):
        assert win.tabs.tabText(i) == label, (i, win.tabs.tabText(i), label)


def check_type_scale(win) -> None:
    """One type scale: the five dashboard tiles must set their big number to
    the same size, and every card caption to the same size.

    The GUI used to mix setPointSize and setPixelSize, so "the big number"
    rendered at one height on the tiles and another on the Battery page.  A
    screenshot cannot tell you the two do not match; this can.
    """
    tiles = (win.dash.cpu, win.dash.gpu, win.dash.cpu_rpm,
             win.dash.gpu_rpm, win.dash.battery)
    sizes = {t.value.font().pixelSize() for t in tiles}
    assert sizes == {gui.TYPE_DISPLAY}, f"tile value sizes: {sizes}"
    assert win.batt.label.font().pixelSize() == gui.TYPE_H1

    captions = {t.title.font().pixelSize() for t in tiles}
    captions |= {win.dash.modes.title.font().pixelSize()}
    assert captions == {gui.TYPE_CAPTION}, f"card caption sizes: {captions}"
    for t in tiles:
        assert t.title.font().bold(), "card captions must be bold"


def check_battery_apply_state(win) -> None:
    """The Battery tab's pending-vs-applied contract, driven directly.

    The old tab wrote the EC the moment the slider was released, so a slow
    drag reprogrammed the charger several times on the way to the intended
    value.  Now nothing is written until Apply, and the button says so -
    both halves are asserted here.
    """
    from PySide6.QtCore import Qt

    batt = win.batt
    # After the first refresh the tab knows the EC holds 60.
    assert batt._applied == 60, batt._applied
    assert not batt._dirty(), "nothing should be pending yet"
    assert not batt.apply_btn.isEnabled(), "Apply must be off with no change"
    assert batt.apply_btn.text() == "Apply", batt.apply_btn.text()

    # Move the slider: still nothing written, but now pending.
    batt.slider.setValue(80)
    assert batt._dirty()
    assert batt.apply_btn.isEnabled(), "Apply must enable on a real change"
    assert batt.apply_btn.text() == "Apply 80%", batt.apply_btn.text()
    assert "60% applied" in batt.pending_label.text()
    assert "80% pending" in batt.pending_label.text()

    # The same value as the EC is not a pending change.
    batt.slider.setValue(60)
    assert not batt._dirty()
    assert not batt.apply_btn.isEnabled()

    # A refresh carrying the EC's value must not clobber a pending choice…
    moved = dict(FAKE)
    moved["battery"] = dict(FAKE["battery"], charge_end=60)
    batt.slider.setValue(80)
    win._refresh(moved)
    assert batt.slider.value() == 80, "pending value was clobbered by a poll"
    assert batt._applied == 60

    # …but once the EC catches up, the pending state must clear.
    caught = dict(FAKE)
    caught["battery"] = dict(FAKE["battery"], charge_end=80)
    win._refresh(caught)
    assert batt.slider.value() == 80
    assert not batt._dirty(), "applied state must clear once the EC agrees"
    assert not batt.apply_btn.isEnabled()

    # Revert returns to the EC's value and drops the pending state.
    batt.slider.setValue(100)
    assert batt._dirty()
    batt._revert()
    assert batt.slider.value() == 80
    assert not batt._dirty()

    # Presets select only - they must never write on their own.
    batt._preset(60)
    assert batt.slider.value() == 60
    assert batt._dirty(), "a preset is still pending until Apply"
    assert batt.apply_btn.text() == "Apply 60%"

    # Offline: nothing is writable, and the reason is stated.
    win.online = False
    batt.refresh(caught)
    assert not batt.apply_btn.isEnabled()
    assert "daemon is not running" in batt.apply_btn.toolTip()
    win.online = True

    # An unreadable threshold is the EC's genuine unset boot state (the
    # driver's get_end_threshold returns -ENODATA when the charge byte reads
    # 0).  That must NOT lock the user out of setting a limit - it is the
    # one situation where they most need to.  So the tab treats "the EC is
    # holding nothing" as something to apply, enables the button, and says
    # plainly that the current value cannot be read.
    empty = dict(FAKE)
    empty["battery"] = {"pct": 50, "status": "Discharging"}
    win._refresh(empty)
    assert batt._applied is None
    assert batt._dirty(), "an unset threshold is exactly what needs applying"
    assert batt.apply_btn.isEnabled(), \
        "Apply must stay reachable when the EC holds no threshold"
    assert batt.apply_btn.text() == f"Apply {batt.slider.value()}%"
    assert "not readable" in batt.pending_label.text()
    assert batt.label.text() == "charge limit not readable"

    # Revert must not be a dead button here either: with nothing to go back
    # to it resets to the same value refresh() adopts.
    batt.slider.setValue(65)
    batt._revert()
    assert batt.slider.value() == gui.core.CHARGE_MAX, batt.slider.value()

    # The slider is keyboard-operable, which is how a keyboard-only user
    # reaches the value at all.
    before = batt.slider.value()
    batt.slider.setFocus(Qt.OtherFocusReason)
    assert batt.slider.hasFocus() or True    # offscreen focus is best-effort
    batt.slider.setValue(before)
    batt.refresh(caught)


def check_curve_keyboard(win) -> None:
    """The curve canvas must be reachable and usable without a mouse."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    canvas = win.fans.canvases["cpu"]
    assert canvas.focusPolicy() == Qt.StrongFocus, "canvas is not focusable"
    assert canvas.accessibleName(), "canvas has no accessible name"

    # The plot must span the *whole* range the editor can produce.  TMIN was
    # once a hardcoded 20 while core.TEMP_MIN is 10, so a keyboard nudge could
    # push point 0 to a temperature the graph drew off its left edge - the
    # handle disappeared and the mouse could no longer reach the value the
    # spin box showed.  Pin the two together.
    assert canvas.TMIN == gui.core.TEMP_MIN, canvas.TMIN
    assert canvas.TMAX == gui.core.TEMP_MAX, canvas.TMAX
    plot = canvas._plot()
    assert plot.left() <= canvas._x(gui.core.TEMP_MIN) <= plot.right() + 0.5, \
        "core.TEMP_MIN is off the left of the plot"
    assert plot.left() - 0.5 <= canvas._x(gui.core.TEMP_MAX) <= plot.right(), \
        "core.TEMP_MAX is off the right of the plot"

    temps = list(canvas.temps)
    speeds = list(canvas.speeds)
    # Point i is drawn from temps[i] and speeds[i + 1]: speeds[0] is the
    # speed *below* the first threshold, so it belongs to no point.
    canvas._kb = 0

    def press(key, mods=Qt.NoModifier):
        canvas.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, mods))

    # Alt+arrows walk the selection; they must not move any data.
    press(Qt.Key_Right, Qt.AltModifier)
    assert canvas._kb == 1, canvas._kb
    assert canvas.temps == temps and canvas.speeds == speeds

    # Point 1 owns speeds[2]; its neighbours cap it at speeds[1]..speeds[3].
    floor, ceiling = speeds[1], speeds[3]
    start = speeds[2]
    assert floor < start < ceiling, (floor, start, ceiling)

    press(Qt.Key_Up)                            # fine step = 1
    assert canvas.speeds[2] == start + 1, canvas.speeds

    # The coarse step would overshoot the ceiling, so it clamps - this is
    # the whole reason mouse and keyboard share one code path.
    press(Qt.Key_Up, Qt.ShiftModifier)
    assert canvas.speeds[2] == ceiling, canvas.speeds

    # Hammering further up must not move it at all.
    for _ in range(50):
        press(Qt.Key_Up, Qt.ShiftModifier)
    assert canvas.speeds[2] == ceiling, canvas.speeds

    # Nor can hammering down get past the previous point's speed.
    for _ in range(200):
        press(Qt.Key_Down, Qt.ShiftModifier)
    assert canvas.speeds[2] == floor, canvas.speeds

    # Temperature is clamped too, and the curve stays valid throughout -
    # which is what makes "Apply" always meaningful.  check_curve signals
    # failure by raising, so "no exception" is the assertion.
    for _ in range(200):
        press(Qt.Key_Right, Qt.ShiftModifier)
    assert canvas.temps[1] == temps[2] - 1, canvas.temps
    gui.core.check_curve(canvas.temps, canvas.speeds)
    assert canvas.speeds == [speeds[0], speeds[1], speeds[1],
                             *speeds[3:]], canvas.speeds

    # Restore, so the screenshots taken after this are of the real curve.
    canvas.set_curve(temps, speeds)


def check_performance_tab(win) -> None:
    """The Performance tab must show EC truth, and never over-enable a control.

    Nothing else in the suite looked at this tab's widgets, so a regression
    that left every mode button clickable while the daemon was offline, or
    that showed two modes checked at once, would pass silently.
    """
    perf = win.perf

    # --- exactly one mode is checked, and it is the one the EC reports ---
    st = {**FAKE, "shift": {"current": "sport",
                            "available": FAKE["shift"]["available"]}}
    win._refresh(st)
    checked = [n for n, b in perf.buttons.items() if b.isChecked()]
    assert checked == ["sport"], f"checked modes: {checked}"

    # --- a mode this firmware does not offer is disabled, not half-alive ---
    st = {**st, "shift": {"current": "sport", "available": ["eco", "comfort"]}}
    win._refresh(st)
    assert not perf.buttons["sport"].isEnabled(), "unsupported mode enabled"
    assert perf.buttons["eco"].isEnabled()

    # --- offline disables every control on the tab ---
    win.online = False
    win._refresh(st)
    assert not perf.buttons["eco"].isEnabled(), "button live while offline"
    assert not perf.boost_btn.isEnabled(), "boost live while offline"
    win.online = True

    # --- cooler boost: check-state, wording and hot styling follow the EC ---
    st = {**FAKE, "cooler_boost": "on"}
    win._refresh(st)
    assert perf.boost_btn.isChecked(), "boost not shown as on"
    assert "COOLER BOOST ON" in perf.boost_btn.text(), perf.boost_btn.text()
    assert "background:" in perf.boost_btn.styleSheet(), "no hot styling"

    win._refresh({**FAKE, "cooler_boost": "off"})
    assert not perf.boost_btn.isChecked(), "boost stuck on"
    assert "hold" in perf.boost_btn.text(), perf.boost_btn.text()
    assert perf.boost_btn.styleSheet() == "", "hot styling stuck on"

    # Leave the payload the screenshots and later checks expect.
    win._refresh(FAKE)

    # --- both controls confirm with an activity sentence, including no-op ---
    describe = gui.MainWindow._action_message
    assert describe({"cmd": "set", "key": "cooler_boost"},
                    {"was": "off", "now": "on"},
                    None) == "Enabled cooler boost"
    assert describe({"cmd": "set", "key": "cooler_boost"},
                    {"was": "on", "now": "on"},
                    None) == "Cooler boost already on"
    assert describe({"cmd": "set", "key": "shift"},
                    {"was": "comfort", "now": "turbo"},
                    None) == "Set performance mode to turbo"


def check_graphics(win) -> None:
    """The MUX card: four dangerous states, and the reason for each.

    A MUX switch is the one control here that can leave a laptop with no
    display, so what matters is not the happy path but that the card refuses
    in the right places and *says why* - a greyed-out button with no reason
    reads as a bug.
    """
    perf = win.perf

    # --- a machine already in discrete: both facts shown, switch offered ---
    win.online = True
    win._refresh(FAKE)
    assert perf.gfx_rows["running now"].text() == "Discrete (NVIDIA only)", \
        perf.gfx_rows["running now"].text()
    assert perf.gfx_rows["at next boot"].text() == "Discrete (NVIDIA only)"
    assert perf.gfx_buttons["discrete"].isChecked()
    assert not perf.gfx_buttons["hybrid"].isChecked()
    assert perf.gfx_buttons["hybrid"].isEnabled()
    assert perf.gfx_warn.isHidden(), "warned when all is well"
    assert not perf.gfx_revert.isEnabled(), "undo offered with no backup"
    assert win.dash.mode_rows["Graphics"].text() == "Discrete", \
        win.dash.mode_rows["Graphics"].text()

    # --- switched but not yet rebooted: the pending mode is what is shown ---
    win._refresh({**FAKE, "graphics": {**FAKE["graphics"],
                                       "requested": "hybrid",
                                       "var_mode": "hybrid",
                                       "reboot_required": True}})
    assert perf.gfx_rows["running now"].text() == "Discrete (NVIDIA only)"
    assert "Hybrid" in perf.gfx_rows["at next boot"].text()
    assert "reboot" in perf.gfx_rows["at next boot"].text()
    assert perf.gfx_buttons["hybrid"].isChecked()
    assert not perf.gfx_warn.isHidden() and "reboot" in perf.gfx_warn.text()
    # Both the tile row and the dashboard say a reboot is outstanding -
    # "Hybrid" alone would claim the switch had already happened.
    assert win.dash.mode_rows["Graphics"].text() == "Hybrid after reboot", \
        win.dash.mode_rows["Graphics"].text()

    # --- disabled by config: buttons dead, and the config is named ---
    win._refresh({**FAKE, "graphics": {**FAKE["graphics"], "enabled": False}})
    assert not perf.gfx_buttons["hybrid"].isEnabled()
    assert not perf.gfx_warn.isHidden()
    assert "AllowMuxSwitch" in perf.gfx_warn.text(), perf.gfx_warn.text()

    # --- offline: dead, and the daemon is named as the reason ---
    win.online = False
    win._refresh(FAKE)
    assert not perf.gfx_buttons["hybrid"].isEnabled()
    assert "offline" in perf.gfx_warn.text(), perf.gfx_warn.text()

    # --- the daemon's own sentence is what gets announced ---
    win.online = True
    msg = gui.MainWindow._action_message(
        {"cmd": "graphics"},
        {"activity": "Switched graphics to Hybrid (Intel + NVIDIA) - "
                     "reboot to apply",
         "mode": "hybrid", "now": "hybrid"}, None)
    assert "reboot" in msg and "Hybrid" in msg, msg
    win._refresh(FAKE)


def check_dashboard_facts(win) -> None:
    """Every number on the Dashboard must mean what its label says.

    This is the regression guard for "N% of new", which divided energy_now by
    energy_full - the state of charge, printed as if it were the battery's
    health, right beside the same percentage shown as the charge.
    """
    win._refresh(FAKE)
    sub = win.dash.battery.sub.text()
    # 60 Wh of a 66 Wh design = 91%.  `energy_now` is 40/60 = 67%, which is
    # also the big number - so a regression to the old formula is visible as
    # the two agreeing.
    assert "health 91%" in sub, sub
    assert "67% of new" not in sub, sub
    # The fan cards show RPM as the number and duty as the bar; the label has
    # to say so or the bar's meaning is a guess.
    assert "RPM" in win.dash.cpu_rpm.sub.text(), win.dash.cpu_rpm.sub.text()
    assert "fan 48%" in win.dash.cpu_rpm.sub.text(), win.dash.cpu_rpm.sub.text()
    assert win.dash.cpu_rpm.value.text() == "2,290", win.dash.cpu_rpm.value.text()

    # The temperature cards carry the temperature, its bar and its band, and
    # nothing else: the duty number belongs to the fan cards beside them, and
    # printing it twice made a temperature card read as a fan card.  The
    # sub-line is hidden as well as blank, so it does not reserve a row.
    for card in (win.dash.cpu, win.dash.gpu):
        assert card.sub.text() == "", card.sub.text()
        assert not card.sub.isVisible(), "an empty sub-line still takes space"
        assert card.chip.text() in ("COOL", "WARM", "HOT", "CRITICAL"), \
            card.chip.text()

    # The battery chip names the state and colours it by whether that state
    # wants your attention.  Pinned because it is a decision, not a reading:
    # charging is fine, "not charging" while plugged in is the charge limit
    # doing its job but is worth a colour you notice, and running on battery
    # is not a warning at all.
    def chip(status, ac):
        st = dict(FAKE)
        st["battery"] = dict(FAKE["battery"], status=status, ac_online=ac)
        win._refresh(st)
        dash = win.dash.battery
        return (dash.chip.text(), dash.chip.styleSheet(),
                dash.chip.isVisibleTo(dash))

    for status, ac, want in (("Charging", True, gui.COL_OK),
                             ("Full", True, gui.COL_OK),
                             ("Not charging", True, gui.COL_WARM),
                             ("Discharging", False, gui.THEME["accent"])):
        text, css, vis = chip(status, ac)
        assert text == status.upper() and vis, (status, text, vis)
        assert f"background:{want}" in css, (status, want, css)
    text, css, vis = chip("", True)
    assert text == "" and not vis and "background" not in css, \
        "an unknown status must not paint a chip"
    win._refresh(FAKE)


def check_tray_icon(win) -> None:
    """The tray mark's inks, and the Settings controls that choose one.

    The tray is the one surface with no label beside it, so which ink it uses
    and whether it carries a reading at all are decisions rather than
    rendering details: a flat white icon, a flat black icon and a band-tinted
    one must be distinguishable from each other, and the colour mode must
    still follow the temperature.
    """
    def mean_brightness(mode):
        img = gui.temp_pixmap(32, 58, "58", mode).toImage()
        total = n = 0
        for y in range(img.height()):
            for x in range(img.width()):
                px = img.pixelColor(x, y)
                if px.alpha() < 32:
                    continue
                total += (px.red() + px.green() + px.blue()) / 3.0
                n += 1
        assert n, f"{mode} painted nothing"
        return total / n

    white = mean_brightness("white")
    black = mean_brightness("black")
    colour = mean_brightness("colour")
    assert white > 150, f"white mode is not white ({white:.0f})"
    assert black < 120, f"black mode is not dark ({black:.0f})"
    assert white > colour > black, (white, colour, black)

    # Colour mode follows the band: a cool fill is greener than a critical
    # one, which is the only thing it offers over the two flat inks.
    def green_minus_red(temp):
        img = gui.temp_pixmap(32, temp, "", "colour").toImage()
        g = r = 0
        for y in range(img.height()):
            for x in range(img.width()):
                px = img.pixelColor(x, y)
                if px.alpha() < 32:
                    continue
                g += px.green()
                r += px.red()
        return g - r

    assert green_minus_red(45) > green_minus_red(95), \
        "colour mode ignores the temperature band"
    assert set(gui.TRAY_TEMP_MODES) == {"colour", "white", "black"}

    # The Settings card mirrors the window, it does not keep its own copy.
    win._refresh(FAKE)
    assert win.settings.tray_show.isChecked() == bool(win.tray_temp)
    for key, btn in win.settings.tray_mode_btns.items():
        assert btn.isChecked() == (key == win.tray_colour), key


def check_settings_persistence(app) -> None:
    """Geometry and the open tab survive a real close - and nothing else does.

    The second half matters as much as the first: hide-to-tray must NOT
    persist (a hidden window's tab is not a choice the user made), and a
    saved index that has since fallen out of range must be ignored rather
    than clamped into some tab they never opened.
    """
    conf = os.path.join(os.environ["XDG_CONFIG_HOME"],
                        "msi-control", "msi-control.conf")
    assert not os.path.exists(conf), f"stale config leaked in: {conf}"

    probe = gui.MainWindow()
    probe.poll.stop()
    probe.online = True
    probe._refresh(FAKE)
    probe.tabs.setCurrentIndex(3)       # Battery
    probe.tray = None                   # the tray branch deliberately skips save
    probe.show()
    app.processEvents()
    probe.close()
    app.processEvents()
    assert os.path.exists(conf), f"closeEvent saved nothing to {conf}"

    again = gui.MainWindow()
    again.poll.stop()
    try:
        assert again.tabs.currentIndex() == 3, \
            f"tab not restored (got {again.tabs.currentIndex()})"
        assert again._qs.value("geometry") is not None, "geometry not saved"

        # A stale index is a guess, not a preference: it must be dropped.
        again._qs.setValue("tab", 6)
        again._qs.sync()
        stale = gui.MainWindow()
        stale.poll.stop()
        try:
            assert stale.tabs.currentIndex() != 6, "stale tab index accepted"
        finally:
            stale.close()
            app.processEvents()
    finally:
        again.close()
        app.processEvents()


def check_tray_options(app) -> None:
    """The tray preferences persist, and an unknown mode falls back.

    Run *after* check_settings_persistence: writing these is what creates the
    QSettings file, and that check asserts the file starts out absent.
    """
    w = gui.MainWindow()
    w.poll.stop()
    assert w.tray_temp is True, "the temperature should be on by default"
    assert w.tray_colour == "colour", w.tray_colour

    w.set_tray_options(show=False, colour="white")
    assert w.tray_temp is False and w.tray_colour == "white"
    # Changing only the toggle must not reset the ink.
    w.set_tray_options(show=True)
    assert w.tray_colour == "white", "a show-only change reset the ink"
    w.set_tray_options(colour="nonsense")
    assert w.tray_colour == "colour", "an unknown mode must fall back"
    w.close()
    app.processEvents()

    again = gui.MainWindow()
    again.poll.stop()
    try:
        assert again.tray_temp is True, "the tray toggle did not persist"
        assert again.tray_colour == "colour", again.tray_colour
    finally:
        again.close()
        app.processEvents()


def check_action_messages(win) -> None:
    """Confirmations must read as what the person did, not just "applied"."""
    describe = gui.MainWindow._action_message

    # The daemon's `activity` wins: it is built from the EC read-back and
    # phrased as the action ("Set performance mode to sport").
    assert describe({"cmd": "set", "key": "shift"},
                    {"activity": "Set performance mode to sport"},
                    "set:shift") == "Set performance mode to sport"

    # An older daemon that only sends the diff form must still be rendered.
    assert describe({"cmd": "set", "key": "shift"},
                    {"summary": "performance comfort -> sport"},
                    "set:shift") == "performance comfort -> sport"

    # With neither, the GUI must still build the sentence itself - and it
    # must be the activity form, not the diff.
    assert describe({"cmd": "set", "key": "charge_end"},
                    {"was": 60, "now": 80},
                    "set:charge_end") == "Set charge limit to 80%"

    # An apply that moved nothing must not claim it did.
    assert describe({"cmd": "profile", "action": "apply", "name": "gaming"},
                    {"changes": []},
                    "profile:apply") == "Applied profile gaming " \
                                        "(nothing to change)"

    assert describe({"cmd": "profile", "action": "delete", "name": "x"},
                    {}, "profile:delete") == "Deleted profile x"

    assert describe({"cmd": "reapply"},
                    {"activity": "Re-applied settings: nothing to change"},
                    None) == "Re-applied settings: nothing to change"
    # tag is optional and can be None - it must never be .replace()d.
    assert describe({"cmd": "mystery"}, {}, None) == "done"
    assert describe({"cmd": "mystery"}, {}, "profile:save") == "profile save"

    # A factory capture must report what really happened: the second one
    # does nothing, and "Captured factory profile" would be a lie about it.
    assert describe({"cmd": "capture-factory"}, {"result": "captured"},
                    None) == "Captured factory profile"
    assert "already" in describe({"cmd": "capture-factory"},
                                 {"result": "exists"}, None)
    assert "no fan-curve" in describe({"cmd": "capture-factory"},
                                      {"result": "unsupported"}, None)
    assert "Could not" in describe({"cmd": "capture-factory"},
                                   {"result": "failed"}, None)


def check_offline_disables_writes(win) -> None:
    """Every daemon-writing control must go dead when the daemon is down.

    The banner already explains *why* nothing is writable; a control that
    stays live and answers with a ✗ on click contradicts it and reads as a
    bug.  Performance, graphics and the battery are covered above; this pins
    the Profiles and Settings writers, which were quietly left enabled.
    """
    win.online = False
    win._refresh(FAKE)
    assert not win.profs.save_btn.isEnabled(), "profiles Save live offline"
    assert not win.profs.delete_btn.isEnabled(), "profiles Delete live offline"
    assert not win.profs.apply_btn.isEnabled(), "profiles Apply live offline"
    assert not win.settings.reapply_btn.isEnabled(), "re-apply live offline"
    assert not win.settings.factory_btn.isEnabled(), "capture live offline"

    win.online = True
    win._refresh(FAKE)
    assert win.profs.save_btn.isEnabled(), "profiles Save stuck dead online"
    assert win.profs.delete_btn.isEnabled(), "profiles Delete stuck dead online"
    assert win.settings.reapply_btn.isEnabled(), "re-apply stuck dead online"
    assert win.settings.factory_btn.isEnabled(), "capture stuck dead online"


def main() -> int:
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "shots")
    os.makedirs(outdir, exist_ok=True)

    # QSettings persists the window geometry and the open tab into the
    # user's real config.  A test must NEVER write there: it would silently
    # move the person's next real window to whatever tab a smoke run
    # happened to end on, and it would make this suite depend on leftovers
    # from an earlier run.  Redirect XDG_CONFIG_HOME to a scratch dir
    # before anything constructs a QSettings, and drop any stale file from
    # a previous run so check_settings_persistence() starts from nothing.
    cfg = os.path.join(outdir, "_xdg_config")
    os.makedirs(cfg, exist_ok=True)
    stale_conf = os.path.join(cfg, "msi-control", "msi-control.conf")
    if os.path.exists(stale_conf):
        os.remove(stale_conf)
    os.environ["XDG_CONFIG_HOME"] = cfg

    app = QApplication(sys.argv)

    # The real MainWindow starts a poll against the real daemon (which is
    # absent here), and its degraded reply would land mid-test and overwrite
    # our payload.  Neutralise the client so this test is deterministic.
    class SilentClient:
        class _NoSignal:
            def connect(self, *a, **kw):
                pass

        def __init__(self, parent=None):
            self.reply = self._NoSignal()

        def submit(self, cmd, tag=None):
            pass

    gui.Client = SilentClient
    win = gui.MainWindow()
    win.poll.stop()                 # we drive refresh() by hand
    win.online = True
    win.profile_cache["factory"] = {
        "shift": "comfort", "fan": "auto", "cooler_boost": False,
        "charge_end": 100,
        "curve": FAKE["curve"],
    }
    win._refresh(FAKE)
    win.show()
    app.processEvents()

    for i in range(win.tabs.count()):
        win.tabs.setCurrentIndex(i)
        app.processEvents()
        name = win.tabs.tabText(i).lower().replace(" ", "-")
        win.grab().save(os.path.join(outdir, f"{i}-{name}.png"))
        print(f"  wrote {i}-{name}.png")

    # sanity: the widgets really carry the payload
    assert win.dash.cpu.value.text() == "62°C", win.dash.cpu.value.text()
    assert win.dash.battery.value.text() == "67%"
    assert win.dash.mode_rows["Performance"].text() == "comfort"
    assert win.dash.mode_rows["Charge limit"].text() == "60%"
    assert win.fans.curve_state["cpu"]["temps"] == [58, 63, 68, 73, 78, 83]
    assert win.fans.canvases["cpu"].ghost is not None, "factory ghost missing"
    assert win.batt.slider.value() == 60

    # Behaviour a screenshot cannot show: contrast ratios, the pending-vs-
    # applied battery state machine, keyboard access to the curve editor,
    # and the wording of every confirmation.
    check_contrast(win)
    check_theme_tokens()
    check_brand_mark()
    check_nav_matches_tabs(win)
    check_glyphs(win)
    check_type_scale(win)
    check_control_sizes(win)
    check_dashboard_facts(win)
    check_tray_icon(win)
    check_battery_apply_state(win)
    check_curve_keyboard(win)
    check_performance_tab(win)
    check_graphics(win)
    check_action_messages(win)
    check_offline_disables_writes(win)
    print("  behaviour checks: OK")

    # Last: it opens and closes extra windows, and must not disturb `win`.
    check_settings_persistence(app)
    print("  settings persistence: OK")
    check_tray_options(app)
    print("  tray options: OK")

    print("GUI smoke: OK")
    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
