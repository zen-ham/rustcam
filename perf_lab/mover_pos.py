# Like mover.py but takes CX, CY args for orbit center.
# Usage: python mover_pos.py SECS CX CY [RX RY]
import sys, time, math
from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QColor, QPainter

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0
CX = int(sys.argv[2]) if len(sys.argv) > 2 else 840
CY = int(sys.argv[3]) if len(sys.argv) > 3 else 430
RX = int(sys.argv[4]) if len(sys.argv) > 4 else 520
RY = int(sys.argv[5]) if len(sys.argv) > 5 else 360


class Box(QWidget):
    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(255, 40, 40))


app = QApplication(sys.argv)
w = Box()
w.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
w.setAttribute(Qt.WA_ShowWithoutActivating)
w.resize(200, 200)
w.show()

t0 = time.perf_counter()
def tick():
    t = time.perf_counter() - t0
    if t > SECS:
        app.quit(); return
    a = t * 2 * math.pi * 0.6
    w.move(int(CX + RX * math.cos(a)), int(CY + RY * math.sin(a)))

timer = QTimer(); timer.timeout.connect(tick); timer.start(0)
app.exec_()
