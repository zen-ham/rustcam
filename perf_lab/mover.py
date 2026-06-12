# A frameless red box that moves continuously in a circle on the primary monitor
# (position is a function of time, updated as fast as the event loop allows, so
# every monitor refresh shows it at a new position). Stand-in for "dragging a
# window around" -- used to measure whether the capture pipeline records the
# motion at the full refresh rate or collapses it.
import sys, time, math
from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QColor, QPainter

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0

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
    a = t * 2 * math.pi * 0.6           # ~0.6 Hz orbit
    w.move(int(840 + 520 * math.cos(a)), int(430 + 360 * math.sin(a)))

timer = QTimer(); timer.timeout.connect(tick); timer.start(0)
app.exec_()
