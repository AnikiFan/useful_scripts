# 登录时显示磁盘容量份额历史

`disk_usage_trend` 读取现有 `get_user_topk` 文本缓存，将每次更新保存为本机 SQLite
快照，在终端显示百分比堆叠面积图。默认过去 14 天、80 列、15 行绘图区，
纵轴固定 0–100%，以原报告的整盘总容量为分母。

每块磁盘只显示一个 19 行区块（默认 15 行绘图区）：标题给出总容量，右侧将图例、
用户名、GiB/TiB 用量和百分比合并为同一行。编号 1–8 为当前用户排名，9 为空余空间，
O 为 Others。当前 Top 5 就是其中 1–5；不再重复显示磁盘摘要、静态排行榜或底部图例。
底部保留最新统计时间和缺测说明，横轴为所选时间段。

## 份额口径

- 按最新快照中的排名单列用户/目录。单列前 8 名用户，加上 `9 空余` 共 9 个明确份额，
  其余为 `O Others`。旧缓存仅有 Top 5 时暂时显示五人，下一次 Top 8 统计后自动扩展。
- `O = 整盘总容量 - 已单列目录用量 - 可用空余空间`，包含未单列目录、系统占用、
  文件系统保留空间及原报告舍入差额，不能解释成一个用户。
- `9` 使用原报告“🆓 剩”的可用空间，与原始缓存一致。零值也可正常表示。
- 所有份额合计为 100%；不会把 Top 5 单独归一化成 100%。容量口径无法核对时不绘制
  该快照，仍提示原因并显示当前 Top 5 大小。
- 用户在旧快照中未被单列时，其未知用量归入 `O`，不能把当时未显示的个人份额理解为实际零用量。
- 用户颜色对应图例，`O` 为灰色，`9` 为白色。使用八分之一字符块显示面积边界；
  极小份额可能小于终端分辨率，准确读数以右侧百分比和图例大小为准。
- 用量和容量继承原报告的四舍五入精度。缓存统计对象是目录，可能包含非账号的备份目录。

## 安装或升级

在每台服务器上执行：

```sh
sudo /usr/bin/python3 -I /path/to/useful_scripts/disk-trend/install.py
# gnode：先核验现有任务，再备份并把 K=5 扩展到 K=8（不执行扫描）
sudo /usr/bin/python3 -I /path/to/useful_scripts/disk-trend/expand_topk.py
sudo /usr/bin/python3 -I /path/to/useful_scripts/disk-trend/expand_topk.py --apply
```

安装程序将被替换文件及 SQLite 数据库备份到
`/var/backups/disk-usage-trend/<timestamp>/`，保留管理员配置、登录说明和原有扫描计划。
它识别既有三组报告或旧版交错布局，再迁移为整合了容量和空余信息的份额图；自定义布局无法明确匹配时停止。
`expand_topk.py` 只修改每台 root 的 /home 任务及 gnode1 的 xfan 共享存储任务的 K 参数，
保留执行时间和其他任务，并将旧 crontab 保存到权限受限的备份目录。

系统文件：

- `/usr/local/libexec/disk-usage-trend.py` 与 `/usr/local/bin/disk_usage_trend`：root 所有的程序及启动器。
- `/etc/disk-usage-trend.json`：来源路径、默认时间范围和保留天数。
- `/etc/update-motd.d/99-custom-message`：保留登录说明，按来源展示整合后的份额图。
- `/etc/update-motd.d/99-z-disk-trends`：备用入口；检测到已迁移布局时自动静默。
- `disk-usage-trend.service` / `disk-usage-trend.timer`：每小时采集小型缓存。
- `/var/lib/disk-usage-trend/history.sqlite3`：本机历史，默认保留 365 天。

登录绘图及空余信息仅读取本机 SQLite；显式运行 `summary` 才读取原报告缓存。
不会在登录时扫描磁盘。各节点本地保留共享存储观测，避免通过 NFS 共享数据库锁。
需 Linux、Python 3.8+、systemd、update-motd；只使用 Python 标准库。
源码更新后需重新安装系统副本。

## 查看

```sh
disk_usage_trend show                            # 默认百分比堆叠图
disk_usage_trend show --days 30
disk_usage_trend show --since 2026-11-01 --until 2026-11-15
disk_usage_trend show --source home --width 100 --height 20
disk_usage_trend show --style lines              # 保留的旧版 log₂ 用量曲线
disk_usage_trend summary --source home           # 仅容量摘要
```

日期按本地时区解释，结束日期包含当天；也接受带时区的 ISO 时间。
来源为 `home`、`storage`、`storage2`。修改配置的 `default_days` 可调整登录默认范围。
百分比图至少需要足够行数放置全部份额标注，默认 15 行；窄图会自动减少时间刻度。

## 历史与验证

总容量、空余空间都来自同一份原报告，并沿用其修改时间。
升级只给内容哈希相同的已有快照补录真实缓存字段；不会把今天的容量倒填到旧日期。
缺少完整容量信息的日期留空，可用 `--style lines` 查看原有用量历史。
只有一个完整快照时显示一列，未来采样累积后形成堆叠面积；只在相邻完整快照间距
不超过 48 小时时连接，缺测不连接，不向最新观测之后外推。

```sh
python3 -m unittest discover -s tests -v
python3 disk-trend/install.py --destdir /tmp/disk-trend-install-preview
sudo systemctl status disk-usage-trend.timer
sudo journalctl -u disk-usage-trend.service --no-pager -n 20
sudo /etc/update-motd.d/99-custom-message
```

`--destdir` 仅生成安装目录树，不运行 systemctl。撤回时停用 timer，删除备用钩子，
并恢复备份的 `99-custom-message`；其他系统文件按备份清单恢复。数据库可保留。
