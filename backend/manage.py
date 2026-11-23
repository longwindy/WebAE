"""Manage AE networks and users. Run from backend/:  uv run python manage.py <command>

  net add <id> [--name 名字]      add an AE network, print the token for its OC computer
  net list                        list networks and their members
  net token <id>                  issue a new OC token (the old one stops working)
  net rename <id> <名字>
  net remove <id>                 forget a network (its data folder is kept)

  user add <名字> [--admin]       add a user, print their login key
  user list
  user reset <名字>               issue a new login key (logs them out everywhere)
  user admin <名字> on|off        admins see every network as owner
  user remove <名字>

  grant <用户> <网络> owner|member|viewer
  ungrant <用户> <网络>

The backend picks up changes by itself; no restart needed.
"""

import argparse
import sys
import time

from accounts import NET_ID, ROLE_NAMES, ROLES, USER_NAME, Accounts, digest, load_config, new_key, new_token


def fail(msg: str) -> None:
    sys.exit(f"错误：{msg}")


def when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "-"


def main() -> None:
    p = argparse.ArgumentParser(description="管理 AE 网络和用户", formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("\n\n", 1)[1])
    sub = p.add_subparsers(dest="cmd", required=True)

    net = sub.add_parser("net").add_subparsers(dest="action", required=True)
    a = net.add_parser("add"); a.add_argument("id"); a.add_argument("--name", default="")
    net.add_parser("list")
    a = net.add_parser("token"); a.add_argument("id")
    a = net.add_parser("rename"); a.add_argument("id"); a.add_argument("name")
    a = net.add_parser("remove"); a.add_argument("id")

    user = sub.add_parser("user").add_subparsers(dest="action", required=True)
    a = user.add_parser("add"); a.add_argument("name"); a.add_argument("--admin", action="store_true")
    user.add_parser("list")
    a = user.add_parser("reset"); a.add_argument("name")
    a = user.add_parser("admin"); a.add_argument("name"); a.add_argument("state", choices=["on", "off"])
    a = user.add_parser("remove"); a.add_argument("name")

    a = sub.add_parser("grant"); a.add_argument("user"); a.add_argument("net"); a.add_argument("role", choices=ROLES)
    a = sub.add_parser("ungrant"); a.add_argument("user"); a.add_argument("net")
    args = p.parse_args()

    cfg = load_config()
    acc = Accounts(cfg["data_dir"])
    acc.refresh()

    def need_net(nid):
        if nid not in acc.networks:
            fail(f"没有网络 {nid}（用 net list 查看）")
        return acc.networks[nid]

    def need_user(name):
        if name not in acc.users:
            fail(f"没有用户 {name}（用 user list 查看）")
        return acc.users[name]

    if args.cmd == "net":
        if args.action == "add":
            if not NET_ID.fullmatch(args.id):
                fail("网络 id 只能用小写字母、数字、- 和 _，最长 32 个字符，例如 xiaoming")
            if args.id in acc.networks:
                fail(f"网络 {args.id} 已存在")
            token = acc.add_network(args.id, args.name)
            acc.save()
            print(f"已添加网络 {args.id}（{args.name or args.id}）")
            print(f"OC 令牌：{token}")
            print("把它填进这个网络那台 OC 电脑的 aeweb.cfg（TOKEN = \"...\"）。令牌只显示这一次。")
            print(f"再用 grant <用户> {args.id} owner 授权给用户。")
        elif args.action == "list":
            if not acc.networks:
                print("还没有网络。用 net add <id> 添加。")
            for nid, n in acc.networks.items():
                members = [f"{u}（{ROLE_NAMES[r]}）" for u, info in acc.users.items()
                           for net_id, r in info.get("networks", {}).items() if net_id == nid]
                print(f"{nid}  {n.get('name') or nid}  创建于 {when(n.get('created'))}")
                print(f"    成员：{'、'.join(members) if members else '无'}")
        elif args.action == "token":
            n = need_net(args.id)
            token = new_token()
            n["token_hash"] = digest(token)
            acc.save()
            print(f"网络 {args.id} 的新 OC 令牌：{token}")
            print("旧令牌已失效，更新 OC 电脑上的 aeweb.cfg 后重新运行 aeweb。")
        elif args.action == "rename":
            need_net(args.id)["name"] = args.name
            acc.save()
            print(f"网络 {args.id} 已改名为 {args.name}")
        elif args.action == "remove":
            need_net(args.id)
            del acc.networks[args.id]
            for info in acc.users.values():
                info.get("networks", {}).pop(args.id, None)
            acc.save()
            print(f"已删除网络 {args.id}，它的 OC 令牌已失效。")
            print(f"库存和订单记录还留在 {cfg['data_dir'] / 'networks' / args.id}，不需要的话可以手动删掉。")

    elif args.cmd == "user":
        if args.action == "add":
            if not USER_NAME.fullmatch(args.name):
                fail("用户名最长 32 个字符，不能有空格、/ 或 \\")
            if args.name in acc.users:
                fail(f"用户 {args.name} 已存在（换密钥用 user reset）")
            key = acc.add_user(args.name, args.admin)
            acc.save()
            print(f"已添加{'管理员' if args.admin else '用户'} {args.name}")
            print(f"登录密钥：{key}")
            print("密钥只显示这一次，复制后发给对方。")
            if not args.admin:
                print(f"再用 grant {args.name} <网络> member 授权网络。")
        elif args.action == "list":
            if not acc.users:
                print("还没有用户。用 user add <名字> 添加。")
            for name, u in acc.users.items():
                nets = "所有网络（管理员）" if u.get("admin") else "、".join(
                    f"{nid}（{ROLE_NAMES[r]}）" for nid, r in u.get("networks", {}).items()) or "无"
                print(f"{name}  创建于 {when(u.get('created'))}  网络：{nets}")
        elif args.action == "reset":
            u = need_user(args.name)
            key = new_key()
            u["key_hash"] = digest(key)
            acc.save()
            print(f"{args.name} 的新登录密钥：{key}")
            print("旧密钥和所有已登录的设备都已失效。")
        elif args.action == "admin":
            need_user(args.name)["admin"] = args.state == "on"
            acc.save()
            print(f"{args.name} {'现在是管理员' if args.state == 'on' else '不再是管理员'}")
        elif args.action == "remove":
            need_user(args.name)
            del acc.users[args.name]
            acc.save()
            print(f"已删除用户 {args.name}，他的密钥和所有登录都已失效。")

    elif args.cmd == "grant":
        u = need_user(args.user)
        need_net(args.net)
        u.setdefault("networks", {})[args.net] = args.role
        acc.save()
        print(f"{args.user} 在网络 {args.net} 的角色：{ROLE_NAMES[args.role]}")
        if u.get("admin"):
            print("（他是管理员，在所有网络里都按所有者对待）")

    elif args.cmd == "ungrant":
        u = need_user(args.user)
        if u.get("networks", {}).pop(args.net, None) is None:
            fail(f"{args.user} 不在网络 {args.net} 里")
        acc.save()
        print(f"已把 {args.user} 移出网络 {args.net}")


if __name__ == "__main__":
    main()
