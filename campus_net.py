#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校园网自动登录脚本（Dr.COM ePortal / Portal 协议）
==================================================

原理（完全还原自抓包文件 10.60.200.35.har）：

1) 未认证时，访问任意外网地址会被 AC（无线控制器）拦截，
   并 302 到认证页，例如：

       http://10.60.200.35/a79.htm?userip=10.42.0.29&usermac=b0-25-aa-6f-71-0f
           &uservlan=1645&nasip=10.10.19.250&firsturl=http://1.1.1.1/&portaltype=custom

   这一串参数里就白嫖到了本次认证需要的「用户 IP / 用户 MAC / AC 的 IP」。

2) 认证接口是一个 JSONP 的 GET 请求（服务器配置 en_md5=0，密码明文传输）：

       GET http://10.60.200.35:801/eportal/portal/login
           ?callback=dr1004&login_method=1
           &user_account=,0,你的学号           # 前缀 ",0," 来自服务端 account_prefix 配置
           &user_password=xxxx               # 明文（若 en_md5=1 则传 MD5）
           &wlan_user_ip=10.42.0.29&wlan_user_ipv6=
           &wlan_user_mac=b025aa6f710f
           &wlan_ac_ip=10.10.19.250&wlan_ac_name=
           &jsVersion=4.2.1&terminal_type=1&lang=zh-cn&v=10124&lang=zh

       返回：dr1004({"result":1,"msg":"Portal协议认证成功！"});

3) 在线状态查询（用来判断有没有掉线）：

       GET http://10.60.200.35/drcom/chkstatus?callback=dr1002&jsVersion=4.X&v=4420&lang=zh
       返回：dr1002({"result":1,...})   # 1 = 已在线，0 = 未认证

用法：
    python campus_net.py              # 检查并登录一次
    python campus_net.py --status     # 只看状态
    python campus_net.py --watch      # 掉线自动重连（守护模式）
    python campus_net.py --logout     # 注销（实验性）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import socket
import sys
import time
import uuid
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖，请先执行：pip install requests")

# 输出被重定向到文件/管道时，Windows 默认用 GBK，中文会乱码，这里统一成 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):  # pragma: no cover
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
)

# 断网访问这些地址会被 AC 拦截并跳到认证页，从跳转地址里解析用户信息
DETECT_URLS = [
    "http://connect.rom.miui.com/generate_204",
    "http://www.msftconnecttest.com/connecttest.txt",
    "http://detectportal.firefox.com/success.txt",
    "http://1.1.1.1/",
]

# 拦截跳转里各字段可能的别名
KEY_ALIASES = {
    "userip": ("userip", "wlanuserip", "wlan_user_ip", "user_ip", "ss5"),
    "usermac": ("usermac", "wlanusermac", "wlan_user_mac", "user_mac"),
    "nasip": ("nasip", "wlanacip", "wlan_ac_ip", "acip", "nas_ip"),
}

DEFAULT_CONFIG = {
    "portal_host": "10.60.200.35",   # 认证服务器（Portal 服务器）地址
    "portal_port": 801,              # eportal 的 http 端口，抓包里的 epHTTPPort
    "account": "",                   # 学号 / 账号
    "password": "",                  # 密码
    "account_prefix": ",0,",         # 账号前缀，抓包里 user_account 形如 ",0,学号"
    "enable_md5": False,             # 服务端 en_md5=1 时改为 true（密码传 MD5）
    "user_ip": "",                   # 留空则自动探测
    "user_mac": "",                  # 留空则自动探测
    "ac_ip": "",                     # 留空则自动探测
    "timeout": 6,                    # 单次请求超时（秒）
    "retry": 3,                      # 单轮尝试次数
    "interval": 30,                  # --watch 模式下的检查间隔（秒）
}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

_LOGGER = None


def set_logger(fn) -> None:
    """给 GUI 用：把日志接到自己的输出控件上。传 None 恢复为打印到控制台。"""
    global _LOGGER
    _LOGGER = fn


def log(msg: str) -> None:
    line = "[{}] {}".format(time.strftime("%H:%M:%S"), msg)
    if _LOGGER is not None:
        try:
            _LOGGER(line)
        except Exception:  # 日志本身不能把主流程带崩
            pass
    # 打包成 --noconsole 的 exe 后 sys.stdout 是 None，这里必须判空
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass


def _sleep(seconds: float, should_stop=None) -> bool:
    """可被打断的 sleep，返回 False 表示中途要求停止。"""
    end = time.time() + seconds
    while time.time() < end:
        if should_stop and should_stop():
            return False
        time.sleep(min(0.2, max(0.0, end - time.time())))
    return not (should_stop and should_stop())


def rand_v() -> int:
    """URL 里的随机版本号参数 v，随便给个数字都行。"""
    return random.randint(1000, 99999)


def parse_jsonp(text: str):
    """把 dr1004({...}); 这种 JSONP 拆成 dict。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def normalize_mac(raw: str) -> str:
    """b0-25-aa-6f-71-0f -> b025aa6f710f"""
    return re.sub(r"[^0-9a-fA-F]", "", raw or "").lower()


def local_ip(host: str) -> str:
    """拿本机在校园网里的 IP（不真正发包，只是让系统选路由）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def local_mac() -> str:
    node = uuid.getnode()
    return "{:012x}".format(node)


def parse_info_from_url(url: str) -> dict:
    """从形如 .../a79.htm?userip=..&usermac=..&nasip=.. 的地址里提取三个关键值。"""
    if not url:
        return {}
    q = parse_qs(urlparse(url).query)
    flat = {k.lower(): v[0] for k, v in q.items() if v}
    out = {}
    for field, aliases in KEY_ALIASES.items():
        for a in aliases:
            if flat.get(a):
                out[field] = flat[a]
                break
    if "usermac" in out:
        out["usermac"] = normalize_mac(out["usermac"])
    return out


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------

def build_session(cfg: dict) -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 不走系统代理，避免代理把校园网请求搅黄
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "http://{}/".format(cfg["portal_host"]),
    })
    return s


def get_status(session: requests.Session, cfg: dict, debug: bool = False):
    """
    返回 True=已在线 / False=未认证 / None=探测失败。
    优先问 Dr.COM 自己的 chkstatus，失败再退回外网探针。
    """
    url = "http://{}/drcom/chkstatus".format(cfg["portal_host"])
    params = {"callback": "dr1002", "jsVersion": "4.X", "v": rand_v(), "lang": "zh"}
    try:
        r = session.get(url, params=params, timeout=cfg["timeout"])
        data = parse_jsonp(r.text)
        if debug:
            log("chkstatus -> {}".format((r.text or "").strip()[:200]))
        if data is not None and "result" in data:
            return str(data.get("result")) == "1"
    except requests.RequestException as exc:
        if debug:
            log("chkstatus 请求失败：{}".format(exc))

    # 兜底：外网探针。联网时 generate_204 返回 204 空响应
    try:
        r = session.get(DETECT_URLS[0], allow_redirects=False, timeout=cfg["timeout"])
        if r.status_code == 204 and not r.content:
            return True
        if r.status_code in (301, 302, 303, 307, 308):
            return False
        if r.status_code == 200 and not (r.headers.get("Location")):
            return True
    except requests.RequestException:
        pass
    return None


def discover_info(session: requests.Session, cfg: dict, debug: bool = False) -> dict:
    """自动探测 userip / usermac / nasip：配置里有就用配置，没有就去拦截跳转里捞。"""
    info = {
        "userip": (cfg.get("user_ip") or "").strip(),
        "usermac": normalize_mac(cfg.get("user_mac") or ""),
        "nasip": (cfg.get("ac_ip") or "").strip(),
    }
    if info["userip"] and info["usermac"] and info["nasip"]:
        return info

    for url in DETECT_URLS:
        try:
            r = session.get(url, allow_redirects=False, timeout=cfg["timeout"])
        except requests.RequestException as exc:
            if debug:
                log("探测 {} 失败：{}".format(url, exc))
            continue

        candidates = []
        loc = r.headers.get("Location")
        if loc:
            candidates.append(loc)
        # 有的 AC 不返回 302，而是 200 + HTML/JS 里带跳转地址
        candidates += re.findall(r"https?://[^\"'\s<>]*a79\.htm\?[^\"'\s<>]+", r.text or "")
        candidates += re.findall(r"a79\.htm\?[^\"'\s<>]+", r.text or "")

        for cand in candidates:
            got = parse_info_from_url(cand)
            if got.get("userip"):
                if debug:
                    log("从 {} 解析到：{}".format(url, got))
                for k, v in got.items():
                    if not info.get(k):
                        info[k] = v
                break
        if info["userip"] and info["nasip"]:
            break

    # 还没拿到就用本机信息补齐
    if not info["userip"]:
        info["userip"] = local_ip(cfg["portal_host"])
    if not info["usermac"]:
        info["usermac"] = local_mac()

    return info


def _login_params(cfg: dict, info: dict, action: str):
    """拼认证/注销参数。用 list 是为了保留抓包里 lang 出现两次的原样。"""
    account = cfg["account"].strip()
    prefix = cfg.get("account_prefix") or ""
    if prefix and not account.startswith(prefix):
        account = prefix + account

    password = cfg["password"]
    if cfg.get("enable_md5"):
        password = hashlib.md5(password.encode("utf-8")).hexdigest()

    return [
        ("callback", "dr1004" if action == "login" else "dr1005"),
        ("login_method", "1"),
        ("user_account", account),
        ("user_password", password),
        ("wlan_user_ip", info.get("userip", "")),
        ("wlan_user_ipv6", ""),
        ("wlan_user_mac", info.get("usermac", "")),
        ("wlan_ac_ip", info.get("nasip", "")),
        ("wlan_ac_name", ""),
        ("jsVersion", "4.2.1"),
        ("terminal_type", "1"),
        ("lang", "zh-cn"),
        ("v", rand_v()),
        ("lang", "zh"),
    ]


def do_login(session: requests.Session, cfg: dict, info: dict, debug: bool = False):
    """发起认证，返回 (是否成功, 提示文本)。"""
    url = "http://{}:{}/eportal/portal/login".format(cfg["portal_host"], cfg["portal_port"])
    params = _login_params(cfg, info, "login")
    if debug:
        from urllib.parse import urlencode
        log("POST? -> {}?{}".format(url, urlencode(params)))

    try:
        r = session.get(url, params=params, timeout=cfg["timeout"])
    except requests.RequestException as exc:
        return False, "请求失败：{}".format(exc)

    # 服务器声明了 charset=utf-8，但保险起见自己解码
    text = r.content.decode("utf-8", "replace")
    if debug:
        log("login resp -> {}".format(text.strip()[:300]))

    data = parse_jsonp(text)
    if data is None:
        return False, "响应无法解析：{}".format(text.strip()[:120])
    ok = str(data.get("result")) == "1"
    return ok, str(data.get("msg", ""))


def do_logout(session: requests.Session, cfg: dict, info: dict, debug: bool = False):
    """注销（实验性，不同版本 AC 的下线接口可能不同）。"""
    url = "http://{}:{}/eportal/portal/logout".format(cfg["portal_host"], cfg["portal_port"])
    params = _login_params(cfg, info, "logout")
    try:
        r = session.get(url, params=params, timeout=cfg["timeout"])
    except requests.RequestException as exc:
        return False, "请求失败：{}".format(exc)
    text = r.content.decode("utf-8", "replace")
    if debug:
        log("logout resp -> {}".format(text.strip()[:300]))
    data = parse_jsonp(text)
    if data is None:
        return False, "响应无法解析：{}".format(text.strip()[:120])
    return str(data.get("result")) == "1", str(data.get("msg", ""))


# --------------------------------------------------------------------------
# 业务编排
# --------------------------------------------------------------------------

def _connect_round(cfg: dict, debug: bool = False, should_stop=None, quiet: bool = False):
    """
    跑一轮：查状态 → 需要就补认证。

    返回 (online, relogged)：
        online   本轮结束时是否在线
        relogged 本轮是否真的成功重连了一次（"已经在线"不算）

    quiet=True 时不输出过程日志，只留给调用方报汇总（守护模式用）；
    但 debug=True 时照常输出，方便排查。
    """
    def say(msg: str) -> None:
        if not quiet or debug:
            log(msg)

    session = build_session(cfg)

    status = get_status(session, cfg, debug)
    if status is True:
        say("当前已在线，无需登录。")
        return True, False

    info = discover_info(session, cfg, debug)
    say("用户 IP={userip}  MAC={usermac}  AC IP={nasip}".format(**info))
    if not info.get("nasip"):
        say("警告：没探测到 AC IP（wlan_ac_ip），认证可能失败，可在 config.json 里手填 ac_ip。")

    for i in range(1, max(1, int(cfg.get("retry", 3))) + 1):
        if should_stop and should_stop():
            return False, False
        ok, msg = do_login(session, cfg, info, debug)
        say("第 {} 次认证：{} {}".format(i, "成功" if ok else "失败", msg))
        if ok:
            _sleep(0.5, should_stop)
            if get_status(session, cfg, debug) is not False:
                return True, True
            say("认证接口返回成功，但状态检测仍未在线，稍后重试……")
        if not _sleep(1, should_stop):
            return False, False

    return False, False


def connect_once(cfg: dict, debug: bool = False, should_stop=None) -> bool:
    """单次：查状态，掉线就补认证。命令行和界面上的“立即连接”都用它。"""
    online, _ = _connect_round(cfg, debug, should_stop, quiet=False)
    return online


def watch(cfg: dict, debug: bool = False, should_stop=None, on_status=None) -> None:
    """
    守护模式：循环检查并自动补认证，日志只报两个计数，不刷过程细节。

    should_stop: 返回 True 时退出（GUI 用它来“停止自动重连”）。
    on_status:   每轮结束后回调 True/False，GUI 用它刷新状态灯。
    """
    interval = max(5, int(cfg.get("interval", 30)))
    checks = 0
    reconnects = 0
    log("进入守护模式，每 {} 秒检查一次。".format(interval))
    while True:
        if should_stop and should_stop():
            break
        checks += 1
        online = None
        try:
            online, relogged = _connect_round(cfg, debug, should_stop, quiet=True)
            if relogged:
                reconnects += 1
        except Exception as exc:  # 守护模式别被单次异常打断
            log("[守护] 本轮异常：{}".format(exc))
        log("[守护] 已检查 {} 次，重连成功 {} 次".format(checks, reconnects))
        if on_status is not None and online is not None:
            try:
                on_status(online)
            except Exception:
                pass
        if not _sleep(interval, should_stop):
            break
    log("[守护] 已退出，共检查 {} 次，重连成功 {} 次".format(checks, reconnects))


# --------------------------------------------------------------------------
# 配置与命令行
# --------------------------------------------------------------------------

def load_config(path: str, args) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, json.JSONDecodeError) as exc:
            sys.exit("配置文件 {} 读取失败：{}".format(path, exc))
    elif getattr(args, "command", "login") not in ("init",):
        log("没找到配置文件 {}，将使用命令行参数/默认值。".format(path))

    for key in ("account", "password", "portal_host", "portal_port", "account_prefix",
                "user_ip", "user_mac", "ac_ip", "interval", "timeout", "retry"):
        val = getattr(args, key, None)
        if val not in (None, ""):
            cfg[key] = val
    if getattr(args, "md5", False):
        cfg["enable_md5"] = True
    return cfg


def main() -> int:
    p = argparse.ArgumentParser(
        description="校园网自动登录脚本（Dr.COM ePortal / Portal 协议）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("command", nargs="?", default="login", choices=["login", "status", "logout", "init"],
                   help="login=检查并登录(默认) status=只看状态 logout=注销 init=生成配置文件")
    p.add_argument("--watch", action="store_true", help="守护模式，掉线自动重连")
    p.add_argument("--account", help="账号（默认读 config.json）")
    p.add_argument("--password", help="密码（默认读 config.json）")
    p.add_argument("--host", dest="portal_host", help="Portal 服务器地址，如 10.60.200.35")
    p.add_argument("--port", dest="portal_port", type=int, help="eportal 端口，默认 801")
    p.add_argument("--prefix", dest="account_prefix", help='账号前缀，默认 ",0,"')
    p.add_argument("--user-ip", dest="user_ip", help="手动指定用户 IP")
    p.add_argument("--user-mac", dest="user_mac", help="手动指定用户 MAC")
    p.add_argument("--ac-ip", dest="ac_ip", help="手动指定 AC IP（探测不到时用）")
    p.add_argument("--interval", type=int, help="守护模式检查间隔（秒）")
    p.add_argument("--timeout", type=int, help="请求超时（秒）")
    p.add_argument("--retry", type=int, help="单轮认证重试次数")
    p.add_argument("--md5", action="store_true", help="密码按 MD5 发送（服务器 en_md5=1 时）")
    p.add_argument("--config", default=CONFIG_PATH, help="配置文件路径")
    p.add_argument("--debug", action="store_true", help="打印请求/响应细节")
    args = p.parse_args()

    if args.command == "init":
        if os.path.exists(args.config):
            sys.exit("{} 已存在，未覆盖。".format(args.config))
        with open(args.config, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print("已生成 {}，请填入账号密码。".format(args.config))
        return 0

    cfg = load_config(args.config, args)

    if not cfg.get("account") or not cfg.get("password"):
        sys.exit("缺少账号或密码：请在 {} 里填写，或用 --account/--password 传入。".format(args.config))

    if args.command == "status":
        session = build_session(cfg)
        status = get_status(session, cfg, args.debug)
        if status is None:
            log("状态未知：探测失败（可能不在校园网内）。")
            return 2
        log("当前状态：{}".format("已在线" if status else "未认证"))
        return 0 if status else 1

    if args.command == "logout":
        session = build_session(cfg)
        info = discover_info(session, cfg, args.debug)
        ok, msg = do_logout(session, cfg, info, args.debug)
        log("注销：{} {}".format("成功" if ok else "失败", msg))
        return 0 if ok else 1

    if args.watch:
        try:
            watch(cfg, args.debug)
        except KeyboardInterrupt:
            log("已退出守护模式。")
        return 0

    return 0 if connect_once(cfg, args.debug) else 1


if __name__ == "__main__":
    sys.exit(main())
