from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal


class MapBridge(QObject):
    """
    QtWebChannel 桥接层
    处理 JavaScript 引擎与 Python 后台的异步信号通信。
    """
    point_added = pyqtSignal(float, float, str)
    point_moved = pyqtSignal(int, float, float, str)
    search_converted = pyqtSignal(float, float, str)
    road_block_added = pyqtSignal(float, float)
    road_block_moved = pyqtSignal(int, float, float)
    generate_path_requested = pyqtSignal(str, str)
    point_deleted_from_map = pyqtSignal(int, str)
    point_double_clicked = pyqtSignal(float, float)
    traffic_requested = pyqtSignal(float, float, float, float)
    coord_search_requested = pyqtSignal(float, float)
    warning_requested = pyqtSignal(str, str)
    text_search_requested = pyqtSignal(str)

    @pyqtSlot(float, float, str)
    def add_point(self, lat, lng, p_type): self.point_added.emit(lat, lng, p_type)

    @pyqtSlot(int, float, float, str)
    def move_point(self, index, lat, lng, p_type): self.point_moved.emit(index, lat, lng, p_type)

    @pyqtSlot(float, float, str)
    def convert_search(self, lat, lng, name): self.search_converted.emit(lat, lng, name)

    @pyqtSlot(float, float)
    def add_road_block(self, lat, lng): self.road_block_added.emit(lat, lng)

    @pyqtSlot(str, str)
    def generate_complex_road_segment(self, coords_json, mode): self.generate_path_requested.emit(coords_json, mode)

    @pyqtSlot(int, str)
    def delete_point(self, index, p_type): self.point_deleted_from_map.emit(index, p_type)

    @pyqtSlot(int, float, float)
    def move_road_block(self, index, lat, lng):
        self.road_block_moved.emit(index, lat, lng)

    @pyqtSlot(float, float)
    def on_point_double_clicked(self, lat, lng):
        self.point_double_clicked.emit(lat, lng)

    @pyqtSlot(float, float, float, float)
    def request_traffic(self, sw_lat, sw_lng, ne_lat, ne_lng):
        self.traffic_requested.emit(sw_lat, sw_lng, ne_lat, ne_lng)

    @pyqtSlot(float, float)
    def search_coordinate(self, lat, lng):
        self.coord_search_requested.emit(lat, lng)

    @pyqtSlot(str, str)
    def show_warning(self, title, text):
        self.warning_requested.emit(title, text)

    @pyqtSlot(str)
    def search_text(self, query):
        self.text_search_requested.emit(query)