import contextlib
from datetime import datetime, timedelta, timezone
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    loader.exec_module(result)
    return result


trend = module('disk_usage_trend', ROOT / 'disk_usage_trend')
installer = module('disk_trend_installer', ROOT / 'disk-trend/install.py')
UTC = timezone.utc
NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)
REPORT = '🏆 占用空间 Top 2\n  🥇  1.0 TiB    alice\n  🥈  512.0 GiB    bob\n'


class TrendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / 'report'
        self.config = {'database': str(self.root / 'history.sqlite3'),
                       'sources': [{'id': 'home', 'report': str(self.cache)}]}

    def write_report(self, text=REPORT, days=1):
        self.cache.write_text(text)
        stamp = int((NOW-timedelta(days=days)).timestamp()*10**9)
        os.utime(self.cache, ns=(stamp, stamp))

    def test_binary_decimal_units_and_ansi(self):
        self.assertEqual(trend.parse_report('\x1b[31m'+REPORT+'\x1b[0m'),
                         (('alice', 1024**4), ('bob', 512*1024**3)))
        self.assertEqual(trend.parse_report('占用空间 Top 1\n  1. 1.5 GB a\n'), (('a', 1500000000),))

    def test_incomplete_duplicates_and_control_text(self):
        for text in [REPORT.replace('bob', 'alice'), 'incomplete\n', REPORT.rsplit('  🥈',1)[0]]:
            with self.assertRaises(ValueError):
                trend.parse_report(text)
        self.assertNotIn('\x1b', trend.safe_text('bad\x1b[2Jname'))

    def test_cache_dedup_and_original_timestamp(self):
        self.write_report()
        self.assertEqual(trend.collect(self.config, now=NOW), [('home', '新增观测')])
        self.assertEqual(trend.collect(self.config, now=NOW), [('home', '缓存未变')])
        with contextlib.closing(sqlite3.connect(self.config['database'])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM snapshots').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT timestamp_ns FROM snapshots').fetchone()[0], self.cache.stat().st_mtime_ns)
        self.write_report(days=0.5)
        trend.collect(self.config, now=NOW)
        with contextlib.closing(sqlite3.connect(self.config['database'])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM snapshots').fetchone()[0], 2)

    def test_same_timestamp_different_content_keeps_old_history(self):
        self.write_report()
        trend.collect(self.config, now=NOW)
        self.write_report(REPORT.replace('1.0', '2.0'))
        self.assertIn('跳过', trend.collect(self.config, now=NOW)[0][1])
        with contextlib.closing(sqlite3.connect(self.config['database'])) as db:
            self.assertEqual(db.execute('SELECT bytes FROM usages WHERE name="alice"').fetchone()[0], 1024**4)

    def test_recent_file_and_symlink_and_fifo_are_rejected(self):
        self.write_report(days=0)
        with self.assertRaises(ValueError):
            trend.read_observation(self.cache, now=NOW)
        link = self.root / 'link'
        link.symlink_to(self.cache)
        with self.assertRaises(OSError):
            trend.read_observation(link, now=NOW)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            trend.read_observation(fifo, now=NOW)

    def test_latest_top_five_has_gaps_for_missing_users(self):
        self.write_report(days=2)
        trend.collect(self.config, now=NOW)
        self.write_report(REPORT.replace('alice', 'carol'), days=1)
        trend.collect(self.config, now=NOW)
        with contextlib.closing(sqlite3.connect(self.config['database'])) as db:
            times, names, series = trend.load_series(db, 'home', NOW-timedelta(days=14), NOW)
        self.assertEqual([name for name, _ in names], ['carol', 'bob'])
        self.assertEqual(series[0], [None, 1024**4])
        self.assertEqual(len(times), 2)

    def test_log_two_equal_doubling_distances(self):
        rows = [trend.logarithmic_row(v*1024**3, 38, 42, 9) for v in (256,512,1024,2048,4096)]
        self.assertEqual(rows, [8,6,4,2,0])
        with self.assertRaises(ValueError):
            trend.logarithmic_row(0,38,42,9)

    def test_default_window_and_custom_bounds(self):
        self.assertEqual(trend.get_window(now=NOW), (NOW-timedelta(days=14),NOW))
        start,end = trend.get_window(since='2026-11-01T00:00:00Z', until='2026-11-10T00:00:00Z')
        self.assertEqual((end-start).days,9)
        self.assertEqual(trend.parse_date('2026-11-10',end=True)-trend.parse_date('2026-11-10'),timedelta(days=1))
        with self.assertRaises(ValueError):
            trend.get_window(days=0,now=NOW)

    def test_chart_breaks_missing_and_long_gaps(self):
        start = NOW-timedelta(days=14)
        times = [start+timedelta(days=i) for i in [1,2,3,10]]
        chart = trend.render_chart({'id':'home'},times,[('alice',1024**4)],
                                   [[1024**4,None,1024**4,1024**4]],start,NOW)
        graph = '\n'.join(line.split('│',1)[1] for line in chart.splitlines() if '│' in line)
        dots = sum(bin(ord(char) - 0x2800).count('1') for char in graph if '\u2800' <= char <= '\u28ff')
        self.assertEqual(dots, 3)  # three points, no fabricated connecting line

    def test_braille_uses_eight_subpixels_in_one_character(self):
        self.assertEqual(sum(sum(row) for row in trend.BRAILLE_BITS), 255)
        start = NOW - timedelta(days=14)
        values = [[2 ** exponent] for exponent in (38.0, 38.03)]
        cells, _ = trend.curve_cells([NOW-timedelta(days=1)], values, start, NOW, 38, 39, 67, 9)
        masks = [cell for row in cells for cell in row if len(cell) == 2]
        self.assertEqual(len(masks), 1)
        self.assertNotEqual(masks[0][1], masks[0][2])

    def test_color_legend_matches_curves_without_growing_plot(self):
        start = NOW - timedelta(days=14)
        times = [NOW-timedelta(days=2), NOW-timedelta(days=1)]
        names = [('alice', 2**40), ('bob', 2**38)]
        chart = trend.render_chart({'id':'home'}, times, names,
                                   [[2**39, 2**40], [2**37, 2**38]], start, NOW, color=True)
        graph = [line for line in chart.splitlines() if '│' in line]
        self.assertEqual(len(graph), 9)
        self.assertTrue(all(trend.display_width(line) <= 80 for line in graph))
        self.assertIn('\x1b[96m━ 1 alice\x1b[0m', chart)
        self.assertIn('\x1b[93m━ 2 bob\x1b[0m', chart)
        self.assertTrue(any('\x1b[96m' in line for line in graph))
        self.assertTrue(any('\x1b[93m' in line for line in graph))
        self.assertTrue(all(trend.display_width(line) <= 80 for line in trend.legend_lines(names, 80, True)))

    def test_interleaved_motd_preserves_banner_and_is_idempotent(self):
        sources = installer.CONFIG['sources']
        original = '#!/bin/sh\necho "existing banner"\n' + ''.join('cat '+s['report']+'\necho ""\n' for s in sources)
        changed = installer.interleave_hook(original, sources)
        self.assertEqual(installer.interleave_hook(changed, sources), changed)
        self.assertIn('echo "existing banner"\n', changed)
        positions = []
        for source in sources:
            self.assertEqual(changed.count('cat '+source['report']), 1)
            positions.extend([changed.index('cat '+source['report']), changed.index(installer.chart_command(source['id']))])
        self.assertEqual(positions, sorted(positions))
        with self.assertRaises(ValueError):
            installer.interleave_hook('cat '+sources[0]['report']+'\n', sources)

    def test_one_point_readonly_show_and_empty_period(self):
        self.write_report()
        trend.collect(self.config, now=NOW)
        start,end = trend.get_window(now=NOW)
        before = Path(self.config['database']).stat().st_mtime_ns
        chart = trend.show(self.config,start=start,end=end)
        self.assertIn('只有一个真实快照',chart)
        self.assertIn('1 TiB',chart)
        self.assertEqual(Path(self.config['database']).stat().st_mtime_ns,before)
        self.assertIn('尚无历史观测',trend.show(self.config,start=NOW,end=NOW+timedelta(days=1)))

    def test_retention_deletes_child_rows(self):
        self.config['retention_days'] = 14
        self.write_report(days=15)
        trend.collect(self.config,now=NOW)
        with contextlib.closing(sqlite3.connect(self.config['database'])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM usages').fetchone()[0],0)

    def test_quiet_motd_fail_is_silent(self):
        config = self.root/'config.json'
        config.write_text(json.dumps(self.config))
        stdout,stderr=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            self.assertEqual(trend.main(['show','--config',str(config),'--quiet']),1)
        self.assertEqual(stdout.getvalue()+stderr.getvalue(),'')

    def test_installer_preserves_existing_motd_config_and_backs_up(self):
        hook=self.root/'etc/update-motd.d/99-custom-message'
        hook.parent.mkdir(parents=True)
        hook.write_text('original banner')
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.root)
            config=self.root/'etc/disk-usage-trend.json'
            custom=json.loads(config.read_text());custom['default_days']=30
            config.write_text(json.dumps(custom))
            old=self.root/'usr/local/libexec/disk-usage-trend.py'
            old.write_text('previous version')
            backup=installer.install(self.root)
        self.assertEqual(hook.read_text(),'original banner')
        self.assertEqual(json.loads(config.read_text())['default_days'],30)
        self.assertEqual((backup/'usr/local/libexec/disk-usage-trend.py').read_text(),'previous version')
        self.assertTrue(os.access(self.root/'etc/update-motd.d/99-z-disk-trends',os.X_OK))


if __name__=='__main__':
    unittest.main()
