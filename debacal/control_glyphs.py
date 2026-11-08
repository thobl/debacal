"""Small theme-colored SVGs for Qt's stylesheet subcontrols."""
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

from .theme import valid_rgb

_directory = None


def _glyph_directory():
    global _directory
    if _directory is None:
        _directory = TemporaryDirectory(prefix='debacal-controls-')
    return Path(_directory.name)


@lru_cache(maxsize=128)
def control_glyphs(color):
    # Qt stylesheets load image URLs rather than QIcons. Keep generated glyphs
    # outside user data; the temporary directory lives only for this process.
    directory = _glyph_directory()
    if not valid_rgb(color):
        color = '#596570'
    paths = []
    for direction, points in (('up', '3 8 7 4 11 8'), ('down', '3 4 7 8 11 4')):
        path = directory / (color[1:] + '-' + direction + '.svg')
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="14" height="12" viewBox="0 0 14 12">'
                        f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.5" '
                        'stroke-linecap="round" stroke-linejoin="round"/></svg>')
        paths.append(path.as_posix())
    return tuple(paths)


@lru_cache(maxsize=64)
def checkmark_glyph(color):
    """A contrasting tick for the compact, filled checkbox style."""
    if not valid_rgb(color):
        color = '#ffffff'
    path = _glyph_directory() / (color[1:] + '-tick.svg')
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">'
                    f'<path d="m4 10 4 4 8-8" fill="none" stroke="{color}" stroke-width="2.2" '
                    'stroke-linecap="round" stroke-linejoin="round"/></svg>')
    return path.as_posix()
