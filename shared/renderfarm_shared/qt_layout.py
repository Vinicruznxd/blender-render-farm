"""Qt layouts for the existing desktop apps; imported only by Qt clients."""
from PySide6.QtWidgets import QGridLayout, QWidget, QScrollArea, QFrame, QLayout, QSizePolicy
from PySide6.QtCore import QSize


class AdaptiveGrid(QWidget):
    def __init__(self, *, columns: int, compact_columns: int, breakpoint: int):
        super().__init__()
        self.columns = columns
        self.compact_columns = compact_columns
        self.breakpoint = breakpoint
        self.items = []
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0,0,0,0)
        self.grid.setSpacing(8)
        self.grid.setSizeConstraint(QLayout.SetNoConstraint)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self._columns = 0

    def addWidget(self, widget):
        self.items.append(widget)
        self._arrange(force=True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange()

    def minimumSizeHint(self):
        # The wide arrangement must not stop the container reaching its breakpoint.
        return QSize(0, self.grid.minimumSize().height())

    def _arrange(self, force=False):
        columns = self.columns if self.width() >= self.breakpoint else self.compact_columns
        if columns == self._columns and not force:
            return
        self._columns = columns
        while self.grid.count():
            self.grid.takeAt(0)
        for col in range(self.columns):
            self.grid.setColumnStretch(col, 1 if col < columns else 0)
        for index, widget in enumerate(self.items):
            self.grid.addWidget(widget, index//columns, index%columns)
        self.grid.invalidate()
        self.updateGeometry()


def scrollable(widget, *, min_width=0):
    widget.setMinimumWidth(min_width)
    if widget.layout():
        widget.layout().setSizeConstraint(QLayout.SetMinAndMaxSize)
    area = QScrollArea()
    area.setObjectName('responsiveScroll')
    area.setFrameShape(QFrame.NoFrame)
    area.setWidgetResizable(True)
    area.setWidget(widget)
    return area
