# Event reminder performance

All measurements use synthetic calendars, file-backed SQLite, fake delivery and five rounds on one Intel Core i7-8700K Linux desktop with 12 logical CPUs, Python 3.14.10, PySide6 6.12.0 and SQLite 3.54.1. Qt uses the xcb backend under Xvfb, Fusion, DejaVu Sans and a 1,400 × 900 workspace. Each startup launches a new process. Both matched application checkouts have valid CPython bytecode for every application module before launch. The OS filesystem cache remains active between runs. Times are milliseconds.

## Common calendar workflows

The two matched runs use demo mode with the delivery runtime gated off and the same 22,365-resource fixture, five cold launches, seeded navigation windows and interaction order. The harness waits for asynchronous editor preparation while its modal Save driver is active. Between event pumps it sleeps for one millisecond, which lets SQLite worker threads reacquire Python's GIL. Both sources use these changes. The original preflight used an uncompiled application checkout. Its timings remain available for provenance. [Matched before](notification-ui-common-before.json) · [Matched after](notification-ui-common-after.json) · [Original preflight](notification-ui-before.json)

| Operation | Before mean | Before max | Before worst gap | After mean | After max | After worst gap |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Next week | 95.4 | 104.7 | 93.2 | 96.0 | 103.4 | 94.3 |
| Edit and save event | 260.8 | 268.1 | 134.6 | 255.6 | 265.8 | 103.1 |
| Move event | 134.2 | 144.3 | 142.8 | 101.7 | 103.2 | 101.8 |
| Move paste preview | 22.7 | 25.8 | 25.8 | 21.9 | 23.2 | 23.2 |
| Confirm paste | 107.3 | 120.6 | 118.8 | 106.5 | 120.9 | 119.1 |
| Commit import | 426.3 | 510.8 | 314.7 | 437.6 | 521.1 | 320.2 |
| Open Calendar sources | 48.9 | 52.4 | 47.6 | 75.1 | 79.8 | 73.2 |
| Apply theme | 888.9 | 913.4 | 910.0 | 921.0 | 932.1 | 928.6 |

The 1,211 matched actions averaged 86.9 ms before and 87.7 ms after. Cold process launch to first paint averaged 1,722.4 ms before and 1,752.7 ms after, with maxima of 1,768.9 and 1,778.2 ms. Peak process RSS was 413.2 and 419.8 MiB. The calendar-source page now builds local reminder policy controls on first use. Applying a theme remains the most expensive common action. Weekly event creation averaged 296.6 ms after and 295.3 ms before. Opening an import preview averaged 264.8 ms after and 245.4 ms before.

Tracing editor Save found two whole-calendar ID enumerations and a wait for the periodic workspace poll. Reselection now reads new resource IDs from the saved history operation. A completed editor read posts directly to the GUI thread. In the final matched run Save dispatched in 0.5 ms on average and finished after 255.6 ms, including the real database commit and reselection.

## Notification workflows

The 20,000-source workload includes hidden and disabled calendars, relative and fixed triggers, remote-only alarms, repeats and legacy master/detached VALARM families. Each fresh process opens a normal, non-demo workspace with the delivery runtime available. The disabled run measures first paint, then enables notifications and builds its index in place. A fresh enabled process reopens the same canonical database and sidecar and verifies its retained partition. Launch-to-paint includes process creation and imports. GUI gap and dispatch measurements on those records cover the window-opening interval, once Qt can process events. A separate history workload uses real dispositions, an active snooze and a dense rule in America/New_York. [Full reminder measurements](notification-performance.json)

| Normal workspace operation | Mean | Max | Worst GUI gap |
| --- | ---: | ---: | ---: |
| Fresh process to paint, delivery off | 1,433.1 | 1,449.4 | 142.8 |
| Fresh process to paint, prepared enabled index | 1,429.5 | 1,445.3 | 137.9 |
| Enable and build index | 51,308.3 | 51,648.8 | 78.5 |
| Enabled index ready after first paint | 7.7 | 7.7 | 6.1 |
| Idle worker inbox response | 30.4 | 49.0 | 11.8 |
| One edit through canonical and derived publication | 1,025.8 | 1,074.2 | 21.0 |
| Drain 72 due reminders | 413.5 | 423.6 | 11.9 |
| Dispatch an edit during a sync write | 1.3 | 1.6 | 1.6 |
| Navigate during a sync write | 15.8 | 20.6 | 20.6 |
| Inbox response during a sync write | 13.4 | 44.1 | 11.6 |
| Release sync writer through pending edit commit | 25.0 | 33.2 | 15.0 |

The fixture contained 4,201 alarm-bearing components, 79 legacy families, 2,679 hidden sources and 1,172 disabled sources. The index held 3,262 source rows and 5,014 frontier rows. SQLite allocated 3.06 MiB to the derived database. At the measurement point the main file occupied 3.05 MiB and the WAL occupied 4.01 MiB. The process's resident footprint grew by 3.8 MiB on average while building the index. One-source latency includes waiting for the scheduler to observe the canonical revision. A separate traced 96-source run counted 97 compilations on bootstrap, including one journal boundary revisit, zero on prepared restart and one after editing a single source.

A separate worker-only profile processed 2,000 sources in 4.70 s wall time and 4.68 s thread CPU time with its requested sleeps omitted. The requested backlog waits totaled 0.17 s, all writer exits including SQLite commit totaled 0.08 s, and partition refresh and identity checks took 2.04 s. First enable therefore spends substantial CPU time verifying the canonical file identity as it builds the index.

In the original five-round run at `8e6bee7`, each historical edit used 2,049 delivered occurrence groups. The active snooze survived the metadata, dense-rule and duration edits. The rule edit averaged 137.9 ms to publish, and the duration edit averaged 144.3 ms. The scheduler prepares pages of 32 groups outside the main sidecar writer transaction. Within the final writer transaction, the longest measured SQL section excluding commit was 21.8 ms for the rule edit, 17.5 ms for the duration edit and 0.33 ms for metadata. A real read receipt on a competing connection completed in a maximum of 19.9 ms including lock retries and commit.

A review found that a removed recurrence or a clock in a spring DST gap could misdirect a saved receipt and suppress an unrelated reminder. Revision `b27f226` checks membership in both the old and new recurrence. In five fresh runs of the same 2,049-group workload, dense-rule publication averaged **295.6 ms** (maximum 314.3 ms). The longest event-loop gap during that edit was **18.2 ms**. Its longest writer section was **24.7 ms**. Duration edits averaged 152.3 ms, and a competing read receipt took at most 22.1 ms. The snooze survived every round. The follow-up ran `scripts.benchmark_reminders.history_stress(..., size=2048)` on five distinct empty synthetic files under Xvfb with the xcb backend and Fusion. The common UI and 20,000-source figures above retain their original revision. [Five-round history follow-up](notification-history-after-review.json)


The extended UI run uses a separate synthetic reminder workspace for each round. Its alarm-bearing series has 24 saved master alarms and 12 detached resources. The script measures hover and confirmation separately, along with editor Save and the actual database commit. These reminder controls are new measurements. [Extended interaction samples](notification-ui-after.json)

| Alarm-bearing UI operation | Mean | Max | Worst GUI gap |
| --- | ---: | ---: | ---: |
| Open Notifications preferences | 138.7 | 148.0 | 126.1 |
| Open inbox | 34.4 | 35.4 | 20.0 |
| Copy series | 56.5 | 60.4 | 58.9 |
| Move paste preview | 32.6 | 114.9 | 113.6 |
| Confirm pasted copy | 67.9 | 80.5 | 79.0 |
| Move drag preview | 11.6 | 12.1 | 12.1 |
| Open prepared editor and paint | 255.0 | 263.3 | 232.4 |
| Change start time | 29.1 | 29.4 | 28.0 |
| Remove one of 24 reminders | 139.6 | 145.7 | 125.3 |
| Accept editor draft | 17.4 | 22.5 | 20.6 |
| Commit through guarded writer | 127.6 | 211.1 | 113.3 |
| Choose Remove in import review | 2.1 | 2.3 | 2.3 |
| Commit reviewed import | 183.1 | 272.2 | 111.3 |
| Restore from tray | 59.4 | 63.1 | 61.2 |

The guarded writer dispatched in 0.6 ms on average. Its completion includes durable changes and the workspace's success handling. Opening the prepared dialog and removing a row have visible UI costs on this large list. A separate profile attributed about 35 ms of 37 ms spent loading the reminder list to constructing its Qt widgets. Structural tests counted zero alarm decodes, wire parsing, copied UID assignments or canonical reads on successive placement hovers.

## Reproducing the measurements

The measured application source is `8e6bee7`. The extended driver in `9b29f3e` measures the guarded file-backed writer and has the same application fingerprint. From that development checkout with the project dependencies, run the commands on an otherwise idle machine.

```sh
QT_QPA_PLATFORM=xcb xvfb-run -a .venv/bin/python scripts/benchmark_reminders.py --sources 20000 --rounds 5 --output design/notification-performance.json
QT_QPA_PLATFORM=xcb xvfb-run -a .venv/bin/python scripts/benchmark_extended.py --revision 8e6bee7 --rounds 5 --startup-runs 5 --output design/notification-ui-after.json
```

The matched comparison uses application revisions `e679565` and `8e6bee7`. The following commands reconstruct the measured adapter from Git. Run them from the repository root with two local checkouts at those revisions. Set `BEFORE_CHECKOUT` and `AFTER_CHECKOUT` to the respective paths.

```sh
bench=design/notification-comparison-harness
mkdir -p "$bench"
git show e679565:scripts/benchmark_extended.py > "$bench/benchmark_extended.py"
git show e679565:scripts/benchmark_ranges.py > "$bench/benchmark_ranges.py"
patch -d "$bench" -p1 <<'PATCH'
--- a/benchmark_extended.py
+++ b/benchmark_extended.py
@@ -333,4 +333,5 @@
         commands = getattr(window, 'database_commands', None)
         return bool((commands is not None and commands.pending)
+                    or getattr(window, '_editor_future', None) is not None
                     or getattr(window, '_import_jobs', ())
                     or getattr(window, '_import_requests', ())
@@ -349,5 +350,5 @@
                 raise TimeoutError('UI operation did not complete; active=' + str(active))
             if quiet < 2:
-                QTest.qWait(1)
+                time.sleep(.001)
         if window is not None and getattr(window, 'local_issues', ()):
             raise RuntimeError('Operation produced app issues: ' + repr(window.local_issues))
PATCH
sha256sum "$bench/benchmark_extended.py" "$bench/benchmark_ranges.py"
.venv/bin/python -m compileall -q "$BEFORE_CHECKOUT/debacal" "$AFTER_CHECKOUT/debacal"
QT_QPA_PLATFORM=xcb PYTHONDONTWRITEBYTECODE=1 xvfb-run -a .venv/bin/python "$bench/benchmark_extended.py" --source-root "$BEFORE_CHECKOUT" --revision e679565 --rounds 5 --startup-runs 5 --output design/notification-ui-common-before.json
QT_QPA_PLATFORM=xcb PYTHONDONTWRITEBYTECODE=1 xvfb-run -a .venv/bin/python "$bench/benchmark_extended.py" --source-root "$AFTER_CHECKOUT" --revision 8e6bee7 --rounds 5 --startup-runs 5 --output design/notification-ui-common-after.json
```

The original extended script SHA-256 is `dd7f8c4751c149b68e957051891f54f0e5229fc446427cdc1721f5870a5e57ed`. The measured adapter hash is `bc76a1ca930a5bda0e4a39a5fcaacbff8ab4151507ecad4b7fc27867230e2e6d`, and the common generator hash is `62a04c9356a5df635f904c2720b06e558a51c2c655b1812788bf268ff5551da7`. The raw reports contain application hashes, configurations, seeded plans, individual samples, means and maxima.
