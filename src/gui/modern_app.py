"""Modern GUI for WizLight using CustomTkinter."""

import gc
import json
import sys
from concurrent.futures import Future
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

from ..core.async_runtime import BackgroundAsyncLoop
from ..core.bulb_controller import BulbController, PRESETS, apply_preset
from ..core.config import Config, SCREEN_SYNC_REGIONS
from ..core.diagnostics import build_diagnostics_report
from ..features.clap_detector import ClapConfig, ClapDetector
from ..features.screen_sync import (
    average_colors,
    build_bulb_color_map,
    list_monitors,
)
from ..features.screen_sync_v2 import OptimizedScreenSync, build_optimized_capture_config
from ..features.fast_udp_sender import FastBulbSenderPool
from ..features.sync_dispatcher import CoalescingAsyncDispatcher

# Import new components
from .components.animations import AnimationMixin
from .components.color_wheel import ColorWheelPicker
from .components.dashboard import BulbDashboard, BulbInfo
from .components.smooth_scrollable_frame import SmoothScrollableFrame
from .components.tray import SystemTrayManager, is_tray_available
from .ctk_compat import install_customtkinter_dropdown_destroy_patch


install_customtkinter_dropdown_destroy_patch()
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

UNASSIGNED_REGION = "(Unassigned)"


class ModernColorPicker(ctk.CTkToplevel):
    """Minimal RGB picker (legacy fallback)."""

    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback = callback
        self.title("Pick Color")
        self.geometry("300x320")
        self.resizable(False, False)

        self.r_var = ctk.IntVar(value=255)
        self.g_var = ctk.IntVar(value=255)
        self.b_var = ctk.IntVar(value=255)

        for label, variable in (("Red", self.r_var), ("Green", self.g_var), ("Blue", self.b_var)):
            ctk.CTkLabel(self, text=label, font=("Segoe UI", 13)).pack(pady=(12, 4))
            ctk.CTkSlider(self, from_=0, to=255, variable=variable, command=self._update_preview).pack(
                fill="x", padx=20
            )

        self.preview = ctk.CTkFrame(self, width=90, height=90, corner_radius=14)
        self.preview.pack(pady=18)
        self._update_preview(None)

        ctk.CTkButton(self, text="Apply", command=self._apply).pack(fill="x", padx=20, pady=(0, 16))

    def _update_preview(self, _):
        color = f"#{self.r_var.get():02x}{self.g_var.get():02x}{self.b_var.get():02x}"
        self.preview.configure(fg_color=color)

    def _apply(self):
        self.callback(self.r_var.get(), self.g_var.get(), self.b_var.get())
        self.destroy()


class WizLightModernGUI(AnimationMixin):
    """Modern GUI application for WizLight."""

    @staticmethod
    def _asset_path(*parts: str) -> Path:
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", Path.cwd()))
        else:
            base = Path(__file__).resolve().parents[2]
        return base.joinpath(*parts)

    @staticmethod
    def _surface_color(widget) -> str | tuple[str, str]:
        color = widget.cget("fg_color")
        return color if color != "transparent" else ("gray92", "gray14")

    def __init__(self):
        AnimationMixin.__init__(self)
        self.config = Config.load()
        self.controller = BulbController()
        self.screen_sync: Optional[OptimizedScreenSync] = None
        self._screen_sync_dispatcher: Optional[CoalescingAsyncDispatcher] = None
        self._screen_sync_fast_sender: Optional[FastBulbSenderPool] = None
        self.clap_detector: Optional[ClapDetector] = None
        self._async_runner = BackgroundAsyncLoop()
        self._pending_tasks: list[Future] = []
        self._brightness_debounce_id = None
        self._temp_debounce_id = None
        self._screen_sync_settings_save_id = None
        self._screen_sync_reconfigure_id = None
        self._screen_sync_debug_id = None
        self._screen_layout_vars: dict[str, ctk.StringVar] = {}
        self._monitor_options = self._load_monitor_options()
        self._monitor_label_to_index = {
            option["label"]: int(option["index"]) for option in self._monitor_options
        }
        
        # System tray
        self._tray: Optional[SystemTrayManager] = None
        self._minimized_to_tray = False
        
        # Dashboard
        self._dashboard: Optional[BulbDashboard] = None
        self._current_view = "sync"

        self.root = ctk.CTk()
        self.root.title("WizLight")
        self.root.geometry("860x760")
        self.root.minsize(720, 640)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_window_icon()

        self._build_ui()
        self._update_bulb_status()
        self._setup_system_tray()

    def _apply_window_icon(self) -> None:
        icon_path = self._asset_path("packaging", "assets", "wizlight.ico")
        if not icon_path.exists():
            return
        try:
            self.root.iconbitmap(str(icon_path))
        except Exception:
            pass

    def _load_monitor_options(self) -> list[dict[str, int | str]]:
        try:
            return list_monitors()
        except Exception:
            return [{"index": 0, "label": "Primary monitor"}]

    def _run_async(self, coro):
        future = self._async_runner.submit(coro)
        self._pending_tasks.append(future)
        self._pending_tasks = [task for task in self._pending_tasks if not task.done()]

    def _get_bulb_ips(self) -> list[str]:
        return [bulb.ip for bulb in self.config.bulbs]

    def _card(self, parent, title: str, subtitle: str | None = None):
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.pack(fill="x", pady=(0, 12))
        header = ctk.CTkFrame(card, fg_color=self._surface_color(card))
        header.pack(fill="x", padx=14, pady=(14, 8))
        ctk.CTkLabel(header, text=title, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(
                header,
                text=subtitle,
                text_color=("gray35", "gray70"),
                justify="left",
                wraplength=460,
            ).pack(anchor="w", pady=(2, 0))
        return card

    def _slider_control(
        self,
        parent,
        label: str,
        value_text: str,
        from_: float,
        to: float,
        command,
        value: float,
        steps: int | None = None,
    ):
        row = ctk.CTkFrame(parent, fg_color=self._surface_color(parent))
        row.pack(fill="x", padx=14, pady=(0, 10))
        label_var = ctk.StringVar(value=f"{label}: {value_text}")
        ctk.CTkLabel(row, textvariable=label_var, anchor="w").pack(fill="x")
        slider = ctk.CTkSlider(
            row,
            from_=from_,
            to=to,
            number_of_steps=steps,
            command=command,
        )
        slider.set(value)
        slider.pack(fill="x", pady=(4, 0))
        return label_var, slider

    def _metric_tile(self, parent, key: str, label: str, value: str = "-"):
        tile = ctk.CTkFrame(parent, corner_radius=8, border_width=1)
        ctk.CTkLabel(tile, text=label, text_color=("gray35", "gray70"), font=("Segoe UI", 11)).pack(
            anchor="w", padx=10, pady=(8, 0)
        )
        variable = ctk.StringVar(value=value)
        ctk.CTkLabel(tile, textvariable=variable, font=("Segoe UI", 17, "bold")).pack(
            anchor="w", padx=10, pady=(0, 8)
        )
        self._sync_metric_vars[key] = variable
        return tile

    def _sync_status_color(self, status: str) -> str:
        status_lower = status.lower()
        if "error" in status_lower or "failed" in status_lower:
            return "#B91C1C"
        if "recover" in status_lower or "restart" in status_lower or "warm" in status_lower:
            return "#B7791F"
        if self.screen_sync and self.screen_sync.is_running:
            return "#17803D"
        return "#5F6368"

    def _build_ui(self):
        self._main_container = ctk.CTkFrame(self.root, fg_color="transparent")
        self._main_container.pack(fill="both", expand=True, padx=12, pady=12)

        header = ctk.CTkFrame(self._main_container)
        header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header, text="WizLight", font=("Segoe UI", 26, "bold")).pack(
            side="left", padx=12, pady=12
        )

        view_toggle = ctk.CTkFrame(header, fg_color="transparent")
        view_toggle.pack(side="right", padx=12)
        self._view_buttons: dict[str, ctk.CTkButton] = {}
        for view, label in (
            ("sync", "Sync"),
            ("lights", "Lights"),
            ("bulbs", "Bulbs"),
            ("tools", "Tools"),
        ):
            button = ctk.CTkButton(
                view_toggle,
                text=label,
                width=78,
                command=lambda name=view: self._switch_view(name),
                fg_color=("#3B8ED0", "#1F6AA5") if view == "sync" else "transparent",
                border_width=0 if view == "sync" else 1,
            )
            button.pack(side="left", padx=(0, 4))
            self._view_buttons[view] = button

        if is_tray_available():
            self._tray_btn = ctk.CTkButton(
                view_toggle,
                text="⊟",
                width=32,
                command=self._minimize_to_tray,
                fg_color="transparent",
                border_width=1,
            )
            self._tray_btn.pack(side="left")

        status_bar = ctk.CTkFrame(self._main_container, corner_radius=8, border_width=1)
        status_bar.pack(fill="x", pady=(0, 10))
        self.status_indicator = ctk.CTkFrame(
            status_bar, width=12, height=12, corner_radius=6, fg_color="#5F6368"
        )
        self.status_indicator.pack(side="left", padx=(12, 8), pady=10)
        self.status_label = ctk.CTkLabel(
            status_bar, text="Ready", text_color=("gray25", "gray75"), anchor="w"
        )
        self.status_label.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=8)
        self.header_bulbs_var = ctk.StringVar(value="Bulbs: 0")
        self.header_monitor_var = ctk.StringVar(value="Monitor: -")
        self.header_runtime_var = ctk.StringVar(value="Sync: idle")
        for variable in (self.header_bulbs_var, self.header_monitor_var, self.header_runtime_var):
            ctk.CTkLabel(status_bar, textvariable=variable, text_color=("gray35", "gray70")).pack(
                side="right", padx=(0, 12), pady=8
            )

        self._view_host = ctk.CTkFrame(self._main_container, fg_color="transparent")
        self._view_host.pack(fill="both", expand=True)
        self._view_frames: dict[str, ctk.CTkFrame] = {
            "sync": SmoothScrollableFrame(self._view_host, fg_color=("gray92", "gray14")),
            "lights": SmoothScrollableFrame(self._view_host, fg_color=("gray92", "gray14")),
            "bulbs": SmoothScrollableFrame(self._view_host, fg_color=("gray92", "gray14")),
            "tools": SmoothScrollableFrame(self._view_host, fg_color=("gray92", "gray14")),
        }
        self._controls_frame = self._view_frames["sync"]
        self._dashboard_frame = ctk.CTkFrame(self._view_frames["bulbs"], fg_color="transparent")

        self._build_controls_view()
        self._dashboard = BulbDashboard(
            self._dashboard_frame,
            self.controller,
            self._run_async,
            on_bulb_select=self._on_bulb_select,
        )
        self._dashboard.pack(fill="both", expand=True)
        self._current_view = ""
        self._switch_view("sync")
    
    def _build_controls_view(self):
        """Build Sync, Lights, Bulbs, and Tools views."""
        sync = self._view_frames["sync"]
        lights = self._view_frames["lights"]
        bulbs_view = self._view_frames["bulbs"]
        tools = self._view_frames["tools"]

        cockpit = self._card(sync, "Screen Sync", "Live ambient sync cockpit for Chrome, YouTube, movies, and games.")
        action_row = ctk.CTkFrame(cockpit, fg_color=self._surface_color(cockpit))
        action_row.pack(fill="x", padx=14, pady=(0, 12))
        self.sync_primary_button = ctk.CTkButton(
            action_row,
            text="Start Screen Sync",
            height=42,
            command=self._toggle_screen_sync_from_primary,
            font=("Segoe UI", 14, "bold"),
        )
        self.sync_primary_button.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.screen_sync_switch = ctk.CTkSwitch(
            action_row,
            text="Sync",
            command=self._toggle_screen_sync,
        )
        self.screen_sync_switch.pack(side="left", padx=(0, 12))
        self.clap_switch = ctk.CTkSwitch(
            action_row,
            text="Clap",
            command=self._toggle_clap_detection,
        )
        self.clap_switch.pack(side="left")

        preview_grid = ctk.CTkFrame(cockpit, fg_color=self._surface_color(cockpit))
        preview_grid.pack(fill="x", padx=14, pady=(0, 12))
        preview_grid.columnconfigure((0, 1), weight=1, uniform="preview")
        self.sync_target_preview = self._color_panel(preview_grid, "Target", "RGB(-)")
        self.sync_target_preview.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.sync_output_preview = self._color_panel(preview_grid, "Output", "RGB(-)")
        self.sync_output_preview.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self._sync_metric_vars: dict[str, ctk.StringVar] = {}
        metric_grid = ctk.CTkFrame(cockpit, fg_color=self._surface_color(cockpit))
        metric_grid.pack(fill="x", padx=14, pady=(0, 12))
        for column in range(3):
            metric_grid.columnconfigure(column, weight=1, uniform="metric")
        for index, (key, label) in enumerate(
            (
                ("fps", "FPS"),
                ("frame", "Frame ms"),
                ("send", "Send Hz"),
                ("motion", "Motion"),
                ("capture", "Capture"),
                ("status", "Status"),
            )
        ):
            self._metric_tile(metric_grid, key, label).grid(
                row=index // 3,
                column=index % 3,
                sticky="nsew",
                padx=4,
                pady=4,
            )

        settings = self._card(sync, "Tuning", "Only the controls that matter while watching video.")
        self.monitor_menu = ctk.CTkOptionMenu(
            settings,
            values=[str(option["label"]) for option in self._monitor_options],
            command=self._queue_screen_sync_settings_save,
        )
        selected_monitor = next(
            (
                str(option["label"])
                for option in self._monitor_options
                if int(option["index"]) == self.config.screen_sync.monitor
            ),
            str(self._monitor_options[0]["label"]),
        )
        self.monitor_menu.set(selected_monitor)
        self.monitor_menu.pack(fill="x", padx=14, pady=(0, 10))

        self.fps_value = ctk.IntVar(value=self.config.screen_sync.max_fps)
        self.min_fps_value = ctk.IntVar(value=self.config.screen_sync.min_fps)
        self.smoothing_value = ctk.DoubleVar(value=self.config.screen_sync.smoothing)
        self.boost_value = ctk.DoubleVar(value=self.config.screen_sync.color_boost)
        self.min_brightness_value = ctk.IntVar(value=self.config.screen_sync.min_brightness)
        self.ignore_letterbox_var = ctk.BooleanVar(value=self.config.screen_sync.ignore_letterbox)
        self.algorithm_value = ctk.StringVar(value=self.config.screen_sync.color_algorithm.title())
        self.use_gpu_var = ctk.BooleanVar(value=self.config.screen_sync.use_gpu)
        self.adaptive_fps_var = ctk.BooleanVar(value=self.config.screen_sync.adaptive_fps)
        self.predictive_smoothing_var = ctk.BooleanVar(value=self.config.screen_sync.predictive_smoothing)
        self.color_source_labels = {
            "Auto (detect)": "auto",
            "Standard / sRGB": "srgb",
            "Display P3 OLED": "display-p3",
            "Rec.2020 HDR": "rec2020",
        }
        self.color_source_value = ctk.StringVar(
            value=next(
                (
                    label
                    for label, key in self.color_source_labels.items()
                    if key == self.config.screen_sync.source_color_space
                ),
                "Auto (detect)",
            )
        )
        self.sync_profile_labels = {
            "Auto": "auto",
            "Cinema (calm)": "cinema",
            "Action (fast)": "action",
        }
        self.sync_profile_value = ctk.StringVar(
            value=next(
                (
                    label
                    for label, key in self.sync_profile_labels.items()
                    if key == self.config.screen_sync.sync_profile
                ),
                "Auto",
            )
        )
        self.brightness_mode_labels = {
            "Follow video": "follow",
            "Constant": "constant",
        }
        self.brightness_mode_value = ctk.StringVar(
            value=next(
                (
                    label
                    for label, key in self.brightness_mode_labels.items()
                    if key == self.config.screen_sync.brightness_mode
                ),
                "Follow video",
            )
        )
        self.sync_brightness_value = ctk.IntVar(
            value=self.config.screen_sync.sync_brightness
        )

        self.fps_label, self.fps_slider = self._slider_control(
            settings,
            "Responsiveness",
            f"{self.fps_value.get()} FPS",
            12,
            30,
            self._on_fps_change,
            min(30, max(12, self.fps_value.get())),
            18,
        )
        self.smoothing_label, self.smoothing_slider = self._slider_control(
            settings,
            "Smoothness",
            f"{self.smoothing_value.get():.2f}",
            0.05,
            0.55,
            self._on_smoothing_change,
            min(0.55, self.smoothing_value.get()),
        )
        self.boost_label, self.boost_slider = self._slider_control(
            settings,
            "Color Punch",
            f"{self.boost_value.get():.2f}x",
            1.0,
            1.45,
            self._on_boost_change,
            min(1.45, self.boost_value.get()),
        )
        self.min_brightness_label, self.min_brightness_slider = self._slider_control(
            settings,
            "Dark Scene Floor",
            str(self.min_brightness_value.get()),
            0,
            60,
            self._on_min_brightness_change,
            min(60, self.min_brightness_value.get()),
            60,
        )

        ctk.CTkLabel(settings, text="Color Source", anchor="w").pack(
            fill="x", padx=14, pady=(2, 4)
        )
        self.color_source_menu = ctk.CTkOptionMenu(
            settings,
            values=list(self.color_source_labels.keys()),
            variable=self.color_source_value,
            command=self._queue_screen_sync_settings_save,
        )
        self.color_source_menu.pack(fill="x", padx=14, pady=(0, 10))

        ctk.CTkLabel(settings, text="Sync Feel", anchor="w").pack(
            fill="x", padx=14, pady=(2, 4)
        )
        self.sync_profile_menu = ctk.CTkSegmentedButton(
            settings,
            values=list(self.sync_profile_labels.keys()),
            variable=self.sync_profile_value,
            command=self._queue_screen_sync_settings_save,
        )
        self.sync_profile_menu.pack(fill="x", padx=14, pady=(0, 10))

        ctk.CTkLabel(settings, text="Brightness Mode", anchor="w").pack(
            fill="x", padx=14, pady=(2, 4)
        )
        self.brightness_mode_menu = ctk.CTkSegmentedButton(
            settings,
            values=list(self.brightness_mode_labels.keys()),
            variable=self.brightness_mode_value,
            command=self._on_brightness_mode_change,
        )
        self.brightness_mode_menu.pack(fill="x", padx=14, pady=(0, 4))
        self.brightness_mode_hint = ctk.CTkLabel(
            settings,
            text=self._brightness_mode_hint_text(),
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray60"),
        )
        self.brightness_mode_hint.pack(fill="x", padx=14, pady=(0, 6))
        self.sync_brightness_label, self.sync_brightness_slider = self._slider_control(
            settings,
            "Constant Brightness",
            f"{round(self.sync_brightness_value.get() / 255 * 100)}%",
            30,
            255,
            self._on_sync_brightness_change,
            self.sync_brightness_value.get(),
        )
        self._update_sync_brightness_state()

        flags = ctk.CTkFrame(settings, fg_color=self._surface_color(settings))
        flags.pack(fill="x", padx=14, pady=(0, 10))
        ctk.CTkCheckBox(
            flags,
            text="Fast GPU capture",
            variable=self.use_gpu_var,
            command=self._queue_screen_sync_settings_save,
        ).pack(side="left", padx=(0, 10))

        ctk.CTkLabel(
            flags,
            text="Black bars ignored automatically",
            text_color=("gray35", "gray70"),
        ).pack(side="left")
        self.ignore_letterbox_var.set(True)

        ctk.CTkButton(
            settings,
            text="What do these controls do?",
            command=self._show_control_guide,
            fg_color="transparent",
            border_width=1,
        ).pack(fill="x", padx=14, pady=(0, 14))

        self.advanced_debug_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            sync,
            text="Advanced Debug",
            variable=self.advanced_debug_var,
            command=self._toggle_advanced_debug,
        ).pack(anchor="w", padx=4, pady=(0, 8))
        self._build_screen_sync_debug(sync)

        light_card = self._card(lights, "Lights", "Quick everyday controls for all configured bulbs.")
        button_row = ctk.CTkFrame(light_card, fg_color=self._surface_color(light_card))
        button_row.pack(fill="x", padx=14, pady=(0, 12))
        self.toggle_power_button = ctk.CTkButton(
            button_row, text="Toggle Power", command=self._toggle, width=140
        )
        self.toggle_power_button.pack(side="left", padx=(0, 8))
        self.turn_off_button = ctk.CTkButton(
            button_row,
            text="Turn Off",
            command=self._turn_off,
            width=110,
            fg_color="transparent",
            border_width=1,
        )
        self.turn_off_button.pack(side="left")
        self.brightness_label_var, self.brightness_slider = self._slider_control(
            light_card, "Brightness", "50%", 0, 255, self._on_brightness_change, 128, 255
        )
        color_row = ctk.CTkFrame(light_card, fg_color=self._surface_color(light_card))
        color_row.pack(fill="x", padx=14, pady=(0, 12))
        self.color_preview = ctk.CTkFrame(
            color_row, width=42, height=42, corner_radius=10, fg_color="#ffffff"
        )
        self.color_preview.pack(side="left", padx=(0, 10))
        self.color_preview.pack_propagate(False)
        self.pick_color_button = ctk.CTkButton(color_row, text="Pick Color", command=self._pick_color)
        self.pick_color_button.pack(side="left", padx=(0, 8))
        self.preset_menu = ctk.CTkOptionMenu(
            color_row,
            values=[name.title() for name in PRESETS],
            command=self._apply_preset_from_menu,
        )
        self.preset_menu.pack(side="left", fill="x", expand=True)
        self.preset_menu.set("Preset")
        self.temp_label_var, self.temp_slider = self._slider_control(
            light_card, "Color Temperature", "4000K", 2200, 6500, self._on_temp_change, 4000, 43
        )

        bulbs = self._card(
            bulbs_view,
            "Bulbs",
            "Discover WiZ bulbs on your local network and remove entries that no longer respond.",
        )
        bulb_actions = ctk.CTkFrame(bulbs, fg_color=self._surface_color(bulbs))
        bulb_actions.pack(fill="x", padx=14, pady=(0, 10))
        self.discover_button = ctk.CTkButton(
            bulb_actions, text="Discover Bulbs", command=self._discover_bulbs, width=140
        )
        self.discover_button.pack(side="left", padx=(0, 8))
        self.remove_stale_button = ctk.CTkButton(
            bulb_actions,
            text="Remove Stale",
            command=self._remove_stale_bulbs,
            width=130,
            fg_color="transparent",
            border_width=1,
        )
        self.remove_stale_button.pack(side="left")
        self.bulb_info = ctk.CTkLabel(bulbs, text="No bulbs found", justify="left", text_color="gray")
        self.bulb_info.pack(anchor="w", padx=14, pady=(0, 14))
        self._dashboard_frame.pack(fill="both", expand=True, pady=(0, 12))

        tools_card = self._card(tools, "Diagnostics", "Useful checks and setup helpers.")
        ctk.CTkButton(
            tools_card,
            text="Open Monitor Manager",
            command=self._open_monitor_manager,
            fg_color="transparent",
            border_width=1,
        ).pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkButton(
            tools_card,
            text="Screen Sync Guide",
            command=self._show_control_guide,
            fg_color="transparent",
            border_width=1,
        ).pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkButton(
            tools_card,
            text="Export Diagnostics JSON",
            command=self._export_diagnostics_report,
            fg_color="transparent",
            border_width=1,
        ).pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(
            tools_card,
            text="Calibration and hardware tests are available from the scripts in docs/commands.md.",
            text_color=("gray35", "gray70"),
            wraplength=620,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 14))

    def _color_panel(self, parent, title: str, rgb_text: str):
        panel = ctk.CTkFrame(parent, corner_radius=10, border_width=1)
        ctk.CTkLabel(panel, text=title, text_color=("gray35", "gray70")).pack(
            anchor="w", padx=12, pady=(10, 0)
        )
        swatch = ctk.CTkFrame(panel, height=86, corner_radius=8, fg_color="#000000")
        swatch.pack(fill="x", padx=12, pady=8)
        swatch.pack_propagate(False)
        label_var = ctk.StringVar(value=rgb_text)
        ctk.CTkLabel(panel, textvariable=label_var, font=("Consolas", 13)).pack(
            anchor="w", padx=12, pady=(0, 10)
        )
        panel._swatch = swatch
        panel._label_var = label_var
        return panel

    def _build_screen_sync_debug(self, parent):
        card = ctk.CTkFrame(parent)
        self.advanced_debug_card = card
        if self.advanced_debug_var.get():
            card.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(card, text="Sync Debug", font=("Segoe UI", 16, "bold")).pack(
            anchor="w", padx=12, pady=(12, 8)
        )

        preview_row = ctk.CTkFrame(card, fg_color=self._surface_color(card))
        preview_row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(preview_row, text="Output", width=80, anchor="w").pack(side="left")
        self.screen_sync_debug_preview = ctk.CTkFrame(
            preview_row,
            width=22,
            height=22,
            corner_radius=6,
            fg_color="#000000",
        )
        self.screen_sync_debug_preview.pack(side="left", padx=(0, 8))
        self.screen_sync_debug_preview.pack_propagate(False)

        self.screen_sync_debug_runtime = ctk.StringVar(value="Runtime: idle")
        self.screen_sync_debug_target = ctk.StringVar(value="Target: -")
        self.screen_sync_debug_output = ctk.StringVar(value="Output: -")
        self.screen_sync_debug_perf = ctk.StringVar(value="FPS: -")
        self.screen_sync_debug_cadence = ctk.StringVar(value="Cadence: -")
        self.screen_sync_debug_motion = ctk.StringVar(value="Motion: -")
        self.screen_sync_debug_settings = ctk.StringVar(value="Settings: -")
        self.screen_sync_debug_error = ctk.StringVar(value="")

        for variable in (
            self.screen_sync_debug_runtime,
            self.screen_sync_debug_target,
            self.screen_sync_debug_output,
            self.screen_sync_debug_perf,
            self.screen_sync_debug_cadence,
            self.screen_sync_debug_motion,
            self.screen_sync_debug_settings,
            self.screen_sync_debug_error,
        ):
            ctk.CTkLabel(card, textvariable=variable, anchor="w", justify="left").pack(
                anchor="w", padx=12, pady=(0, 2)
            )

    def _toggle_advanced_debug(self):
        if self.advanced_debug_var.get():
            self.advanced_debug_card.pack(fill="x", pady=(0, 12))
        else:
            self.advanced_debug_card.pack_forget()

    def _refresh_screen_sync_layout_controls(self):
        for child in self.layout_frame.winfo_children():
            child.destroy()
        current_ips = set(self._get_bulb_ips())
        self._screen_layout_vars = {ip: var for ip, var in self._screen_layout_vars.items() if ip in current_ips}
        if not self.config.bulbs:
            ctk.CTkLabel(self.layout_frame, text="Add bulbs first.", text_color="gray").pack(anchor="w")
            return
        values = [UNASSIGNED_REGION] + [region.title() for region in SCREEN_SYNC_REGIONS]
        for bulb in self.config.bulbs:
            row = ctk.CTkFrame(self.layout_frame, fg_color=self._surface_color(self.layout_frame))
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=f"{bulb.name} ({bulb.ip})", anchor="w").pack(side="left")
            selected = self.config.screen_sync.bulb_layout.get(bulb.ip)
            display = selected.title() if selected else UNASSIGNED_REGION
            variable = self._screen_layout_vars.setdefault(bulb.ip, ctk.StringVar(value=display))
            variable.set(display)
            ctk.CTkOptionMenu(
                row,
                values=values,
                variable=variable,
                command=lambda _value, ip=bulb.ip: self._on_layout_change(ip),
                width=170,
            ).pack(side="right")

    def _current_screen_layout(self) -> dict[str, str]:
        layout: dict[str, str] = {}
        for bulb in self.config.bulbs:
            value = self._screen_layout_vars.get(bulb.ip)
            if value is None:
                continue
            region = value.get().strip().lower()
            if region and region != UNASSIGNED_REGION.lower() and region in SCREEN_SYNC_REGIONS:
                layout[bulb.ip] = region
        return layout

    def _save_screen_sync_settings(self):
        if self._screen_sync_settings_save_id:
            self.root.after_cancel(self._screen_sync_settings_save_id)
            self._screen_sync_settings_save_id = None
        settings = self.config.screen_sync
        settings.mode = "single"
        settings.monitor = self._monitor_label_to_index.get(self.monitor_menu.get(), settings.monitor)
        settings.fps = int(self.fps_value.get())
        settings.max_fps = int(self.fps_value.get())
        settings.min_fps = min(max(8, int(self.fps_value.get()) // 2), int(self.fps_value.get()))
        settings.smoothing = float(self.smoothing_value.get())
        settings.color_boost = float(self.boost_value.get())
        settings.min_brightness = int(self.min_brightness_value.get())
        settings.ignore_letterbox = True
        settings.color_algorithm = "auto"
        settings.source_color_space = self.color_source_labels.get(
            self.color_source_value.get(),
            "auto",
        )
        settings.sync_profile = self.sync_profile_labels.get(
            self.sync_profile_value.get(),
            "auto",
        )
        settings.brightness_mode = self.brightness_mode_labels.get(
            self.brightness_mode_value.get(),
            "follow",
        )
        settings.sync_brightness = int(self.sync_brightness_value.get())
        settings.use_gpu = bool(self.use_gpu_var.get())
        settings.adaptive_fps = bool(self.adaptive_fps_var.get())
        settings.predictive_smoothing = bool(self.predictive_smoothing_var.get())
        settings.bulb_layout = {}
        settings.__post_init__()
        self.config.save()
        if hasattr(self, "header_monitor_var"):
            self.header_monitor_var.set(f"Monitor: {self.monitor_menu.get()}")
        if self.screen_sync and self.screen_sync.is_running:
            self._schedule_screen_sync_restart()

    def _queue_screen_sync_settings_save(self, _=None):
        if self._screen_sync_settings_save_id:
            self.root.after_cancel(self._screen_sync_settings_save_id)
        self._screen_sync_settings_save_id = self.root.after(180, self._save_screen_sync_settings)
        if self.screen_sync and self.screen_sync.is_running:
            self._set_status("Restarting sync...")

    def _current_clap_config(self) -> ClapConfig:
        clap = self.config.clap
        return ClapConfig(
            threshold=clap.threshold,
            rms_threshold=clap.rms_threshold,
            min_peak_to_rms=clap.min_peak_to_rms,
            adaptive_multiplier=clap.adaptive_multiplier,
            max_duration=clap.max_duration,
            cooldown=clap.cooldown,
            double_clap=clap.double_clap,
            double_clap_window=clap.double_clap_window,
        )

    def _schedule_screen_sync_restart(self):
        if self._screen_sync_reconfigure_id:
            self.root.after_cancel(self._screen_sync_reconfigure_id)
        self._screen_sync_reconfigure_id = self.root.after(800, self._restart_screen_sync)

    def _restart_screen_sync(self):
        self._screen_sync_reconfigure_id = None
        if self.screen_sync and self.screen_sync.is_running:
            self._set_status("Restarting sync...")
            self._stop_screen_sync(update_status=False)
            self._start_screen_sync()

    def _on_fps_change(self, value):
        self.fps_value.set(int(float(value)))
        self.fps_label.set(f"Responsiveness: {self.fps_value.get()} FPS")
        if self.min_fps_value.get() > self.fps_value.get():
            self.min_fps_value.set(self.fps_value.get())
        self._queue_screen_sync_settings_save()

    def _on_min_fps_change(self, value):
        self.min_fps_value.set(int(float(value)))
        if self.min_fps_value.get() > self.fps_value.get():
            self.min_fps_value.set(self.fps_value.get())
        self._queue_screen_sync_settings_save()

    def _on_smoothing_change(self, value):
        self.smoothing_value.set(float(value))
        self.smoothing_label.set(f"Smoothness: {self.smoothing_value.get():.2f}")
        self._queue_screen_sync_settings_save()

    def _on_boost_change(self, value):
        self.boost_value.set(float(value))
        self.boost_label.set(f"Color Punch: {self.boost_value.get():.2f}x")
        self._queue_screen_sync_settings_save()

    def _on_min_brightness_change(self, value):
        self.min_brightness_value.set(int(float(value)))
        self.min_brightness_label.set(f"Dark Scene Floor: {self.min_brightness_value.get()}")
        self._queue_screen_sync_settings_save()

    def _brightness_mode_hint_text(self) -> str:
        if self.brightness_mode_labels.get(self.brightness_mode_value.get(), "follow") == "follow":
            return "Bulb brightness tracks the scene; true black turns the bulb off."
        return "Brightness stays fixed; only the color follows the screen (black holds the last color)."

    def _update_sync_brightness_state(self) -> None:
        mode = self.brightness_mode_labels.get(self.brightness_mode_value.get(), "follow")
        state = "normal" if mode == "constant" else "disabled"
        self.sync_brightness_slider.configure(state=state)

    def _on_brightness_mode_change(self, _value=None):
        self.brightness_mode_hint.configure(text=self._brightness_mode_hint_text())
        self._update_sync_brightness_state()
        self._queue_screen_sync_settings_save()

    def _on_sync_brightness_change(self, value):
        self.sync_brightness_value.set(int(float(value)))
        self.sync_brightness_label.set(
            f"Constant Brightness: {round(self.sync_brightness_value.get() / 255 * 100)}%"
        )
        self._queue_screen_sync_settings_save()

    def _on_layout_change(self, _ip: str):
        self._queue_screen_sync_settings_save()

    def _set_status(self, msg: str):
        self.status_label.configure(text=msg)
        if hasattr(self, "status_indicator"):
            self.status_indicator.configure(fg_color=self._sync_status_color(msg))

    def _export_diagnostics_report(self) -> None:
        report = build_diagnostics_report(
            platform="windows-desktop",
            app_version="0.1.0",
            bulbs=[
                {"name": bulb.name, "ip": bulb.ip, "mac": bulb.mac}
                for bulb in self.config.bulbs
            ],
            screen_sync_settings=self.config.screen_sync.__dict__,
            runtime_stats=self.screen_sync.debug_snapshot if self.screen_sync else None,
            notes=[
                "WiZ local communication must be enabled on the trusted LAN.",
                "Hardware color accuracy still requires the camera validation flow.",
            ],
        )
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export WizLight diagnostics",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile="wizlight-diagnostics.json",
        )
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(report, indent=2), encoding="utf-8")
            self._set_status(f"Diagnostics exported: {path}")
        except Exception as exc:
            self._set_status(f"Diagnostics export failed: {exc}")

    def _show_control_guide(self):
        guide = ctk.CTkToplevel(self.root)
        guide.title("Screen Sync Guide")
        guide.geometry("460x430")
        guide.transient(self.root)
        guide.grab_set()

        frame = ctk.CTkFrame(guide)
        frame.pack(fill="both", expand=True, padx=14, pady=14)
        ctk.CTkLabel(frame, text="Controls Guide", font=("Segoe UI", 18, "bold")).pack(
            anchor="w", padx=14, pady=(14, 10)
        )
        text = ctk.CTkTextbox(frame, wrap="word", height=280)
        text.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        text.insert(
            "1.0",
            "Responsiveness\n"
            "Higher FPS reacts faster to video changes but sends more network updates. "
            "Use 24-28 for YouTube and movies; lower it if the bulb stutters.\n\n"
            "Smoothness\n"
            "Lower values are snappier. Higher values reduce flicker but can feel delayed.\n\n"
            "Color Punch\n"
            "Adds saturation for richer ambience. Too high can make skin tones and whites look artificial.\n\n"
            "Dark Scene Floor\n"
            "Keeps dark scenes visible on the bulb. Lower is more accurate for black scenes; "
            "higher keeps the room from going too dim.\n\n"
            "Fast GPU capture\n"
            "Uses DXCam when it can target your selected monitor. It falls back to MSS capture automatically.\n\n"
            "Monitor\n"
            "Choose the display that contains Chrome/YouTube. The tray menu has the same monitor selector.",
        )
        text.configure(state="disabled")
        ctk.CTkButton(frame, text="Close", command=guide.destroy).pack(fill="x", padx=14, pady=(0, 14))

    def _format_debug_color(self, color: Optional[tuple[int, int, int]]) -> str:
        if color is None:
            return "-"
        return f"RGB({color[0]}, {color[1]}, {color[2]})"

    def _set_color_panel(self, panel, color: Optional[tuple[int, int, int]]):
        if color is None:
            panel._swatch.configure(fg_color="#000000")
            panel._label_var.set("RGB(-)")
            return
        panel._swatch.configure(fg_color=f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}")
        panel._label_var.set(self._format_debug_color(color))

    def _set_metric(self, key: str, value: str):
        variable = getattr(self, "_sync_metric_vars", {}).get(key)
        if variable is not None:
            variable.set(value)

    def _set_sync_running_ui(self, running: bool):
        if hasattr(self, "sync_primary_button"):
            self.sync_primary_button.configure(
                text="Stop Screen Sync" if running else "Start Screen Sync",
                fg_color="#B91C1C" if running else ("#3B8ED0", "#1F6AA5"),
            )
        if hasattr(self, "header_runtime_var"):
            self.header_runtime_var.set("Sync: running" if running else "Sync: idle")

    def _refresh_screen_sync_debug(self):
        self._screen_sync_debug_id = None
        if not self.screen_sync or not self.screen_sync.is_running:
            self.screen_sync_debug_runtime.set("Runtime: idle")
            self.screen_sync_debug_target.set("Target: -")
            self.screen_sync_debug_output.set("Output: -")
            self.screen_sync_debug_perf.set("FPS: -")
            self.screen_sync_debug_cadence.set("Cadence: -")
            self.screen_sync_debug_motion.set("Motion: -")
            self.screen_sync_debug_settings.set("Settings: -")
            self.screen_sync_debug_error.set("")
            self.screen_sync_debug_preview.configure(fg_color="#000000")
            self._set_color_panel(self.sync_target_preview, None)
            self._set_color_panel(self.sync_output_preview, None)
            for key in ("fps", "frame", "send", "motion", "capture"):
                self._set_metric(key, "-")
            self._set_metric("status", "Idle")
            self._set_sync_running_ui(False)
            return

        snapshot = self.screen_sync.debug_snapshot
        target_colors = snapshot["target_colors"]
        current_colors = snapshot["current_colors"]
        target = target_colors.get("all") or (
            average_colors(tuple(target_colors.values())) if target_colors else None
        )
        output = current_colors.get("all") or (
            average_colors(tuple(current_colors.values())) if current_colors else None
        )

        status = snapshot.get("capture_status") or "OK"
        # Persistent all-black capture during playback = the video is not
        # capturable (DRM-protected app/browser or hardware-overlay player);
        # without this hint the bulb just sits dark and mapping looks broken.
        if snapshot.get("capture_looks_black"):
            status = (
                "Capture is black — protected/overlay video? "
                "Try fullscreen browser playback or disable hardware acceleration"
            )
        status_part = f" | {status}" if status != "OK" else ""
        status_display = "Running" if status == "OK" else status
        self._set_color_panel(self.sync_target_preview, target)
        self._set_color_panel(self.sync_output_preview, output)
        self._set_metric("fps", f"{snapshot['currentFps']}/{snapshot['requestedFps']}")
        self._set_metric("frame", f"{snapshot['average_frame_time_ms']:.1f}")
        self._set_metric("send", f"{snapshot['send_rate_hz']:.1f}" if snapshot["send_rate_hz"] > 0 else "warm")
        self._set_metric("motion", f"{snapshot['motion_score']:.3f}")
        self._set_metric("capture", snapshot["capture_method"].upper())
        self._set_metric("status", status_display)
        self._set_sync_running_ui(True)
        if hasattr(self, "header_runtime_var"):
            self.header_runtime_var.set(
                f"{snapshot['capture_method'].upper()} | {snapshot['current_fps']} FPS | {snapshot['send_rate_hz']:.1f} Hz"
            )
        if status != "OK":
            self.status_indicator.configure(fg_color=self._sync_status_color(status))
        self.screen_sync_debug_runtime.set(
            f"Runtime: {snapshot['capture_method'].upper()} | {snapshot['mode']}{status_part}"
        )
        self.screen_sync_debug_target.set(f"Target: {self._format_debug_color(target)}")
        self.screen_sync_debug_output.set(f"Output: {self._format_debug_color(output)}")
        perf_text = f"FPS: {snapshot['current_fps']} | frame {snapshot['average_frame_time_ms']:.1f} ms"
        if snapshot.get("capture_time_ms") is not None:
            perf_text += (
                f" | capture {snapshot['capture_time_ms']:.1f} ms"
                f" | processing {snapshot['processing_time_ms']:.1f} ms"
            )
        self.screen_sync_debug_perf.set(perf_text)
        if snapshot["send_rate_hz"] > 0:
            self.screen_sync_debug_cadence.set(
                f"Cadence: {snapshot['send_rate_hz']:.1f} Hz | {snapshot['send_interval_ms']:.0f} ms | sends {snapshot['updates_sent']}"
            )
        else:
            self.screen_sync_debug_cadence.set(
                f"Cadence: warming up | sends {snapshot['updates_sent']}"
            )
        self.screen_sync_debug_motion.set(
            f"Motion: {snapshot['motion_score']:.3f} | smooth {snapshot['smoothing_factor']:.2f} | predict {snapshot['prediction_weight']:.2f}"
        )
        mode_label = "follow" if snapshot.get("brightness_mode") == "follow" else "constant"
        self.screen_sync_debug_settings.set(
            f"Settings: boost {snapshot['effectiveColorBoost']:.2f}x | delta {snapshot['deltaThreshold']} | "
            f"brightness {mode_label} {round(snapshot.get('output_brightness', 255) / 255 * 100)}%"
        )
        self.screen_sync_debug_error.set(
            f"Error: {snapshot['last_error']}" if snapshot["last_error"] else ""
        )
        if output is not None:
            self.screen_sync_debug_preview.configure(
                fg_color=f"#{output[0]:02x}{output[1]:02x}{output[2]:02x}"
            )

        self._screen_sync_debug_id = self.root.after(350, self._refresh_screen_sync_debug)

    def _update_bulb_status(self):
        has_bulbs = bool(self.config.bulbs)
        if self.config.bulbs:
            text = "\n".join([f"- {bulb.name} ({bulb.ip})" for bulb in self.config.bulbs])
            self.bulb_info.configure(text=text, text_color=("gray20", "gray80"))
        else:
            self.bulb_info.configure(text="No bulbs found", text_color="gray")
        if hasattr(self, "header_bulbs_var"):
            self.header_bulbs_var.set(f"Bulbs: {len(self.config.bulbs)}")
        if hasattr(self, "header_monitor_var"):
            self.header_monitor_var.set(f"Monitor: {self.monitor_menu.get()}")
        for attr in (
            "sync_primary_button",
            "screen_sync_switch",
            "clap_switch",
            "toggle_power_button",
            "turn_off_button",
            "brightness_slider",
            "pick_color_button",
            "preset_menu",
            "temp_slider",
            "remove_stale_button",
        ):
            widget = getattr(self, attr, None)
            if widget is not None:
                try:
                    widget.configure(state="normal" if has_bulbs else "disabled")
                except Exception:
                    pass
        if hasattr(self, "layout_frame"):
            self._refresh_screen_sync_layout_controls()

    def _discover_bulbs(self):
        self._set_status("Discovering...")

        async def discover():
            try:
                bulbs = await self.controller.discover()
                for index, bulb in enumerate(bulbs, 1):
                    self.config.add_bulb(bulb["ip"], f"Bulb {index}", bulb["mac"])
                self.root.after(0, self._update_bulb_status)
                self.root.after(0, lambda: self._set_status(f"Found {len(bulbs)} bulb(s)"))
            except Exception as exc:
                message = f"Error: {exc}"
                self.root.after(0, lambda msg=message: self._set_status(msg))

        self._run_async(discover())

    def _remove_stale_bulbs(self):
        configured_ips = self._get_bulb_ips()
        if not configured_ips:
            self._set_status("No bulbs configured")
            return

        self._set_status("Checking stale bulbs...")

        async def prune():
            try:
                stale_ips = await self.controller.find_stale_bulbs(configured_ips)
                removed = self.config.remove_bulbs(stale_ips)
                self.root.after(0, self._update_bulb_status)
                if removed:
                    self.root.after(0, lambda: self._set_status(f"Removed {removed} stale bulb(s)"))
                else:
                    self.root.after(0, lambda: self._set_status("No stale bulbs found"))
            except Exception as exc:
                message = f"Stale check failed: {exc}"
                self.root.after(0, lambda msg=message: self._set_status(msg))

        self._run_async(prune())

    def _turn_on(self):
        ips = self._get_bulb_ips()
        if ips:
            self._run_async(self.controller.turn_on_all(ips))
            self._set_status("Turned on")

    def _turn_off(self):
        ips = self._get_bulb_ips()
        if ips:
            self._run_async(self.controller.turn_off_all(ips))
            self._set_status("Turned off")

    def _toggle(self):
        ips = self._get_bulb_ips()
        if ips:
            self._run_async(self.controller.toggle_all(ips))
            self._set_status("Toggled")

    def _on_brightness_change(self, value):
        brightness = int(float(value))
        self.brightness_label_var.set(f"Brightness: {int((brightness / 255) * 100)}%")
        if self._brightness_debounce_id:
            self.root.after_cancel(self._brightness_debounce_id)

        def send():
            ips = self._get_bulb_ips()
            if ips:
                self._run_async(self.controller.turn_on_all(ips, brightness))

        self._brightness_debounce_id = self.root.after(140, send)

    def _pick_color(self):
        def on_color(r, g, b):
            # Animate color preview transition
            self.animate_color_preview(self.color_preview, (r, g, b))
            ips = self._get_bulb_ips()
            if ips:
                self._run_async(self.controller.set_rgb_all(ips, r, g, b))
                self._set_status(f"Color: RGB({r}, {g}, {b})")

        # Use new color wheel picker
        try:
            # Get current color from preview
            current = self.color_preview.cget("fg_color")
            if isinstance(current, str) and current.startswith("#"):
                r = int(current[1:3], 16)
                g = int(current[3:5], 16)
                b = int(current[5:7], 16)
                initial = (r, g, b)
            else:
                initial = None
        except Exception:
            initial = None
        
        ColorWheelPicker(self.root, on_color, initial_color=initial)

    def _apply_preset_from_menu(self, label: str):
        if label.lower() in PRESETS:
            self._apply_preset(label.lower())

    def _apply_preset(self, preset_name: str):
        ips = self._get_bulb_ips()
        if ips:
            self._run_async(apply_preset(self.controller, ips, preset_name))
            self._set_status(f"Preset: {preset_name}")

    def _on_temp_change(self, value):
        kelvin = int(float(value))
        self.temp_label_var.set(f"Color Temperature: {kelvin}K")
        if self._temp_debounce_id:
            self.root.after_cancel(self._temp_debounce_id)

        def send():
            ips = self._get_bulb_ips()
            if ips:
                self._run_async(self.controller.set_color_temp_all(ips, kelvin))

        self._temp_debounce_id = self.root.after(160, send)

    def _start_screen_sync(self):
        configured_ips = self._get_bulb_ips()
        try:
            ips = self._async_runner.run(
                self.controller.refresh_screen_sync_targets(configured_ips),
                timeout=6.0,
            )
        except Exception as exc:
            self.screen_sync_switch.deselect()
            self._set_sync_running_ui(False)
            self._set_status(f"Sync preflight failed: {exc}")
            return
        if not ips:
            self.screen_sync_switch.deselect()
            self._set_sync_running_ui(False)
            self.config.screen_sync.enabled = False
            self.config.save()
            self._set_status("No reachable bulbs available for screen sync")
            return

        self._save_screen_sync_settings()
        settings = self.config.screen_sync
        try:
            from src.features.screen_sync_v2 import resolve_auto_display_profile
            resolved = resolve_auto_display_profile(settings)
        except Exception:
            resolved = {}
        effective_color_space = resolved.get(
            "source_color_space", settings.source_color_space
        )
        if effective_color_space == "auto":
            effective_color_space = "srgb"
        effective_profile = resolved.get("device_profile", "generic")
        if effective_profile in (None, "", "auto"):
            effective_profile = "generic"
        cool_bias = float(resolved.get("cool_mix_bias", 0.0))
        try:
            self._async_runner.run(
                self.controller.prepare_screen_sync_mappers(
                    ips,
                    source_color_space=effective_color_space,
                    device_profile=effective_profile,
                    cool_mix_bias=cool_bias,
                ),
                timeout=8.0,
            )
        except Exception:
            pass
        active_regions: tuple[str, ...] = ()
        mode = "single"
        self._screen_sync_dispatcher = CoalescingAsyncDispatcher(
            self._async_runner.submit,
            self.controller.set_screen_sync_map,
        )
        self._screen_sync_fast_sender = FastBulbSenderPool(
            max_send_hz=min(28, max(12, settings.max_fps)),
            prewarm_ips=ips,
        )

        def on_color_change(colors_by_target):
            bulb_colors = build_bulb_color_map(ips, colors_by_target, "single", {})
            if bulb_colors:
                # Brightness rides the setPilot dimming field so hue is always
                # rendered at full LED drive (colors are value-normalized by
                # the engine); read it at send time from the engine.
                engine = self.screen_sync
                brightness = engine.output_brightness if engine else None
                payloads = self.controller.map_screen_sync_payloads_cached(bulb_colors)
                if self._screen_sync_fast_sender and len(payloads) == len(bulb_colors):
                    self._screen_sync_fast_sender.send_payload_map(payloads, brightness)
                elif self._screen_sync_dispatcher:
                    self._screen_sync_dispatcher.submit(bulb_colors)
            preview = average_colors(tuple(colors_by_target.values()))
            self.root.after(
                0,
                lambda: self.color_preview.configure(
                    fg_color=f"#{preview[0]:02x}{preview[1]:02x}{preview[2]:02x}"
                ),
            )

        self.screen_sync = OptimizedScreenSync(
            on_color_change=on_color_change,
            config=build_optimized_capture_config(settings, active_regions),
        )
        self.screen_sync.start()
        self._set_sync_running_ui(True)
        if self._screen_sync_debug_id:
            self.root.after_cancel(self._screen_sync_debug_id)
        self._refresh_screen_sync_debug()

        profile = "cinematic single" if mode == "single" and settings.color_algorithm == "auto" else settings.color_algorithm.upper()
        mapping = self.controller.summarize_screen_sync_mapping(ips)
        runtime = f"{self.screen_sync.capture_method.upper()} / {profile}"
        if mapping:
            runtime = f"{runtime} / {mapping}"
        skipped = len(configured_ips) - len(ips)
        self._set_status(
            f"Screen sync ON ({runtime}{', skipped ' + str(skipped) + ' stale bulb(s)' if skipped else ''})"
        )

    def _stop_screen_sync(self, update_status: bool = True):
        if self.screen_sync:
            self.screen_sync.stop()
            self.screen_sync = None
        if self._screen_sync_dispatcher:
            self._screen_sync_dispatcher.close()
            self._screen_sync_dispatcher = None
        if self._screen_sync_fast_sender:
            self._screen_sync_fast_sender.close()
            self._screen_sync_fast_sender = None
        if self._screen_sync_debug_id:
            self.root.after_cancel(self._screen_sync_debug_id)
            self._screen_sync_debug_id = None
        self._set_sync_running_ui(False)
        self._refresh_screen_sync_debug()
        if update_status:
            self._set_status("Screen sync OFF")

    def _toggle_screen_sync(self):
        self.config.screen_sync.enabled = bool(self.screen_sync_switch.get())
        self.config.save()
        if self.screen_sync_switch.get():
            self._start_screen_sync()
        else:
            self._stop_screen_sync()

    def _toggle_screen_sync_from_primary(self):
        if self.screen_sync and self.screen_sync.is_running:
            self.screen_sync_switch.deselect()
        else:
            self.screen_sync_switch.select()
        self._toggle_screen_sync()

    def _toggle_clap_detection(self):
        self.config.clap.enabled = bool(self.clap_switch.get())
        self.config.save()
        if self.clap_switch.get():
            ips = self._get_bulb_ips()
            if not ips:
                self.clap_switch.deselect()
                self.config.clap.enabled = False
                self.config.save()
                self._set_status("No bulbs configured")
                return

            def on_clap():
                self._run_async(self.controller.toggle_all(ips))
                self.root.after(0, lambda: self._set_status("Clap detected - toggled"))

            self.clap_detector = ClapDetector(
                on_clap=on_clap,
                config=self._current_clap_config(),
            )
            self.clap_detector.start()
            mode = "double clap" if self.config.clap.double_clap else "single clap"
            self._set_status(f"Clap detection ON ({mode})")
        else:
            if self.clap_detector:
                self.clap_detector.stop()
                self.clap_detector = None
            self._set_status("Clap detection OFF")
    
    # === View Switching ===
    
    def _switch_view(self, view: str):
        """Switch between primary app sections."""
        if view == self._current_view:
            return

        for frame in self._view_frames.values():
            frame.pack_forget()
        self._current_view = view

        for name, button in self._view_buttons.items():
            active = name == view
            button.configure(
                fg_color=("#3B8ED0", "#1F6AA5") if active else "transparent",
                border_width=0 if active else 1,
            )
        self._view_frames[view].pack(fill="both", expand=True)

        if view == "bulbs":
            self._update_dashboard()
        elif self._dashboard:
            self._dashboard.stop_auto_refresh()
    
    def _update_dashboard(self):
        """Update dashboard with current bulbs."""
        if self._dashboard:
            bulbs = [
                BulbInfo(ip=b.ip, name=b.name)
                for b in self.config.bulbs
            ]
            self._dashboard.set_bulbs(bulbs)
            self._dashboard.start_auto_refresh(interval_ms=5000)
    
    def _on_bulb_select(self, ip: str):
        """Handle bulb selection from dashboard."""
        self._switch_view("lights")
        self._set_status(f"Selected: {ip}")
    
    # === System Tray ===
    
    def _setup_system_tray(self):
        """Initialize system tray if available."""
        if not is_tray_available():
            return
        
        try:
            self._tray = SystemTrayManager(
                on_show=lambda: self.root.after(0, self._show_from_tray),
                on_quit=lambda: self.root.after(0, self._quit_app),
                on_toggle=lambda: self.root.after(0, self._toggle),
                on_toggle_screen_sync=lambda: self.root.after(0, self._toggle_screen_sync_from_tray),
                on_toggle_clap=lambda: self.root.after(0, self._toggle_clap_from_tray),
                on_brightness=lambda value: self.root.after(0, lambda v=value: self._set_brightness_quick(v)),
                on_color_temp=lambda value: self.root.after(0, lambda v=value: self._set_temp_quick(v)),
                on_monitor=lambda index: self.root.after(0, lambda idx=index: self._set_monitor_from_tray(idx)),
                on_manage_monitors=lambda: self.root.after(0, self._open_monitor_manager),
                on_preset=lambda preset: self.root.after(0, lambda name=preset: self._apply_preset(name)),
                presets=list(PRESETS.keys()),
                monitors=self._monitor_options,
                icon_path=self._asset_path("packaging", "assets", "wizlight_icon.png"),
            )
            self._tray.start()
        except Exception as e:
            print(f"System tray init failed: {e}")
            self._tray = None

    def _toggle_screen_sync_from_tray(self):
        if self.screen_sync and self.screen_sync.is_running:
            self.screen_sync_switch.deselect()
            self._toggle_screen_sync()
        else:
            self.screen_sync_switch.select()
            self._toggle_screen_sync()

    def _toggle_clap_from_tray(self):
        if self.clap_detector and self.clap_detector.is_running:
            self.clap_switch.deselect()
            self._toggle_clap_detection()
        else:
            self.clap_switch.select()
            self._toggle_clap_detection()

    def _set_brightness_quick(self, brightness: int):
        brightness = max(0, min(255, int(brightness)))
        self.brightness_slider.set(brightness)
        self._on_brightness_change(brightness)
        self._set_status(f"Brightness: {int((brightness / 255) * 100)}%")

    def _set_temp_quick(self, kelvin: int):
        kelvin = max(2200, min(6500, int(kelvin)))
        self.temp_slider.set(kelvin)
        self._on_temp_change(kelvin)
        self._set_status(f"Color temperature: {kelvin}K")

    def _set_monitor_from_tray(self, monitor_index: int):
        selected = next(
            (
                str(option["label"])
                for option in self._monitor_options
                if int(option["index"]) == int(monitor_index)
            ),
            None,
        )
        if selected is None:
            self._set_status("Monitor is no longer available")
            return
        self.monitor_menu.set(selected)
        self._save_screen_sync_settings()
        self._set_status(f"Screen sync monitor: {selected}")

    def _open_monitor_manager(self):
        self._show_from_tray()
        guide = ctk.CTkToplevel(self.root)
        guide.title("Monitor Selection")
        guide.geometry("430x260")
        guide.transient(self.root)
        guide.grab_set()

        frame = ctk.CTkFrame(guide)
        frame.pack(fill="both", expand=True, padx=14, pady=14)
        ctk.CTkLabel(frame, text="Screen Sync Monitor", font=("Segoe UI", 18, "bold")).pack(
            anchor="w", padx=14, pady=(14, 8)
        )
        ctk.CTkLabel(
            frame,
            text="Choose the display that contains Chrome/YouTube. This is the same monitor used by screen sync.",
            wraplength=360,
            justify="left",
            text_color=("gray35", "gray70"),
        ).pack(anchor="w", padx=14, pady=(0, 12))
        selector = ctk.CTkOptionMenu(
            frame,
            values=[str(option["label"]) for option in self._monitor_options],
        )
        selector.set(self.monitor_menu.get())
        selector.pack(fill="x", padx=14, pady=(0, 12))

        def apply_selection():
            self.monitor_menu.set(selector.get())
            self._save_screen_sync_settings()
            self._set_status(f"Screen sync monitor: {selector.get()}")
            guide.destroy()

        ctk.CTkButton(frame, text="Use This Monitor", command=apply_selection).pack(
            fill="x", padx=14, pady=(0, 14)
        )
    
    def _minimize_to_tray(self):
        """Minimize window to system tray."""
        if self._tray and self._tray.is_running:
            self._minimized_to_tray = True
            self.root.withdraw()
            self._tray.show_notification("WizLight", "Minimized to tray. Click icon to restore.")
    
    def _show_from_tray(self):
        """Restore window from system tray."""
        self._minimized_to_tray = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
    
    def _quit_app(self):
        """Quit application from tray."""
        self._minimized_to_tray = False
        self._on_close()

    def _on_close(self):
        # If minimized to tray, just hide
        if self._tray and self._tray.is_running and self._minimized_to_tray:
            return
        
        # Stop tray
        if self._tray:
            self._tray.stop()
        
        # Stop dashboard refresh
        if self._dashboard:
            self._dashboard.stop_auto_refresh()
        
        if self.screen_sync:
            self.screen_sync.stop()
        if self.clap_detector:
            self.clap_detector.stop()
        if self._brightness_debounce_id:
            self.root.after_cancel(self._brightness_debounce_id)
        if self._temp_debounce_id:
            self.root.after_cancel(self._temp_debounce_id)
        if self._screen_sync_settings_save_id:
            self.root.after_cancel(self._screen_sync_settings_save_id)
        if self._screen_sync_reconfigure_id:
            self.root.after_cancel(self._screen_sync_reconfigure_id)
        if self._screen_sync_debug_id:
            self.root.after_cancel(self._screen_sync_debug_id)

        for future in self._pending_tasks:
            if not future.done():
                future.cancel()

        try:
            self._async_runner.cancel_pending(timeout=2.0)
            self._async_runner.run(self.controller.close_async(), timeout=2.0)
        finally:
            gc.collect()
            self._async_runner.shutdown()
            self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    app = WizLightModernGUI()
    app.run()


if __name__ == "__main__":
    main()
