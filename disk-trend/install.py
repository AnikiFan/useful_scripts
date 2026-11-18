#!/usr/bin/env python3
"""Install the reviewed disk trend package; use --destdir for a staging test."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile


CONFIG = {
    'database': '/var/lib/disk-usage-trend/history.sqlite3',
    'default_days': 14,
    'retention_days': 365,
    'sources': [
        {'id': 'home', 'label': '/home（本机）', 'report': '/var/cache/home_topk_user'},
        {'id': 'storage', 'label': '/mnt/storage/users（共享）',
         'report': '/mnt/storage2/users/xfan_data/storage_topk_user'},
        {'id': 'storage2', 'label': '/mnt/storage2/users（共享）',
         'report': '/mnt/storage2/users/xfan_data/storage2_topk_user'},
    ],
}
WRAPPER = '''#!/bin/sh
exec /usr/bin/python3 -I /usr/local/libexec/disk-usage-trend.py "$@"
'''
MOTD = '''#!/bin/sh
# Only reads the local SQLite history. Never scans disks during login.
if grep -qx '# disk-usage-trend: interleaved-v1' /etc/update-motd.d/99-custom-message 2>/dev/null; then
    exit 0
fi
/usr/local/bin/disk_usage_trend show --width 80 --height 9 --color always --quiet || :
'''
SERVICE = '''[Unit]
Description=Record existing disk Top 5 cache snapshots

[Service]
Type=oneshot
ExecStart=/usr/local/bin/disk_usage_trend collect
TimeoutStartSec=45
StateDirectory=disk-usage-trend
StateDirectoryMode=0755
UMask=0022
Nice=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/disk-usage-trend
RestrictAddressFamilies=AF_UNIX
'''
TIMER = '''[Unit]
Description=Collect disk Top 5 history every hour

[Timer]
OnCalendar=hourly
OnBootSec=2min
RandomizedDelaySec=120
Persistent=true
Unit=disk-usage-trend.service

[Install]
WantedBy=timers.target
'''


def interleave_hook(text, sources):
    """Insert a graph after each known report; refuse ambiguous partial layouts."""
    marker = '# disk-usage-trend: interleaved-v1'
    if marker in text.splitlines():
        for source in sources:
            if text.count(chart_command(source['id'])) != 1:
                raise ValueError('Managed MOTD layout was edited; inspect before updating')
        return text
    lines = text.splitlines(keepends=True)
    matches = {}
    for index, line in enumerate(lines):
        try:
            command = shlex.split(line, comments=True)
        except ValueError:
            continue
        for source in sources:
            if command == ['cat', source['report']]:
                if source['id'] in matches.values():
                    raise ValueError('Duplicate report in MOTD; inspect before updating')
                matches[index] = source['id']
    if not matches:
        return None
    if len(matches) != len(sources):
        raise ValueError('Only some reports matched MOTD; inspect before updating')
    output = []
    for index, line in enumerate(lines):
        output.append(line)
        if index in matches:
            if not line.endswith('\n'):
                output.append('\n')
            output.append(chart_command(matches[index]) + '\n')
    output.append(marker + '\n')
    return ''.join(output)


def chart_command(source_id):
    """Generate a safe source-specific graph command for a cached MOTD."""
    return ('/usr/local/bin/disk_usage_trend show --source ' + shlex.quote(source_id) +
            ' --width 80 --height 9 --color always --quiet || :')


def install(root, *, activate=False):
    """Back up replaced files, preserve existing config, and write atomically."""
    source = Path(__file__).resolve().parent.parent / 'disk_usage_trend'
    files = {
        'usr/local/libexec/disk-usage-trend.py': (source.read_bytes(), 0o644),
        'usr/local/bin/disk_usage_trend': (WRAPPER.encode(), 0o755),
        'etc/update-motd.d/99-z-disk-trends': (MOTD.encode(), 0o755),
        'etc/systemd/system/disk-usage-trend.service': (SERVICE.encode(), 0o644),
        'etc/systemd/system/disk-usage-trend.timer': (TIMER.encode(), 0o644),
    }
    config_name = 'etc/disk-usage-trend.json'
    config = CONFIG
    if not (root / config_name).exists():
        files[config_name] = ((json.dumps(CONFIG, ensure_ascii=False, indent=2)+'\n').encode(), 0o644)
    else:
        config = json.loads((root / config_name).read_text())
    original_hook = root / 'etc/update-motd.d/99-custom-message'
    if original_hook.exists():
        interleaved = interleave_hook(original_hook.read_text(), config['sources'])
        if interleaved is not None:
            files['etc/update-motd.d/99-custom-message'] = (interleaved.encode(), original_hook.stat().st_mode & 0o777)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup = root / 'var/backups/disk-usage-trend' / stamp
    backup.mkdir(parents=True, mode=0o700)
    manifest = {'replaced': [], 'new': []}
    for name, (data, mode) in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise RuntimeError(f'refusing symlink destination: {path}')
        if path.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
            manifest['replaced'].append(name)
        else:
            manifest['new'].append(name)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        try:
            temporary.chmod(mode)
            if os.geteuid() == 0:
                os.chown(temporary, 0, 0)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    (root / 'var/lib/disk-usage-trend').mkdir(parents=True, mode=0o755, exist_ok=True)
    if activate:
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'start', 'disk-usage-trend.service'], check=True)
        subprocess.run(['systemctl', 'enable', '--now', 'disk-usage-trend.timer'], check=True)
    print(f'Installed. Backup manifest: {backup / "manifest.json"}')
    print('Existing banner and reports preserved; matching reports now have inline graphs.')
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destdir', type=Path, help='Stage files under this directory; never activates systemd')
    parser.add_argument('--no-enable', action='store_true', help='Install without starting the collector or timer')
    args = parser.parse_args()
    root = args.destdir.resolve() if args.destdir else Path('/')
    if root == Path('/') and os.geteuid() != 0:
        parser.error('System installation needs sudo; use --destdir for an unprivileged staging test.')
    if root == Path('/') and args.destdir:
        parser.error('--destdir must not point to /')
    install(root, activate=args.destdir is None and not args.no_enable)


if __name__ == '__main__':
    main()
