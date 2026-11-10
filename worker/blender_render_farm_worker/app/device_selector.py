from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
from queue import SimpleQueue, Empty
import threading
import urllib.request
from typing import Any

from renderfarm_shared.i18n import LANGUAGE_NAMES, normalize_language, tr
from renderfarm_shared.presentation import relative_age, resource_path, parse_timestamp, SESSION_STATUS_LABELS
from renderfarm_shared.tk_layout import ScrollableFrame

from .config import ConfigManager, render_device_preference_to_use_gpu
from .models import RenderDevicePreferenceRequest
from .status_manager import WorkerStatusManager


STATUS_LABELS = {
    **SESSION_STATUS_LABELS,
    "connecting": "Connecting",
    "registered": "Connection established",
    "starting": "Starting",
    "idle": "Ready",
    "rendering": "Rendering",
    "validating": "Validating",
    "cancelling": "Stopping",
    "disabled": "Paused",
    "error": "Error",
    "discovering": "Discovering",
    "connected": "Connection established",
    "conflict": "Conflict",
    "disconnected": "Offline",
}


def humanize_status(status: str, language: str = "en") -> str:
    normalized = status.strip().lower()
    return tr(STATUS_LABELS.get(normalized, normalized.replace("_", " ").title()), language)


def display_status(snapshot: dict[str, Any], language: str = "en") -> str:
    api_state = str(snapshot.get("apiServerState") or "").lower()
    if api_state in {"error", "stopped"}:
        return "API Offline" if normalize_language(language) == "en" else tr("API offline", language)

    status = str(snapshot.get("status") or "")
    if snapshot.get("error") and status.lower() not in {"rendering", "cancelling"}:
        return tr("Attention", language)

    return humanize_status(status, language)


def format_relative_time(raw_value: str | None, language: str = "en") -> str:
    return relative_age(raw_value, language, empty=tr("Waiting", language))


@dataclass
class DeviceCardWidgets:
    border: Any
    body: Any
    content: Any
    text_wrap: Any
    indicator_border: Any
    indicator_fill: Any
    indicator_label: Any
    icon_canvas: Any
    title_label: Any
    description_label: Any


class RenderDeviceWindow:
    WINDOW_WIDTH = 1620
    WINDOW_HEIGHT = 940
    COLORS = {
        "app_bg": "#030A15",
        "shell_border": "#0D60D7",
        "shell_bg": "#071120",
        "shell_inner": "#041324",
        "panel_border": "#174366",
        "panel_bg": "#071b2e",
        "panel_bg_alt": "#0A1524",
        "card_bg": "#0F1B2D",
        "card_bg_alt": "#101D31",
        "card_selected_bg": "#112540",
        "card_selected_edge": "#2E81FF",
        "card_idle_edge": "#2A3C53",
        "card_hover_edge": "#3E5C80",
        "line": "#1D2D42",
        "text": "#F5F7FB",
        "muted": "#8EA3BF",
        "soft": "#657A99",
        "accent": "#5CA4FF",
        "accent_strong": "#2E81FF",
        "accent_deep": "#0F3E79",
        "warning": "#F6A532",
        "warning_bg": "#362410",
        "success": "#79E88A",
        "success_bg": "#133022",
        "danger": "#FF7C7C",
        "danger_bg": "#39171B",
        "neutral_bg": "#19273B",
    }

    def __init__(
        self,
        *,
        config_manager: ConfigManager,
        status_manager: WorkerStatusManager,
        logger,
    ) -> None:
        self.config_manager = config_manager
        self.status_manager = status_manager
        self.logger = logger
        self._tk = None
        self._root = None
        self._logo_images: dict[str, Any] = {}
        self._ui: dict[str, Any] = {}
        self._device_cards: dict[str, DeviceCardWidgets] = {}
        self._updating = False
        self._content_width = 1180
        self.language = normalize_language(config_manager.config.language)
        self._on_close_callback: Callable[[], None] | None = None
        self._connection_results = SimpleQueue()
        self._testing_connection = False

    def _t(self, text: str, **values: Any) -> str:
        return tr(text, self.language, **values)

    def run(self, *, on_close: Callable[[], None] | None = None) -> bool:
        if not self.config_manager.config.showRenderDeviceWindow:
            self.logger.info("Render device window disabled by config")
            return False

        try:
            import tkinter as tk
        except ImportError as exc:
            self.logger.warning("Tkinter not available, render device window skipped: %s", exc)
            return False

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.logger.warning("Render device window could not start in this session: %s", exc)
            return False

        self._tk = tk
        self._root = root
        self._ui = {}
        self._device_cards = {}
        self._updating = False

        root.title("Blender Render Farm Worker")
        root.configure(bg=self.COLORS["app_bg"])
        self._configure_geometry(root)
        self._apply_window_icon(root)
        self._load_brand_images()
        self._build_window(on_close=on_close)
        self._sync_from_config()
        self._refresh_status()
        root.mainloop()
        return True

    def _configure_geometry(self, root) -> None:
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        width = min(self.WINDOW_WIDTH, max(screen_width - 70, 760))
        height = min(self.WINDOW_HEIGHT, max(screen_height - 100, 440))
        x = max((screen_width - width) // 2, 24)
        y = max((screen_height - height) // 2, 24)
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.minsize(760, 440)
        root.resizable(True, True)
        self._content_width = width - 34

    def _build_window(self, *, on_close: Callable[[], None] | None = None) -> None:
        tk = self._tk
        root = self._root
        if tk is None or root is None:
            return

        from tkinter import ttk

        self._on_close_callback = on_close or self._on_close_callback
        self.content_scroll = ScrollableFrame(root, background=self.COLORS["app_bg"], min_width=1120)
        self.content_scroll.pack(fill="both", expand=True)
        shell = tk.Frame(self.content_scroll.content, bg=self.COLORS["shell_border"])
        shell.pack(fill="both", expand=True, padx=10, pady=10)
        shell_inner = tk.Frame(shell, bg=self.COLORS["shell_inner"])
        shell_inner.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(shell_inner, bg=self.COLORS["shell_inner"])
        header.pack(fill="x", padx=24, pady=(16, 14))
        brand = tk.Frame(header, bg=self.COLORS["shell_inner"])
        brand.pack(side="left")

        if self._logo_images.get("header") is not None:
            tk.Label(brand, image=self._logo_images["header"], bg=self.COLORS["shell_inner"]).pack(
                side="left", padx=(0, 14)
            )
        brand_text = tk.Frame(brand, bg=self.COLORS["shell_inner"])
        brand_text.pack(side="left")
        tk.Label(
            brand_text, text="Blender Render Farm Worker", bg=self.COLORS["shell_inner"],
            fg=self.COLORS["text"], font=("Segoe UI Semibold", 23)
        ).pack(anchor="w")
        tk.Label(
            brand_text, text=self._t("Connect, render and collaborate across the farm"),
            bg=self.COLORS["shell_inner"], fg=self.COLORS["muted"], font=("Segoe UI", 12)
        ).pack(anchor="w", pady=(3, 0))

        header_actions = tk.Frame(header, bg=self.COLORS["shell_inner"])
        header_actions.pack(side="right")
        tk.Label(
            header_actions, text=f"v{self.status_manager.runtime_metadata['appVersion']}",
            bg=self.COLORS["shell_inner"], fg=self.COLORS["muted"], font=("Segoe UI", 11)
        ).pack(side="left", padx=(0, 18))
        language_var = tk.StringVar(value=LANGUAGE_NAMES[self.language])
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(
            "RenderFarm.TCombobox", fieldbackground=self.COLORS["panel_bg"],
            background=self.COLORS["panel_bg"], foreground=self.COLORS["text"],
            arrowcolor=self.COLORS["accent"], bordercolor=self.COLORS["panel_border"],
            lightcolor=self.COLORS["panel_border"], darkcolor=self.COLORS["panel_border"],
        )
        style.map("RenderFarm.TCombobox",
                  fieldbackground=[("readonly", self.COLORS["panel_bg"])],
                  foreground=[("readonly", self.COLORS["text"])])
        style.configure("RenderFarm.TButton", background=self.COLORS["card_bg_alt"],
                        foreground=self.COLORS["text"], bordercolor=self.COLORS["panel_border"],
                        lightcolor=self.COLORS["panel_border"], darkcolor=self.COLORS["panel_border"],
                        padding=(16, 6), font=("Segoe UI Semibold", 11))
        style.map("RenderFarm.TButton", background=[("active", self.COLORS["accent_deep"])],
                  foreground=[("disabled", self.COLORS["soft"])])
        style.configure("TScrollbar", background=self.COLORS["accent_deep"],
                        troughcolor=self.COLORS["shell_inner"], bordercolor=self.COLORS["shell_inner"],
                        arrowcolor=self.COLORS["accent"])
        root.option_add("*TCombobox*Listbox.background", self.COLORS["panel_bg"])
        root.option_add("*TCombobox*Listbox.foreground", self.COLORS["text"])
        language_menu = ttk.Combobox(
            header_actions, width=11, state="readonly", textvariable=language_var,
            values=[LANGUAGE_NAMES[code] for code in ("en", "pt", "es")],
            style="RenderFarm.TCombobox"
        )
        language_menu.pack(side="left", padx=(0, 16))
        language_menu.bind("<<ComboboxSelected>>", self._on_language_selected)

        pill_border = tk.Frame(header_actions, bg=self.COLORS["panel_border"])
        pill_border.pack(side="left")
        pill_inner = tk.Frame(pill_border, bg=self.COLORS["neutral_bg"])
        pill_inner.pack(fill="both", expand=True, padx=1, pady=1)
        pill_text = tk.Label(
            pill_inner, text=self._t("Starting"), bg=self.COLORS["neutral_bg"],
            fg=self.COLORS["warning"], font=("Segoe UI Semibold", 12), padx=16, pady=7
        )
        pill_text.pack()

        tk.Frame(shell_inner, bg=self.COLORS["line"], height=1).pack(fill="x")
        content = tk.Frame(shell_inner, bg=self.COLORS["shell_inner"])
        content.pack(fill="both", expand=True, padx=24, pady=18)

        hero_border, hero = self._create_panel(content, padding=(22, 24))
        hero_border.pack(fill="x", pady=(0, 16))
        hero.grid_columnconfigure(1, weight=1)
        hero.grid_columnconfigure(2, minsize=220)

        badge_border = tk.Frame(hero, bg=self.COLORS["accent_deep"])
        badge_border.grid(row=0, column=0, rowspan=3, sticky="nsw", padx=(0, 22))
        badge_inner = tk.Frame(badge_border, bg=self.COLORS["card_bg_alt"], width=120, height=124)
        badge_inner.pack(fill="both", expand=True, padx=1, pady=1)
        badge_inner.pack_propagate(False)
        if self._logo_images.get("hero") is not None:
            tk.Label(badge_inner, image=self._logo_images["hero"], bg=self.COLORS["card_bg_alt"]).pack(expand=True)

        tk.Label(
            hero, text=self._t("Worker"), bg=self.COLORS["panel_bg"], fg=self.COLORS["muted"],
            font=("Segoe UI", 11)
        ).grid(row=0, column=1, sticky="w")
        worker_name = tk.Label(
            hero, text=self.status_manager.machine_name, bg=self.COLORS["panel_bg"],
            fg=self.COLORS["text"], font=("Segoe UI Semibold", 30)
        )
        worker_name.grid(row=1, column=1, sticky="w", pady=(3, 2))
        worker_detail = tk.Label(
            hero, text=self._t("Checking Blender availability and waiting for the next render block"),
            bg=self.COLORS["panel_bg"], fg=self.COLORS["accent"], font=("Segoe UI", 12),
            justify="left", anchor="w", wraplength=max(self._content_width - 500, 400)
        )
        worker_detail.grid(row=2, column=1, sticky="w")

        status_container = tk.Frame(hero, bg=self.COLORS["panel_bg"])
        status_container.grid(row=0, column=2, rowspan=3, sticky="e", padx=(26, 0))
        tk.Label(
            status_container, text=self._t("Worker status"), bg=self.COLORS["panel_bg"],
            fg=self.COLORS["muted"], font=("Segoe UI", 11)
        ).pack(anchor="w")
        hero_status_border = tk.Frame(status_container, bg=self.COLORS["panel_border"])
        hero_status_border.pack(fill="x", pady=(8, 6))
        hero_status_inner = tk.Frame(hero_status_border, bg=self.COLORS["warning_bg"])
        hero_status_inner.pack(fill="both", expand=True, padx=1, pady=1)
        hero_status_label = tk.Label(
            hero_status_inner, text=self._t("Starting"), bg=self.COLORS["warning_bg"],
            fg=self.COLORS["warning"], font=("Segoe UI Semibold", 18), padx=30, pady=10
        )
        hero_status_label.pack(fill="x")
        hero_hint = tk.Label(
            status_container, text=self._t("Ready to receive jobs"), bg=self.COLORS["panel_bg"],
            fg=self.COLORS["muted"], font=("Segoe UI", 11), wraplength=230, justify="left"
        )
        hero_hint.pack(anchor="w")

        metrics = tk.Frame(content, bg=self.COLORS["shell_inner"])
        metrics.pack(fill="x", pady=(0, 16))
        metric_specs = [
            ("machine", self._t("Machine name"), self.status_manager.machine_name, "Windows", "worker", self.COLORS["accent"]),
            ("blender", "BLENDER", self._t("Waiting"), self._t("Preparing services"), "blender", self.COLORS["success"]),
            ("gpu", "GPU", self._t("Waiting"), self._t("Preparing services"), "gpu", self.COLORS["accent"]),
            ("heartbeat", self._t("Last heartbeat"), self._t("Waiting"), self._t("No coordinator heartbeat yet"), "pulse", self.COLORS["success"]),
            ("job", self._t("Current job"), "-", self._t("Waiting..."), "shield", self.COLORS["accent"]),
        ]
        metric_cards = {}
        for index, (key, title, value, detail, icon_name, color) in enumerate(metric_specs):
            metrics.grid_columnconfigure(index, weight=1, uniform="metrics")
            card = self._create_metric_card(
                metrics, title=title, value=value, detail=detail, icon_name=icon_name, icon_color=color
            )
            card["border"].grid(
                row=0, column=index, sticky="nsew",
                padx=(0 if index == 0 else 5, 0 if index == len(metric_specs) - 1 else 5)
            )
            metric_cards[key] = card

        device_border, device_panel = self._create_panel(content, padding=(22, 18))
        device_border.pack(fill="x", pady=(0, 16))
        tk.Label(
            device_panel, text=self._t("Render device for this machine"),
            bg=self.COLORS["panel_bg"], fg=self.COLORS["accent"],
            font=("Segoe UI Semibold", 14)
        ).pack(anchor="w")
        tk.Label(
            device_panel, text=self._t("Choose the device used to render on this worker"),
            bg=self.COLORS["panel_bg"], fg=self.COLORS["muted"], font=("Segoe UI", 11)
        ).pack(anchor="w", pady=(3, 12))
        cards = tk.Frame(device_panel, bg=self.COLORS["panel_bg"])
        cards.pack(fill="x")
        cards.grid_columnconfigure(0, weight=1)
        cards.grid_columnconfigure(1, weight=1)
        cpu_card = self._create_device_card(
            cards, preference="CPU", title="CPU", description=self._t("Use CPU for rendering"),
            icon_name="cpu", command=self._set_cpu
        )
        cpu_card.border.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        gpu_card = self._create_device_card(
            cards, preference="GPU", title="GPU", description=self._t("Use GPU for rendering"),
            icon_name="gpu", command=self._set_gpu
        )
        gpu_card.border.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        self._device_cards = {"CPU": cpu_card, "GPU": gpu_card}

        api_border, api_panel = self._create_panel(content, padding=(22, 15))
        api_border.pack(fill="x")
        api_row = tk.Frame(api_panel, bg=self.COLORS["panel_bg"])
        api_row.pack(fill="x")
        self._create_icon_canvas(
            api_row, icon_name="globe", color=self.COLORS["accent"], size=34,
            background=self.COLORS["panel_bg"]
        ).pack(side="left", padx=(0, 16))
        api_text = tk.Frame(api_row, bg=self.COLORS["panel_bg"])
        api_text.pack(side="left", fill="x", expand=True)
        tk.Label(
            api_text, text=self._t("API / Coordinator"), bg=self.COLORS["panel_bg"],
            fg=self.COLORS["muted"], font=("Segoe UI", 11)
        ).pack(anchor="w")
        api_value = tk.Label(
            api_text, text=f"http://{self.status_manager.ip}:{self.status_manager.port}",
            bg=self.COLORS["panel_bg"], fg=self.COLORS["accent"], font=("Segoe UI Semibold", 18)
        )
        api_value.pack(anchor="w", pady=(3, 1))
        api_detail = tk.Label(
            api_text, text=self._t("Awaiting coordinator instructions"), bg=self.COLORS["panel_bg"],
            fg=self.COLORS["soft"], font=("Segoe UI", 11), anchor="w", justify="left", wraplength=750
        )
        api_detail.pack(anchor="w")

        test_box = tk.Frame(api_row, bg=self.COLORS["panel_bg"])
        test_box.pack(side="right", padx=(24, 0))
        test_button = ttk.Button(test_box, text=self._t("Test connection"), command=self._test_connection, style="RenderFarm.TButton")
        test_button.pack(anchor="e", ipady=7)
        test_detail = tk.Label(test_box, text="", bg=self.COLORS["panel_bg"], fg=self.COLORS["muted"],
                               font=("Segoe UI", 9), wraplength=200, justify="left")
        test_detail.pack(anchor="e", pady=(4, 0))

        connection = tk.Frame(api_row, bg=self.COLORS["panel_bg"])
        connection.pack(side="right", padx=(20, 0))
        tk.Label(
            connection, text=self._t("Connection status"), bg=self.COLORS["panel_bg"],
            fg=self.COLORS["muted"], font=("Segoe UI", 11)
        ).pack(anchor="w")
        connection_value = tk.Label(
            connection, text=self._t("Waiting"), bg=self.COLORS["panel_bg"],
            fg=self.COLORS["warning"], font=("Segoe UI Semibold", 13)
        )
        connection_value.pack(anchor="w", pady=(4, 0))

        footer = tk.Frame(shell_inner, bg=self.COLORS["shell_inner"])
        footer.pack(fill="x", padx=24, pady=(0, 12))
        tk.Label(
            footer, text="Blender Render Farm Worker  •  " + self._t("Coordinator controlled"),
            bg=self.COLORS["shell_inner"], fg=self.COLORS["soft"], font=("Segoe UI", 11)
        ).pack(side="left")
        tk.Label(
            footer, text=self._t("A faster tomorrow, one frame at a time."),
            bg=self.COLORS["shell_inner"], fg=self.COLORS["soft"], font=("Segoe UI", 11)
        ).pack(side="right")

        self._ui = {
            "root": root, "language_menu": language_menu, "language_var": language_var,
            "top_status_border": pill_border, "top_status_inner": pill_inner, "top_status_label": pill_text,
            "hero_status_border": hero_status_border, "hero_status_inner": hero_status_inner,
            "hero_status_label": hero_status_label, "hero_hint": hero_hint, "worker_name": worker_name,
            "worker_detail": worker_detail, "api_value": api_value, "api_detail": api_detail,
            "connection_value": connection_value, "test_button": test_button, "test_detail": test_detail,
            "machine_value": metric_cards["machine"]["value"],
            "machine_detail": metric_cards["machine"]["detail"],
            "blender_value": metric_cards["blender"]["value"],
            "blender_detail": metric_cards["blender"]["detail"],
            "gpu_value": metric_cards["gpu"]["value"],
            "gpu_detail": metric_cards["gpu"]["detail"],
            "heartbeat_value": metric_cards["heartbeat"]["value"],
            "heartbeat_detail": metric_cards["heartbeat"]["detail"],
            "job_value": metric_cards["job"]["value"],
            "job_detail": metric_cards["job"]["detail"],
        }

        def handle_close() -> None:
            self.logger.info("Render device window closed by user")
            if self._on_close_callback is not None:
                self._on_close_callback()
            root.destroy()
        root.protocol("WM_DELETE_WINDOW", handle_close)

    def _create_panel(self, parent, *, padding: tuple[int, int]) -> tuple[Any, Any]:
        tk = self._tk
        border = tk.Frame(parent, bg=self.COLORS["panel_border"])
        inner = tk.Frame(border, bg=self.COLORS["panel_bg"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        if padding != (0, 0):
            content = tk.Frame(inner, bg=self.COLORS["panel_bg"])
            content.pack(fill="both", expand=True, padx=padding[0], pady=padding[1])
            return border, content
        return border, inner

    def _create_device_card(
        self,
        parent,
        *,
        preference: str,
        title: str,
        description: str,
        icon_name: str,
        command: Callable[[], None],
    ) -> DeviceCardWidgets:
        tk = self._tk
        border = tk.Frame(parent, bg=self.COLORS["card_idle_edge"], cursor="hand2")
        body = tk.Frame(border, bg=self.COLORS["card_bg"], cursor="hand2")
        body.pack(fill="both", expand=True, padx=1, pady=1)

        content = tk.Frame(body, bg=self.COLORS["card_bg"], cursor="hand2")
        content.pack(fill="both", expand=True, padx=22, pady=22)
        content.grid_columnconfigure(2, weight=1)

        indicator_border = tk.Frame(content, bg=self.COLORS["soft"], cursor="hand2")
        indicator_border.grid(row=0, column=0, sticky="w", pady=4)
        indicator_fill = tk.Frame(
            indicator_border,
            bg=self.COLORS["card_bg"],
            width=36,
            height=36,
            cursor="hand2",
        )
        indicator_fill.pack(fill="both", expand=True, padx=1, pady=1)
        indicator_fill.pack_propagate(False)
        indicator_label = tk.Label(
            indicator_fill,
            text="",
            bg=self.COLORS["card_bg"],
            fg=self.COLORS["text"],
            font=("Segoe UI Semibold", 13),
            cursor="hand2",
        )
        indicator_label.pack(expand=True)

        icon_canvas = self._create_icon_canvas(
            content,
            icon_name=icon_name,
            color=self.COLORS["soft"],
            size=40,
            background=self.COLORS["card_bg"],
        )
        icon_canvas.grid(row=0, column=1, sticky="w", padx=(18, 20))

        text_wrap = tk.Frame(content, bg=self.COLORS["card_bg"], cursor="hand2")
        text_wrap.grid(row=0, column=2, sticky="nsew")

        title_label = tk.Label(
            text_wrap,
            text=title,
            bg=self.COLORS["card_bg"],
            fg=self.COLORS["text"],
            font=("Segoe UI Semibold", 18),
            cursor="hand2",
        )
        title_label.pack(anchor="w")

        description_label = tk.Label(
            text_wrap,
            text=description,
            bg=self.COLORS["card_bg"],
            fg=self.COLORS["muted"],
            font=("Segoe UI", 11),
            anchor="w",
            justify="left",
            cursor="hand2",
        )
        description_label.pack(anchor="w", pady=(2, 0))

        self._bind_click(
            [border, body, content, indicator_border, indicator_fill, indicator_label, icon_canvas, text_wrap, title_label, description_label],
            command,
        )
        border._icon_name = preference.lower()
        return DeviceCardWidgets(
            border=border,
            body=body,
            content=content,
            text_wrap=text_wrap,
            indicator_border=indicator_border,
            indicator_fill=indicator_fill,
            indicator_label=indicator_label,
            icon_canvas=icon_canvas,
            title_label=title_label,
            description_label=description_label,
        )

    def _create_metric_card(
        self,
        parent,
        *,
        title: str,
        value: str,
        detail: str,
        icon_name: str,
        icon_color: str,
    ) -> dict[str, Any]:
        tk = self._tk
        border = tk.Frame(parent, bg=self.COLORS["panel_border"])
        body = tk.Frame(border, bg=self.COLORS["panel_bg"])
        body.pack(fill="both", expand=True, padx=1, pady=1)

        row = tk.Frame(body, bg=self.COLORS["panel_bg"])
        row.pack(fill="both", expand=True, padx=14, pady=22)

        icon_canvas = self._create_icon_canvas(
            row,
            icon_name=icon_name,
            color=icon_color,
            size=40,
            background=self.COLORS["panel_bg"],
        )
        icon_canvas.pack(side="left", padx=(0, 8))

        text_wrap = tk.Frame(row, bg=self.COLORS["panel_bg"])
        text_wrap.pack(side="left", fill="x", expand=True)

        tk.Label(
            text_wrap,
            text=title,
            wraplength=210, anchor="w", justify="left",
            bg=self.COLORS["panel_bg"],
            fg=self.COLORS["muted"],
            font=("Segoe UI", 10),
        ).pack(anchor="w")

        value_label = tk.Label(
            text_wrap,
            text=value,
            bg=self.COLORS["panel_bg"],
            fg=self.COLORS["text"],
            font=("Segoe UI Semibold", 12),
        )
        value_label.configure(wraplength=210, anchor="w", justify="left")
        value_label.pack(anchor="w", pady=(6, 5))

        detail_label = tk.Label(
            text_wrap,
            text=detail,
            bg=self.COLORS["panel_bg"],
            fg=self.COLORS["soft"],
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=210,
        )
        detail_label.pack(anchor="w")

        return {
            "border": border,
            "body": body,
            "icon": icon_canvas,
            "value": value_label,
            "detail": detail_label,
        }

    def _bind_click(self, widgets: list[Any], command: Callable[[], None]) -> None:
        for widget in widgets:
            widget.bind("<Button-1>", lambda _event, callback=command: callback())

    def _set_cpu(self) -> None:
        self._apply_selection("CPU")

    def _set_gpu(self) -> None:
        self._apply_selection("GPU")

    def _on_language_selected(self, _event=None) -> None:
        variable = self._ui.get("language_var")
        if variable is None:
            return
        selected_name = str(variable.get())
        language = next(
            (code for code, name in LANGUAGE_NAMES.items() if name == selected_name),
            "en",
        )
        snapshot = self.status_manager.status_snapshot()
        coordinator_url = str(snapshot.get("coordinatorApiUrl") or "").rstrip("/")
        self._apply_language(language, persist=True)
        if coordinator_url:
            threading.Thread(
                target=self._publish_language_to_coordinator,
                args=(coordinator_url, language),
                daemon=True,
            ).start()

    def _publish_language_to_coordinator(self, coordinator_url: str, language: str) -> None:
        try:
            body = json.dumps({"language": language}).encode("utf-8")
            request = urllib.request.Request(
                f"{coordinator_url}/settings/language",
                data=body,
                method="PUT",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=3.0) as response:
                response.read()
        except Exception as exc:
            self.logger.warning("Could not update Coordinator language: %s", exc)

    def _apply_language(self, language: str, *, persist: bool) -> None:
        normalized = normalize_language(language)
        if persist:
            self.config_manager.set_language(normalized)
        self.status_manager.set_ui_language(normalized)
        if normalized == self.language:
            return
        self.language = normalized
        root = self._root
        if root is None or not root.winfo_exists():
            return
        for child in root.winfo_children():
            child.destroy()
        self._ui = {}
        self._device_cards = {}
        self._build_window(on_close=self._on_close_callback)
        self._sync_from_config()

    def _apply_selection(self, preference: str) -> None:
        if self._updating:
            return

        try:
            saved_preference = self.config_manager.set_render_device_preference(preference)
        except Exception as exc:
            self.logger.exception("Failed to persist render device preference")
            if self._ui.get("preference_status") is not None:
                self._ui["preference_status"].config(
                    text=self._t("Could not save preference: {error}", error=exc),
                    fg=self.COLORS["danger"],
                )
            return

        self.status_manager.set_render_device_preference(saved_preference)
        self.status_manager.append_log(f"Render device preference changed to {saved_preference}")
        self.logger.info("Render device preference changed to %s", saved_preference)
        self._sync_from_config()

    def _sync_from_config(self) -> None:
        preference = self.config_manager.config.renderDevicePreference

        self._updating = True
        try:
            for key, card in self._device_cards.items():
                selected = key == preference
                border_color = (
                    self.COLORS["card_selected_edge"] if selected else self.COLORS["card_idle_edge"]
                )
                body_color = (
                    self.COLORS["card_selected_bg"] if selected else self.COLORS["card_bg"]
                )
                indicator_border = (
                    self.COLORS["card_selected_edge"] if selected else self.COLORS["soft"]
                )
                indicator_fill = self.COLORS["accent_strong"] if selected else body_color
                indicator_text = "\u2713" if selected else ""
                indicator_text_color = self.COLORS["text"] if selected else body_color
                icon_color = self.COLORS["accent"] if selected else self.COLORS["soft"]
                description_color = (
                    "#D9E6FF" if selected else self.COLORS["muted"]
                )

                card.border.config(bg=border_color)
                card.body.config(bg=body_color)
                card.content.config(bg=body_color)
                card.text_wrap.config(bg=body_color)
                card.indicator_border.config(bg=indicator_border)
                card.indicator_fill.config(bg=indicator_fill)
                card.indicator_label.config(
                    text=indicator_text,
                    bg=indicator_fill,
                    fg=indicator_text_color,
                )
                card.title_label.config(bg=body_color, fg=self.COLORS["text"])
                card.description_label.config(bg=body_color, fg=description_color)
                self._redraw_icon(
                    card.icon_canvas,
                    icon_name=key.lower(),
                    color=icon_color,
                    background=body_color,
                )
        finally:
            self._updating = False

    def _refresh_status(self) -> None:
        root = self._root
        if root is None or not root.winfo_exists():
            return

        snapshot = self.status_manager.status_snapshot()
        incoming_language = normalize_language(snapshot.get("language"))
        if incoming_language != self.language:
            self._apply_language(incoming_language, persist=True)
            snapshot = self.status_manager.status_snapshot()

        status_text = display_status(snapshot, self.language)
        tone = self._status_tone(snapshot)
        heartbeat_text = format_relative_time(snapshot.get("coordinatorLastSeenAt"), self.language)

        self._apply_pill_style(
            self._ui["top_status_border"], self._ui["top_status_inner"],
            self._ui["top_status_label"], status_text, tone["fg"], tone["bg"]
        )
        self._apply_pill_style(
            self._ui["hero_status_border"], self._ui["hero_status_inner"],
            self._ui["hero_status_label"], status_text, tone["fg"], tone["bg"]
        )

        hint = "Currently rendering an assigned frame" if snapshot.get("currentJob") else "Ready to receive jobs"
        session_status = str(snapshot.get("status") or "").lower()
        if session_status in SESSION_STATUS_LABELS and snapshot.get("currentFrame") is None:
            hint = "Preparing to switch jobs" if session_status in {"draining", "closing"} else "Waiting for next frames"
        if snapshot.get("error") or str(snapshot.get("apiServerState")) in {"error", "stopped"}:
            hint = "Worker needs attention"
        elif not snapshot.get("allowJobs", True):
            hint = "This worker is not accepting new jobs"
        self._ui["hero_hint"].config(text=self._t(hint))
        self._ui["worker_name"].config(text=self.status_manager.machine_name)
        self._ui["worker_detail"].config(
            text=self._build_worker_detail(snapshot),
            fg=tone["fg"] if snapshot.get("currentJob") else self.COLORS["accent"],
        )
        self._ui["api_value"].config(
            text=str(snapshot.get("coordinatorApiUrl") or f"http://{snapshot['ip']}:{snapshot['port']}")
        )
        self._ui["api_detail"].config(text=self._build_api_detail(snapshot))
        connected = bool(snapshot.get("coordinatorConnected"))
        self._ui["connection_value"].config(
            text=self._t("Connection established") if connected else self._t("Waiting"),
            fg=self.COLORS["success"] if connected else self.COLORS["warning"],
        )

        self._ui["machine_value"].config(text=str(snapshot.get("machineName") or "-"))
        self._ui["machine_detail"].config(text=f"{snapshot.get('ip', '-')}:{snapshot.get('port', '-')}")
        blender_version = str(snapshot.get("blenderVersion") or "")
        self._ui["blender_value"].config(
            text=self._t("Detected") if snapshot.get("blenderFound") else self._t("Waiting"),
            fg=self.COLORS["success"] if snapshot.get("blenderFound") else self.COLORS["warning"],
        )
        self._ui["blender_detail"].config(text=blender_version or self._t("Preparing services"))
        gpu = str(snapshot.get("gpu") or "unknown")
        self._ui["gpu_value"].config(text=gpu if gpu.lower() != "unknown" else self._t("Waiting"))
        preference = str(snapshot.get("renderDevicePreference") or "GPU")
        gpu_detail = f"{preference} • {snapshot.get('gpuBackend', 'AUTO')}"
        if snapshot.get("gpuDeviceIds"):
            gpu_detail += f" • {len(snapshot['gpuDeviceIds'])} GPU(s)"
        self._ui["gpu_detail"].config(text=gpu_detail)
        heartbeat_at = parse_timestamp(snapshot.get("coordinatorLastSeenAt"))
        self._ui["heartbeat_value"].config(text=heartbeat_at.astimezone().strftime("%d/%m/%Y %H:%M:%S") if heartbeat_at else heartbeat_text)
        self._ui["heartbeat_detail"].config(
            text=heartbeat_text if heartbeat_at else self._t("No coordinator heartbeat yet")
        )
        current_job = str(snapshot.get("currentJob") or "-")
        self._ui["job_value"].config(text=current_job)
        if snapshot.get("currentJob"):
            progress = float(snapshot.get("progress") or 0.0)
            frame = snapshot.get("currentFrame")
            self._ui["job_detail"].config(text=f"{progress:.0f}% • frame {frame or '-'}")
        else:
            self._ui["job_detail"].config(text=self._t("Waiting..."))

        try:
            ok, detail = self._connection_results.get_nowait()
        except Empty:
            pass
        else:
            self._testing_connection = False
            self._ui["test_detail"].config(text=self._t("Test completed") if ok else self._t("Connection failed: {error}", error=detail),
                                          fg=self.COLORS["success"] if ok else self.COLORS["danger"])
        self._ui["test_button"].configure(state="disabled" if self._testing_connection else "normal")
        self.content_scroll.refresh()
        root.after(1000, self._refresh_status)

    def _test_connection(self) -> None:
        if self._testing_connection:
            return
        url = str(self.status_manager.status_snapshot().get("coordinatorApiUrl") or "").rstrip("/")
        if not url:
            self._ui["test_detail"].config(text=self._t("Coordinator is not connected yet."))
            return
        self._testing_connection = True
        self._ui["test_button"].configure(state="disabled")
        self._ui["test_detail"].config(text=self._t("Testing connection..."))
        threading.Thread(target=self._probe_connection, args=(url,), daemon=True).start()

    def _probe_connection(self, url: str) -> None:
        try:
            with urllib.request.urlopen(url + "/health", timeout=3.0) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or not payload.get("coordinatorId") or payload.get("status") != "ok":
                raise ValueError("Invalid health response")
            self._connection_results.put((True, ""))
        except Exception as exc:
            self._connection_results.put((False, str(exc)))

    def _build_worker_detail(self, snapshot: dict[str, Any]) -> str:
        api_state = str(snapshot.get("apiServerState") or "").lower()
        if api_state in {"error", "stopped"}:
            return self._t("Worker API is unavailable") + ": " + str(snapshot.get("apiServerMessage") or snapshot.get("startupError") or "-")

        current_job = snapshot.get("currentJob")
        if current_job:
            session_status = str(snapshot.get("status") or "").lower()
            if session_status in SESSION_STATUS_LABELS and snapshot.get("currentFrame") is None:
                return self._t("{status} | Job: {job} | Device: {device}",
                    status=humanize_status(session_status, self.language), job=current_job,
                    device=snapshot.get("renderDevicePreference") or "-")
            start_frame = snapshot.get("startFrame")
            end_frame = snapshot.get("endFrame")
            progress = float(snapshot.get("progress") or 0.0)
            render_device = snapshot.get("renderDevicePreference") or "Unknown"
            return self._t(
                "Rendering {job} | Frames {start}-{end} | {progress}% complete | Device: {device}",
                job=current_job, start=start_frame if start_frame is not None else '-', end=end_frame if end_frame is not None else '-',
                progress=f"{progress:.0f}", device=render_device,
            )

        if snapshot.get("error"):
            return self._t("Attention: {error}", error=snapshot["error"])

        if snapshot.get("blenderFound"):
            blender_version = snapshot.get("blenderVersion") or "Detected"
            preference = snapshot.get("renderDevicePreference") or "GPU"
            return self._t("Blender {version} ready | Next jobs will use {device}",
                           version=blender_version, device=preference)

        return self._t("Checking Blender availability and waiting for the next render block")

    def _build_api_detail(self, snapshot: dict[str, Any]) -> str:
        api_server_state = str(snapshot.get("apiServerState") or "").lower()
        api_server_message = str(snapshot.get("apiServerMessage") or "").strip()
        coordinator_api_url = snapshot.get("coordinatorApiUrl")
        coordinator_status = humanize_status(
            str(snapshot.get("coordinatorDiscoveryStatus") or ""), self.language
        )
        blender_path = snapshot.get("blenderPath") or ""
        blender_name = Path(blender_path).name if blender_path else self._t("Not detected")
        detail_parts = []

        if api_server_state in {"error", "stopped"}:
            detail_parts.append(f"API: {api_server_message or self._t('Unavailable')}")
        elif coordinator_api_url:
            detail_parts.append(f"Coordinator: {coordinator_api_url}")
        elif snapshot.get("coordinatorDiscoveryEnabled"):
            detail_parts.append(f"Coordinator: {coordinator_status}")
        else:
            detail_parts.append(self._t("Coordinator discovery is disabled"))

        blender_version = snapshot.get("blenderVersion")
        if blender_version:
            detail_parts.append(f"Blender: {blender_version} ({blender_name})")
        else:
            detail_parts.append(self._t("Blender: waiting for detection"))

        return " | ".join(detail_parts)







    def _status_tone(self, snapshot: dict[str, Any]) -> dict[str, str]:
        api_state = str(snapshot.get("apiServerState") or "").lower()
        if api_state in {"error", "stopped"}:
            return {"fg": self.COLORS["danger"], "bg": self.COLORS["danger_bg"]}
        status = str(snapshot.get("status") or "").lower()
        if snapshot.get("error") and status not in {"rendering", "cancelling"}:
            return {"fg": self.COLORS["danger"], "bg": self.COLORS["danger_bg"]}
        if status in {"idle", "session_open"}:
            return {"fg": self.COLORS["success"], "bg": self.COLORS["success_bg"]}
        if status in {"rendering", "queued"}:
            return {"fg": self.COLORS["accent"], "bg": self.COLORS["accent_deep"]}
        if status in {"starting", "validating", "cancelling", "draining", "closing"}:
            return {"fg": self.COLORS["warning"], "bg": self.COLORS["warning_bg"]}
        if status == "disabled":
            return {"fg": self.COLORS["muted"], "bg": self.COLORS["neutral_bg"]}
        return {"fg": self.COLORS["text"], "bg": self.COLORS["neutral_bg"]}

    def _apply_pill_style(
        self,
        border,
        inner,
        label,
        text: str,
        fg: str,
        bg: str,
    ) -> None:
        border.config(bg=fg)
        inner.config(bg=bg)
        label.config(text=text, fg=fg, bg=bg)

    def _create_icon_canvas(
        self,
        parent,
        *,
        icon_name: str,
        color: str,
        size: int,
        background: str,
    ):
        tk = self._tk
        canvas = tk.Canvas(
            parent,
            width=size,
            height=size,
            bg=background,
            highlightthickness=0,
            bd=0,
        )
        self._redraw_icon(canvas, icon_name=icon_name, color=color, background=background)
        return canvas

    def _redraw_icon(self, canvas, *, icon_name: str, color: str, background: str) -> None:
        canvas.configure(bg=background)
        canvas.delete("all")
        size = int(canvas.cget("width"))
        if icon_name == "blender":
            path = resource_path(self.config_manager.config_path.parent, "img", "brand_48.png")
        else:
            names = {"worker": "monitor", "globe": "network", "shield": "file-text", "pulse": "heart-pulse"}
            tone = "green" if color == self.COLORS["success"] else "muted" if color == self.COLORS["soft"] else "blue"
            asset_size = min((20, 28, 40), key=lambda v: abs(v-size))
            path = resource_path(self.config_manager.config_path.parent, "img", "icons", f"{names.get(icon_name, icon_name)}_{tone}_{asset_size}.png")
        if path.is_file():
            canvas._icon_image = self._tk.PhotoImage(file=str(path))
            canvas.create_image(size/2, size/2, image=canvas._icon_image)

    def _load_brand_images(self) -> None:
        if self._tk is None:
            return
        for key, size in (("header", 64), ("hero", 88)):
            path = resource_path(self.config_manager.config_path.parent, "img", f"brand_{size}.png")
            if path.is_file():
                self._logo_images[key] = self._tk.PhotoImage(file=str(path))

    def _apply_window_icon(self, root) -> None:
        icon_dir = self.config_manager.config_path.parent / "img"
        ico_path = icon_dir / "icon_worker.ico"
        png_path = icon_dir / "icon_worker.png"
        icon_applied = False

        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "BlenderRenderFarmWorker.App"
            )
        except Exception:
            pass

        if ico_path.is_file():
            try:
                root.iconbitmap(default=str(ico_path))
                icon_applied = True
            except Exception as exc:
                self.logger.warning("Failed to apply ICO window icon %s: %s", ico_path, exc)

        if png_path.is_file():
            try:
                root._icon_image = self._tk.PhotoImage(file=str(png_path))
                root.iconphoto(True, root._icon_image)
                icon_applied = True
            except Exception as exc:
                self.logger.warning("Failed to apply PNG window icon %s: %s", png_path, exc)

        if icon_applied:
            self.logger.info("Worker window icon loaded successfully")

    def show_startup_error(self, message: str) -> bool:
        if not self.config_manager.config.showRenderDeviceWindow:
            self.logger.error("Worker startup failed: %s", message)
            return False

        try:
            import tkinter as tk
            from tkinter import messagebox
        except ImportError:
            self.logger.error("Worker startup failed: %s", message)
            return False

        try:
            root = tk.Tk()
        except tk.TclError:
            self.logger.error("Worker startup failed: %s", message)
            return False

        self._tk = tk
        root.withdraw()
        self._apply_window_icon(root)
        try:
            messagebox.showerror(
                self._t("Worker startup error"),
                self._t("The Worker API could not start.\n\n{error}", error=message),
                parent=root,
            )
        finally:
            root.destroy()
        return True


def build_render_device_response(render_device_preference: str) -> dict[str, object]:
    payload = RenderDevicePreferenceRequest(renderDevicePreference=render_device_preference)
    return {
        "renderDevicePreference": payload.renderDevicePreference,
        "effectiveUseGpu": render_device_preference_to_use_gpu(payload.renderDevicePreference),
        "saved": True,
    }
