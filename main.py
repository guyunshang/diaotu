import sys
import os
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from config.settings import MBTILES_PATH
from core.mbtiles_server import start_mbtiles_server
from gui.main_window import LogisticApp


def main():
    """
    系统引导入口
    负责执行前置检查、启动基础服务并挂载主应用程序环境。
    """
    if not os.path.exists(MBTILES_PATH):
        print(f"警告：未找到地图数据库文件 zhuhai_custom_area.mbtiles！地图可能无法正常加载。")

    # 拉起本地高并发瓦片地图服务后台
    mbtiles_server = start_mbtiles_server()

    # 注入 Chromium 渲染引擎底层标识，规避由于硬件加速产生的渲染异常
    os.environ[
        "QTWEBENGINE_CHROMIUM_FLAGS"] = "--ignore-gpu-blocklist --disable-gpu-driver-bug-workarounds --disable-direct-composition --disable-web-security"

    app = QApplication(sys.argv)

    # 解决高分屏 (High DPI) 缩放模糊问题
    app.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    # 实例化并展示主窗口
    window = LogisticApp()
    window.browser.setZoomFactor(1.0)
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()