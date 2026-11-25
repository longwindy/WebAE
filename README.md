# WebAE：在网页上监工你的 AE 网络

这是一个专为整合包GTNH设计的系统，游戏内使用OC进行连接。你可以在浏览器中查看你的ae合成情况与库存，也可以下单让你在摸鱼的ae动起来。


## 一、部署后端

部署地址要求 **游戏服务器能访问到它**，游戏里 OC 电脑的因特网卡要把数据发过来，所以需要一个从游戏服务器能访问的地址。用网页的人也通过该地址访问。


### 1. 安装

需要 [git](https://git-scm.com/) 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/longwindy/WebAE.git
cd WebAE/backend
uv sync
```

### 2. 配置

把 `backend/config.example.toml` 复制一份，命名为 `config.toml`，按需修改：

| 选项 | 默认值 | 说明 |
|---|---|---|
| `host` | `127.0.0.1` | 监听地址。前面有反向代理或穿透工具时保持默认，只有直接对外开放端口时才改成 `0.0.0.0` |
| `port` | `8765` | 监听端口 |
| `data_dir` | `data` | 数据库与资源素材位置，相对路径以 `backend/` 为准 |
| `max_order_amount` | `2147483647` | 单次下单的物品数量上限 |
| `max_order_fluid_amount` | `2147483647` | 单次下单的流体数量上限（L） |

### 3. 创建网络和管理员

在 `backend/` 下运行：
```bash
uv run python manage.py net add MyAE                   # 此处会打印 OC 令牌
uv run python manage.py user add YourName --admin      # 此处会打印网页登录密钥
```
令牌和密钥**只显示这一次**，请妥善保管。更多用法见下一节"用户和网络"。

### 4. 启动

```bash
uv run python app.py
```
终端里出现 `Uvicorn running on http://127.0.0.1:8765` 就说明启动成功了。保持它运行，另开一个终端，在同一台机器上检查：
```bash
curl http://127.0.0.1:8765/healthz
```
返回 `{"ok":true}` 说明后端正常。Windows PowerShell 里要写成 `curl.exe`。

网络数据保存在 `data/networks/<网络名>/`。

### 5. 对外开放访问

后端默认只监听本机，对外开放请按实际网络情况选择：

| 情况 | 做法 |
|---|---|
| **有公网 IP**（云服务器、VPS） | 用 [Caddy](https://caddyserver.com/) 或 Nginx 做反向代理，配上自己的域名。Caddy 会自动申请 HTTPS 证书，配置示例见 [`deploy/Caddyfile`](deploy/Caddyfile) |
| **没有公网 IP**（家里的电脑、内网服务器） | 用内网穿透：Cloudflare Tunnel（需要自己的域名）、frp 或其他穿透服务 |

完成后，在浏览器里打开 `https://你的地址/healthz`，返回 `{"ok":true}`，再确认登录页能打开，然后继续配置 OC。

### 6. 保持运行

- **Linux**：用 systemd 管理，开机自启，崩溃后自动重启。服务文件见 [`deploy/aeweb.service`](deploy/aeweb.service)。在一台全新 Ubuntu 服务器上从零部署的完整步骤（包括 Caddy 和 HTTPS）见 [`deploy/README.md`](deploy/README.md)。
- **Windows**：可以用"任务计划程序"设置开机运行，或者用 [NSSM](https://nssm.cc/) 注册成服务。只是在终端里运行的话，关掉窗口后端就停了。

### 7. 更新与迁移

**更新到新版本**：
1. 在仓库目录下拉取新版本，并同步依赖：
   ```bash
   git pull
   cd backend
   uv sync
   ```
2. 重启后端。
3. 在游戏里的 OC 电脑上重新下载脚本，然后重新运行 `aeweb`（下载命令见第三节"更新脚本"）。

账号、库存、订单和配置都不会受影响。

**换一台机器运行**：需要带走的只有 `backend/config.toml` 和 `backend/data/` 目录。在新机器上按上面第 1 步装好代码，把这两样复制到新机器的 `backend/` 下，再启动就行。如果 OC 里填的是域名，把域名改成指向新机器即可，OC 不用改；如果填的是 IP 地址，就要在 OC 的 `aeweb.cfg` 里改成新地址。

## 二、用户和网络

一个后端可以同时接入**多套 AE 网络**，也可以让**多人**使用。

- **网络**：一套 AE 加上接在它上面的一台 OC 电脑。每个网络有自己的 OC 令牌。
- **用户**：一个用户仅一个**登录密钥**，网页登录时只需要输入密钥。
- 用户在每个网络里有一个角色：

  | 角色 | 查看 | 下单 | 撤回订单 | 取消 CPU 上的任务 |
  |---|---|---|---|---|
  | `viewer` 只读 | ✓ | | | |
  | `member` 成员 | ✓ | ✓ | 只能撤回自己的 | |
  | `owner` 所有者 | ✓ | ✓ | 所有人的 | ✓ |

  管理员（`--admin`）在所有网络里都按所有者对待。只有所有者能取消 CPU 任务。

所有管理都在运行后端的机器上用 `manage.py` 完成（在 `backend/` 下运行 `uv run python manage.py ...`），**以下修改无需重启后端**：

```
net add <网络名>                  添加网络，打印 OC 令牌
net list                         列出网络和成员
net token <网络名>                换一个新的 OC 令牌（旧的立即失效）
net rename <网络名> <新网络名>     改名，数据和成员授权自动跟过去，OC 令牌不变
net remove <网络名>               删除网络（数据文件夹保留）

user add <名字> [--admin]        添加用户，打印登录密钥
user list
user reset <名字>                换一个新的密钥，旧密钥和所有已登录的设备立即失效
user admin <名字> on|off
user remove <名字>

grant <用户> <网络> owner|member|viewer
ungrant <用户> <网络>
```
网络名和用户名都可以用中文，最长 32 个字符，不能有空格和 `/ \ : * ? " < > |`。网络名同时也是它数据文件夹的名字。

例子：Alice 有一套自己的 AE；Bob 负责一套公共 AE，Alice 也要用。
```bash
# 两套 AE：把各自打印的令牌填进对应那台 OC 的 aeweb.cfg
uv run python manage.py net add AliceAE
uv run python manage.py net add SharedAE

# 两个用户：把各自打印的密钥发给本人
uv run python manage.py user add Alice
uv run python manage.py user add Bob

# 授权
uv run python manage.py grant Alice AliceAE owner    # Alice 管理自己的 AE
uv run python manage.py grant Bob SharedAE owner      # Bob 管理公共 AE
uv run python manage.py grant Alice SharedAE member   # Alice 可以在公共 AE 下单
```
结果：Alice 登录后能在 AliceAE 和 SharedAE 之间切换，Bob 只能看到 SharedAE。

**令牌和密钥只在生成时显示一次**。如果遗失或泄露，请用 `net token` / `user reset` 重新生成。账号数据在 `data/accounts.json`。

## 三、游戏内的 OC 电脑

**硬件**：
- 机箱：T2及以上
- CPU: T1即可，建议T2及以上
- 内存：建议 2 根 T3.5
- 硬盘：装好 OpenOS
- 因特网卡
- 适配器：贴着 **ME 控制器**或**方块形式的 ME 接口**放
- 合成 CPU 中需要有**合成监控器**，OC依此读取合成目标
- 区块须要加载

**安装脚本**（后端提供了脚本下载）：
```
wget https://你的地址/oc/aeweb.lua /home/aeweb.lua
aeweb
```
第一次运行会生成配置文件 `/home/aeweb.cfg`，然后退出。编辑它：
```
edit /home/aeweb.cfg
```
填好两项（编辑器里 Ctrl+S 保存，Ctrl+W 退出）：
- `BASE_URL`：你的服务器地址
- `TOKEN`：这个网络的 OC 令牌（`manage.py net add` 后展示的即为OC令牌）

再运行 `aeweb`，正常情况下会打印：
```
[  237.2s] ae-web uploader started, target https://...
[  237.2s] memory: 1617 KB free of 2048 KB
[  244.9s] inventory: 3853 entries in 6.2s
```
用 **Ctrl+Alt+C** 停止。可执行 `echo aeweb >> /home/.shrc` 来启用开机自启。

**更新脚本**：只需要 `wget -f https://你的地址/oc/aeweb.lua /home/aeweb.lua`。

想用别的配置文件，可以运行 `aeweb /路径/xxx.cfg`。

**可调参数**（在 `aeweb.cfg` 里取消注释后修改）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `STATUS_INTERVAL` | 5 | 能量和 CPU 的上报间隔（秒） |
| `ITEMS_INTERVAL` | 60 | 完整库存快照的间隔（秒）。扫描耗时较长时可以调大 |
| `CHUNK_LINES` | 500 | 库存上传时每个包的行数 |
| `CPU_ITEM_LIMIT` | 300 | 每个 CPU 的"现存 / 正在合成 / 计划合成"列表各最多上报多少条 |
| `CPU_CALL_BUDGET` | 60 | 每轮上报最多花多少次 AE 调用（约等于 tick 数）刷新忙碌 CPU 的详情，见下方说明 |
| `CPU_FINAL_REFRESH` | 60 | 忙碌 CPU 的合成目标多久重新读取一次（秒） |
| `HTTP_TIMEOUT` | 20 | 单次请求超时（秒） |
| `MAX_ITEM_ID` / `ID_BATCH` | 32767 / 512 | 库存扫描的物品 ID 上限 / 每批读取的 ID 数 |
| `NBT_LIMIT` | 1024 | 上报物品 NBT 的大小上限（字节，压缩后），用来识别改过名的物品；0 为关闭 |

**合成 CPU 详情的刷新方式**：OC 每调用一次 AE 接口，都要等1 tick。每轮上报都会读一次 CPU 列表，然后最多花 `CPU_CALL_BUDGET` 次调用，按"最久没刷新的优先"刷新忙碌 CPU 的详情（现存、正在合成、计划合成，每个 CPU 3 次调用）。

说人话就是：
- 同时忙碌的 CPU 不多时，每轮全部刷新（5 秒）；
- CPU 很多时轮流刷新，比如 64 个同时忙碌时，每个 CPU 大约 15~20 秒刷新一次。

无论哪种情况，状态上报和下单都不会变慢。

## 四、你在网页上能看到什么

- **能量**：AE 储能、平均消耗、平均输入、待机消耗
- **合成 CPU**：布局和游戏内的合成监控器类似。
  - **左侧**：所有 CPU，显示合成目标、容量、协处理器数和已合成时间。
  - **右侧**：选中 CPU 的每种物品，显示"现存 / 正在合成 / 计划合成"的数量。正在合成的格子是绿色，计划合成的是黄色。可以搜索（支持拼音），也可以取消任务。
  - **已合成时间**：OC 不提供这个数据，是从后端**第一次看到 CPU 进入忙碌**时开始计时的，误差约一个上报间隔。如果开始时间没被观察到（比如后端重启时任务已经在跑），时间前面会标"≥"，表示实际时间更长。
  - **合成目标**：需要 CPU 里有合成监控器，没有时显示"目标未知"。
- **库存**：物品和流体，可以搜索中文名、拼音、英文名或注册名，可按数量或名称排序，按"物品 / 流体 / 可合成"筛选。
- 页面顶部显示上一次状态上报和库存快照的时间。若超时变黄或变红，请检查游戏内 OC 程序状态。

## 五、汉化

请在游戏中使用NEI导出功能，由于 NEI 导出的是**物品面板当前显示的列表**，所以导出前需要先：
- **清空 NEI 的搜索框**；
- **展开所有折叠的物品组**：在 NEI 选项里关闭"可折叠物品"。打开背包左下角 `NEI设置` -> `物品栏` -> `可折叠的物品` -> `禁用`

1. **导出中文版**（客户端语言为简体中文时）：
   - 打开背包，点左下角的**NEI选项**；
   - 进入 **工具（Tools）→ 导出数据（Data Dumps）**；
   - 把 **物品面板（Item Panel）** 一行的模式切换到 **CSV**，点 **导出（Dump）**；
   - 生成的文件在游戏目录的 `dumps/itempanel.csv`。把它复制到 `backend/data/i18n/`，**重命名为 `itempanel_zh.csv`**。
2. **导出英文版**（客户端语言为简体英文时）：
   - 把游戏语言切换成 English (US)，按同样的步骤再导出一次；
   - 复制到同一个目录，**重命名为 `itempanel_en.csv`**；

后端会自动检测文件变化并重新加载。库存标题后面会显示"汉化 N 条（含中英对照 M 条）"。

匹配规则，按顺序：
1. 按 `注册名:meta` 匹配；
2. 同一个 meta 有多个 NBT 变体（名字不同）时，按英文名查中英对照表；
3. 只有一个 meta 的物品（工具、装备，它们的 meta 是耐久值），用这一条的名字；
4. GTNH 在 NEI 里隐藏了 GT 的小撮粉、小堆粉、含杂粉、纯净粉，以及粉碎、洗净、离心矿石。这些物品按材料名推导中文名，例如"小撮碳粉""粉碎的金矿石"。
   - 小撮、小堆：100% 一致；
   - 粉碎、洗净、离心：约 70%~90% 一致，不一致的多是"的"字和"矿矿石"这类写法差异；
   - 含杂、纯净：没有可以对照的样本，是参照 GT++ 的命名推出来的。
5. 均无法匹配时，显示英文原名。

**更新整合包或加 mod 均需重新导出。**

## 六、物品图标

图标来自 NEI 物品面板的 **PNG 导出**。

1. 准备：游戏语言为中文，NEI 搜索框清空，在 NEI 选项里关闭"可折叠物品"。
2. 在 **NEI选项 → 工具（Tools） → 数据导出（Data Dumps） → 物品面板（Item Panel）**：
   - 切用 **CSV** 模式导出一次；
   - 切到 **PNG** 模式，选 **32x**，再导出一次。
3. 导入：NEI 会把文件导出到游戏实例目录下的 `dumps` 文件夹（和 `mods`、`config` 同级）。在仓库根目录运行：
   ```powershell
   cd backend
   uv run python import_icons.py "<游戏实例目录>\dumps"
   ```

**更新整合包或加 mod 均需重新导出。**

## 七、下单合成

可合成项会在库存中以蓝色"可合成"标记标识，点击后弹出合成窗口：
- 填数量：可以直接输入，也可以用 ±1/10/100/1000 按钮调整。流体的单位是 L（mB），默认 1000 L；
- 选合成 CPU：自动，或者指定一个有名字的 CPU；
- 点**开始合成**。

**需要注意：** OC 接口无法**预览合成计划**。

订单会以卡片形式出现在"历史订单"里，状态依次是：排队中 → 已发送 → 计算中 → 合成中 → 完成。

每个网络只保留**最近 10 条已结束的订单**，更早的会从 `data/networks/<网络名>/commands.json` 里删除；进行中的订单不受限制。网页上的"取消 CPU 任务"不算订单，不出现在历史订单里。

合成中的 CPU 卡片上有**取消任务**按钮（只有这个网络的所有者能看到），同游戏内合成状态界面里的取消按钮。

工作方式：
- 网页下单后，指令先放进后端队列。OC 每次上报状态（默认 5 秒一次）时领取指令，结果在下一次上报时带回。
- 每条指令只会执行一次。OC 在调用下单接口**之前**，会先把指令 ID 记进 `/home/aeweb.handled`。即使网络重发或 OC 重启，同一条指令也不会执行第二次。
- 指令在队列里只会存在5分钟，超时将作废，而未被 OC 领取的订单可以被**撤回**。
- OC 超过 1 分钟没有上报时，网页拒绝下单。
- 单次下单数量上限：物品由 `config.toml` 里的 `max_order_amount` 决定，流体由 `max_order_fluid_amount` 决定，默认都是 2147483647（约 2.1g）。



## 已知限制

- 物品数量取自 AE 转出来的物品堆。**超过约 21 亿的数量可能被截断**，还需要实测确认。流体不受影响。
- 只能指定**有名字**的合成 CPU。没命名的 CPU 只能通过"自动选择"使用；如果想指定，先在铁砧里给一个合成存储器或合成单元改名，再把它放进 CPU 结构里。CPU 会使用这个名字。
- 库存按**物品数字 ID 分批**读取（`getItemsInNetworkById`，默认每批 512 个 ID，扫描 0~32767）。每批都会让 AE 生成一次物品列表，网络非常大时可以把 `ITEMS_INTERVAL` 调大。
  - **不可使用 OC 的 `allItems()`**。它的迭代器会注册为 AE 的变化监听，在 AE 处理物品变动的过程中重建整个物品列表，会破坏 AE 内部的数据结构，导致服务器崩溃（`ArrayIndexOutOfBoundsException` at `ItemList.putItemRecord`）。
  - 一批数据太大、内存放不下时，会自动对半拆开重试。如果单个 ID 下的物品都放不下，就跳过这个 ID，日志里会写 `skipped item IDs`。遇到这种情况，可以把服务端 `config/OpenComputers.cfg` 里的 `allowItemStackNBTTags` 改成 `false`，让物品数据不再附带 NBT。
- 汉化只覆盖 NEI 里能看到的物品。NEI 中隐藏的物品，以及没有对应英文名的 NBT 变体，会显示英文原名。按名称排序时按字符编码排序，不是按拼音。

## 安全

- OC 令牌写在游戏里那台 OC 电脑上，请妥善保管该电脑。若令牌泄露，可用 `manage.py net token <网络名>` 更换令牌。
- 若需弃用登录密钥，使用 `user remove` ，已登录设备会立即失效。
- `data/accounts.json` 里只有令牌和密钥的哈希，但也有签发登录 cookie 用的密钥，请妥善保管。

## 许可证

本项目以 [MIT 许可证](LICENSE) 发布。
