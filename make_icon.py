# -*- coding: utf-8 -*-
"""
生成 exe 用的图标 app.ico（不依赖 Pillow，直接手写 ICO 容器 + 内嵌 PNG）。

用法：python make_icon.py
"""
import io
import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPen

SIZES = (16, 24, 32, 48, 64, 128, 256)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")


def render(size: int) -> QImage:
    """画一个圆角蓝底 + 白色 WiFi 弧线的图标。"""
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)

    # 圆角背景（留一点透明边距，任务栏里更好看）
    m = size * 0.04
    rect = QRectF(m, m, size - 2 * m, size - 2 * m)
    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0.0, QColor("#3b82f6"))
    grad.setColorAt(1.0, QColor("#1d4ed8"))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(rect, size * 0.22, size * 0.22)

    # WiFi 弧线
    cx, cy = size * 0.5, size * 0.74
    pen_w = max(1.0, size * 0.075)
    pen = QPen(QColor(255, 255, 255, 235))
    pen.setWidthF(pen_w)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    for r in (size * 0.16, size * 0.30, size * 0.44):
        box = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        p.drawArc(box, 40 * 16, 100 * 16)

    # 底部圆点
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 245))
    dot = size * 0.058
    p.drawEllipse(QRectF(cx - dot, cy - dot, 2 * dot, 2 * dot))
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    # 注意：QByteArray 必须保持引用，否则 QBuffer 会指向已回收的内存
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def write_ico(frames, path: str) -> None:
    """手写 ICO：ICONDIR + n 个 ICONDIRENTRY + 各帧 PNG 数据。"""
    count = len(frames)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries, blobs = b"", b""
    for size, data in frames:
        w = 0 if size >= 256 else size          # 256 在 ICO 里用 0 表示
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + entries + blobs)


def main() -> int:
    QGuiApplication(sys.argv)  # 画图需要 QGuiApplication
    frames = [(s, png_bytes(render(s))) for s in SIZES]
    write_ico(frames, OUT)
    print("已生成 {}（{} 帧，{} 字节）".format(OUT, len(frames), os.path.getsize(OUT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
