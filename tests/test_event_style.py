from dataclasses import FrozenInstanceError

import pytest

from debacal.event_style import contrast_ratio, event_colors, source_outline, _event_colors
from debacal.theme import SCHEMES


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
