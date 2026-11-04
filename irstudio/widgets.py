# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""Generic Tk widgets: hover tooltips and a scrolling tab body."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class ScrollFrame(ttk.Frame):
    """A tab body that scrolls vertically when the window is too short for it (13" laptops),
    and simply fills the window when there's room (graphs then grow)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self._last = None
        self._vsb_on = False
        self.body.bind("<Configure>", self._sync, add="+")
        self.canvas.bind("<Configure>", self._sync, add="+")
        self.bind("<Enter>", lambda e: self.bind_all("<MouseWheel>", self._wheel), add="+")
        self.bind("<Leave>", lambda e: self.unbind_all("<MouseWheel>"), add="+")

    def _sync(self, _e=None):
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        rh = self.body.winfo_reqheight()
        if cw < 2 or ch < 2:
            return
        h = max(rh, ch)
        key = (cw, h)
        if key != self._last:
            self._last = key
            self.canvas.itemconfigure(self._win, width=cw, height=h)
            self.canvas.configure(scrollregion=(0, 0, cw, h))
        need = rh > ch + 2
        if need and not self._vsb_on:
            self.vsb.pack(side="right", fill="y", before=self.canvas)
            self._vsb_on = True
        elif not need and self._vsb_on:
            self.vsb.pack_forget()
            self._vsb_on = False
            self.canvas.yview_moveto(0)

    def _wheel(self, e):
        if self._vsb_on:
            w = e.widget
            if isinstance(w, (tk.Text, tk.Listbox)) or str(w).endswith("popdown.f.l"):
                return                                  # let lists / logs scroll themselves
            self.canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")


class Tooltip:
    """Hover help: shows a small yellow box after the mouse rests on a widget for ~0.5 s."""

    def __init__(self, widget, text, wrap=380):
        self.w, self.text, self.wrap = widget, text, wrap
        self.tip = self.job = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _e=None):
        self._cancel()
        self.job = self.w.after(500, self._show)

    def _cancel(self):
        if self.job is not None:
            try:
                self.w.after_cancel(self.job)
            except Exception:
                pass
            self.job = None

    def _show(self):
        self.job = None
        if self.tip is not None or not self.text:
            return
        try:
            x = self.w.winfo_rootx() + 16
            y = self.w.winfo_rooty() + self.w.winfo_height() + 4
            self.tip = tk.Toplevel(self.w)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(self.tip, text=self.text, justify="left", wraplength=self.wrap, background="#ffffe0",
                     relief="solid", borderwidth=1, font=("Segoe UI", 9), padx=6, pady=4).pack()
        except Exception:
            self.tip = None

    def _leave(self, _e=None):
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None
