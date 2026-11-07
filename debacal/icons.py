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
    'year': '<rect x="2" y="2" width="16" height="16" rx="2"/><path d="M6 2v16M10 2v16M14 2v16M2 7h16M2 12h16"/>',
    'today': '<rect x="3" y="3" width="14" height="14" rx="2"/><path d="M3 7h14M7 1v4M13 1v4"/><circle cx="10" cy="12" r="2"/>',
    'single': '<rect x="5" y="5" width="10" height="10" rx="2"/>',
    'series': '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M13 4H6a2 2 0 0 0-2 2v7M10 1H3a2 2 0 0 0-2 2v7"/>',
    'following': '<rect x="2" y="5" width="7" height="10" rx="2"/><path d="M11 10h7m-3-3 3 3-3 3"/>',
    'expand': '<path d="M4 2h12M4 18h12M10 4v12M7 7l3-3 3 3M7 13l3 3 3-3"/>',
    'eye': '<path d="M1 10s3-6 9-6 9 6 9 6-3 6-9 6-9-6-9-6Z"/><circle cx="10" cy="10" r="2.5"/>',
    'eye_off': '<path d="M3 3l14 14M7 4.5A9 9 0 0 1 10 4c6 0 9 6 9 6a16 16 0 0 1-3 3.5M13 15.5A9 9 0 0 1 10 16c-6 0-9-6-9-6a16 16 0 0 1 3-3.5"/>',
    'lock': '<rect x="4" y="8" width="12" height="10" rx="2"/><path d="M6 8V5a4 4 0 0 1 8 0v3M10 12v2"/>',
    'search': '<circle cx="8" cy="8" r="5.5"/><path d="m12 12 5 5"/>',
    'import': '<path d="M10 2v10m-4-4 4 4 4-4M3 12v5h14v-5"/>',
    'export': '<path d="M10 12V2M6 6l4-4 4 4M3 12v5h14v-5"/>',
    'history': '<path d="M3 8a7 7 0 1 1 0 5M3 3v5h5M10 6v5l3 2"/>',
    'undo': '<path d="M3 8h8a6 6 0 0 1 6 6v2M7 4 3 8l4 4"/>',
    'redo': '<path d="M17 8H9a6 6 0 0 0-6 6v2M13 4l4 4-4 4"/>',
    'filter': '<path d="M2 3h16l-6 7v6l-4 2v-8Z"/>',
    'sync': '<path d="M16 8a6.5 6.5 0 0 0-11-3L3 7m0-4v4h4M4 12a6.5 6.5 0 0 0 11 3l2-2m0 4v-4h-4"/>',
    'settings': '<path d="M8 2h4l.6 2 1.7 1 2-.5 2 3.5-1.5 1.5v2l1.5 1.5-2 3.5-2-.5-1.7 1-.6 2H8l-.6-2-1.7-1-2 .5-2-3.5L3.2 11V9L1.7 7.5l2-3.5 2 .5 1.7-1Z"/><circle cx="10" cy="10" r="3"/>',
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
