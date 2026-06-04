import os
import logging
from PyQt5.QtGui import QIcon, QPixmap, QPainter
from PyQt5.QtCore import QByteArray, Qt

logger = logging.getLogger(__name__)

ICON_PATH = os.path.dirname(__file__)

# Color that SVG `currentColor` resolves to. Feather-style icons declare
# stroke="currentColor", which is meant to inherit the surrounding text color;
# Qt has no such context and falls back to black, so on a dark theme the strokes
# look harsh. We substitute an explicit color instead. None -> leave to Qt
# (black). Set per theme via set_icon_color().
_ICON_COLOR = None
_RENDER_SIZES = (16, 20, 24, 32, 48, 64)


def set_icon_color(color):
    """Set the color SVG `currentColor` resolves to (None = Qt default/black)."""
    global _ICON_COLOR
    _ICON_COLOR = color


def icon_color_for_theme(theme_name):
    """Pick a `currentColor` replacement that reads well on the given theme.

    Dark themes get a soft light-grey; other themes keep the default (black-ish)
    look by returning None.
    """
    if theme_name and "dark" in str(theme_name).lower():
        return "#c8c8c8"
    return None


def style_mpl_toolbar(toolbar, dark: bool):
    """Recolor a matplotlib NavigationToolbar2QT for a dark/light theme.

    Matplotlib inverts its black glyph icons to the palette foreground color
    when the toolbar's palette background is dark (value < 128). A Qt
    *stylesheet* doesn't touch the palette, so we set it here and rebuild the
    icons. Uses the same grey as our own icons on dark; restores black on light.
    """
    from PyQt5.QtGui import QColor

    bg, fg = ("#3a3a3a", _ICON_COLOR or "#c8c8c8") if dark else ("#f0f0f0", "#000000")
    pal = toolbar.palette()
    pal.setColor(toolbar.backgroundRole(), QColor(bg))
    pal.setColor(toolbar.foregroundRole(), QColor(fg))
    toolbar.setPalette(pal)
    try:
        for _text, _tip, image, callback in toolbar.toolitems:
            if not image:
                continue
            action = toolbar._actions.get(callback)
            if action is not None:
                action.setIcon(toolbar._icon(image + ".png"))
    except Exception as e:
        logger.debug(f"Could not restyle matplotlib toolbar icons: {e}")


def get_icon(name: str, recolor: bool = True):
    """Load an icon, recoloring `currentColor` strokes to the active theme color.

    Only monochrome `currentColor` icons are affected; explicitly-colored icons
    (gradients, the blue checkmark, the folder icons) render unchanged because
    they don't reference `currentColor` for their visible parts. Pass
    recolor=False to force the original (e.g. the multicolor app icon).
    """
    path = os.path.join(ICON_PATH, name)
    color = _ICON_COLOR if recolor else None
    if not color:
        return QIcon(path)
    try:
        from PyQt5.QtSvg import QSvgRenderer

        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        if "currentColor" not in data:
            return QIcon(path)

        renderer = QSvgRenderer(QByteArray(data.replace("currentColor", color).encode("utf-8")))
        icon = QIcon()
        for size in _RENDER_SIZES:
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            icon.addPixmap(pixmap)
        return icon
    except Exception as e:
        logger.debug(f"Could not recolor icon {name}: {e}")
        return QIcon(path)
