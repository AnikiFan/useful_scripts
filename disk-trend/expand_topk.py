#!/usr/bin/env python3
"""Expand existing daily cache jobs to eight users without running a scan."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import subprocess

HOME = [('/home', '/var/cache/home_topk_user')]
SHARED = [('/mnt/storage/users', '/mnt/storage2/users/xfan_data/storage_topk_user'),
          ('/mnt/storage2/users', '/mnt/storage2/users/xfan_data/storage2_topk_user')]


def expand(text, targets):
    """Change only K in exactly one active, recognized command per target."""
    for path, report in targets:
        pattern = re.compile(r'(?P<prefix>(?:[^\s;|&]+/)?get_user_topk[ \t]+' + re.escape(path) +
                             r'[ \t]+)(?P<count>5|8)(?P<suffix>(?:[ \t]+>[ \t]*' + re.escape(report) +
                             r'|[ \t]*\|[ \t]*(?:sudo[ \t]+)?tee[ \t]+' + re.escape(report) + r'[ \t]+>[ \t]*/dev/null))(?=[ \t]*$)')
        matches = [(i, pattern.search(line)) for i, line in enumerate(text.splitlines(keepends=True))
                   if line.strip() and not line.lstrip().startswith('#')]
        matches = [(i, match) for i, match in matches if match is not None]
        if len(matches) != 1:
            raise ValueError('Expected exactly one known scan job for ' + path)
        i, match = matches[0]
        lines = text.splitlines(keepends=True)
        line = lines[i]
        lines[i] = line[:match.start('count')] + '8' + line[match.end('count'):]
        text = ''.join(lines)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Root is required to inspect and update the existing root crontab')
    targets = [('root', HOME)]
    if socket.gethostname() == 'gnode1':
        targets.append(('xfan', SHARED))
    plans = []
    for user, sources in targets:
        old = subprocess.run(['crontab','-u',user,'-l'],check=True,capture_output=True,text=True).stdout
        new = expand(old, sources)
        plans.append((user, old, new))
    if not args.apply:
        print(json.dumps({'validated_jobs':sum(len(s) for _,s in targets),'will_scan':False}))
        return
    backup = Path('/var/backups/disk-usage-trend') / ('top8-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
    backup.mkdir(mode=0o700,parents=True)
    for user, old, new in plans:
        (backup / (user + '.crontab')).write_text(old)
        current = subprocess.run(['crontab','-u',user,'-l'],check=True,capture_output=True,text=True).stdout
        if current != old:
            raise RuntimeError('Crontab changed after inspection; refusing to overwrite')
        if old != new:
            subprocess.run(['crontab','-u',user,'-'],input=new,text=True,check=True)
        actual = subprocess.run(['crontab','-u',user,'-l'],check=True,capture_output=True,text=True).stdout
        if actual != new:
            raise RuntimeError('Crontab read-back mismatch')
    print(json.dumps({'updated_jobs':sum(len(s) for _,s in targets),'backup':str(backup),'will_scan':False}))


if __name__ == '__main__':
    main()
