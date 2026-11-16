"""Compare three compatible local extended benchmark reports.

Reject changed workloads, display geometry, dependencies, profiling and missing
interaction families. Preserve means and maxima separately; no aggregate score
can hide one slow interaction. Public outputs contain no input filesystem paths.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path
import re
import sys


LABELS = ('before', 'current', 'optimized')
ENVIRONMENT_KEYS = ('backend', 'font', 'window_size', 'screen_size', 'qt_style', 'python',
                    'pyside6', 'dependency_versions', 'cpus_available', 'platform')
MANDATORY_OPERATIONS = frozenset({
    'startup_workspace', 'jump_date', 'view_day', 'view_week', 'view_month', 'view_year',
    'hide_calendar_week', 'show_calendar_month', 'preferences_open', 'preferences_save',
    'preferences_apply_theme', 'preferences_apply_font_size', 'preferences_source_color',
    'create_timed_event', 'create_all_day_event', 'create_weekly_event', 'create_custom_event',
    'create_by_mouse_drag', 'create_by_month_doubleclick', 'edit_event_save', 'move_event',
    'delete_event', 'move_series_future', 'heavy_selection_group', 'heavy_selection_future',
    'heavy_selected_apply_preferences_future', 'copy_event', 'paste_confirm', 'undo_paste',
    'redo_paste', 'calendar_add', 'calendar_edit', 'calendar_reorder', 'search_type_many_series',
    'search_more_dates', 'import_commit', 'export_calendar_file', 'export_selection_file',
    'editor_sequence_type', 'picker_open', 'history_open', 'conflict_open',
})
STAT_KEYS = ('samples', 'mean_ms', 'p95_ms', 'max_ms', 'worst_event_loop_gap_ms')


def speedup(old, new):
    return round(old / new, 4) if new else None


def public_source(source):
    revision = source.get('revision', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{0,80}', revision):
        raise ValueError('Revision labels must be short public identifiers, not paths.')
    digest = source.get('application_sha256', '')
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('Missing application source fingerprint.')
    return {'revision': revision, 'application_sha256': digest}


def compare_reports(reports):
    if set(reports) != set(LABELS):
        raise ValueError('Provide exactly before, current and optimized reports.')
    reference = reports['current']
    inventory = set(reference['operations'])
    if any(not re.fullmatch(r'[a-z][a-z0-9_]{0,100}', name) for name in inventory):
        raise ValueError('Interaction names must be public identifiers, not arbitrary labels.')
    if not MANDATORY_OPERATIONS <= inventory:
        raise ValueError('The current report omits mandatory interaction families.')
    warnings = []
    if any(not isinstance(value, (int, float, bool)) for value in reference['configuration'].values()):
        raise ValueError('Unexpected non-numeric benchmark configuration; inspect before publication.')
    for key in ('script_sha256', 'generator_sha256'):
        if not re.fullmatch(r'[a-f0-9]{64}', reference['harness'].get(key, '')):
            raise ValueError('Missing benchmark harness fingerprint.')
    for label, report in reports.items():
        for key in ('configuration', 'harness', 'plan'):
            if key not in report or report[key] != reference.get(key):
                raise ValueError(f'{label}: benchmark {key} differs.')
        if set(report['operations']) != inventory:
            raise ValueError(f'{label}: interaction inventory differs.')
        for key in ENVIRONMENT_KEYS:
            if key not in report['environment'] or report['environment'][key] != reference['environment'].get(key):
                raise ValueError(f'{label}: environment field {key} differs.')
        if report['environment'].get('profiled'):
            raise ValueError(f'{label}: profiled timings cannot be used for comparisons.')
        counts = Counter(record['operation'] for record in report['records'])
        if set(counts) != inventory:
            raise ValueError(f'{label}: individual records do not cover the reported inventory.')
        rounds = report['configuration']['rounds']
        for operation, stats in report['operations'].items():
            if stats['samples'] != counts[operation]:
                raise ValueError(f'{label}: sample count disagrees with individual records.')
            for key in ('mean_ms', 'p95_ms', 'max_ms', 'worst_event_loop_gap_ms'):
                if not math.isfinite(stats[key]) or stats[key] < 0:
                    raise ValueError(f'{label}: invalid timing value.')
            required = 1 if operation == 'startup_workspace' else rounds
            if stats['samples'] < required:
                warnings.append({'version': label, 'operation': operation,
                                 'samples': stats['samples'], 'required': required})
        startups = report['startup_samples']
        if len(startups) != report['configuration']['startup_runs']:
            warnings.append({'version': label, 'operation': 'fresh_startup',
                             'samples': len(startups), 'required': report['configuration']['startup_runs']})
        if 'process_to_first_paint_ms' not in report['startup_summary']:
            raise ValueError(f'{label}: external process-to-first-paint timing is missing.')
    rows = []
    for operation in sorted(inventory):
        row = {'operation': operation}
        for label in LABELS:
            row.update({f'{label}_{key}': reports[label]['operations'][operation][key] for key in STAT_KEYS})
        for key in ('mean_ms', 'max_ms'):
            row['current_to_optimized_' + key.replace('_ms', '_speedup')] = speedup(
                reports['current']['operations'][operation][key], reports['optimized']['operations'][operation][key])
        rows.append(row)
    memory = {}
    for label, report in reports.items():
        usage = report.get('usage_samples', [])
        memory[label] = {'peak_rss_mib': report['peak_rss_mib'],
                         'first_round_resident_mib': usage[0]['resident_mib'] if usage else None,
                         'last_round_resident_mib': usage[-1]['resident_mib'] if usage else None,
                         'last_round_threads': usage[-1]['threads'] if usage else None,
                         'last_round_open_fds': usage[-1]['open_fds'] if usage else None}
    return {'schema_version': 1, 'comparable': not warnings, 'under_sampled': warnings,
            'sources': {label: public_source(report['source']) for label, report in reports.items()},
            'configuration': reference['configuration'],
            'harness': {key: reference['harness'][key] for key in ('script_sha256', 'generator_sha256')},
            'environment': {key: reference['environment'][key] for key in ENVIRONMENT_KEYS if key != 'platform'},
            'startup': {label: report['startup_summary'] for label, report in reports.items()},
            'memory': memory, 'operations': rows}


def markdown(comparison):
    lines = ['# Extended interaction timings', '', 'All times are milliseconds. Speedup is current / optimized.', '',
             '| Interaction | Before mean / max | Current mean / max | Optimized mean / max | Mean speedup | Max speedup |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    for row in comparison['operations']:
        cells = [f'`{row["operation"]}`']
        cells.extend(f'{row[label + "_mean_ms"]:.3f} / {row[label + "_max_ms"]:.3f}' for label in LABELS)
        cells.extend(f'{row[key]:.2f}×' if row[key] is not None else '—'
                     for key in ('current_to_optimized_mean_speedup', 'current_to_optimized_max_speedup'))
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for label in LABELS:
        parser.add_argument(label, type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--markdown', action='store_true', help='Also write a complete Markdown interaction table.')
    args = parser.parse_args(argv)
    try:
        reports = {label: json.loads(getattr(args, label).read_text()) for label in LABELS}
        comparison = compare_reports(reports)
    except (OSError, json.JSONDecodeError):
        print('Could not read one of the three benchmark reports.', file=sys.stderr)
        return 2
    except (ValueError, KeyError, TypeError) as error:
        print('Comparison refused: ' + str(error), file=sys.stderr)
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / 'comparison.json').write_text(json.dumps(comparison, indent=2) + '\n')
    with (args.output_dir / 'operations.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(comparison['operations'][0]))
        writer.writeheader()
        writer.writerows(comparison['operations'])
    if args.markdown:
        (args.output_dir / 'all-operations.md').write_text(markdown(comparison))
    print(json.dumps({'operations': len(comparison['operations']), 'comparable': comparison['comparable'],
                      'under_sampled': len(comparison['under_sampled'])}))
    return 0 if comparison['comparable'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
