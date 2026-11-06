"""Small, consistent line icons for workspace actions."""
from functools import lru_cache

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


_SHAPES = {
    'new': '<path d="M10 4v12M4 10h12"/>',
    'previous': '<path d="m12 5-5 5 5 5"/>',
    'next': '<path d="m8 5 5 5-5 5"/>',
    'day': '<rect x="3" y="3" width="14" height="14" rx="2"/><path d="M3 7h14"/>',
    'week': '<rect x="2" y="3" width="16" height="14" rx="2"/><path d="M2 7h16M7 7v10M13 7v10"/>',
    'month': '<rect x="2" y="3" width="16" height="14" rx="2"/><path d="M2 7h16M2 12h16M7 7v10M13 7v10"/>',
    'single': '<rect x="5" y="5" width="10" height="10" rx="2"/>',
    'series': '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M13 4H6a2 2 0 0 0-2 2v7M10 1H3a2 2 0 0 0-2 2v7"/>',
    'following': '<rect x="2" y="5" width="7" height="10" rx="2"/><path d="M11 10h7m-3-3 3 3-3 3"/>',
    'clock': '<circle cx="10" cy="10" r="7.5"/><path d="M10 5v5l3 2"/>',
    'sync': '<path d="M16 8a6.5 6.5 0 0 0-11-3L3 7m0-4v4h4M4 12a6.5 6.5 0 0 0 11 3l2-2m0 4v-4h-4"/>',
    'settings': '<path d="M8 2h4l.6 2 1.7 1 2-.5 2 3.5-1.5 1.5v2l1.5 1.5-2 3.5-2-.5-1.7 1-.6 2H8l-.6-2-1.7-1-2 .5-2-3.5L3.2 11V9L1.7 7.5l2-3.5 2 .5 1.7-1Z"/><circle cx="10" cy="10" r="3"/>',
    'more': '<circle cx="4" cy="10" r="1"/><circle cx="10" cy="10" r="1"/><circle cx="16" cy="10" r="1"/>',
    'conflict': '<path d="m10 2 8 15H2Z"/><path d="M10 7v4m0 3v.1"/>',
    'confirm': '<path d="m4 10 4 4 8-8"/>',
}


@lru_cache(maxsize=64)
def icon(name, color='#4d6253'):
    source = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" '
              f'fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" '
              f'stroke-linejoin="round">{_SHAPES[name]}</svg>')
    renderer = QSvgRenderer(QByteArray(source.encode()))
    pixmap = QPixmap(40, 40)
    pixmap.fill('transparent')
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)
