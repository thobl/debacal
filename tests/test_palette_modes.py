"""Light/dark and custom palettes remain readable and persist per profile."""
from dataclasses import FrozenInstanceError
from itertools import product
from types import SimpleNamespace

import pytest

from debacal.event_style import contrast_ratio, event_colors, _event_colors
from debacal.models import Calendar
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.theme import CUSTOM_COLORS, SCHEMES, scheme_for


@pytest.mark.parametrize('name,mode', tuple(product(SCHEMES, ('light', 'dark'))))
def test_predefined_palettes_preserve_light_default_and_offer_readable_dark_roles(name, mode):
    preferences = Preferences(color_scheme=name, appearance_mode=mode)
    palette = scheme_for(preferences)
    assert scheme_for(SimpleNamespace(preferences=preferences)) is palette
    assert scheme_for(palette) is palette
    if mode == 'light':
        assert palette is SCHEMES[name]
    else:
        assert palette != SCHEMES[name]
        assert contrast_ratio('#ffffff', palette.background) > contrast_ratio('#000000', palette.background)
    for foreground in (palette.ink, palette.muted, palette.weekend):
        for background in (palette.background, palette.sidebar, palette.panel, palette.selection, palette.weekend_background):
            assert contrast_ratio(foreground, background) >= 4.5
    assert contrast_ratio(palette.on_accent, palette.accent) >= 4.5
    assert contrast_ratio(palette.on_accent, palette.accent_hover) >= 4.5
    assert contrast_ratio(palette.notice_ink, palette.notice_background) >= 4.5
    assert palette.weekend_background != palette.background


CUSTOM = [
    ('#475f52', '#88939b', '#925866'),
    ('#ffffff', '#ffffff', '#ffffff'),
    ('#000000', '#000000', '#000000'),
    ('#ffff00', '#00ff00', '#ff00ff'),
    ('#0000ff', '#ff0000', '#00ffff'),
]


@pytest.mark.parametrize('colors,mode', tuple(product(CUSTOM, ('light', 'dark'))))
def test_custom_palette_derives_text_edges_and_both_modes_from_three_color_choices(colors, mode):
    preferences = Preferences(color_scheme='custom', appearance_mode=mode,
                              **dict(zip(CUSTOM_COLORS, colors)))
    palette = scheme_for(preferences)
    assert scheme_for(preferences) is palette
    for foreground in (palette.ink, palette.muted, palette.weekend):
        for background in (palette.background, palette.sidebar, palette.panel, palette.selection, palette.weekend_background):
            assert contrast_ratio(foreground, background) >= 4.5
    assert contrast_ratio(palette.on_accent, palette.accent) >= 4.5
    assert contrast_ratio(palette.on_accent, palette.accent_hover) >= 4.5
    assert contrast_ratio(palette.focus, palette.background) >= 3
    assert palette.weekend_background != palette.background
    with pytest.raises(FrozenInstanceError):
        palette.ink = '#000000'


@pytest.mark.parametrize('name,mode,source', tuple(product((*SCHEMES, 'custom'), ('light', 'dark'),
                         ('#ffff00', '#ffffff', '#000000', '#587c67', '#b6815c', '#7f79a5'))))
def test_event_cards_keep_source_identity_and_text_and_edges_readable_in_every_palette(name, mode, source):
    preferences = Preferences(color_scheme=name, appearance_mode=mode)
    palette = scheme_for(preferences)
    colors = event_colors(source, preferences)
    assert contrast_ratio(colors.ink, colors.fill) >= 4.5
    assert contrast_ratio(colors.muted, colors.fill) >= 4.5
    assert contrast_ratio(colors.outline, colors.fill) >= 3
    assert contrast_ratio(colors.selected_outline, colors.fill) >= 3
    assert contrast_ratio(colors.outline, palette.background) >= 3
    assert contrast_ratio(colors.selected_outline, palette.background) >= 3


def test_event_color_cache_uses_resolved_palette_not_only_custom_name():
    _event_colors.cache_clear()
    first = Preferences(color_scheme='custom', custom_surface='#ff0000')
    second = Preferences(color_scheme='custom', custom_surface='#0000ff')
    values = [event_colors('#008000', preferences) for preferences in
              (first, second, Preferences(color_scheme='custom', appearance_mode='dark'))]
    assert len({value.fill for value in values}) == 3
    assert event_colors('#008000', first) is values[0]
    assert _event_colors.cache_info().hits >= 1 and _event_colors.cache_info().maxsize == 1024


def test_appearance_fields_survive_restart_without_touching_calendar_sources(tmp_path):
    preferences = Preferences(color_scheme='custom', appearance_mode='dark', custom_accent='#aabbcc',
                              custom_surface='#234567', custom_weekend='#cc6688', weekend_style='background',
                              show_sidebar_title=False, sidebar_title='Research calendar')
    calendar = Calendar('source', 'Calendar source', '#112233', server_color='#ffff00')
    path = tmp_path / 'profile.sqlite3'
    with Store(path) as store:
        store.save_calendar(calendar)
        preferences.save(store)
    with Store(path) as store:
        assert Preferences.load(store) == preferences
        assert store.get_calendar(calendar.id) == calendar


@pytest.mark.parametrize('field,invalid', [
    ('appearance_mode', 'other'), ('appearance_mode', []),
    ('custom_accent', '#abc'), ('custom_surface', 'white'), ('custom_weekend', '#ffffff80'),
    ('custom_accent', None), ('weekend_style', 'color'), ('weekend_style', None),
    ('show_sidebar_title', 1), ('sidebar_title', ''), ('sidebar_title', 'a\nb'),
    ('sidebar_title', 'x' * 121),
])
def test_bad_appearance_fields_fall_back_individually(field, invalid):
    with pytest.raises(ValueError):
        Preferences(**{field: invalid})
    values = Preferences.from_dict({field: invalid, 'date_format': 'iso'})
    assert values == Preferences(date_format='iso')


def test_old_settings_and_positional_calls_keep_previous_light_appearance():
    values = Preferences(6, 'iso', '12h', 'both', False, 15, 'de_DE', 120, True, 6, 22,
                         'warm', 'grouped', 'filled')
    assert values.appearance_mode == 'light' and values.show_sidebar_title
    assert values.weekend_style == 'text' and values.sidebar_title == 'debacal'
    assert scheme_for(values) is SCHEMES['warm']
    assert Preferences.from_dict({'color_scheme': 'cool'}).appearance_mode == 'light'
