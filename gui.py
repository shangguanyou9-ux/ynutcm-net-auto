#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校园网自动连接 —— PySide6 图形界面

界面只负责“收集参数 + 展示状态”，真正的认证逻辑全部复用 campus_net.py，
网络请求放在 QThread 里跑，主线程不会卡死。
"""

from __future__ import annotations

import json
import os
import shutil
import sys

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

import campus_net as cn

APP_NAME = "校园网自动连接"

# --------------------------------------------------------------------------
# 路径：打包成 exe 后，配置要放在 exe 旁边，而不是临时解包目录里
# --------------------------------------------------------------------------

def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", app_dir())
    return os.path.join(base, name)


APP_DIR = app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

GUI_DEFAULTS = {"auto_watch": False}


def load_config() -> dict:
    cfg = dict(cn.DEFAULT_CONFIG)
    cfg.update(GUI_DEFAULTS)

    # 首次运行：把打包进 exe 的模板配置复制到 exe 旁边
    # 发布版打进去的是脱敏的 config.example.json，本地开发时是 config.json
    if not os.path.exists(CONFIG_PATH):
        for name in ("config.json", "config.example.json"):
            tpl = resource_path(name)
            if os.path.exists(tpl) and os.path.abspath(tpl) != os.path.abspath(CONFIG_PATH):
                try:
                    shutil.copyfile(tpl, CONFIG_PATH)
                except OSError:
                    pass
                break

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 后台工作线程
# --------------------------------------------------------------------------

class Worker(QThread):
    logged = Signal(str)     # 一行日志
    status = Signal(str)     # online / offline / unknown
    done = Signal(str)       # 结束说明

    def __init__(self, cfg: dict, task: str, debug: bool = False, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.task = task          # status / connect / watch / logout
        self.debug = debug
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _emit_status(self, value) -> None:
        self.status.emit({True: "online", False: "offline", None: "unknown"}[value])

    def run(self) -> None:
        cn.set_logger(self.logged.emit)
        try:
            session = cn.build_session(self.cfg)

            if self.task == "status":
                self._emit_status(cn.get_status(session, self.cfg, self.debug))
                self.done.emit("状态检测完成")

            elif self.task == "logout":
                info = cn.discover_info(session, self.cfg, self.debug)
                ok, msg = cn.do_logout(session, self.cfg, info, self.debug)
                cn.log("注销：{} {}".format("成功" if ok else "失败", msg))
                self._emit_status(cn.get_status(session, self.cfg, self.debug))
                self.done.emit("注销流程结束")

            elif self.task == "connect":
                ok = cn.connect_once(self.cfg, self.debug, should_stop=self._stopped)
                self._emit_status(cn.get_status(cn.build_session(self.cfg), self.cfg, self.debug))
                self.done.emit("连接成功" if ok else "连接失败，可点“检测状态”复查")

            elif self.task == "watch":
                cn.watch(self.cfg, self.debug, should_stop=self._stopped,
                         on_status=self._emit_status)
                self.done.emit("自动重连已停止")

        except Exception as exc:  # 线程里任何意外都不能静默吞掉
            cn.log("发生异常：{}".format(exc))
            self.done.emit("异常：{}".format(exc))
        finally:
            cn.set_logger(None)

    def _stopped(self) -> bool:
        return self._stop


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------

STATE_STYLE = {
    "online":  ("#16a34a", "已在线",   "网络正常，无需登录"),
    "offline": ("#dc2626", "未认证",   "当前未登录，点“立即连接”补认证"),
    "unknown": ("#ca8a04", "状态未知", "探测不到认证服务器，确认已连上校园 WiFi"),
    "busy":    ("#2563eb", "处理中…",  "正在与认证服务器通信"),
}


class MainWindow(QWidget):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.worker = None
        self._task = ""
        self._last_status = None
        self._pending = None        # 排队等待启动的任务，防止被正在跑的任务挤掉
        self._closing = False
        self._auto_resume = False

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(700, 580)

        self._build_ui()
        self._fill_ui(cfg)

        if cfg.get("auto_watch"):
            # 自动重连模式直接进守护：守护循环第一轮本身就会做状态检测，
            # 不能再另外发一个 status 任务——它会占着线程把 watch 请求挤掉，
            # 结果就是复选框勾着、守护却没跑起来。
            QTimer.singleShot(200, self._resume_auto_watch)
        else:
            QTimer.singleShot(300, lambda: self._start("status"))

    def _resume_auto_watch(self) -> None:
        """启动时恢复自动重连：勾上复选框，toggled 会去启动守护任务。"""
        self._auto_resume = True
        self.btn_watch.setChecked(True)
        self._auto_resume = False

    # ---------------- 界面搭建 ----------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        # 状态卡片
        card = QFrame()
        card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setContentsMargins(18, 14, 18, 14)
        row.setSpacing(14)

        self.dot = QLabel()
        self.dot.setFixedSize(16, 16)
        self.lbl_state = QLabel("就绪")
        self.lbl_state.setObjectName("state")
        self.lbl_hint = QLabel("正在初始化…")
        self.lbl_hint.setObjectName("hint")

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.addWidget(self.lbl_state)
        text_col.addWidget(self.lbl_hint)

        self.btn_connect = QPushButton("立即连接")
        self.btn_connect.setObjectName("primary")
        self.btn_connect.clicked.connect(lambda: self._start("connect"))
        self.btn_status = QPushButton("检测状态")
        self.btn_status.clicked.connect(lambda: self._start("status"))
        self.btn_logout = QPushButton("注销")
        self.btn_logout.clicked.connect(lambda: self._start("logout"))

        row.addWidget(self.dot)
        row.addLayout(text_col, 1)
        row.addWidget(self.btn_connect)
        row.addWidget(self.btn_status)
        row.addWidget(self.btn_logout)
        root.addWidget(card)

        # 参数表单
        form_card = QFrame()
        form_card.setObjectName("card")
        grid = QGridLayout(form_card)
        grid.setContentsMargins(18, 14, 18, 14)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        def label(text):
            lb = QLabel(text)
            lb.setObjectName("field")
            return lb

        self.ed_host = QLineEdit()
        self.ed_port = QLineEdit()
        self.ed_port.setMaximumWidth(90)
        self.ed_account = QLineEdit()
        self.ed_password = QLineEdit()
        self.ed_password.setEchoMode(QLineEdit.Password)
        self.ed_prefix = QLineEdit()
        self.chk_show_pwd = QCheckBox("显示")

        self.chk_show_pwd.toggled.connect(
            lambda on: self.ed_password.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password))

        grid.addWidget(label("服务器"), 0, 0)
        grid.addWidget(self.ed_host, 0, 1)
        grid.addWidget(label("端口"), 0, 2)
        grid.addWidget(self.ed_port, 0, 3)
        grid.addWidget(label("账号"), 1, 0)
        grid.addWidget(self.ed_account, 1, 1, 1, 3)
        grid.addWidget(label("密码"), 2, 0)
        grid.addWidget(self.ed_password, 2, 1, 1, 2)
        grid.addWidget(self.chk_show_pwd, 2, 3)
        grid.addWidget(label("账号前缀"), 3, 0)
        grid.addWidget(self.ed_prefix, 3, 1)
        tip = QLabel("按抓包结果默认 ,0,，不要删")
        tip.setObjectName("hint")
        grid.addWidget(tip, 3, 2, 1, 2)
        grid.setColumnStretch(1, 1)
        root.addWidget(form_card)

        # 选项行
        opts = QHBoxLayout()
        opts.setSpacing(12)
        self.btn_watch = QPushButton("自动重连")
        self.btn_watch.setCheckable(True)
        self.btn_watch.toggled.connect(self._toggle_watch)

        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(5, 3600)
        self.spin_interval.setSuffix(" 秒")
        self.spin_interval.setMaximumWidth(100)

        self.chk_debug = QCheckBox("详细日志")
        self.btn_save = QPushButton("保存配置")
        self.btn_save.clicked.connect(self._save)

        opts.addWidget(self.btn_watch)
        opts.addWidget(QLabel("检查间隔"))
        opts.addWidget(self.spin_interval)
        opts.addWidget(self.chk_debug)
        opts.addStretch(1)
        opts.addWidget(self.btn_save)
        root.addLayout(opts)

        # 日志
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(1000)
        self.log_box.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        root.addWidget(self.log_box, 1)

        self._apply_style()
        self._set_state("unknown", "就绪", "点击“立即连接”开始")

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QWidget { background: #f5f6f8; color: #1f2430;
                      font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; font-size: 13px; }
            QFrame#card { background: #ffffff; border: 1px solid #e3e6ec; border-radius: 10px; }
            QLabel#state { font-size: 19px; font-weight: 600; }
            QLabel#hint, QLabel#field { color: #6b7280; font-size: 12px; }
            QLineEdit, QSpinBox { background: #ffffff; border: 1px solid #d7dbe3; border-radius: 6px;
                                  padding: 6px 8px; selection-background-color: #2563eb; }
            QLineEdit:focus, QSpinBox:focus { border: 1px solid #2563eb; }
            QPushButton { background: #ffffff; border: 1px solid #d7dbe3; border-radius: 6px;
                          padding: 7px 14px; }
            QPushButton:hover { background: #f0f3f8; }
            QPushButton:disabled { color: #9aa1ad; background: #f2f3f5; }
            QPushButton#primary { background: #2563eb; border: 1px solid #2563eb; color: #ffffff; font-weight: 600; }
            QPushButton#primary:hover { background: #1d4ed8; }
            QPushButton#primary:disabled { background: #9db4ea; border-color: #9db4ea; color: #ffffff; }
            QPushButton:checked { background: #e8f0fe; border-color: #2563eb; color: #1d4ed8; font-weight: 600; }
            QPlainTextEdit { background: #12161c; color: #cbd5e1; border: 1px solid #e3e6ec;
                             border-radius: 10px; padding: 8px; }
            QCheckBox { color: #374151; }
        """)

    # ---------------- 状态与日志 ----------------

    def _set_state(self, key: str, text: str = None, hint: str = None) -> None:
        color, default_text, default_hint = STATE_STYLE[key]
        self.dot.setStyleSheet("background:%s; border-radius:8px;" % color)
        self.lbl_state.setText(text or default_text)
        self.lbl_hint.setText(hint or default_hint)

    def _on_status(self, key: str) -> None:
        self._last_status = key
        self._set_state(key)

    def append_log(self, line: str) -> None:
        self.log_box.appendPlainText(line)

    # ---------------- 配置读写 ----------------

    def _fill_ui(self, cfg: dict) -> None:
        self.ed_host.setText(str(cfg.get("portal_host", "")))
        self.ed_port.setText(str(cfg.get("portal_port", 801)))
        self.ed_account.setText(str(cfg.get("account", "")))
        self.ed_password.setText(str(cfg.get("password", "")))
        self.ed_prefix.setText(str(cfg.get("account_prefix", ",0,")))
        self.spin_interval.setValue(int(cfg.get("interval", 30)))

    def _collect(self) -> dict:
        cfg = dict(self.cfg)
        cfg["portal_host"] = self.ed_host.text().strip()
        cfg["account"] = self.ed_account.text().strip()
        cfg["password"] = self.ed_password.text()
        cfg["account_prefix"] = self.ed_prefix.text()
        cfg["interval"] = self.spin_interval.value()
        try:
            cfg["portal_port"] = int(self.ed_port.text().strip() or 801)
        except ValueError:
            cfg["portal_port"] = 801
        cfg["auto_watch"] = self.btn_watch.isChecked()
        return cfg

    def _save(self) -> None:
        cfg = self._collect()
        try:
            save_config(cfg)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.cfg = cfg
        self.append_log("[配置] 已保存到 {}".format(CONFIG_PATH))

    # ---------------- 任务调度 ----------------

    def _busy(self, busy: bool) -> None:
        self.btn_connect.setDisabled(busy)
        self.btn_status.setDisabled(busy)
        self.btn_logout.setDisabled(busy)

    def _is_busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _start(self, task: str) -> None:
        if self._is_busy():
            if task == "watch":
                # 不能直接丢掉：先排队，等当前任务收尾再启动，
                # 否则界面勾上了“自动重连”，守护模式其实没跑
                self._pending = "watch"
                self.append_log("[提示] 正在等待当前任务结束，随后开启自动重连…")
            else:
                self.append_log("[提示] 上一个任务还没结束，请稍候。")
            return

        cfg = self._collect()
        if task in ("connect", "watch", "logout") and not (cfg["account"] and cfg["password"]):
            if self._auto_resume:
                # 开机自动恢复时不弹模态框打断用户
                self.append_log("[提示] 自动重连已启用，但还没填账号密码，填好后点“保存配置”。")
            else:
                QMessageBox.warning(self, "缺少信息", "请先填写账号和密码。")
            if task == "watch":
                self.btn_watch.setChecked(False)
            return
        if task == "watch":
            self.cfg = cfg
            try:
                save_config(cfg)
            except OSError:
                pass

        self._task = task
        self._busy(True)
        if task != "status":
            self._set_state("busy")

        self.worker = Worker(cfg, task, self.chk_debug.isChecked(), self)
        self.worker.logged.connect(self.append_log)
        self.worker.status.connect(self._on_status)
        self.worker.done.connect(self._on_done)
        self.worker.finished.connect(self._on_thread_finished)
        self.worker.start()

    def _toggle_watch(self, checked: bool) -> None:
        if checked:
            self.btn_watch.setText("停止自动重连")
            self._start("watch")
        else:
            self.btn_watch.setText("自动重连")
            self._pending = None          # 撤销还没轮到的守护启动
            if self._is_busy():
                self.append_log("[提示] 正在停止自动重连…")
                self.worker.stop()

    def _on_done(self, message: str) -> None:
        self.append_log("[完成] {}".format(message))
        if self._task == "watch":
            self.btn_watch.blockSignals(True)
            self.btn_watch.setChecked(False)
            self.btn_watch.setText("自动重连")
            self.btn_watch.blockSignals(False)
            if self._last_status:
                self._set_state(self._last_status)   # 停掉守护后仍然显示真实网络状态
            else:
                self._set_state("unknown", "已停止", "自动重连已关闭")

    def _on_thread_finished(self) -> None:
        # 线程真正结束后再收尾并启动排队中的任务，避免 isRunning() 还没翻转
        finished, self.worker = self.worker, None
        if finished is not None:
            finished.deleteLater()      # 长期挂机时别让 QThread 对象越攒越多
        self._busy(False)
        if self._closing:
            return
        if self._pending:
            task, self._pending = self._pending, None
            QTimer.singleShot(0, lambda t=task: self._start(t))

    def closeEvent(self, event) -> None:
        self._closing = True
        self._pending = None
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        event.accept()


def main() -> int:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow(load_config())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
