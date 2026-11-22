from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from debacal.event_style import EventColors, contrast_ratio, event_colors, source_outline, _event_colors
from debacal.preferences import Preferences
from debacal.theme import SCHEMES, scheme_for


@pytest.mark.parametrize('scheme', SCHEMES)
@pytest.mark.parametrize('source', ['#ffff00', '#ffffff', '#000000', '#587c67', '#b6815c', '#7f79a5', '#c4b939'])
def test_card_text_and_edges_stay_readable_with_difficult_source_colors(scheme, source):
    colors = event_colors(source, scheme)
    assert contrast_ratio(colors.ink, colors.fill) >= 4.5
    assert contrast_ratio(colors.muted, colors.fill) >= 4.5
    assert contrast_ratio(colors.outline, colors.fill) >= 3
    assert contrast_ratio(colors.selected_outline, colors.fill) >= 3


def test_card_colors_keep_source_identity_without_mutable_cached_qcolors():
    yellow = event_colors('#ffff00')
    assert yellow.fill != event_colors('#0000ff').fill
    assert yellow == event_colors('yellow')
    with pytest.raises(FrozenInstanceError):
        yellow.fill = '#ffffff'
    assert _event_colors.cache_info().maxsize == 1024


def test_invalid_source_uses_interface_accent_and_badge_edge_darkens_white():
    assert event_colors('not-a-color') == event_colors(SCHEMES['neutral'].accent)
    assert contrast_ratio(source_outline('#ffffff', '#ffffff'), '#ffffff') >= 3


@pytest.mark.parametrize('mode,source,expected', [
    ('light', '#587c67', ('#dde4e0', '#587c67', '#3d5647', '#27313a', '#596570')),
    ('light', '#ffffff', ('#ffffff', '#919191', '#8c8c8c', '#27313a', '#596570')),
    ('light', '#000000', ('#cbcbcb', '#000000', '#000000', '#27313a', '#475159')),
    ('light', '#ffff00', ('#ffffcb', '#919100', '#8c8c00', '#27313a', '#596570')),
    ('dark', '#587c67', ('#303834', '#6c8c79', '#80b495', '#edf1f5', '#b1bac3')),
    ('dark', '#ffffff', ('#525353', '#ffffff', '#ffffff', '#edf1f5', '#c2c9d0')),
    ('dark', '#000000', ('#1e1f1f', '#797979', '#797979', '#edf1f5', '#b1bac3')),
    ('dark', '#ffff00', ('#52531f', '#ffff00', '#ffff73', '#edf1f5', '#c2c9d0')),
])
def test_default_event_preferences_preserve_existing_palette_exactly(mode, source, expected):
    preferences = Preferences(appearance_mode=mode)
    assert event_colors(source, preferences) == EventColors(*expected)
    assert event_colors(source, preferences) == event_colors(source, scheme_for(preferences))


@pytest.mark.parametrize('appearance', ['light', 'dark'])
@pytest.mark.parametrize('source', ['#ffffff', '#000000', '#ffff00', '#587c67', '#c4b939', '#777777'])
def test_literal_calendar_colors_are_exact_opaque_rgb_with_readable_text(appearance, source):
    preferences = Preferences(appearance_mode=appearance, event_color_mode='calendar')
    colors = event_colors(source, SimpleNamespace(preferences=preferences))
    assert colors.fill == source
    assert colors.ink in ('#000000', '#ffffff')
    assert contrast_ratio(colors.ink, colors.fill) >= 4.5
    assert contrast_ratio(colors.muted, colors.fill) >= 4.5
    assert contrast_ratio(colors.outline, colors.fill) >= 3
    assert contrast_ratio(colors.selected_outline, colors.fill) >= 3
    assert colors == event_colors(source, replace(preferences, event_color_brightness=-50,
                                                event_color_intensity=300), alpha=0)


def test_uniform_brightness_and_intensity_adjust_only_the_derived_fill():
    preferences = Preferences()
    original = event_colors('#587c67', preferences)
    assert event_colors('#587c67', replace(preferences, event_color_intensity=0)).fill == '#ffffff'
    assert event_colors('#587c67', replace(preferences, event_color_intensity=300)).fill == '#99afa2'
    assert event_colors('#587c67', replace(preferences, event_color_brightness=-50)).fill == '#6e7270'
    assert event_colors('#587c67', replace(preferences, event_color_brightness=50)).fill == '#eef2f0'
    assert original == event_colors('#587c67', preferences)


@pytest.mark.parametrize('appearance', ['light', 'dark'])
@pytest.mark.parametrize('brightness,intensity', [(-50, 0), (-50, 300), (50, 0), (50, 300)])
@pytest.mark.parametrize('source', ['#ffffff', '#000000', '#ffff00', '#587c67', '#c4b939', '#777777'])
def test_uniform_adjustment_extremes_keep_text_and_card_boundaries_readable(appearance, brightness,
                                                                         intensity, source):
    colors = event_colors(source, Preferences(appearance_mode=appearance,
                          event_color_brightness=brightness, event_color_intensity=intensity))
    assert contrast_ratio(colors.ink, colors.fill) >= 4.5
    assert contrast_ratio(colors.muted, colors.fill) >= 4.5
    assert contrast_ratio(colors.outline, colors.fill) >= 3
    assert contrast_ratio(colors.selected_outline, colors.fill) >= 3


@pytest.mark.parametrize('appearance,source', [('light', '#000000'), ('dark', '#ffffff'),
                                            ('light', '#587c67'), ('dark', '#587c67')])
def test_literal_selection_edges_differ_where_both_can_be_readable(appearance, source):
    preferences = Preferences(appearance_mode=appearance, event_color_mode='calendar')
    colors = event_colors(source, preferences)
    assert contrast_ratio(colors.outline, colors.selected_outline) >= 1.25
    assert contrast_ratio(colors.outline, scheme_for(preferences).background) >= 3
    assert contrast_ratio(colors.selected_outline, scheme_for(preferences).background) >= 3


def test_paint_cache_keys_include_all_event_options_and_remain_bounded():
    _event_colors.cache_clear()
    preferences = Preferences()
    event_colors('#587c67', preferences)
    event_colors('#587c67', replace(preferences, event_color_brightness=10))
    event_colors('#587c67', replace(preferences, event_color_intensity=150))
    event_colors('#587c67', replace(preferences, event_color_mode='calendar'))
    event_colors('#587c67', replace(preferences, event_color_mode='calendar', event_color_intensity=0))
    assert _event_colors.cache_info().misses == 5
    event_colors('#587c67', preferences)
    assert _event_colors.cache_info().hits == 1
    assert _event_colors.cache_info().maxsize == 1024


def test_partial_preview_preferences_clamp_options_and_reject_invalid_types():
    assert event_colors('#587c67', SimpleNamespace(event_color_brightness=500, event_color_intensity=-1)) == \
        event_colors('#587c67', Preferences(event_color_brightness=50, event_color_intensity=0))
    assert event_colors('#587c67', SimpleNamespace(event_color_mode=[], event_color_brightness=True,
                                                 event_color_intensity='100')) == event_colors('#587c67')
