# 内容过时，施工中...

# 部署到 VPS（Ubuntu 24.04）

这份步骤假设：
- 服务器是 Ubuntu 24.04，本地已经能用 `ssh aeweb` 登录（`~/.ssh/config` 里配好了 `aeweb`）；
- 有一个域名（下文写作 `<你的域名>`，例如 `webae.example.com`），DNS 在 Cloudflare 上（在别家也一样，只是界面不同）；
- 后端装在 `/opt/webae`，用一个专门的系统用户 `aeweb` 运行，前面用 Caddy 提供 HTTPS。

用 `root` 登录时直接运行下面的命令。用 `ubuntu` 这类普通用户登录时，在命令前面加 `sudo`，或者先 `sudo -i` 切换到 root。

目录里的文件：

| 文件 | 作用 |
|---|---|
| `aeweb.service` | systemd 服务：开机自启，崩溃后自动重启 |
| `Caddyfile` | 反向代理 + 自动申请 HTTPS 证书 |
| `update.sh` | 在服务器上拉取新版本，按需重启 |
| `deploy.ps1` | 在本地运行，一条命令完成部署 |

---

## 一、云服务器控制台

在轻量服务器的**防火墙**里放行：
- TCP 22（SSH，一般默认已开）
- TCP 80、TCP 443（Caddy 申请证书和提供 HTTPS 都要用）

**不要**开放 8765，后端只给本机的 Caddy 访问。

## 二、安装软件

```bash
apt update && apt upgrade -y
apt install -y git caddy curl
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
```

**只有 0.5G 内存的机器**，再加 1G 交换空间：
```bash
fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

## 三、创建运行用户、拉取代码

```bash
useradd --system --create-home --home-dir /var/lib/aeweb --shell /usr/sbin/nologin aeweb
mkdir -p /opt/webae && chown aeweb:aeweb /opt/webae
```

**仓库是公开的**：
```bash
sudo -u aeweb -H git clone https://github.com/longwindy/WebAE.git /opt/webae
```

**仓库是私有的**：先给服务器生成一个只读的 deploy key：
```bash
sudo -u aeweb mkdir -p -m 700 /var/lib/aeweb/.ssh
sudo -u aeweb -H ssh-keygen -t ed25519 -N "" -f /var/lib/aeweb/.ssh/id_ed25519 -C webae-deploy
cat /var/lib/aeweb/.ssh/id_ed25519.pub
```
把打印出的公钥加到 GitHub 仓库的 **Settings → Deploy keys**（不要勾选 Allow write access），然后：
```bash
sudo -u aeweb -H git clone git@github.com:longwindy/WebAE.git /opt/webae
```
（第一次连接 GitHub 时会问是否信任主机指纹，输入 `yes`。）以后仓库改成公开了也不用动，deploy key 照样能用。

安装依赖：
```bash
cd /opt/webae/backend && sudo -u aeweb -H uv sync --frozen
```

## 四、上传配置和数据

`config.toml` 和 `data/` 不在仓库里，要从本地传上去。**在本地 PowerShell 里**，从仓库根目录运行：
```powershell
cd backend
tar -czf $env:TEMP\webae-data.tgz config.toml data
scp $env:TEMP\webae-data.tgz aeweb:/tmp/
```
`data/` 里有几万个图标文件，打包成一个文件再传会快很多（大约 120 MB）。

**回到服务器**解包：
```bash
tar -xzf /tmp/webae-data.tgz -C /opt/webae/backend
chown -R aeweb:aeweb /opt/webae/backend/data /opt/webae/backend/config.toml
chmod 600 /opt/webae/backend/config.toml
rm /tmp/webae-data.tgz
```
检查 `config.toml` 里是 `host = "127.0.0.1"`、`port = 8765`（和 Caddyfile 一致）：
```bash
grep -E '^(host|port)' /opt/webae/backend/config.toml
```

## 五、启动后端

```bash
cp /opt/webae/deploy/aeweb.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aeweb
systemctl status aeweb --no-pager
journalctl -u aeweb -n 20 --no-pager
```
如果上传的是旧的单密码版本数据，日志里会出现 `Moved the existing AE network to network 'main'`：原来的网络已经迁移成 `main`，OC 那边的令牌不用改。

**设置一个方便的管理命令**，以后管理用户和网络都用它：
```bash
echo "alias webae='sudo -u aeweb /opt/webae/backend/.venv/bin/python /opt/webae/backend/manage.py'" >> ~/.bashrc
source ~/.bashrc
webae user add 你的名字 --admin        # 记下打印出的登录密钥
```
一定要用 `aeweb` 用户运行 `manage.py`（上面的 alias 已经处理了），这样 `accounts.json` 的所有者才是对的。

**先通过 SSH 隧道测试**（这时 DNS 还没切过来）。在本地另开一个 PowerShell：
```powershell
ssh -N -L 18765:127.0.0.1:8765 aeweb
```
浏览器打开 <http://127.0.0.1:18765>，用刚才的密钥登录，能看到原来的库存，就说明后端正常。测完按 Ctrl+C 关掉隧道。

## 六、HTTPS 和切换域名

先改 DNS，再启用 Caddy。反过来的话，Caddy 会在 DNS 指过来之前反复申请证书失败，可能撞上 Let's Encrypt 的失败次数限制。

1. **把域名指向服务器**。在 DNS 里给 `<你的域名>` 添加一条 `A` 记录，内容填 VPS 的公网 IP。用 Cloudflare 的话：
   - **代理状态选"仅 DNS"（灰色云朵）**。开了代理（橙色云朵）的话，流量又会绕回 Cloudflare，Caddy 也申请不到证书；
   - 这个域名之前如果配在 Cloudflare Tunnel 上，先在 **Zero Trust → Networks → Tunnels → 你的隧道 → Public Hostname** 里删掉它，否则会和 A 记录冲突。
2. **确认 DNS 生效**（在本地运行），返回的是 VPS 的 IP 才继续：
   ```powershell
   nslookup <你的域名> 1.1.1.1
   ```
3. **配置并启动 Caddy**：
   ```bash
   cp /opt/webae/deploy/Caddyfile /etc/caddy/Caddyfile
   sed -i 's/webae\.example\.com/<你的域名>/' /etc/caddy/Caddyfile
   caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   systemctl reload caddy
   ```
4. **检查证书和 HTTPS**。一般一分钟内就能拿到证书：
   ```bash
   journalctl -u caddy -n 30 --no-pager | grep -iE 'certificate|error'   # 看到 "certificate obtained successfully"
   curl -sS -w '\nHTTP %{http_code}\n' https://<你的域名>/healthz
   ```
5. 浏览器打开 `https://<你的域名>`，用密钥登录。
6. **让 OC 连到新地址**。如果 OC 里 `aeweb.cfg` 的 `BASE_URL` 已经是这个域名，就不用改，DNS 生效后上报会自动发到新服务器。如果是别的地址，在 OC 上运行 `edit /home/aeweb.cfg`，把它改成 `https://<你的域名>`，保存后按 Ctrl+Alt+C 停掉程序，再运行一次 `aeweb`。网页上"状态 x 秒前"一直在刷新，就说明 OC 已经连上了。
7. 确认一切正常后，关掉原来运行后端的机器上的后端和穿透工具（比如 `cloudflared`）。

---

## 日常更新

本地改完、提交并 `git push` 之后，在仓库根目录运行：
```powershell
.\deploy\deploy.ps1
```
脚本会先检查本地有没有还没 push 的提交，然后登录服务器拉取新版本：
- 只改了网页、文档或 OC 脚本：**不重启**，刷新网页就能看到；
- 改了后端的 Python 代码：重启服务，大约中断几秒，并等待 `/healthz` 正常后才算完成；
- 改了依赖（`pyproject.toml` / `uv.lock`）：先 `uv sync`，再重启。

改了 `aeweb.lua` 的话，部署后在 OC 上照常运行 `wget -f https://<你的域名>/oc/aeweb.lua /home/aeweb.lua`。

**回滚**：在本地 `git revert` 有问题的提交，push 之后再运行一次 `deploy.ps1`。

## 常用命令（在服务器上）

```bash
systemctl status aeweb             # 运行状态
journalctl -u aeweb -f             # 实时日志（Ctrl+C 退出）
systemctl restart aeweb            # 手动重启
webae net list / webae user list   # 查看网络和用户
```

## 备份

需要备份的只有 `/opt/webae/backend/config.toml` 和 `/opt/webae/backend/data/`，其余的都能从仓库恢复。最省事的办法是定期在轻量服务器控制台里做一次**快照**。只想备份账户和订单的话，可以只拷 `data/accounts.json` 和 `data/networks/`，图标可以随时重新导入。
