"""Scrollable native layout that leaves every control reachable on small displays."""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk


class ScrollableFrame(tk.Frame):
    def __init__(self, parent, *, background: str, min_width: int, min_height: int = 0):
        super().__init__(parent, bg=background)
        self.min_width, self.min_height = min_width, min_height
        self._refresh_id = None
        self.canvas = tk.Canvas(self, bg=background, highlightthickness=0, bd=0)
        self.content = tk.Frame(self.canvas, bg=background)
        self._item = self.canvas.create_window(0, 0, window=self.content, anchor='nw')
        self.vertical = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.horizontal = ttk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vertical.set, xscrollcommand=self.horizontal.set)
        self.canvas.grid(row=0,column=0,sticky='nsew')
        self.vertical.grid(row=0,column=1,sticky='ns')
        self.horizontal.grid(row=1,column=0,sticky='ew')
        self.rowconfigure(0,weight=1); self.columnconfigure(0,weight=1)
        self.canvas.bind('<Configure>', self._resize)
        self.content.bind('<Configure>', self._resize)
        # Bind only on this toplevel, preserving native scrolling in tables/editors.
        self._wheel_binding = self.winfo_toplevel().bind('<MouseWheel>', self._wheel, add='+')
        self._destroy_binding = self.bind('<Destroy>', self._cleanup, add='+')

    def refresh(self):
        """Re-measure after dynamic labels change their requested size."""
        if self._refresh_id is None:
            self._refresh_id = self.after_idle(self._refresh_requested_size)

    def _refresh_requested_size(self):
        self._refresh_id = None
        self._resize()

    def _resize(self, _event=None):
        requested_width = max(self.min_width, self.content.winfo_reqwidth())
        requested_height = max(self.min_height, self.content.winfo_reqheight())
        available_width, available_height = self.winfo_width(), self.winfo_height()
        need_x = requested_width > available_width
        need_y = requested_height > available_height - (self.horizontal.winfo_reqheight() if need_x else 0)
        need_x = requested_width > available_width - (self.vertical.winfo_reqwidth() if need_y else 0)
        need_y = requested_height > available_height - (self.horizontal.winfo_reqheight() if need_x else 0)
        self.vertical.grid() if need_y else self.vertical.grid_remove()
        self.horizontal.grid() if need_x else self.horizontal.grid_remove()
        width = max(requested_width, available_width - (self.vertical.winfo_reqwidth() if need_y else 0))
        height = max(requested_height, available_height - (self.horizontal.winfo_reqheight() if need_x else 0))
        self.canvas.itemconfigure(self._item, width=width, height=height)
        self.canvas.configure(scrollregion=(0,0,width,height))

    def _wheel(self, event):
        widget = event.widget
        if widget.winfo_toplevel() != self.winfo_toplevel():
            return
        if widget.winfo_class() in ('Treeview','Text','Listbox','TCombobox','Entry','TEntry'):
            return
        if self.canvas.yview() != (0.0,1.0):
            self.canvas.yview_scroll(-int(event.delta/120), 'units')
            return 'break'

    def _cleanup(self, event):
        if event.widget is self:
            if self._refresh_id is not None:
                self.after_cancel(self._refresh_id)
                self._refresh_id = None
            self.winfo_toplevel().unbind('<MouseWheel>', self._wheel_binding)
