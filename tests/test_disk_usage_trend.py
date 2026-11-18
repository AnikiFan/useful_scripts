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
        graph = '\n'.join(line[12:].split('│')[0] for line in chart.splitlines() if '│' in line)
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
                                   [[2**39, 2**40], [2**37, 2**38]], start, NOW, color=True, height=9)
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
            self.assertNotIn('cat '+source['report'], changed)
            self.assertNotIn(installer.summary_command(source['id']),changed)
            positions.append(changed.index(installer.chart_command(source['id'])))
        self.assertEqual(positions, sorted(positions))
        legacy = original
        for source in sources:
            report = 'cat '+source['report']+'\n'
            legacy = legacy.replace(report, report+installer.chart_command(source['id']).replace('--height 15','--height 9')+'\n')
        legacy += '# disk-usage-trend: interleaved-v1\n'
        self.assertEqual(installer.interleave_hook(legacy,sources),changed)
        with self.assertRaises(ValueError):
            installer.interleave_hook('cat '+sources[0]['report']+'\n', sources)

    def test_latest_values_never_overlap_or_expand_chart(self):
        names = [(f'user{i}', 1024**4) for i in range(5)]
        labels = trend.latest_value_labels(names, 39, 41, 5)
        self.assertEqual(sorted(labels), list(range(5)))
        self.assertEqual([labels[row][0] for row in sorted(labels)], [1,2,3,4,5])
        for width in (40,80):
            chart = trend.render_chart({'id':'home'}, [NOW-timedelta(hours=1)], names,
                                       [[value] for _,value in names], NOW-timedelta(days=14), NOW,
                                       width=width, height=9, color=True)
            graph = [line for line in chart.splitlines() if '│' in line]
            self.assertEqual(len(graph), 9)
            self.assertTrue(all(trend.display_width(line) <= width for line in graph))
            values = '\n'.join(line.split('│')[-1] for line in graph)
            for index in range(1,6):
                self.assertIn(f'{index}: 1 TiB', values)
            self.assertIn('最新值', chart)

    def test_free_space_parsing_and_zero(self):
        self.assertEqual(trend.parse_free_space('🆓 剩 1.4 TiB\n'), int(1.4*1024**4))
        self.assertEqual(trend.parse_free_space('\x1b[31m🆓 剩 0 B\x1b[0m'), 0)
        self.assertIsNone(trend.parse_free_space(REPORT))

    def test_migrate_free_space_without_changing_original_snapshots(self):
        self.write_report(days=2)
        trend.collect(self.config,now=NOW)
        self.write_report('🆓 剩 256 GiB\n'+REPORT,days=1)
        trend.collect(self.config,now=NOW)
        with contextlib.closing(sqlite3.connect(self.config['database'])) as db:
            db.execute('DROP TABLE disk_space');db.commit()  # old deployed schema
            before=db.execute('SELECT * FROM snapshots ORDER BY id').fetchall()
            _, names, _=trend.load_series(db,'home',NOW-timedelta(days=14),NOW)
            self.assertEqual(len(names),2)
        trend.collect(self.config,now=NOW)
        trend.collect(self.config,now=NOW)
        with contextlib.closing(sqlite3.connect(self.config['database'])) as db:
            self.assertEqual(db.execute('SELECT * FROM snapshots ORDER BY id').fetchall(),before)
            _,names,series=trend.load_series(db,'home',NOW-timedelta(days=14),NOW)
            self.assertEqual(names[-1],('剩余空间',256*1024**3))
            self.assertEqual(series[-1],[None,256*1024**3])
            self.assertEqual(db.execute('SELECT count(*) FROM disk_space').fetchone()[0],1)

    def test_six_series_with_free_space_keep_nine_rows(self):
        names=[(f'user{i}',1024**4) for i in range(5)]+[('剩余空间',0)]
        chart=trend.render_chart({'id':'home'},[NOW-timedelta(hours=1)],names,
                                 [[value] for _,value in names],NOW-timedelta(days=14),NOW,color=True,height=9)
        graph=[line for line in chart.splitlines() if '│' in line]
        self.assertEqual(len(graph),9)
        self.assertTrue(all(trend.display_width(line)<=80 for line in graph))
        self.assertIn('6: 0 B',chart)
        self.assertIn('\x1b[97m━ 6 剩余空间',chart)
        self.assertIn('Top 5 + 剩余空间',chart)
        with self.assertRaises(ValueError):
            trend.latest_value_labels(names,39,41,5)

    def test_grid_and_ticks_preserve_curve_pixels_and_dimensions(self):
        start=NOW-timedelta(days=14)
        times=[NOW-timedelta(days=2),NOW-timedelta(days=1)]
        chart=trend.render_chart({'id':'home'},times,[('alice',2**40)],[[2**39,2**40]],start,NOW,color=True,height=9)
        graph=[line for line in chart.splitlines() if '│' in line]
        self.assertEqual(len(graph),9)
        self.assertTrue(all(trend.display_width(line)<=80 for line in graph))
        self.assertIn('\x1b[90m┄\x1b[0m',chart)
        self.assertIn('\x1b[90m┆\x1b[0m',chart)
        self.assertIn('├',chart)
        baseline=next(line for line in chart.splitlines() if '└' in line)
        self.assertEqual(baseline.count('┬'),5)
        plain_graph=[trend.ANSI.sub('',line)[12:].split('│')[0] for line in graph]
        cells,_=trend.curve_cells(times,[[2**39,2**40]],start,NOW,39,40,len(plain_graph[0])-1,9)
        for row,values in enumerate(cells):
            for column,cell in enumerate(values):
                if cell:
                    self.assertEqual(plain_graph[row][column],chr(0x2800+cell[1]))

    def test_time_ticks_adapt_to_narrow_and_short_windows(self):
        self.assertEqual(len(trend.time_ticks(NOW-timedelta(days=14),NOW,13)),2)
        self.assertEqual(len(trend.time_ticks(NOW-timedelta(days=14),NOW,30)),3)
        ticks=trend.time_ticks(NOW-timedelta(hours=12),NOW,53)
        self.assertEqual(len(ticks),5)
        self.assertTrue(all(':' in label for _,label in ticks))

    def test_disk_summary_keeps_capacity_without_static_ranking(self):
        summary='💾 磁盘剩余空间 · 📂 /home\n  💿 /dev/test · 总 2 TiB · 🆓 剩 256 GiB\n'
        self.write_report(summary+'\n'+REPORT)
        before=self.cache.read_bytes()
        output=trend.show_summary(self.config,source_id='home')
        self.assertIn('🆓 剩 256 GiB',output)
        self.assertIn('总 2 TiB',output)
        self.assertNotIn('alice',output)
        self.assertNotIn('占用空间 Top',output)
        self.assertEqual(self.cache.read_bytes(),before)

    def test_default_graph_height_is_fifteen(self):
        chart=trend.render_chart({'id':'home'},[NOW-timedelta(hours=1)],[('alice',2**40)],
                                 [[2**40]],NOW-timedelta(days=14),NOW)
        self.assertEqual(sum('│' in line for line in chart.splitlines()),15)

    def test_capacity_upgrade_preserves_snapshots_and_uses_whole_disk(self):
        self.write_report('📦 总 2 TiB · 🆓 剩 256 GiB\n'+REPORT)
        trend.collect(self.config,now=NOW)
        with contextlib.closing(sqlite3.connect(self.config['database'])) as db:
            before=db.execute('SELECT * FROM snapshots').fetchall()
            db.execute('DROP TABLE disk_capacity');db.commit()
            history=trend.load_share_history(db,'home',NOW-timedelta(days=14),NOW)
            self.assertIsNone(history.amounts[-1])
        trend.collect(self.config,now=NOW)
        with contextlib.closing(sqlite3.connect(self.config['database'])) as db:
            self.assertEqual(db.execute('SELECT * FROM snapshots').fetchall(),before)
            history=trend.load_share_history(db,'home',NOW-timedelta(days=14),NOW)
        self.assertEqual(history.names,['alice','bob','Others','空余'])
        self.assertEqual(trend.share_percentages(history.amounts[-1],history.totals[-1]),[50,25,12.5,12.5])

    def test_unranked_share_stays_in_other_and_does_not_invent_usage(self):
        self.write_report('📦 总 2 TiB · 🆓 剩 256 GiB\n'+REPORT,days=2)
        trend.collect(self.config,now=NOW)
        self.write_report('📦 总 2 TiB · 🆓 剩 256 GiB\n'+REPORT.replace('alice','carol'),days=1)
        trend.collect(self.config,now=NOW)
        with contextlib.closing(sqlite3.connect(self.config['database'])) as db:
            history=trend.load_share_history(db,'home',NOW-timedelta(days=14),NOW)
        self.assertEqual(trend.share_percentages(history.amounts[0],history.totals[0]),[0,25,62.5,12.5])
        self.assertEqual(trend.share_percentages(history.amounts[1],history.totals[1]),[50,25,12.5,12.5])

    def test_inconsistent_totals_are_not_normalized_to_fake_shares(self):
        self.write_report('📦 总 1 TiB · 🆓 剩 256 GiB\n'+REPORT)
        trend.collect(self.config,now=NOW)
        with contextlib.closing(sqlite3.connect(self.config['database'])) as db:
            history=trend.load_share_history(db,'home',NOW-timedelta(days=14),NOW)
        self.assertIsNone(history.amounts[-1])
        with self.assertRaises(ValueError):
            trend.share_percentages([60,60],100)

    def test_share_interpolation_preserves_hundred_percent_and_missing_gaps(self):
        start=NOW-timedelta(days=14)
        times=[start+timedelta(days=day) for day in (1,2,3,10)]
        history=trend.ShareHistory(times,['a','其他/保留','空余空间'],
                                   [[20,60,20],[40,40,20],None,[10,70,20]],[100,100,None,100],[('a',10)])
        columns=trend.share_columns(history,start,NOW,141)
        for values in columns:
            if values is not None:self.assertAlmostEqual(sum(values),100)
        self.assertEqual(columns[15],[30,50,20])
        self.assertIsNone(columns[30])
        self.assertIsNone(columns[70])
        self.assertIsNone(columns[140])

    def test_share_chart_keeps_top_five_sizes_and_free_share_readable(self):
        unit=1024**3
        amounts=[20*unit,10*unit,8*unit,6*unit,4*unit,47*unit,5*unit]
        names=[f'user{i}' for i in range(1,6)]+['Others','空余']
        history=trend.ShareHistory([NOW-timedelta(days=2),NOW-timedelta(days=1)],names,
                                   [amounts,amounts],[100*unit,100*unit],list(zip(names[:5],amounts[:5])))
        chart=trend.render_share_chart({'id':'home'},history,NOW-timedelta(days=14),NOW,color=True)
        graph=[line for line in chart.splitlines() if '│' in line]
        self.assertEqual(len(graph),15)
        self.assertTrue(all(trend.display_width(line)<=80 for line in graph))
        self.assertIn('100%',chart);self.assertIn('0%',chart)
        self.assertNotIn('当前 Top 5:',chart)
        self.assertLessEqual(len(chart.splitlines()),19)
        for i,value in enumerate((20,10,8,6,4),1):self.assertIn(f'{i} user{i} {value} GiB',chart)
        self.assertIn('O Others 47 GiB',chart)
        self.assertIn('9 空余 5 GiB',chart)
        self.assertIn('9 空余 5 GiB 5.0%',chart)
        self.assertTrue('█' in chart or '▄' in chart)
        self.assertEqual(trend.percent_text(0.005),'<0.01%')
        self.assertEqual(trend.percent_text(0.05),'0.05%')

    def test_eight_users_plus_free_and_others_fit_compact_chart(self):
        unit=1024**3
        amounts=[unit]*8+[81*unit,11*unit]
        names=[f'username_{i}' for i in range(1,9)]+['Others','空余']
        history=trend.ShareHistory([NOW-timedelta(days=1)],names,[amounts],[100*unit],list(zip(names[:8],amounts[:8])))
        chart=trend.render_share_chart({'id':'home'},history,NOW-timedelta(days=14),NOW,color=True)
        self.assertEqual(len(chart.splitlines()),19)
        self.assertTrue(all(trend.display_width(line)<=80 for line in chart.splitlines()))
        for i in range(1,9):self.assertIn(f'{i} username_{i} 1 GiB 1.0%',chart)
        self.assertIn('9 空余 11 GiB 11.0%',chart)
        self.assertIn('O Others 81 GiB 81.0%',chart)

    def test_compact_v2_migration_removes_only_duplicate_summaries(self):
        sources=installer.CONFIG['sources']
        old='#!/bin/sh\necho "banner"\n'+''.join(installer.summary_command(s['id'])+'\n'+installer.chart_command(s['id'])+'\n' for s in sources)+'# disk-usage-trend: compact-v2\n'
        new=installer.interleave_hook(old,sources)
        self.assertIn('echo "banner"',new)
        self.assertIn('compact-v3',new)
        self.assertNotIn(' summary ',new)
        self.assertEqual(installer.interleave_hook(new,sources),new)
        with self.assertRaises(ValueError):installer.interleave_hook(old.replace('--quiet','--changed',1),sources)

    def test_expand_scan_ranking_preserves_schedule_and_other_jobs(self):
        expander=module('expand_topk',ROOT/'disk-trend/expand_topk.py')
        old='# get_user_topk /home 5 > /var/cache/home_topk_user\n0 2 * * * /home/xfan/.local/bin/get_user_topk /home 5 > /var/cache/home_topk_user\n0 8 * * * unrelated_job\n'
        new=expander.expand(old,expander.HOME)
        self.assertEqual(new,old.replace('/bin/get_user_topk /home 5','/bin/get_user_topk /home 8'))
        self.assertEqual(expander.expand(new,expander.HOME),new)
        piped=old.replace(' > /var/cache/home_topk_user',' | sudo tee /var/cache/home_topk_user > /dev/null')
        self.assertEqual(expander.expand(piped,expander.HOME),piped.replace('/bin/get_user_topk /home 5','/bin/get_user_topk /home 8'))
        with self.assertRaises(ValueError):expander.expand(old+old,expander.HOME)
        with self.assertRaises(ValueError):expander.expand(old.replace('/home 5','/home 6'),expander.HOME)

    def test_one_point_readonly_show_and_empty_period(self):
        self.write_report()
        trend.collect(self.config, now=NOW)
        start,end = trend.get_window(now=NOW)
        before = Path(self.config['database']).stat().st_mtime_ns
        chart = trend.show(self.config,start=start,end=end,style="lines")
        self.assertIn('只有一个真实快照',chart)
        self.assertIn('1 TiB',chart)
        self.assertEqual(Path(self.config['database']).stat().st_mtime_ns,before)
        self.assertIn('尚无历史观测',trend.show(self.config,start=NOW,end=NOW+timedelta(days=1),style="lines"))

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
            database=self.root/'var/lib/disk-usage-trend/history.sqlite3'
            with contextlib.closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE preserved(value INTEGER)')
                db.execute('INSERT INTO preserved VALUES (42)');db.commit()
            old=self.root/'usr/local/libexec/disk-usage-trend.py'
            old.write_text('previous version')
            backup=installer.install(self.root)
        self.assertEqual(hook.read_text(),'original banner')
        self.assertEqual(json.loads(config.read_text())['default_days'],30)
        self.assertEqual((backup/'usr/local/libexec/disk-usage-trend.py').read_text(),'previous version')
        with contextlib.closing(sqlite3.connect(backup/'history.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT value FROM preserved').fetchone()[0],42)
        self.assertTrue(os.access(self.root/'etc/update-motd.d/99-z-disk-trends',os.X_OK))


if __name__=='__main__':
    unittest.main()
