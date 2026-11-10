from __future__ import annotations

import ctypes
import os
import threading
import tkinter as tk
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

from fastapi import FastAPI
from renderfarm_shared.i18n import LANGUAGE_NAMES, normalize_language, tr
from renderfarm_shared.presentation import relative_age, parse_timestamp, resource_path
from renderfarm_shared.tk_layout import ScrollableFrame

from app.gui.runtime import CoordinatorServerRuntime


REFRESH_INTERVAL_MS = 2000

TITLE_FONT = ("Segoe UI Semibold", 20)
SECTION_FONT = ("Bahnschrift SemiBold", 10)
METRIC_VALUE_FONT = ("Segoe UI Semibold", 23)
BODY_FONT = ("Segoe UI", 10)
SMALL_FONT = ("Segoe UI", 9)
MONO_FONT = ("Consolas", 9)

COLORS = {
    "bg": "#07111d",
    "bg_alt": "#0b1625",
    "panel": "#0d1828",
    "panel_alt": "#101f33",
    "panel_soft": "#12243a",
    "border": "#1c3656",
    "text": "#edf4ff",
    "muted": "#89a0bd",
    "accent": "#2d8cff",
    "accent_soft": "#153960",
    "cyan": "#4ca9ff",
    "orange": "#ff9d2f",
    "green": "#62d26f",
    "green_soft": "#14331d",
    "amber": "#f3b33d",
    "amber_soft": "#3f2c10",
    "red": "#ff6a63",
    "red_soft": "#3b1a1a",
}

WORKER_STATUS_OPTIONS = (
    "All statuses",
    "online",
    "idle",
    "ready",
    "rendering",
    "busy",
    "offline",
    "error",
    "unknown",
)

from renderfarm_shared.presentation import SESSION_STATUS_LABELS

STATUS_DISPLAY = {
    **SESSION_STATUS_LABELS,
    "online": "Online",
    "idle": "Idle",
    "ready": "Ready",
    "rendering": "Rendering",
    "busy": "Busy",
    "offline": "Offline",
    "error": "Error",
    "unknown": "Unknown",
}

JOB_STATUS_DISPLAY = {
    "queued": "Waiting",
    "validating": "Validating",
    "running": "Rendering",
    "paused": "Paused",
    "completed": "Completed",
    "cancelled": "Cancelled",
    "error": "Error",
    "blocked": "Blocked",
}

TREE_STATUS_TAGS = {
    "status_online": COLORS["text"],
    "status_idle": COLORS["green"],
    "status_ready": COLORS["green"],
    "status_rendering": COLORS["cyan"],
    "status_busy": COLORS["cyan"],
    "status_offline": COLORS["red"],
    "status_error": COLORS["red"],
    "status_unknown": COLORS["amber"],
    "status_queued": COLORS["amber"],
    "status_validating": COLORS["cyan"],
    "status_running": COLORS["cyan"],
    "status_completed": COLORS["green"],
    "status_cancelled": COLORS["muted"],
    "status_blocked": COLORS["red"],
    "status_paused": "#d0b3ff",
}


class CoordinatorWindow:
    def __init__(self, app: FastAPI, runtime: CoordinatorServerRuntime):
        self.app = app
        self.runtime = runtime
        self._configure_windows_identity()
        self.root = tk.Tk()
        self.root.title("Blender Render Farm Coordinator")
        self.root.geometry(f"{min(1640, self.root.winfo_screenwidth()-70)}x{min(940, self.root.winfo_screenheight()-100)}")
        self.root.minsize(760, 440)
        self._ui_images = {}
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id: str | None = None
        self._latest_logs = ""
        self._rebuilding_language = False
        self._busy_discovering_workers = False
        self._latest_state: dict[str, Any] | None = None
        self._latest_health: dict[str, Any] | None = None
        self._latest_logs_payload: dict[str, Any] | None = None
        self.language = normalize_language(self.app.state.service.settings.language)

        self.worker_search_var = tk.StringVar()
        self.worker_status_filter_var = tk.StringVar(value=self._t("All statuses"))
        self.subtitle_var = tk.StringVar(value=self._t("Manage workers, jobs and resources across your render farm"))
        self.coordinator_status_var = tk.StringVar(value="OFFLINE")
        self.server_mode_var = tk.StringVar(value="-")
        self.coordinator_id_var = tk.StringVar(value="-")
        self.api_urls_var = tk.StringVar(value="-")
        self.discovery_var = tk.StringVar(value="-")
        self.runtime_started_var = tk.StringVar(value="-")
        self.runtime_uptime_var = tk.StringVar(value="-")
        self.runtime_primary_url_var = tk.StringVar(value=self.runtime.local_url())
        self.worker_table_hint_var = tk.StringVar(value="0 worker(s)")
        self.footer_runtime_var = tk.StringVar(value=self._t("Coordinator offline"))
        self.footer_workers_var = tk.StringVar(value="Workers online: 0")
        self.footer_rendering_var = tk.StringVar(value="Renderizando: 0")
        self.footer_jobs_var = tk.StringVar(value="Jobs totais: 0")
        self.footer_clock_var = tk.StringVar(value="Hora local: -")
        self.header_date_var = tk.StringVar(value="-")
        self.header_time_var = tk.StringVar(value="-")
        self.metric_value_vars: dict[str, tk.StringVar] = {
            "masters": tk.StringVar(value="0"),
            "workers": tk.StringVar(value="0"),
            "rendering": tk.StringVar(value="0"),
            "jobs": tk.StringVar(value="0"),
            "queue": tk.StringVar(value="0"),
            "uptime": tk.StringVar(value="-"),
        }
        self.metric_detail_vars: dict[str, tk.StringVar] = {
            "masters": tk.StringVar(value=self._t("Online")),
            "workers": tk.StringVar(value=self._t("Connected")),
            "rendering": tk.StringVar(value=self._t("Rendering")),
            "jobs": tk.StringVar(value=self._t("Sent")),
            "queue": tk.StringVar(value=self._t("Waiting")),
            "uptime": tk.StringVar(value=self._t("Since startup")),
        }

        self._configure_style()
        self._load_icon()
        self._build_layout()
        self.worker_search_var.trace_add("write", self._on_worker_filter_changed)
        self.worker_status_filter_var.trace_add("write", self._on_worker_filter_changed)
        self._refresh_dashboard()

    def _t(self, text: str, **values: Any) -> str:
        return tr(text, self.language, **values)

    def run(self) -> None:
        self.root.mainloop()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.root.configure(bg=COLORS["bg"])

        style.configure(
            ".",
            background=COLORS["bg"],
            foreground=COLORS["text"],
            fieldbackground=COLORS["panel_soft"],
            troughcolor=COLORS["panel"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            font=BODY_FONT,
        )
        style.configure("Dashboard.TNotebook", background=COLORS["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure(
            "Dashboard.TNotebook.Tab",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            padding=(16, 8),
            font=SECTION_FONT,
            borderwidth=0,
        )
        style.map(
            "Dashboard.TNotebook.Tab",
            background=[("selected", COLORS["panel_soft"]), ("active", COLORS["panel_alt"])],
            foreground=[("selected", COLORS["text"]), ("active", COLORS["text"])],
        )
        style.configure(
            "Dashboard.Treeview",
            background=COLORS["panel_soft"],
            fieldbackground=COLORS["panel_soft"],
            foreground=COLORS["text"],
            rowheight=26,
            relief="flat",
            borderwidth=0,
            font=BODY_FONT,
        )
        style.configure(
            "Dashboard.Treeview.Heading",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            relief="flat",
            borderwidth=0,
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "Dashboard.Treeview",
            background=[("selected", COLORS["accent_soft"])],
            foreground=[("selected", COLORS["text"])],
        )
        style.map("Dashboard.Treeview.Heading", background=[("active", COLORS["panel_alt"])])
        style.configure(
            "Primary.TButton",
            background=COLORS["accent"],
            foreground=COLORS["text"],
            borderwidth=0,
            padding=(12, 8),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#4ca9ff"), ("pressed", "#256ebb")],
            foreground=[("disabled", COLORS["muted"])],
        )
        style.configure(
            "Secondary.TButton",
            background=COLORS["panel_alt"],
            foreground=COLORS["text"],
            borderwidth=0,
            padding=(12, 8),
            font=("Segoe UI", 9),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", COLORS["panel_soft"]), ("pressed", COLORS["accent_soft"])],
            foreground=[("disabled", COLORS["muted"])],
        )
        style.configure(
            "Danger.TButton",
            background="#21151a",
            foreground=COLORS["red"],
            borderwidth=0,
            padding=(12, 8),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#351d24"), ("pressed", "#4a2024")],
            foreground=[("active", "#ff8d87")],
        )
        style.configure(
            "Dashboard.TCombobox",
            fieldbackground=COLORS["panel_soft"],
            background=COLORS["panel_soft"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["muted"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            padding=4,
        )
        style.map(
            "Dashboard.TCombobox",
            fieldbackground=[("readonly", COLORS["panel_soft"])],
            background=[("readonly", COLORS["panel_soft"])],
            foreground=[("readonly", COLORS["text"])],
        )
        style.configure(
            "TScrollbar",
            background=COLORS["panel_alt"],
            troughcolor=COLORS["panel"],
            arrowcolor=COLORS["muted"],
            bordercolor=COLORS["panel"],
        )
        style.configure(
            "Dark.Horizontal.TScrollbar",
            background=COLORS["panel_alt"],
            troughcolor=COLORS["panel"],
            arrowcolor=COLORS["muted"],
            bordercolor=COLORS["panel"],
        )

    def _configure_windows_identity(self) -> None:
        if os.name != "nt":
            return
        app_id = "blender_render_farm_coordinator.desktop"
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            return

    def _load_icon(self) -> None:
        icon_dir = self.app.state.settings.project_root / "img"
        png_path = icon_dir / "icon_coordinator.png"
        ico_path = icon_dir / "icon_coordinator.ico"
        self._header_logo = None
        self._apply_windows_icon(ico_path)
        try:
            if not png_path.exists():
                return
            self._icon_image = tk.PhotoImage(file=str(png_path))
            self.root.iconphoto(True, self._icon_image)
            brand_path = resource_path(self.app.state.settings.project_root, "img", "brand_64.png")
            self._header_logo = tk.PhotoImage(file=str(brand_path)) if brand_path.is_file() else self._icon_image.subsample(4, 4)
        except tk.TclError:
            self._icon_image = None
            self._header_logo = None

    def _apply_windows_icon(self, ico_path: Path) -> None:
        if os.name != "nt":
            return
        if not ico_path.exists():
            return
        try:
            self.root.iconbitmap(default=str(ico_path))
        except tk.TclError:
            return

    def _build_layout(self) -> None:
        self.content_scroll = ScrollableFrame(self.root, background=COLORS["bg"], min_width=1180, min_height=800)
        self.content_scroll.pack(fill="both", expand=True)
        outer = tk.Frame(self.content_scroll.content, bg=COLORS["bg"], padx=10, pady=10)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=COLORS["bg"])
        header.pack(fill="x", pady=(0, 6))
        self._build_header(header)

        actions = tk.Frame(outer, bg=COLORS["bg"])
        actions.pack(fill="x", pady=(4, 10))
        self._build_actions_card(actions)

        metrics_row = tk.Frame(outer, bg=COLORS["bg"])
        metrics_row.pack(fill="x", pady=(0, 8))
        self._build_metrics_row(metrics_row)

        notebook = ttk.Notebook(outer, style="Dashboard.TNotebook")
        self.notebook = notebook
        notebook.pack(fill="both", expand=True)

        workers_tab = tk.Frame(notebook, bg=COLORS["bg"])
        jobs_tab = tk.Frame(notebook, bg=COLORS["bg"])
        masters_tab = tk.Frame(notebook, bg=COLORS["bg"])
        logs_tab = tk.Frame(notebook, bg=COLORS["bg"])

        notebook.add(workers_tab, text=self._t("Workers"))
        notebook.add(jobs_tab, text=self._t("Jobs"))
        notebook.add(masters_tab, text=self._t("Masters"))
        notebook.add(logs_tab, text=self._t("Logs"))

        self._build_workers_tab(workers_tab)
        self._build_jobs_tab(jobs_tab)
        self._build_masters_tab(masters_tab)
        self._build_logs_tab(logs_tab)

        footer_card, footer_body = self._create_card(outer, bg=COLORS["bg_alt"], padding=(12, 8))
        footer_card.pack(side="bottom", fill="x", pady=(8, 0), before=notebook)
        self._build_footer(footer_body)

    def _build_header(self, parent: tk.Frame) -> None:
        brand = tk.Frame(parent, bg=COLORS["bg"], padx=10, pady=14)
        brand.pack(side="left", fill="both")
        if self._header_logo is not None:
            tk.Label(brand, image=self._header_logo, bg=COLORS["bg"]).pack(side="left", padx=(0, 14))
        titles = tk.Frame(brand, bg=COLORS["bg"])
        titles.pack(side="left", fill="x", expand=True)
        tk.Label(titles, text="Blender Render Farm Coordinator", bg=COLORS["bg"], fg=COLORS["text"], font=TITLE_FONT).pack(anchor="w")
        tk.Label(titles, textvariable=self.subtitle_var, wraplength=530, justify="left", anchor="w",
                 bg=COLORS["bg"], fg=COLORS["muted"], font=BODY_FONT).pack(anchor="w", pady=(5, 0))
        card, body = self._create_card(parent, padding=(16, 14))
        card.pack(side="right", fill="both", expand=True, padx=(18, 0))
        self._build_coordinator_card(body)

    def _on_language_selected(self, _event=None) -> None:
        selected = self.language_var.get()
        language = next(
            (code for code, name in LANGUAGE_NAMES.items() if name == selected),
            "en",
        )
        if language == self.language:
            return
        self.app.state.service.set_language(language)
        self._rebuild_for_language(language)
        self._refresh_dashboard()

    def _rebuild_for_language(self, language: str) -> None:
        selected_tab = self.notebook.index(self.notebook.select())
        status_filter = self.worker_status_filter_var.get()
        is_all_statuses = status_filter == self._t("All statuses")
        previous_status = next((key for key, text in STATUS_DISPLAY.items() if self._t(text) == status_filter), status_filter)
        self._rebuilding_language = True
        self.language = language
        try:
            if is_all_statuses:
                self.worker_status_filter_var.set(self._t("All statuses"))
            else:
                self.worker_status_filter_var.set(self._t(STATUS_DISPLAY.get(previous_status, previous_status)))
            for key, label in {
                "masters": "Online", "workers": "Connected", "rendering": "Rendering",
                "jobs": "Sent", "queue": "Waiting", "uptime": "Since startup",
            }.items():
                self.metric_detail_vars[key].set(self._t(label))
            for child in self.root.winfo_children():
                child.destroy()
            # The new text widgets need the cached log contents inserted again.
            self._latest_logs = None
            self._build_layout()
            self.notebook.select(selected_tab)
        finally:
            self._rebuilding_language = False

    def _build_coordinator_card(self, parent: tk.Frame) -> None:
        for col in range(4):
            parent.grid_columnconfigure(col, weight=1)
        status = tk.Frame(parent, bg=COLORS["panel"])
        status.grid(row=0, column=0, sticky="nsew", padx=(0, 20))
        self.status_badge = tk.Label(status, textvariable=self.coordinator_status_var,
            bg=COLORS["green_soft"], fg=COLORS["green"], font=("Segoe UI Semibold", 10), padx=8, pady=3)
        self.status_badge.pack(anchor="w", pady=(0, 5))
        tk.Label(status, textvariable=self.runtime_primary_url_var, bg=COLORS["panel"], fg=COLORS["cyan"], font=BODY_FONT).pack(anchor="w")
        tk.Label(status, textvariable=self.runtime_uptime_var, bg=COLORS["panel"], fg=COLORS["muted"], font=BODY_FONT).pack(anchor="w")
        identity = tk.Frame(parent, bg=COLORS["panel"])
        identity.grid(row=0, column=1, sticky="nsew", padx=(0, 20))
        self._build_info_pair(identity, "Coordinator ID", self.coordinator_id_var, multiline=True)
        discovery = tk.Frame(parent, bg=COLORS["panel"])
        discovery.grid(row=0, column=2, sticky="nsew")
        self._build_info_pair(discovery, "Discovery", self.discovery_var, multiline=True)
        clock = tk.Frame(parent, bg=COLORS["panel"])
        clock.grid(row=0, column=3, sticky="nsew", padx=(18, 0))
        tk.Label(clock, textvariable=self.header_date_var, bg=COLORS["panel"], fg=COLORS["muted"], font=SMALL_FONT).pack(anchor="e")
        tk.Label(clock, textvariable=self.header_time_var, bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI Semibold", 18)).pack(anchor="e", pady=(5, 0))

    def _build_actions_card(self, parent: tk.Frame) -> None:
        self.language_var = tk.StringVar(value=LANGUAGE_NAMES[self.language])
        language_menu = ttk.Combobox(parent, textvariable=self.language_var,
            values=[LANGUAGE_NAMES[code] for code in ("en", "pt", "es")], state="readonly", width=11, style="Dashboard.TCombobox")
        language_menu.pack(side="left", padx=(0, 14))
        language_menu.bind("<<ComboboxSelected>>", self._on_language_selected)
        credit = tk.Label(parent, text="By Vinicius D'Arcadia Cruz", bg=COLORS["bg"], fg=COLORS["muted"], font=SMALL_FONT, cursor="hand2")
        credit.pack(side="left")
        credit.bind("<Button-1>", lambda _event: webbrowser.open("https://novaxd.tech/"))
        actions = tk.Frame(parent, bg=COLORS["bg"])
        actions.pack(side="right")
        specs = [("Refresh", self._refresh_dashboard, "Primary.TButton", "refresh-cw"),
                 ("Discover workers", self._discover_workers, "Secondary.TButton", "network"),
                 ("Open web dashboard", lambda: webbrowser.open(self.runtime.dashboard_url()), "Secondary.TButton", "external-link"),
                 ("Open docs", lambda: webbrowser.open(self.runtime.docs_url()), "Secondary.TButton", "book-open"),
                 ("Open state", lambda: webbrowser.open(self.runtime.state_url()), "Secondary.TButton", "database"),
                 ("Shut down", self._on_close, "Danger.TButton", "power")]
        for text, command, style, icon in specs:
            ttk.Button(actions, text=self._t(text), command=command, style=style,
                       image=self._icon(icon, "red" if icon == "power" else "blue", 20), compound="left").pack(side="left", padx=(6, 0))

    def _icon(self, name: str, tone: str = "blue", size: int = 40):
        key = f"{name}_{tone}_{size}"
        if key not in self._ui_images:
            path = resource_path(self.app.state.settings.project_root, "img", "icons", key + ".png")
            self._ui_images[key] = tk.PhotoImage(file=str(path)) if path.is_file() else None
        return self._ui_images[key]

    def _build_metrics_row(self, parent: tk.Frame) -> None:
        cards = [
            ("masters", self._t("Masters"), COLORS["orange"]),
            ("workers", self._t("Workers online"), COLORS["cyan"]),
            ("rendering", self._t("Rendering"), COLORS["accent"]),
            ("jobs", self._t("Jobs total"), COLORS["muted"]),
            ("queue", self._t("Queue waiting"), COLORS["accent"]),
            ("uptime", self._t("Uptime"), COLORS["orange"]),
        ]
        for index, (key, title, accent) in enumerate(cards):
            parent.grid_columnconfigure(index, weight=1)
            card, body = self._create_card(parent, bg=COLORS["panel_alt"], accent=accent, padding=(14, 12))
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 4, 0))
            self._build_metric_card(body, title, self.metric_value_vars[key], self.metric_detail_vars[key], accent, key=key)

    def _build_metric_card(
        self,
        parent: tk.Frame,
        title: str,
        value_var: tk.StringVar,
        detail_var: tk.StringVar,
        accent: str,
        *, key: str,
    ) -> None:
        names = {"masters": ("server", "purple"), "workers": ("users", "green"),
                 "rendering": ("settings", "blue"), "jobs": ("file-text", "muted"),
                 "queue": ("clock", "amber"), "uptime": ("timer", "green")}
        name, tone = names[key]
        tk.Label(parent, image=self._icon(name, tone), bg=parent["bg"], padx=8).pack(side="left", padx=(0, 12))
        text_column = tk.Frame(parent, bg=parent["bg"])
        text_column.pack(side="left", fill="both", expand=True)
        parent = text_column
        tk.Label(
            parent,
            text=title,
            bg=parent["bg"],
            fg=accent,
            font=("Segoe UI Semibold", 10),
        ).pack(anchor="w")
        tk.Label(
            parent,
            textvariable=value_var,
            bg=parent["bg"],
            fg=COLORS["text"],
            font=METRIC_VALUE_FONT,
        ).pack(anchor="w", pady=(4, 1))
        tk.Label(
            parent,
            textvariable=detail_var,
            bg=parent["bg"],
            fg=COLORS["muted"],
            font=SMALL_FONT,
        ).pack(anchor="w")

    def _build_workers_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=5)
        parent.grid_rowconfigure(2, weight=4)

        toolbar_card, toolbar_body = self._create_card(parent, bg=COLORS["panel"], padding=(12, 8))
        toolbar_card.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._build_workers_toolbar(toolbar_body)

        table_card, table_body = self._create_card(parent, bg=COLORS["panel"], accent=COLORS["accent"], padding=(0, 0))
        table_card.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        table_body.pack(fill="both", expand=True)

        table_header = tk.Frame(table_body, bg=COLORS["panel"], padx=12, pady=8)
        table_header.pack(fill="x")
        tk.Label(
            table_header,
            text=self._t("Workers"),
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=SECTION_FONT,
        ).pack(side="left")
        tk.Label(
            table_header,
            textvariable=self.worker_table_hint_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=SMALL_FONT,
        ).pack(side="right")

        table_body_inner = tk.Frame(table_body, bg=COLORS["panel"], padx=10, pady=0)
        table_body_inner.pack(fill="both", expand=True, pady=(0, 8))
        self.workers_tree = self._build_tree(
            table_body_inner,
            columns=(
                ("name", self._t("Name"), 150, "w"),
                ("status", "Status", 110, "center"),
                ("blender", "Blender", 90, "center"),
                ("address", "Address / IP", 150, "w"),
                ("port", self._t("Port"), 70, "center"),
                ("gpu", "GPU", 180, "w"),
                ("job", self._t("Current job"), 180, "w"),
                ("frame", "Frame", 85, "center"),
                ("progress", self._t("Progress"), 95, "center"),
                ("heartbeat", self._t("Last heartbeat"), 120, "center"),
                ("error", self._t("Error"), 220, "w"),
            ),
            height=5,
        )

        lower = tk.Frame(parent, bg=COLORS["bg"])
        lower.grid(row=2, column=0, sticky="nsew")
        lower.grid_columnconfigure(0, weight=3)
        lower.grid_columnconfigure(1, weight=4)
        lower.grid_rowconfigure(0, weight=0)

        queue_card, queue_body = self._create_card(lower, bg=COLORS["panel"], accent=COLORS["orange"], padding=(0, 0))
        queue_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        queue_header = tk.Frame(queue_body, bg=COLORS["panel"], padx=12, pady=8)
        queue_header.pack(fill="x")
        tk.Label(queue_header, text=self._t("Queue / Jobs"), bg=COLORS["panel"], fg=COLORS["orange"], font=SECTION_FONT).pack(side="left")
        queue_body_inner = tk.Frame(queue_body, bg=COLORS["panel"], padx=10, pady=0)
        queue_body_inner.pack(fill="both", expand=True, pady=(0, 8))
        self.queue_tree = self._build_tree(
            queue_body_inner,
            columns=(
                ("name", "Job", 180, "w"),
                ("status", "Status", 110, "center"),
                ("created", self._t("Created at"), 140, "center"),
                ("queue", self._t("Position"), 85, "center"),
                ("frames", "Frames", 160, "w"),
            ),
            height=6,
        )

        recent_logs_card, recent_logs_body = self._create_card(lower, bg=COLORS["panel"], accent=COLORS["amber"], padding=(0, 0))
        recent_logs_card.grid(row=0, column=1, sticky="nsew")
        recent_logs_header = tk.Frame(recent_logs_body, bg=COLORS["panel"], padx=12, pady=8)
        recent_logs_header.pack(fill="x")
        tk.Label(recent_logs_header, text=self._t("Recent logs"), bg=COLORS["panel"], fg=COLORS["amber"], font=SECTION_FONT).pack(side="left")
        recent_logs_inner = tk.Frame(recent_logs_body, bg=COLORS["panel"], padx=10, pady=0)
        recent_logs_inner.pack(fill="both", expand=True, pady=(0, 8))
        self.recent_logs_text = self._build_log_text(recent_logs_inner, height=7)

    def _build_workers_toolbar(self, parent: tk.Frame) -> None:
        left = tk.Frame(parent, bg=COLORS["panel"])
        left.pack(side="left", fill="x", expand=True)

        tk.Label(left, text=self._t("Search workers..."), bg=COLORS["panel"], fg=COLORS["muted"], font=SMALL_FONT).pack(side="left")
        search_entry = tk.Entry(
            left,
            textvariable=self.worker_search_var,
            bg=COLORS["panel_soft"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
            font=BODY_FONT,
            width=28,
        )
        search_entry.pack(side="left", padx=(6, 10), ipady=4)

        tk.Label(left, text=self._t("Status"), bg=COLORS["panel"], fg=COLORS["muted"], font=SMALL_FONT).pack(side="left")
        self.worker_status_combo = ttk.Combobox(
            left,
            textvariable=self.worker_status_filter_var,
            values=(self._t("All statuses"),) + tuple(self._t(STATUS_DISPLAY[k]) for k in WORKER_STATUS_OPTIONS[1:]),
            state="readonly",
            width=18,
            style="Dashboard.TCombobox",
        )
        self.worker_status_combo.pack(side="left", padx=(6, 0))

    def _build_jobs_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        card, body = self._create_card(parent, bg=COLORS["panel"], accent=COLORS["accent"], padding=(0, 0))
        card.grid(row=0, column=0, sticky="nsew")
        header = tk.Frame(body, bg=COLORS["panel"], padx=12, pady=8)
        header.pack(fill="x")
        tk.Label(header, text=self._t("Jobs"), bg=COLORS["panel"], fg=COLORS["text"], font=SECTION_FONT).pack(side="left")
        tk.Label(header, text=self._t("All jobs known to the Coordinator"), bg=COLORS["panel"], fg=COLORS["muted"], font=SMALL_FONT).pack(side="right")
        content = tk.Frame(body, bg=COLORS["panel"], padx=10, pady=0)
        content.pack(fill="both", expand=True, pady=(0, 8))
        self.jobs_tree = self._build_tree(
            content,
            columns=(
                ("jobId", "Job ID", 150, "w"),
                ("status", "Status", 110, "center"),
                ("name", "Name", 170, "w"),
                ("blender", "Blender", 200, "w"),
                ("progress", "Progress", 95, "center"),
                ("frames", "Frames", 200, "w"),
                ("master", "Master", 150, "w"),
                ("queue", "Queue", 70, "center"),
                ("error", "Last error", 240, "w"),
            ),
            height=18,
        )

    def _build_masters_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        card, body = self._create_card(parent, bg=COLORS["panel"], accent=COLORS["orange"], padding=(0, 0))
        card.grid(row=0, column=0, sticky="nsew")
        header = tk.Frame(body, bg=COLORS["panel"], padx=12, pady=8)
        header.pack(fill="x")
        tk.Label(header, text="MASTERS", bg=COLORS["panel"], fg=COLORS["text"], font=SECTION_FONT).pack(side="left")
        content = tk.Frame(body, bg=COLORS["panel"], padx=10, pady=0)
        content.pack(fill="both", expand=True, pady=(0, 8))
        self.masters_tree = self._build_tree(
            content,
            columns=(
                ("masterId", "Master ID", 180, "w"),
                ("name", "Name", 160, "w"),
                ("host", "Host", 170, "w"),
                ("user", "User", 140, "w"),
                ("version", "Version", 180, "w"),
                ("lastSeen", "Last heartbeat", 180, "center"),
            ),
            height=18,
        )

    def _build_logs_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        card, body = self._create_card(parent, bg=COLORS["panel"], accent=COLORS["amber"], padding=(0, 0))
        card.grid(row=0, column=0, sticky="nsew")
        header = tk.Frame(body, bg=COLORS["panel"], padx=12, pady=8)
        header.pack(fill="x")
        tk.Label(header, text=self._t("Coordinator log"), bg=COLORS["panel"], fg=COLORS["text"], font=SECTION_FONT).pack(side="left")
        tk.Label(header, text=self._t("Automatically refreshed every 2 seconds"), bg=COLORS["panel"], fg=COLORS["muted"], font=SMALL_FONT).pack(side="right")
        content = tk.Frame(body, bg=COLORS["panel"], padx=10, pady=0)
        content.pack(fill="both", expand=True, pady=(0, 8))
        self.logs_text = self._build_log_text(content, height=24)

    def _build_footer(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_columnconfigure(2, weight=1)
        parent.grid_columnconfigure(3, weight=1)
        parent.grid_columnconfigure(4, weight=1)

        labels = [
            self.footer_runtime_var,
            self.footer_workers_var,
            self.footer_rendering_var,
            self.footer_jobs_var,
            self.footer_clock_var,
        ]
        for index, variable in enumerate(labels):
            anchor = "w" if index < 4 else "e"
            tk.Label(
                parent,
                textvariable=variable,
                bg=COLORS["bg_alt"],
                fg=COLORS["muted"] if index != 0 else COLORS["green"],
                font=BODY_FONT,
                anchor=anchor,
            ).grid(row=0, column=index, sticky="ew", padx=(0, 10) if index < 4 else 0)

    def _build_info_pair(self, parent: tk.Frame, label: str, value_var: tk.StringVar, *, multiline: bool = False) -> None:
        block = tk.Frame(parent, bg=parent["bg"])
        block.pack(fill="x", pady=(0, 4))
        if not multiline:
            row = tk.Frame(block, bg=parent["bg"])
            row.pack(fill="x")
            tk.Label(row, text=f"{self._t(label)}:", bg=parent["bg"], fg=COLORS["muted"], font=SMALL_FONT).pack(side="left")
            tk.Label(
                row,
                textvariable=value_var,
                bg=parent["bg"],
                fg=COLORS["text"],
                font=BODY_FONT,
                justify="left",
            ).pack(side="left", padx=(6, 0))
            return

        tk.Label(block, text=self._t(label), bg=parent["bg"], fg=COLORS["muted"], font=SMALL_FONT).pack(anchor="w")
        tk.Label(
            block,
            textvariable=value_var,
            bg=parent["bg"],
            fg=COLORS["text"],
            font=BODY_FONT,
            justify="left",
            wraplength=195 if multiline else 0,
        ).pack(anchor="w", pady=(1, 0))

    def _create_card(
        self,
        parent: tk.Widget,
        *,
        bg: str | None = None,
        accent: str | None = None,
        padding: tuple[int, int] = (12, 10),
    ) -> tuple[tk.Frame, tk.Frame]:
        card_bg = bg or COLORS["panel"]
        card = tk.Frame(
            parent,
            bg=card_bg,
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        if accent:
            tk.Frame(card, bg=accent, height=2).pack(fill="x")
        body = tk.Frame(card, bg=card_bg, padx=padding[0], pady=padding[1])
        body.pack(fill="both", expand=True)
        return card, body

    def _build_tree(
        self,
        parent: tk.Widget,
        *,
        columns: tuple[tuple[str, str, int, str], ...],
        height: int,
    ) -> ttk.Treeview:
        container = tk.Frame(parent, bg=parent["bg"])
        container.pack(fill="both", expand=True)

        column_names = [column[0] for column in columns]
        tree = ttk.Treeview(
            container,
            columns=column_names,
            show="headings",
            height=height,
            style="Dashboard.Treeview",
        )
        y_scroll = ttk.Scrollbar(container, orient="vertical", command=tree.yview, style="Dark.Vertical.TScrollbar")
        x_scroll = ttk.Scrollbar(container, orient="horizontal", command=tree.xview, style="Dark.Horizontal.TScrollbar")
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        for column_name, heading, width, anchor in columns:
            tree.heading(column_name, text=self._t(heading))
            tree.column(column_name, width=width, minwidth=max(70, int(width * 0.65)), anchor=anchor)

        for tag_name, color in TREE_STATUS_TAGS.items():
            tree.tag_configure(tag_name, foreground=color)

        tree.pack(side="left", fill="both", expand=True)
        y_scroll.pack(side="right", fill="y")
        x_scroll.pack(side="bottom", fill="x")
        return tree

    def _build_log_text(self, parent: tk.Widget, *, height: int) -> ScrolledText:
        text = ScrolledText(
            parent,
            wrap="word",
            height=height,
            font=MONO_FONT,
            bg=COLORS["panel_soft"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=10,
            pady=10,
        )
        text.tag_configure("info", foreground="#dfe9f8")
        text.tag_configure("warning", foreground=COLORS["amber"])
        text.tag_configure("error", foreground=COLORS["red"])
        text.tag_configure("timestamp", foreground=COLORS["green"])
        text.pack(fill="both", expand=True)
        text.configure(state="disabled")
        return text

    def _refresh_dashboard(self) -> None:
        try:
            state = self.app.state.service.get_state()
            health = self.app.state.service.health()
            logs = self.app.state.service.get_logs()
            self._latest_state = state
            self._latest_health = health
            self._latest_logs_payload = logs
            self._render_cached_state()
        except Exception as exc:
            self.subtitle_var.set(self._t("Dashboard refresh failed: {error}", error=exc))
        finally:
            self._schedule_next_refresh()

    def _render_cached_state(self) -> None:
        if not self._latest_state or not self._latest_health or self._latest_logs_payload is None:
            return
        self._apply_dashboard_state(self._latest_state, self._latest_health, self._latest_logs_payload)

    def _apply_dashboard_state(self, state: dict[str, Any], health: dict[str, Any], logs: dict[str, Any]) -> None:
        coordinator = state.get("coordinator", {})
        incoming_language = normalize_language(coordinator.get("language"))
        if incoming_language != self.language:
            self._rebuild_for_language(incoming_language)
        discovery = coordinator.get("discovery") or health.get("discovery") or {}
        api_urls = discovery.get("apiUrls") or [self.runtime.local_url()]
        active_job = state.get("activeJob")
        metrics = state.get("metrics", {})
        workers = state.get("workers", [])
        jobs = state.get("jobs", [])
        masters = state.get("masters", [])

        server_state = "online" if self.runtime.is_running else "offline"
        self.server_mode_var.set(self._t(STATUS_DISPLAY.get(server_state, "Unknown")))
        self.coordinator_id_var.set(str(coordinator.get("coordinatorId", "-")))
        self.api_urls_var.set(self._format_api_urls(api_urls))
        discovery_status = str(discovery.get('status', '-'))
        discovery_label = self._t({'running': 'Running', 'disabled': 'Disabled', 'error': 'Error', 'stopped': 'Offline'}.get(discovery_status, discovery_status))
        self.discovery_var.set(
            f"{self._short_text(discovery.get('namespace', '-'), limit=28)} | "
            f"udp {discovery.get('discoveryPort', '-')} | {discovery_label}"
        )
        self.runtime_started_var.set(self._format_datetime(health.get("startedAt")))
        self.runtime_uptime_var.set(self._format_uptime(health.get("startedAt")))
        self.runtime_primary_url_var.set(api_urls[0] if api_urls else self.runtime.local_url())
        self._set_status_badge(server_state)

        if active_job and active_job.get("status") == "blocked" and active_job.get("lastError"):
            self.subtitle_var.set(self._t("Blocked job: {error}", error=self._short_text(active_job.get("lastError"), limit=180)))
        elif active_job:
            active_name = active_job.get("jobName", active_job.get("jobId", "-"))
            self.subtitle_var.set(self._t(
                "Active job: {name} | {status} | {progress}",
                name=active_name,
                status=self._job_status_label(active_job.get("status")),
                progress=self._format_percent(active_job.get("progressPercent")),
            ))
        else:
            self.subtitle_var.set(self._t("Manage workers, jobs and resources across your render farm"))

        self.metric_value_vars["masters"].set(str(len(masters)))
        self.metric_value_vars["workers"].set(str(metrics.get("machinesOnline", 0)))
        self.metric_value_vars["rendering"].set(str(metrics.get("machinesRendering", 0)))
        self.metric_value_vars["jobs"].set(str(len(jobs)))
        self.metric_value_vars["queue"].set(str(metrics.get("totalQueuedJobs", 0)))
        self.metric_value_vars["uptime"].set(self._format_uptime(health.get("startedAt")))

        filtered_workers = self._filter_workers(workers)
        self.worker_table_hint_var.set(self._t("{count} workers found", count=len(filtered_workers)))
        self._fill_tree(self.workers_tree, self._worker_rows(filtered_workers))
        self._fill_tree(self.queue_tree, self._queue_rows(jobs))
        self._fill_tree(self.jobs_tree, self._job_rows(jobs))
        self._fill_tree(self.masters_tree, self._master_rows(masters))
        self._set_logs(logs.get("general", ""))

        self.footer_runtime_var.set(self._t("Coordinator online") if self.runtime.is_running else self._t("Coordinator offline"))
        self.footer_workers_var.set(f"{self._t('Workers online')}: {metrics.get('machinesOnline', 0)}")
        self.footer_rendering_var.set(f"{self._t('Rendering')}: {metrics.get('machinesRendering', 0)}")
        self.footer_jobs_var.set(f"{self._t('Jobs total')}: {len(jobs)}")
        self.footer_clock_var.set(self._t("Local time: {value}", value=datetime.now().strftime('%d/%m/%Y %H:%M:%S')))
        self.header_date_var.set(datetime.now().strftime('%d/%m/%Y'))
        self.header_time_var.set(datetime.now().strftime('%H:%M:%S'))
        self.content_scroll.refresh()

    def _fill_tree(self, tree: ttk.Treeview, rows: list[dict[str, Any]]) -> None:
        tree.delete(*tree.get_children())
        for row in rows:
            tree.insert("", "end", values=row["values"], tags=row.get("tags", ()))

    def _worker_rows(self, workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        ordered_workers = sorted(workers, key=self._worker_sort_key)
        for worker in ordered_workers:
            status = str(worker.get("status") or "unknown").lower()
            rows.append(
                {
                    "values": (
                        worker.get("name", worker.get("workerId", "-")),
                        self._worker_status_label(status),
                        worker.get("blenderVersion") or "-",
                        worker.get("ip", "-"),
                        worker.get("port") if worker.get("port") is not None else "-",
                        self._short_text(worker.get("gpu"), limit=28),
                        worker.get("currentJobId") or "-",
                        worker.get("currentFrame") if worker.get("currentFrame") is not None else "-",
                        self._format_progress(worker.get("progress")),
                        self._format_relative_time(worker.get("lastSeen")),
                        self._short_text(worker.get("error"), limit=48),
                    ),
                    "tags": (f"status_{status}",),
                }
            )
        return rows

    def _queue_rows(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        visible_jobs = [
            job
            for job in jobs
            if str(job.get("status") or "").lower() in {"running", "validating", "paused", "blocked", "queued"}
        ]
        visible_jobs.sort(
            key=lambda job: (
                {"running": 0, "validating": 1, "paused": 2, "blocked": 3, "queued": 4}.get(str(job.get("status")).lower(), 9),
                int(job.get("queuePosition") or 9999),
                str(job.get("createdAt") or ""),
            )
        )
        rows: list[dict[str, Any]] = []
        for job in visible_jobs[:8]:
            status = str(job.get("status") or "").lower()
            rows.append(
                {
                    "values": (
                        job.get("jobName", job.get("jobId", "-")),
                        self._job_status_label(status),
                        self._format_datetime(job.get("createdAt"), include_seconds=True),
                        job.get("queuePosition") if job.get("queuePosition") is not None else "-",
                        self._format_job_frames(job),
                    ),
                    "tags": (f"status_{status}",),
                }
            )
        return rows

    def _job_rows(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for job in jobs:
            status = str(job.get("status") or "").lower()
            rows.append(
                {
                    "values": (
                        job.get("jobId", "-"),
                        self._job_status_label(status),
                        job.get("jobName", "-"),
                        self._format_job_blender(job),
                        self._format_percent(job.get("progressPercent")),
                        self._format_job_frames(job),
                        job.get("submittedByMasterName") or job.get("submittedByMasterId") or "-",
                        job.get("queuePosition") if job.get("queuePosition") is not None else "-",
                        self._short_text(job.get("lastError"), limit=56),
                    ),
                    "tags": (f"status_{status}",),
                }
            )
        return rows

    def _master_rows(self, masters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for master in masters:
            rows.append(
                {
                    "values": (
                        master.get("masterId", "-"),
                        master.get("name", "-"),
                        master.get("host", "-"),
                        master.get("username", "-"),
                        self._format_runtime_label(
                            master.get("version"),
                            master.get("protocolVersion"),
                            master.get("buildId"),
                        ),
                        self._format_relative_time(master.get("lastSeen")),
                    ),
                    "tags": ("status_online",),
                }
            )
        return rows

    def _set_logs(self, logs_text: str) -> None:
        if logs_text == self._latest_logs:
            return
        self._latest_logs = logs_text
        self._apply_logs_to_widget(self.logs_text, logs_text or "No logs yet.")
        recent_lines = "\n".join(logs_text.splitlines()[-14:]) if logs_text else "No recent logs."
        self._apply_logs_to_widget(self.recent_logs_text, recent_lines)

    def _apply_logs_to_widget(self, widget: ScrolledText, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for line in text.splitlines() or [text]:
            line_text = line.rstrip()
            if not line_text:
                widget.insert("end", "\n")
                continue
            severity_tag = self._log_severity_tag(line_text)
            widget.insert("end", line_text + "\n", severity_tag)
        widget.configure(state="disabled")
        widget.see("end")

    def _schedule_next_refresh(self) -> None:
        if self._after_id:
            self.root.after_cancel(self._after_id)
        self._after_id = self.root.after(REFRESH_INTERVAL_MS, self._refresh_dashboard)

    def _discover_workers(self) -> None:
        if self._busy_discovering_workers:
            return
        self._busy_discovering_workers = True
        self.subtitle_var.set(self._t("Discovering workers on the LAN..."))
        threading.Thread(target=self._run_discovery_in_background, daemon=True, name="worker-discovery-ui").start()

    def _run_discovery_in_background(self) -> None:
        message = self._t("Worker discovery finished.")
        try:
            result = self.app.state.service.discover_workers(None, None)
            message = self._t("{count} workers found", count=result.get("count", 0))
        except Exception as exc:
            message = self._t("Could not discover workers: {error}", error=exc)
        finally:
            self._busy_discovering_workers = False
            self.root.after(0, lambda: self._finish_background_action(message))

    def _finish_background_action(self, message: str) -> None:
        self.subtitle_var.set(message)
        self._refresh_dashboard()

    def _on_close(self) -> None:
        if self._after_id:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.subtitle_var.set(self._t("Shutting down the Coordinator..."))
        try:
            self.runtime.stop()
        except Exception as exc:
            messagebox.showwarning(self._t("Shut down"), self._t("Shutdown error: {error}", error=exc))
        finally:
            self.root.destroy()

    def _on_worker_filter_changed(self, *_args: Any) -> None:
        if not self._rebuilding_language:
            self._render_cached_state()

    def _filter_workers(self, workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        search_text = self.worker_search_var.get().strip().lower()
        status_filter = self.worker_status_filter_var.get().strip().lower()
        if status_filter in {
            WORKER_STATUS_OPTIONS[0].lower(),
            tr("All statuses", "pt").lower(),
            tr("All statuses", "es").lower(),
        }:
            status_filter = ""

        status_filter = next((key for key, text in STATUS_DISPLAY.items() if self._t(text).lower() == status_filter), status_filter)
        filtered: list[dict[str, Any]] = []
        for worker in workers:
            status = str(worker.get("status") or "").lower()
            if status_filter and status != status_filter:
                continue

            haystack = " ".join(
                [
                    str(worker.get("name") or ""),
                    str(worker.get("workerId") or ""),
                    str(worker.get("ip") or ""),
                    str(worker.get("gpu") or ""),
                    str(worker.get("currentJobId") or ""),
                    str(worker.get("error") or ""),
                ]
            ).lower()
            if search_text and search_text not in haystack:
                continue
            filtered.append(worker)
        return filtered

    def _set_status_badge(self, server_state: str) -> None:
        normalized = str(server_state or "offline").upper()
        self.coordinator_status_var.set(self._t("Coordinator online" if normalized == "ONLINE" else "Coordinator offline"))
        if normalized == "ONLINE":
            self.status_badge.configure(bg=COLORS["green_soft"], fg=COLORS["green"])
        else:
            self.status_badge.configure(bg=COLORS["red_soft"], fg=COLORS["red"])

    @staticmethod
    def _worker_sort_key(worker: dict[str, Any]) -> tuple[int, str]:
        status_rank = {
            "rendering": 0,
            "busy": 0,
            "idle": 1,
            "ready": 1,
            "online": 2,
            "unknown": 3,
            "error": 4,
            "offline": 5,
        }
        status = str(worker.get("status") or "").lower()
        name = str(worker.get("name") or worker.get("workerId") or "").lower()
        return status_rank.get(status, 9), name

    def _worker_status_label(self, status: str) -> str:
        return self._t(STATUS_DISPLAY.get(status, status.title() if status else "-"))

    def _job_status_label(self, status: Any) -> str:
        normalized = str(status or "").lower()
        return self._t(JOB_STATUS_DISPLAY.get(normalized, normalized.title() if normalized else "-"))

    @staticmethod
    def _log_severity_tag(line: str) -> str:
        upper = line.upper()
        if "[ERROR]" in upper or " ERROR " in upper:
            return "error"
        if "[WARNING]" in upper or "[WARN]" in upper:
            return "warning"
        return "info"

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value in {None, ""}:
            return None
        try:
            text = str(value).strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None

    @classmethod
    def _format_datetime(cls, value: Any, *, include_seconds: bool = True) -> str:
        parsed = cls._parse_datetime(value)
        if not parsed:
            return str(value or "-")
        local_dt = parsed.astimezone()
        return local_dt.strftime("%d/%m/%Y %H:%M:%S" if include_seconds else "%d/%m/%Y %H:%M")

    @classmethod
    def _format_uptime(cls, value: Any) -> str:
        parsed = cls._parse_datetime(value)
        if not parsed:
            return "-"
        delta = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
        total_seconds = max(int(delta.total_seconds()), 0)
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        if days > 0:
            return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _format_relative_time(self, value: Any) -> str:
        return relative_age(value, self.language)

    @staticmethod
    def _format_progress(value: Any) -> str:
        if value is None:
            return "-"
        try:
            return f"{float(value):.1f}%"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _format_percent(value: Any) -> str:
        if value is None:
            return "0%"
        try:
            return f"{float(value):.1f}%"
        except (TypeError, ValueError):
            return str(value)

    def _format_job_frames(self, job: dict[str, Any]) -> str:
        stats = job.get("frameStats") or {}
        total = job.get("totalFrames", 0)
        done = stats.get("done", 0)
        return f"{done} / {total}"

    def _format_job_blender(self, job: dict[str, Any]) -> str:
        parts: list[str] = []
        detected = job.get("blendVersionDetected")
        required = job.get("requiredBlenderVersion")
        preferred_major = job.get("preferredBlenderMajor")
        policy = job.get("versionRoutingPolicy")

        if detected:
            parts.append(self._t("File {version}", version=detected))
        if required and required != detected:
            parts.append(self._t("Required {version}", version=required))
        if preferred_major:
            parts.append(self._t("Preferred {version}", version=f"{preferred_major}.x"))
        if policy:
            parts.append(self._t("Compatibility policy: {policy}", policy=policy))
        return " | ".join(parts) if parts else "-"

    @staticmethod
    def _short_text(value: Any, *, limit: int) -> str:
        if value in {None, ""}:
            return "-"
        text = str(value).strip()
        if len(text) <= limit:
            return text
        return text[: max(limit - 3, 0)].rstrip() + "..."

    @staticmethod
    def _format_api_urls(urls: list[Any]) -> str:
        normalized = [str(url).strip() for url in urls if str(url).strip()]
        if not normalized:
            return "-"
        if len(normalized) == 1:
            return normalized[0]
        return f"{normalized[0]} | +{len(normalized) - 1} URL(s)"

    @staticmethod
    def _format_runtime_label(version: Any, protocol: Any, build_id: Any) -> str:
        version_text = str(version).strip() if version not in {None, ""} else "-"
        protocol_text = f"p{protocol}" if protocol not in {None, ""} else "p?"
        build_text = str(build_id).strip() if build_id not in {None, ""} else ""
        if build_text:
            build_text = build_text[:12]
            return f"{version_text} | {protocol_text} | {build_text}"
        return f"{version_text} | {protocol_text}"


def launch_coordinator_window(app: FastAPI) -> None:
    runtime = CoordinatorServerRuntime(app)
    try:
        runtime.start()
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Coordinator", tr("Could not start the Coordinator.\n\n{error}", app.state.settings.language, error=exc))
        root.destroy()
        raise SystemExit(1) from exc

    try:
        window = CoordinatorWindow(app, runtime)
        window.run()
    finally:
        if runtime.is_running:
            runtime.stop()
