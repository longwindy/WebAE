# ae-web：在网页上查看 AE 网络

游戏内的 OC 电脑定时把 AE 网络的**能量、合成 CPU 状态、库存**上传到后端，你在浏览器里登录查看。第一版只读。

```
OC 电脑（因特网卡 + 适配器接 ME） ──HTTPS──▶ 后端（你的电脑）◀── 浏览器
```

| 目录 | 内容 |
|---|---|
| `backend/` | Python 后端（FastAPI）和网页 |
| `oc/aeweb.lua` | 游戏内 OC 电脑运行的上传程序 |

## 一、启动后端（在你的电脑上）

1. 需要 [uv](https://docs.astral.sh/uv/)（已装）。进入 `backend/`：
   ```powershell
   cd D:\oc\ae-web\backend
   copy config.example.toml config.toml
   ```
2. 编辑 `config.toml`：
   - `oc_token`：OC 上传用的密钥。用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成一串。
   - `web_password`：网页登录密码。
3. 启动：
   ```powershell
   uv run python app.py
   ```
   本机打开 <http://127.0.0.1:8765> 能看到登录页就说明后端正常。数据会保存在 `backend/data/state.json`，重启后不会丢。

## 二、让游戏服务器能访问到后端

后端只监听本机（`127.0.0.1`），需要一个内网穿透工具，提供一个公网 HTTPS 地址。

- **临时测试：Cloudflare 快速隧道**（不需要域名）
  ```powershell
  cloudflared tunnel --url http://127.0.0.1:8765
  ```
  它会打印出一个 `https://xxxx.trycloudflare.com` 地址。**每次重启 cloudflared 地址都会变**，OC 脚本里的地址也要跟着改。在国内访问不一定稳定。
- **长期使用**：任选一种
  - Cloudflare 命名隧道加上自己的域名，地址固定；
  - 国内的穿透服务（如 SakuraFrp）；
  - 以后迁移到 VPS。

  迁移时只需要复制 `backend/` 文件夹（包括 `config.toml` 和 `data/`），再改一下 OC 脚本里的地址。

先在浏览器里用这个公网地址能打开登录页，再继续下一步。

## 三、游戏内的 OC 电脑

**硬件**：
- 机箱和 CPU：T2 以上
- 内存：建议 2 根 T3.5
- 硬盘：装好 OpenOS
- 因特网卡
- 适配器：贴着 **ME 控制器**或**方块形式的 ME 接口**放，用线缆连到电脑。面板形式（贴在线缆上的薄片）的 ME 接口没有网络查询功能，不能用
- 合成 CPU 里要有**合成监控器**，网页上才能看到合成目标
- 区块必须一直加载：电脑所在区块卸载后程序就停了

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
- `BASE_URL`：你的公网地址
- `TOKEN`：和 `config.toml` 里的 `oc_token` 完全一致

再运行 `aeweb`，正常情况下会打印：
```
[  237.2s] ae-web uploader started, target https://...
[  237.2s] memory: 1617 KB free of 2048 KB
[  244.9s] inventory: 3853 entries in 6.2s
```
用 **Ctrl+Alt+C** 停止。想开机自动运行，就执行 `echo aeweb >> /home/.shrc`。

**更新脚本**：只需要 `wget -f https://你的地址/oc/aeweb.lua /home/aeweb.lua`。配置在 `aeweb.cfg` 里，不会被覆盖。

想用别的配置文件，可以运行 `aeweb /路径/xxx.cfg`。

**可调参数**（在 `aeweb.cfg` 里取消注释再修改）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `STATUS_INTERVAL` | 5 | 能量和 CPU 的上报间隔（秒） |
| `ITEMS_INTERVAL` | 60 | 完整库存快照的间隔（秒）。扫描耗时较长时可以调大 |
| `CHUNK_LINES` | 500 | 库存上传时每个包的行数 |
| `CPU_ITEM_LIMIT` | 100 | 每个 CPU 最多上报多少条"正在合成"和"等待中"的物品 |
| `HTTP_TIMEOUT` | 20 | 单次请求超时（秒） |

## 四、网页上能看到什么

- **能量**：AE 储能、平均消耗、平均输入、待机消耗
- **合成 CPU**：忙碌和空闲状态、存储大小、协处理器数量、合成目标，以及正在合成和等待中的物品（可展开）
- **库存**：物品和流体，可以搜索名称或注册名、按数量或名称排序、按"物品 / 流体 / 可合成"筛选，分页显示
- 页面顶部显示上一次状态上报和库存快照的时间。超时变黄或变红，说明 OC 程序可能停了。

## 已知限制

- 物品数量取自 AE 转出来的物品堆。**超过约 21 亿的数量可能被截断**，还需要实测确认。流体不受影响。
- `allItems()` 迭代器不一定带"可合成"标记。如果"可合成"筛选结果是空的，说明这个版本不提供，后续加下单功能时会改用 `getCraftables()`。
- 库存扫描期间，ME 网络的每次变动都会让 OC 在服务端重新整理一次物品列表。网络非常大或者变动非常频繁时，把 `ITEMS_INTERVAL` 调大。

## 安全

- `oc_token` 写在游戏里那台 OC 电脑上，能打开这台电脑的人都看得到。给电脑上锁，或者放在别人进不去的地方。
- 网页只有一个密码。分享给别人之前，至少换一个强密码。多用户和下单功能会在后续版本加入。
