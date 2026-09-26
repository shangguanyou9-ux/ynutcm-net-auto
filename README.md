# 云南中医药大学校园网自动登录

Dr.COM ePortal（Portal 协议）校园网自动认证工具。纯 Python 实现，**不依赖浏览器、不用 Selenium**，提供图形界面和命令行两种用法，可打包成单文件 exe。

适用于登录页为 `10.60.200.35/a79.htm` 的 Dr.COM 认证系统（云南中医药大学）。

## 功能

- **图形界面**：状态灯实时显示、一键连接、掉线自动重连、注销
- **命令行**：适合脚本化 / 任务计划程序开机自启
- **参数自动探测**：自动获取本机认证所需的 IP / MAC / AC IP，换电脑、DHCP 重新分配都不用改配置
- **单文件 exe**：打包后 39 MB，拷走即用，不需要装 Python

## 快速开始

### 方式一：直接用 exe

1. 到 [Releases](../../releases) 下载 `校园网自动连接.exe`
2. 和 `config.json` 放在**同一个文件夹**里
3. 双击运行，界面上填学号和密码，点 `保存配置`

> 首次运行会自动生成一份 `config.json`。

### 方式二：从源码运行

```bash
pip install requests PySide6

python gui.py                 # 图形界面
```

或者只用命令行（只需要 `requests`）：

```bash
pip install requests

python campus_net.py              # 检查状态，掉线就登录
python campus_net.py status       # 只看当前状态（已在线 / 未认证）
python campus_net.py --watch      # 守护模式，每 30 秒巡检，掉线自动重连
python campus_net.py logout       # 注销（实验性）
```

## 图形界面

- 顶部状态灯：**已在线 / 未认证 / 状态未知 / 处理中**，启动时自动检测一次
- `立即连接`：检查状态，没登录就补一次认证
- `自动重连`：开关式的守护模式，掉线自动重连，按设定间隔巡检；再点一次停止
- 服务器地址、端口、账号、密码、`账号前缀`都能在界面上改
- 深色日志面板，勾选 `详细日志` 可以看到请求 URL 和服务器原始返回

网络请求全部跑在 `QThread` 里，界面不会卡死。

## 配置说明

`config.json` 字段：

| 字段 | 说明 |
| --- | --- |
| `portal_host` | 认证服务器地址，默认 `10.60.200.35` |
| `portal_port` | eportal 端口，默认 `801` |
| `account` | 学号 |
| `password` | 密码（**注意：走的是 http 明文传输，见下方说明**） |
| `account_prefix` | 账号前缀，默认 `",0,"`，**不要删** |
| `enable_md5` | 服务器要求 MD5 时设为 `true`（本校为 `false`） |
| `user_ip` / `user_mac` / `ac_ip` | 留空则自动探测，探测不到再手填 |
| `interval` | 守护模式巡检间隔（秒） |
| `auto_watch` | 界面启动时是否自动开启重连 |

## 开机自动连

**图形界面版**：把 exe 的快捷方式丢进启动文件夹（`Win+R` 输入 `shell:startup`），界面上勾选 `自动重连` 并 `保存配置`。

**命令行版**：任务计划程序 → 创建任务：

- 触发器：**登录时** / **工作站解锁时**
- 操作：程序 `python`，参数 `campus_net.py --watch`，起始位置填脚本目录

## 打包

双击 `build_exe.bat`，产物在 `dist\` 下。等价的手工命令：

```bash
pip install pyinstaller
python make_icon.py
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "校园网自动连接" ^
  --icon app.ico --add-data "config.example.json;." gui.py
```

- `--onefile` 单文件，启动时要解压到临时目录，首次打开慢几秒；嫌慢可换 `--onedir`
- `--windowed` 不带黑色控制台窗口
- `--add-data` 打进去的是**脱敏的** `config.example.json`，不会把你的账号密码编译进 exe

## 实现原理

登录页是 Dr.COM 的 ePortal，全部流程用抓包还原，只有三个接口：

**① 探测用户信息**——未认证时访问外网会被 AC 拦截并 302 到认证页：

```
http://10.60.200.35/a79.htm?userip=10.42.0.29&usermac=b0-25-aa-6f-71-0f
    &uservlan=1645&nasip=10.10.19.250&firsturl=http://1.1.1.1/&portaltype=custom
```

用 `allow_redirects=False` 请求探针地址，从 `Location` 里取出 `userip / usermac / nasip`。

**② 认证**（JSONP 的 GET 请求）：

```
GET http://10.60.200.35:801/eportal/portal/login
    ?callback=dr1004&login_method=1
    &user_account=,0,你的学号
    &user_password=你的密码&wlan_user_ip=10.42.0.29&wlan_user_ipv6=
    &wlan_user_mac=b025aa6f710f&wlan_ac_ip=10.10.19.250&wlan_ac_name=
    &jsVersion=4.2.1&terminal_type=1&lang=zh-cn&v=10124&lang=zh

→ dr1004({"result":1,"msg":"Portal协议认证成功！"});
```

**③ 状态查询**：

```
GET http://10.60.200.35/drcom/chkstatus?callback=dr1002&jsVersion=4.X&v=4420&lang=zh
→ dr1002({"result":1,...})    # 1 = 已在线，0 = 未认证
```

## 常见问题

**`user_account` 前面那个 `,0,` 是什么？**
来自服务器配置 `account_prefix=1`，不属于账号本身。对应 `account_prefix` 字段，删掉会认证失败。

**密码是明文传的？**
是的。服务器 `loadConfig` 返回 `en_md5: "0"`、`password_cut: "0"`，说明本校 Portal 不收 MD5，直接传原文。如果以后服务端改成 `en_md5=1`，把 `enable_md5` 设为 `true` 即可。

**`logout` 注销能用吗？**
实验性的。抓包里没有注销请求，按 Dr.COM 4.X 通用的 `/eportal/portal/logout` 实现，能不能用取决于 AC 版本，不影响登录功能。

**提示"状态未知"？**
说明探测不到认证服务器，先确认已经连上校园 WiFi。

**杀毒软件报毒？**
PyInstaller 单文件 exe 的常见误报，加白名单即可。

## 免责声明

本项目仅供学习网络协议和**本校师生登录自己的账号**使用。请勿用于任何未授权用途。
使用者需自行承担因使用本工具产生的一切后果。

## License

[MIT](LICENSE)
