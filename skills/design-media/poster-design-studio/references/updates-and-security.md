# 更新与安全

本文说明带有自动更新模块 `scripts/auto_update.py` 的本 Skill。SkillHub、魔搭、WorkBuddy 的国内包使用这份说明。`scripts/mcp_client.py` 能导入该文件，且 `~/.beatra/updates/<id>/state.json` 的 `auto_update` 不是精确的 JSON `false` 时，普通命令开始前尝试自动更新。文件缺失、语法错误，或导入时抛出异常，客户端不改安装目录里的文件，改为下面的版本提示。程序按“文件能否导入”和这个开关决定，不按商店名字分支。

## 运行的是什么

一次操作运行随包脚本，只用 Python 标准库：

- `scripts/mcp_client.py`：调用 Beatra，并在普通命令前安排自动更新或版本提示。
- `scripts/auto_update.py`：检查、下载并替换本包自己的文件。客户端导入它，日常命令不单独启动它。
- `scripts/authorize.py`：取得这一份安装的共享凭据，不在本机监听回调。批准仍需要时，用这台电脑的浏览器打开 Beatra 批准页。
- `scripts/uninstall.py`：按“是不是这台电脑上最后一份 Beatra Skill”处理共享连接。它不删除自己所在的安装目录。

## 访问哪些主机

从 SkillHub、魔搭、WorkBuddy 安装的当前版本固定使用中国大陆网络，只连接下面四个 Beatra 主机，不会切换到国际网络（上传文件另见下一段）：

- `mcp.beatra.cn`：MCP。
- `api.beatra.cn`：API。
- `www.beatra.cn`：官网，读取版本信息。
- `cdn.beatra.cn`：下载发布包和清单。

更早从 SkillHub 或魔搭安装、至今仍按旧渠道更新的版本，网络路线是自动的：先用这台电脑已经选定的一组 Beatra 主机，这一组失败，再换另一组试一次。只有读取版本信息和下载更新的请求会这样重试。

你选择上传的文件，只以 PUT 发到 Beatra 为这次上传签发的一次性地址。该地址必须是 https、带主机名，不能带用户名、密码或片段；方法必须是 PUT；`Content-Type` 与 `Content-Length` 必须与文件一致。主机名不要求是上面列出的 Beatra 主机。上传拒绝 HTTP 重定向。

## 自动更新怎么做

开关开着时，普通命令开始前最多每 24 小时做一次自动检查。更新锁还在、且未超过 10 分钟时，这次自动更新跳过，命令照常执行，也不会改走版本提示。检查或替换失败同样不打断命令。

自动检查读取打包时写进本包的发现地址，并把主机换成当前路线的官网主机。发现文件最大 64 KiB，超时 3 秒。它必须声明本包的标识、渠道、语言和版本。其中的发布基址必须是 https，主机必须落在本包路线表的 CDN 主机里。检查这一步只核对该地址，不从 CDN 取字节。发现的版本低于当前已安装版本时，检查拒绝，不会降级。

有新版本时，若还没有记录本包拥有哪些文件，先从 CDN 下载当前版本的 `skill-manifest.json`（最大 1 MiB，超时 60 秒），只把磁盘内容与清单 SHA-256 一致的文件记入拥有列表。然后下载新清单和新的 zip 归档。归档最大 50 MiB，超时 60 秒。清单和归档的 SHA-256 必须与发现文件一致。清单最多 512 个文件，单文件最大 16 MiB，合计最大 64 MiB。路径必须以本包标识加斜杠开头，不能含 `..`，也不能在大小写折叠后重复。归档里多出来的成员、目录项、符号链接和加密项会被拒绝。每个文件的字节数和 SHA-256 都与清单一致之后，才写入安装目录。下载拒绝 HTTP 重定向，也拒绝超过上述大小的响应。

写入的是新清单里的文件。上一版记录过、新清单里没有、且磁盘内容仍等于所记录 SHA-256 的文件会被删除。你另外放进安装目录、又不占据新清单路径的文件保持原样。若新清单的某个路径上已经有一份不属于上一版记录的文件，这次更新拒绝替换并停下来，该文件不会被改写。

替换开始前写下事务日志和备份。替换中断后，下次自动检查或手动 `update` 若发现事务目录还在，就按日志恢复到替换前（更新锁未满 10 分钟时这一步跳过，留给之后的运行）。日志损坏时恢复失败，事务目录留在原地：普通命令照常继续，手动 `update` 报错停下。

手动 `python3 scripts/mcp_client.py update` 会立刻安装新版本。它不受 24 小时限制，也不看自动更新开关。`python3 scripts/mcp_client.py update --check` 只读取发现文件并打印结果，不下载归档，不改安装目录里的文件。

## 本地写什么

每份安装对应 `~/.beatra/updates/<id>/`。`<id>` 是解析后的安装路径的 SHA-256 的前 24 个十六进制字符。在 POSIX 上该目录模式为 0700。

更新器在这里写入：

- `state.json`（POSIX 0600）：`schema_version`、`auto_update`、`last_checked_at`、`installed_version`、`owned_files`。文件缺失或损坏时，读到的状态只有 `schema_version`，开关视为开。
- `update.lock`（POSIX 0600）：一次更新进行期间存在。
- `transaction/journal.json`（POSIX 0600）和 `transaction/backup/`：替换进行期间存在。备份保存替换前的文件内容。

同一目录里，客户端还写入 `registration.json`（安装登记缓存，24 小时内同一内容不重复发送）和 `source.json`（这份安装来自哪里）。自动更新和版本提示共用 `state.json` 的 `last_checked_at`。更新器不修改 `~/.beatra/` 中上述文件以外的内容。

连接仍使用这些名字：`credentials.json`、`installation.json`、`host.json`、`pending_authorization.json` 由 `scripts/authorize.py` 写入；`network.json`、`skills.json`、`balance-holds.json` 由 `scripts/mcp_client.py` 写入（`skills.json` 在授权时也会写）。

替换进安装目录的新文件，路径上原来没有文件时用 POSIX 0644；原来有文件时沿用原来的权限位。

## 开关与手动操作

在安装目录中运行：

    python3 scripts/mcp_client.py update --auto off

客户端打印 `Automatic Beatra package updates are disabled.` 此后普通命令不再自动更新。

    python3 scripts/mcp_client.py update --auto on

客户端打印 `Automatic Beatra package updates are enabled.` 状态缺失、读不出来，或其他值，都视为开。只有 JSON `false` 是关。

    python3 scripts/mcp_client.py update

有新版本时立刻安装，并打印 `Beatra package updated to <version>; the next process will use it.` 已是当前版本时打印 `Beatra package is current at <version>.`

    python3 scripts/mcp_client.py update --check

只检查。有新版本时打印 `Beatra package update available: <current> -> <available>.` 没有新版本时打印当前版本那一句。

`--check` 和 `--auto` 同时使用时，客户端报 `Use either --check or --auto, not both`。

安装目录里没有可用的 `scripts/auto_update.py` 时，没有 `--auto` 这个参数。`update` 和 `update --check` 都只打印下面的版本报告，不安装。

## 关闭后的版本提示

开关关上，或更新器文件不可用时，普通命令开始前最多每 24 小时读取一次本包的公开版本清单。清单位于发现地址同一官网主机上的 `/skills/<package>/sources.json`，最大 64 KiB，超时 3 秒。客户端只根据本机记录判断这份拷贝来自哪里。该来源有更新的版本时，向标准错误打印一行：`Beatra notice: <package> <version> is available from <store> (installed: <installed>). Update with: <command>`。这一步不安装文件。读取失败时什么都不打印，命令照常运行。认不出安装来源时不提示。

判断按下面的顺序，命中一行就停止。下表的 `<package>` 是本包标识；第 2 行在 ClawHub 记录的 slug 符合包名形式（小写字母、数字、连字符，长度 1 至 64）时改用该 slug。ClawHub 记录先看 `.clawhub/origin.json`，读不到或解析不了再看 `.clawdhub/origin.json`。读到的文件版本不是 1，或 `registry`、`slug`、`installedVersion` 不是非空字符串，或 `installedAt` 不是有限数字时，不采用 ClawHub 记录，继续往后看。`github-repo` 只认 `SKILL.md` 开头 16 KiB 里、前后有 `---` 的前言中一行带缩进的 `github-repo:`。

| 顺序 | 本机记录 | 打印的命令 |
| --- | --- | --- |
| 1 | 上述 ClawHub 记录的 `sourceKind` 为 `skills-sh` | 不提示 |
| 2 | 有上述 ClawHub 记录，且 `sourceKind` 不是 `skills-sh` | `clawhub update <package> --workdir <安装目录向上第 2 级> --dir <安装目录的父目录名>`，并附 `add --force if it reports local changes` |
| 3 | 安装目录的父目录有 `.skills_store_lock.json`，其 `version` 至少为 1，且 `skills` 含本包 | `skillhub install <package> --force --dir <该父目录>` |
| 4 | `SKILL.md` 前言含 `github-repo` | `gh skill update <package>`。安装路径向上第 3 级目录存在、且不是用户主目录时，再附 `(run in <该目录>)` |
| 5 | 从安装目录向上第 2 级到第 5 级，由近及远第一份 `version` 至少为 1 且 `skills` 含本包的 `skills-lock.json`，来源以 `beatra-ai/` 开头 | `npx skills update <package> -p -y (run in <锁所在目录>)` |
| 6 | 同一份锁的来源为 `modelscope.cn` | `npx skills update <package> -p -y (run in <锁所在目录>)`。这是 skills CLI 的锁 |
| 7 | 同一份锁的来源不是上两种 | 不提示 |
| 8 | 安装路径向上第 3 级就是用户主目录，且 `~/.agents/.skill-lock.json` 的 `version` 至少为 3、`skills` 含本包，来源以 `beatra-ai/` 开头或为 `modelscope.cn` | `npx skills update <package> -g -y` |
| 9 | 同一把全局锁满足版本和本包这两条，但来源不是上两种 | 不提示，也不再看第 10、11 行 |
| 10 | 安装目录的 `install-source.json` 版本为 1，且 `source` 为 `workbuddy` | `open this Skill in WorkBuddy and update it there` |
| 11 | 同一份 `install-source.json` 的 `source` 为 `website`，且本包渠道为 `canonical` | `reinstall from <安装页>`。安装页是发现地址以 `/install.json` 结尾时，把这段换成 `install.md`；否则命令里用发现地址本身。渠道不是 `canonical` 时，这一行不成立 |
| 12 | 以上都没有 | 不提示 |

带有 `scripts/auto_update.py` 且开关开着的拷贝走自动更新，不打印上表。魔搭商店的国内包属于这一类。`modelscope.cn` 那一行只在更新器不可用或开关已关、并且本机有 skills CLI 锁时出现。

## 上报什么

本 Skill 主动发出的报告是安装登记，见 [安装登记](installation-registration.md)。登记包含包标识与版本、渠道与语言、网络路线、安装来源，以及粗略运行环境：操作系统、系统版本、架构、Python 版本、区域、时区偏移分钟。不含用户名、路径、环境变量或地址。同一内容在 24 小时内不重复发送。发送失败不打断随后的命令。

建立 MCP 会话时，`initialize` 带上客户端名字 `beatra-skill-<package>` 和包版本。

登记之前，客户端把本包写入 `~/.beatra/skills.json`（标识、代理环境、安装路径、时间），供卸载判断这台电脑上是否还有别的 Beatra Skill。这是本机文件。

## 卸载

运行 `scripts/uninstall.py`。它不删除自己所在的安装目录，把该目录放进结果的 `delete_next`，由调用方最后删除。技能清单不能证明这是最后一份 Beatra Skill 时，`~/.beatra/` 保持不动。能证明时，若本机有凭据，先请求撤销。服务端返回的状态不是 200 也不是 401 时，本地文件一律不改，以便稍后再运行。其余情况删除这些名字：`credentials.json`、`installation.json`、`host.json`、`skills.json`、`registrations.json`、`network.json`、`balance-holds.json`、`pending_authorization.json`、`.authorize.lock`。随后删除 `~/.beatra/updates/` 下每个安装目录里的 `state.json`、`update.lock`、`registration.json`、`source.json`。这些安装目录、`~/.beatra/updates/` 和 `~/.beatra/` 都只在已经为空时删除，所以替换中断留下的 `transaction/` 会保留。
