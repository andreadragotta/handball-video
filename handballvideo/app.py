"""Avvio dell'applicazione."""
import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from . import __app_name__
from .main_window import MainWindow

STYLE = """
QPushButton, QToolButton { padding: 5px 10px; }
QToolButton:checked { background: #2d6cdf; color: white; border-radius: 4px; }
QPushButton#primary { background: #2d6cdf; color: white; font-weight: bold; border-radius: 4px; }
QListWidget::item { padding: 6px 4px; }
QListWidget::item:selected { background: #2d6cdf; color: white; }
"""


def _dark_palette() -> QPalette:
    p = QPalette()
    base, alt, text = QColor("#1e1f22"), QColor("#2b2d31"), QColor("#e8e8e8")
    for role, color in (
        (QPalette.ColorRole.Window, alt), (QPalette.ColorRole.WindowText, text),
        (QPalette.ColorRole.Base, base), (QPalette.ColorRole.AlternateBase, alt),
        (QPalette.ColorRole.Text, text), (QPalette.ColorRole.Button, QColor("#3a3c42")),
        (QPalette.ColorRole.ButtonText, text), (QPalette.ColorRole.Highlight, QColor("#2d6cdf")),
        (QPalette.ColorRole.HighlightedText, QColor("#ffffff")), (QPalette.ColorRole.ToolTipBase, alt),
        (QPalette.ColorRole.ToolTipText, text), (QPalette.ColorRole.PlaceholderText, QColor("#8a8a8a")),
    ):
        p.setColor(role, color)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#6a6a6a"))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#6a6a6a"))
    return p


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    # Doppio click su un file .hvproj o video passato da riga di comando
    if len(sys.argv) > 1:
        path = sys.argv[1]
        win.load_project(path) if path.endswith(".hvproj") else win.load_video(path)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
