from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QComboBox, QHBoxLayout, QPushButton


class ChineseInputDialog(QDialog):
    """
    定制化交互对话框组件
    提供本地化的中文操作界面，用于搜寻结果的角色设定（配送中心/受灾点）。
    """

    def __init__(self, title, prompt, items, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(prompt))
        self.combo = QComboBox()
        self.combo.addItems(items)
        layout.addWidget(self.combo)

        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("确定")
        btn_cancel = QPushButton("取消")
        btn_ok.clicked.connect(self.accept)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)

        layout.addLayout(btn_layout)

    def get_selected_item(self):
        return self.combo.currentText()