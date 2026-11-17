# 登录时显示磁盘 Top 5 历史曲线

`disk_usage_trend` 读取现有 `get_user_topk` 文本缓存，将每次更新存为 SQLite
快照，再在终端绘制用量曲线。默认展示过去 14 天，纵轴为以 2 为底的对数，
例如 256 GiB、512 GiB、1 TiB 的刻度等距。仅依赖 Linux 的 Python 3.8+ 标准库。

## 安装

在每台需要显示本机 `/home` 历史的服务器上执行：

```sh
sudo /usr/bin/python3 -I /path/to/useful_scripts/disk-trend/install.py
```

安装程序会保存被替换文件及清单到 `/var/backups/disk-usage-trend/<timestamp>/`。
它保留既有 `/etc/update-motd.d/99-custom-message`、磁盘扫描定时任务及缓存文件；
若 `/etc/disk-usage-trend.json` 已存在，也保留管理员对该文件的修改。

新增内容：

- `/usr/local/libexec/disk-usage-trend.py`：root 所有的程序副本。
- `/usr/local/bin/disk_usage_trend`：使用 `/usr/bin/python3 -I` 的启动器。
- `/etc/disk-usage-trend.json`：默认时间段、历史保留天数、三个缓存来源。
- `/etc/update-motd.d/99-z-disk-trends`：在原有登录提示之后显示曲线。
- `disk-usage-trend.service` / `disk-usage-trend.timer`：每小时采集缓存。
- `/var/lib/disk-usage-trend/history.sqlite3`：每台服务器本机的历史数据库。

登录提示只读取本机数据库，不扫描磁盘，也不等待 NFS。
每台服务器分别保留本机 `/home` 历史；两个共享存储的历史在各节点中各存一份，
避免通过 NFS 共享 SQLite 数据库锁。采集只读三个很小的文本报告，
不会重复执行昂贵的磁盘用量扫描。

需要 Python 3、systemd，以及正在使用 update-motd 的登录配置。
程序更新后重新执行安装步骤，系统副本不会随用户目录脚本更新而自动改变。

## 查看和设置时间段

```sh
disk_usage_trend show                             # 默认过去两周
disk_usage_trend show --days 30                    # 过去30天
disk_usage_trend show --since 2026-11-01 --until 2026-11-15
disk_usage_trend show --source home --width 100 --height 15
disk_usage_trend show --source storage2 --color always
```

日期按服务器本地时区解释；结束日期包含当天。也支持带时区的 ISO 时间。
`--source` 可选 `home`、`storage`、`storage2`。若要修改所有用户登录时的默认时间段，
编辑 `/etc/disk-usage-trend.json` 中的 `default_days`，无需重启服务。
历史默认保留 365 天，可调整 `retention_days`（14–3650 天）。

## 数据含义与限制

- 历史从首次采集现有缓存开始，无法恢复部署前不存在的时间序列。
  一个真实快照仅显示点，并明确提示尚无足够历史形成曲线。
- 横轴使用原缓存的文件修改时间。每小时轮询同一缓存不会生成伪造的新数据点。
  展示最新观测时间，便于发现原扫描任务没有更新。
- 每张图跟踪所选时间段最后一次快照中的 Top 5 目录。
  用户此前未上榜时无法从 Top 5 缓存知道其用量，相关日期留空；
  零值不进入对数坐标；相邻快照超过 48 小时也不连线。
- 用量继承原文本报告的精度，例如 `1.0 TiB` 已有四舍五入；并非新增逐字节统计。
  统计对象沿用原报告中的目录，可能包括备份等非用户账号目录。
- 曲线用 `1`–`5` 对应图例标识；`*` 表示同一个终端字符格中有多条曲线重叠。
- 正在写入、不完整、符号链接或异常类型的报告会跳过，不覆盖已有历史。

## 验证、预览和撤回

```sh
python3 -m unittest discover -s tests -v
python3 disk-trend/install.py --destdir /tmp/disk-trend-install-preview
sudo systemctl status disk-usage-trend.timer
sudo journalctl -u disk-usage-trend.service --no-pager -n 20
disk_usage_trend show
sudo /etc/update-motd.d/99-z-disk-trends
```

`--destdir` 仅生成安装目录树，不执行 systemctl，不修改系统登录提示。
撤回新增功能时停用 `disk-usage-trend.timer`，删除新增的
`/etc/update-motd.d/99-z-disk-trends`，即可停止采集和登录显示。
其余新增文件可按安装备份清单移除；若清单记录了被替换文件，应恢复对应备份。
数据库可保留以便以后恢复；原有磁盘信息、Top 5 报告和扫描任务始终保留。
