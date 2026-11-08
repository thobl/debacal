"""One typeface choice for native controls and painted calendar text."""
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication


def resolved_font_family(preferences=None):
    family = getattr(preferences, 'font_family', 'system')
    if family != 'system':
        return family
    if QGuiApplication.instance() is not None:
        return QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    return 'Sans Serif'


def ui_font(preferences=None, size=None, bold=False):
    """Choose a family and optionally scale a base point size exactly once.

    Callers with already-scaled sizes can omit ``size`` and set their existing
    point/pixel size on the returned font.
    """
    font = QFont(resolved_font_family(preferences))
    if size is not None:
        font.setPointSizeF(size * getattr(preferences, 'font_scale', 100) / 100)
    font.setBold(bold)
    return font
