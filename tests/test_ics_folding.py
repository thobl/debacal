"""iCalendar folding counts octets and keeps every UTF-8 code point intact."""
import random

import pytest

from debacal.ics_fallback import folded


def reference_fold(lines):
    output = []
    for line in lines:
        part, size = '', 0
        for character in line:
            length = len(character.encode('utf-8'))
            if size + length > 75:
                output.append(part)
                part, size = ' ', 1
            part += character
            size += length
        output.append(part)
    return ('\r\n'.join(output) + '\r\n').encode('utf-8')


@pytest.mark.parametrize('prefix', [0, 1, 70, 71, 72, 73, 74, 75, 76, 148, 149])
@pytest.mark.parametrize('character', ['a', 'é', '漢', '🗓'])
def test_folding_boundaries_match_the_reference_and_unfold_without_loss(prefix, character):
    lines = ['DESCRIPTION:' + 'a' * prefix + character * 80, '', 'SUMMARY:Short line']
    actual = folded(lines)
    assert actual == reference_fold(lines)
    physical = actual[:-2].split(b'\r\n')
    assert all(len(line) <= 75 for line in physical)
    assert all(isinstance(line.decode('utf-8'), str) for line in physical)
    assert actual.replace(b'\r\n ', b'').decode('utf-8') == '\r\n'.join(lines) + '\r\n'


def test_mixed_unicode_long_lines_match_reference_without_per_character_encoding():
    randomizer = random.Random(618)
    alphabet = 'abc ;,:\\äé漢字🗓\u0301'
    lines = [''.join(randomizer.choices(alphabet, k=size)) for size in (0, 1, 75, 100, 2048)]
    assert folded(lines) == reference_fold(lines)
    assert folded([]) == b'\r\n'
