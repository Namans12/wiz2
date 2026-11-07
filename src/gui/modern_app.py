"""Modern GUI for WizLight using CustomTkinter."""

import asyncio
import copy
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
from ..core.device_identity import mac_identity
from ..features.bulb_recovery import fetch_fresh_identity, recover_saved_bulbs, refresh_verified_targets
from ..features.clap_detector import ClapConfig, ClapDetector
from ..features.screen_sync import (
    average_colors,
    build_bulb_color_map,
    effective_screen_sync_mode,
    list_monitors,
    resolve_active_regions,
)
from ..features.screen_sync_v2 import OptimizedScreenSync, build_optimized_capture_config
from ..features.fast_udp_sender import FastBulbSenderPool
from ..features.sync_transport import ScreenSyncTransport
from ..features.sync_preflight import prepare_screen_sync_mapping

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
        self._screen_sync_transport: Optional[ScreenSyncTransport] = None
        self._closing = False
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
        self.root.configure(fg_color="#0D1422")
        scale = self.root._get_window_scaling()
        width = min(1080, int((self.root.winfo_screenwidth() - 80) / scale))
        height = min(780, int((self.root.winfo_screenheight() - 100) / scale))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(860, width), min(580, height))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_window_icon()

        self._build_ui()
        self._update_bulb_status()
        self._setup_system_tray()
        if self.config._load_warning:
            self._set_status(self.config._load_warning)

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
        if self._closing:
            coro.close()
            return
        future = self._async_runner.submit(coro)
        self._pending_tasks.append(future)
        self._pending_tasks = [task for task in self._pending_tasks if not task.done()]

    def _get_bulb_ips(self) -> list[str]:
        return [bulb.ip for bulb in self.config.bulbs]

    def _card(self, parent, title: str, subtitle: str | None = None):
        card = ctk.CTkFrame(parent, corner_radius=14, fg_color="#192638",
                            border_width=1, border_color="#2B3A52")
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
        tile = ctk.CTkFrame(parent, corner_radius=10, border_width=1,
                            fg_color="#111D2E", border_color="#2B3A52")
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
        self._main_container.pack(fill="both", expand=True, padx=16, pady=16)
        sidebar = ctk.CTkFrame(self._main_container, width=164, corner_radius=14,
                              fg_color="#111D2E")
        sidebar.pack(side="left", fill="y", padx=(0, 16))
        sidebar.pack_propagate(False)
        ctk.CTkLabel(sidebar, text="WizLight", font=("Segoe UI", 25, "bold")).pack(
            anchor="w", padx=16, pady=(24, 0))
        ctk.CTkLabel(sidebar, text="Light, in sync.", text_color="#91A4BF").pack(
            anchor="w", padx=16, pady=(2, 28))
        self._page_labels = {
            "sync": ("Screen sync", "Set the mood, then let your screen lead."),
            "lights": ("Light controls", "Color, brightness and presets for your room."),
            "bulbs": ("Your bulbs", "Connect and manage lights on your local network."),
            "tools": ("Tools & help", "Setup guides and diagnostics when you need them."),
        }
        self._view_buttons = {}
        for index, (view, (label, _)) in enumerate(self._page_labels.items(), start=1):
            button = ctk.CTkButton(
                sidebar, text=label, height=46, anchor="w", corner_radius=10,
                font=("Segoe UI", 13), fg_color="transparent", hover_color="#263C58",
                command=lambda name=view: self._switch_view(name),
            )
            button.pack(fill="x", padx=10, pady=4)
            self._view_buttons[view] = button
            self.root.bind(f"<Control-Key-{index}>",
                           lambda event, name=view: self._switch_view(name))
        ctk.CTkLabel(sidebar, text="Local control\nCtrl + 1–4 to navigate", justify="left",
                     text_color="#91A4BF", font=("Segoe UI", 11)).pack(
                         side="bottom", anchor="w", padx=16, pady=20)
        if is_tray_available():
            self._tray_btn = ctk.CTkButton(
                sidebar, text="Minimize to tray", height=36,
                command=self._minimize_to_tray, fg_color="transparent", border_width=1)
            self._tray_btn.pack(side="bottom", fill="x", padx=10, pady=6)
        workspace = ctk.CTkFrame(self._main_container, fg_color="transparent")
        workspace.pack(side="left", fill="both", expand=True)
        self._page_title_var = ctk.StringVar()
        self._page_subtitle_var = ctk.StringVar()
        ctk.CTkLabel(workspace, textvariable=self._page_title_var,
                     font=("Segoe UI", 25, "bold")).pack(anchor="w", pady=(4, 0))
        ctk.CTkLabel(workspace, textvariable=self._page_subtitle_var,
                     text_color="#A5B7CF").pack(anchor="w", pady=(0, 16))
        status_bar = ctk.CTkFrame(workspace, corner_radius=12, fg_color="#111D2E")
        status_bar.pack(side="bottom", fill="x", pady=(12, 0))
        self.status_indicator = ctk.CTkFrame(
            status_bar, width=12, height=12, corner_radius=6, fg_color="#5F6368"
        )
        self.status_indicator.pack(side="left", padx=(12, 8), pady=10)
        self.status_label = ctk.CTkLabel(
            status_bar, text="Ready", text_color="#C6D5E8", anchor="w",
            wraplength=470, justify="left"
        )
        self.status_label.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=8)
        self.header_bulbs_var = ctk.StringVar(value="Bulbs: 0")
        self.header_monitor_var = ctk.StringVar(value="Monitor: -")
        self.header_runtime_var = ctk.StringVar(value="Sync: idle")
        summary_row = ctk.CTkFrame(workspace, fg_color="transparent")
        summary_row.pack(fill="x", pady=(0, 10))
        for variable in (self.header_bulbs_var, self.header_monitor_var, self.header_runtime_var):
            badge = ctk.CTkFrame(summary_row, fg_color="#18283D", corner_radius=9)
            badge.pack(side="left", padx=(0, 8))
            ctk.CTkLabel(
                badge, textvariable=variable, text_color="#CBD9EA",
                font=("Segoe UI", 11), height=28,
            ).pack(padx=10)

        self._view_host = ctk.CTkFrame(workspace, fg_color="transparent")
        self._view_host.pack(fill="both", expand=True)
        self._view_frames: dict[str, ctk.CTkFrame] = {
            "sync": SmoothScrollableFrame(self._view_host, fg_color="#0D1422"),
            "lights": SmoothScrollableFrame(self._view_host, fg_color="#0D1422"),
            "bulbs": SmoothScrollableFrame(self._view_host, fg_color="#0D1422"),
            "tools": SmoothScrollableFrame(self._view_host, fg_color="#0D1422"),
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

        cockpit = self._card(sync, "Screen Sync", "Match your lights to movies, games and everyday screen content.")
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
        # Keep switch state for tray callbacks; the primary button is the visible sync action.
        self.clap_switch = ctk.CTkSwitch(
            action_row,
            text="Clap control",
            command=self._toggle_clap_detection,
        )
        self.clap_switch.pack(side="left")

        ctk.CTkButton(
            cockpit, text="Picture settings ↓", height=32, fg_color="transparent",
            border_width=1, command=self._scroll_to_sync_settings,
        ).pack(anchor="w", padx=14, pady=(0, 12))

        self._live_details_open = False
        self._live_details_button = ctk.CTkButton(
            cockpit, text="Show live details  ▾", height=32, anchor="w",
            fg_color="transparent", hover_color="#263C58",
            command=self._toggle_live_details,
        )
        self._live_details_button.pack(fill="x", padx=14, pady=(0, 8))
        self._live_details = ctk.CTkFrame(cockpit, fg_color=self._surface_color(cockpit))

        preview_grid = ctk.CTkFrame(self._live_details, fg_color=self._surface_color(cockpit))
        preview_grid.pack(fill="x", padx=14, pady=(0, 12))
        preview_grid.columnconfigure((0, 1), weight=1, uniform="preview")
        self.sync_target_preview = self._color_panel(preview_grid, "Target", "RGB(-)")
        self.sync_target_preview.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.sync_output_preview = self._color_panel(preview_grid, "Output", "RGB(-)")
        self.sync_output_preview.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self._sync_metric_vars: dict[str, ctk.StringVar] = {}
        metric_grid = ctk.CTkFrame(self._live_details, fg_color=self._surface_color(cockpit))
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

        settings = self._card(sync, "Picture settings", "Only the controls that matter while watching video.")
        self._sync_settings_card = settings
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
        self.mixing_model_labels = {
            "Standard": "heuristic",
            "Physical (experimental)": "physical",
        }
        self.mixing_model_value = ctk.StringVar(
            value=next(
                (
                    label
                    for label, key in self.mixing_model_labels.items()
                    if key == self.config.screen_sync.mixing_model
                ),
                "Standard",
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
        self.sync_mode_labels = {
            "Single bulb": "single",
            "Left / Right zones": "zones",
        }
        self.sync_mode_value = ctk.StringVar(
            value=next(
                (
                    label
                    for label, key in self.sync_mode_labels.items()
                    if key == self.config.screen_sync.mode
                ),
                "Single bulb",
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

        ctk.CTkLabel(settings, text="Color Mixing", anchor="w").pack(
            fill="x", padx=14, pady=(2, 4)
        )
        self.mixing_model_menu = ctk.CTkSegmentedButton(
            settings,
            values=list(self.mixing_model_labels.keys()),
            variable=self.mixing_model_value,
            command=self._on_mixing_model_change,
        )
        self.mixing_model_menu.pack(fill="x", padx=14, pady=(0, 4))
        self.mixing_model_hint = ctk.CTkLabel(
            settings,
            text=self._mixing_model_hint_text(),
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray60"),
        )
        self.mixing_model_hint.pack(fill="x", padx=14, pady=(0, 10))

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

        zone_card = self._card(
            sync,
            "Zone Layout",
            "Split the screen left/right and drive two bulbs independently, or keep one ambient color for all bulbs.",
        )
        self.sync_mode_menu = ctk.CTkSegmentedButton(
            zone_card,
            values=list(self.sync_mode_labels.keys()),
            variable=self.sync_mode_value,
            command=self._on_sync_mode_change,
        )
        self.sync_mode_menu.pack(fill="x", padx=14, pady=(0, 4))
        self.sync_mode_hint = ctk.CTkLabel(
            zone_card,
            text=self._sync_mode_hint_text(),
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray60"),
        )
        self.sync_mode_hint.pack(fill="x", padx=14, pady=(0, 8))
        self.layout_frame = ctk.CTkFrame(zone_card, fg_color=self._surface_color(zone_card))
        self.layout_frame.pack(fill="x", padx=14, pady=(0, 12))

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

    def _toggle_live_details(self):
        """Keep the primary controls compact while retaining live diagnostics."""
        self._live_details_open = not self._live_details_open
        if self._live_details_open:
            self._live_details.pack(fill="x")
            self._live_details_button.configure(text="Hide live details  ▴")
        else:
            self._live_details.pack_forget()
            self._live_details_button.configure(text="Show live details  ▾")

    def _color_panel(self, parent, title: str, rgb_text: str):
        panel = ctk.CTkFrame(parent, corner_radius=10, border_width=1,
                             fg_color="#111D2E", border_color="#2B3A52")
        ctk.CTkLabel(panel, text=title, text_color=("gray35", "gray70")).pack(
            anchor="w", padx=12, pady=(10, 0)
        )
        swatch = ctk.CTkFrame(panel, height=56, corner_radius=8, fg_color="#000000")
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
        settings.mode = self.sync_mode_labels.get(self.sync_mode_value.get(), "single")
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
        settings.mixing_model = self.mixing_model_labels.get(
            self.mixing_model_value.get(),
            "heuristic",
        )
        settings.use_gpu = bool(self.use_gpu_var.get())
        settings.adaptive_fps = bool(self.adaptive_fps_var.get())
        settings.predictive_smoothing = bool(self.predictive_smoothing_var.get())
        settings.bulb_layout = self._current_screen_layout()
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
        # Dark Scene Floor only feeds the follow-mode brightness curve; in
        # constant mode dimming is pinned by the Constant Brightness slider
        # and the floor has no effect, so disable it rather than leave a
        # control that silently does nothing.
        self.sync_brightness_slider.configure(state="normal" if mode == "constant" else "disabled")
        self.min_brightness_slider.configure(state="normal" if mode == "follow" else "disabled")

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

    def _sync_mode_hint_text(self) -> str:
        if self.sync_mode_labels.get(self.sync_mode_value.get(), "single") == "zones":
            return "Assign each bulb Left or Right below. Needs at least 2 bulbs in different zones."
        return "One ambient color drives every configured bulb."

    def _on_sync_mode_change(self, _value=None):
        self.sync_mode_hint.configure(text=self._sync_mode_hint_text())
        self._queue_screen_sync_settings_save()

    def _mixing_model_hint_text(self) -> str:
        if self.mixing_model_labels.get(self.mixing_model_value.get(), "heuristic") == "physical":
            return (
                "Solves the bulb's real LED colors instead of a fixed formula. "
                "Not proven better -- try it live and compare, then switch back if you prefer Standard."
            )
        return "The proven default color mapping."

    def _on_mixing_model_change(self, _value=None):
        self.mixing_model_hint.configure(text=self._mixing_model_hint_text())
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
        swatch = "#000000" if color is None else f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}"
        label = "RGB(-)" if color is None else self._format_debug_color(color)
        if panel._swatch.cget("fg_color") != swatch:
            panel._swatch.configure(fg_color=swatch)
        if panel._label_var.get() != label:
            panel._label_var.set(label)

    def _set_metric(self, key: str, value: str):
        variable = getattr(self, "_sync_metric_vars", {}).get(key)
        if variable is not None and variable.get() != value:
            variable.set(value)

    def _set_sync_running_ui(self, running: bool):
        if getattr(self, "_displayed_sync_running", None) == running:
            return
        if hasattr(self, "sync_primary_button"):
            self.sync_primary_button.configure(
                text="Stop Screen Sync" if running else "Start Screen Sync",
                fg_color="#B91C1C" if running else ("#3B8ED0", "#1F6AA5"),
            )
        self._displayed_sync_running = running
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
        if output is not None:
            self._update_sync_preview(self.screen_sync, output)

        status = snapshot.get("capture_status") or "OK"
        # A dark scene and an inaccessible surface can both produce black.
        if snapshot.get("capture_looks_black"):
            status = (
                "Capture looks black — the scene may be dark, "
                "or this content may not be available for capture"
            )
        status_part = f" | {status}" if status != "OK" else ""
        status_display = "Running" if status == "OK" else status
        if getattr(self, "_live_details_open", True):
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
        indicator_color = self._sync_status_color(status)
        if self.status_indicator.cget("fg_color") != indicator_color:
            self.status_indicator.configure(fg_color=indicator_color)
        if not self.advanced_debug_var.get():
            self._screen_sync_debug_id = self.root.after(350, self._refresh_screen_sync_debug)
            return
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
            preview_color = f"#{output[0]:02x}{output[1]:02x}{output[2]:02x}"
            if self.screen_sync_debug_preview.cget("fg_color") != preview_color:
                self.screen_sync_debug_preview.configure(fg_color=preview_color)

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
                    self.config.add_bulb(bulb["ip"], f"Bulb {index}", bulb["mac"], preserve_name=True)
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
        if self._closing or (self.screen_sync and self.screen_sync.is_running):
            return
        if getattr(self, "_bulb_recovery_token", None) is not None:
            return
        if getattr(self, "_sync_start_future", None) is not None:
            return
        configured_ips = self._get_bulb_ips()
        self._save_screen_sync_settings()
        settings = copy.deepcopy(self.config.screen_sync)
        bulbs = copy.deepcopy(tuple(self.config.bulbs))

        async def prepare():
            ips = await asyncio.wait_for(refresh_verified_targets(self.controller, bulbs), 6.0)
            warning = None
            if ips:
                try:
                    await asyncio.wait_for(prepare_screen_sync_mapping(self.controller, ips, settings), 8.0)
                except Exception as exc:
                    warning = str(exc) or type(exc).__name__
            return ips, warning

        try:
            future = self._async_runner.submit(prepare())
        except Exception as exc:
            self.screen_sync_switch.deselect()
            self._set_sync_running_ui(False)
            self._set_status(f"Sync preflight failed: {exc}")
            return
        self._sync_start_future = future
        self._set_status("Preparing screen sync...")
        self._sync_start_poll_id = self.root.after(
            50, lambda: self._poll_sync_start(future, configured_ips, settings)
        )

    def _poll_sync_start(self, future, configured_ips, settings):
        self._sync_start_poll_id = None
        if self._closing or getattr(self, "_sync_start_future", None) is not future:
            return
        if not future.done():
            self._sync_start_poll_id = self.root.after(
                50, lambda: self._poll_sync_start(future, configured_ips, settings)
            )
            return
        self._sync_start_future = None
        if not self.screen_sync_switch.get():
            return
        try:
            ips, warning = future.result()
        except Exception as exc:
            self.screen_sync_switch.deselect()
            self._set_sync_running_ui(False)
            self._set_status(f"Sync preflight failed: {exc}")
            return
        if not ips:
            if any(mac_identity(bulb.mac) for bulb in self.config.bulbs):
                self._recover_sync_bulbs()
                return
            self.screen_sync_switch.deselect()
            self._set_sync_running_ui(False)
            self.config.screen_sync.enabled = False
            self.config.save()
            self._set_status("No reachable bulbs available for screen sync")
            return
        self._finish_sync_start(ips, configured_ips, settings, warning)

    def _finish_sync_start(self, ips, configured_ips, settings, warning=None):
        # Zones need >=2 bulbs actually assigned to distinct regions; otherwise
        # fall back to single-bulb ambient (effective_screen_sync_mode enforces
        # this so a half-configured zone layout doesn't strand a bulb with no
        # target region).
        active_regions = resolve_active_regions(settings.bulb_layout, ips)
        mode = effective_screen_sync_mode(settings.mode, active_regions)
        bulb_layout = dict(settings.bulb_layout) if mode == "zones" else {}
        engine_holder = {}
        transport = None

        def on_color_change(colors_by_target):
            engine = engine_holder.get("engine")
            if self._closing or self.screen_sync is not engine or transport is None:
                return
            bulb_colors = build_bulb_color_map(ips, colors_by_target, mode, bulb_layout)
            if bulb_colors:
                # Brightness rides the setPilot dimming field so hue is always
                # rendered at full LED drive (colors are value-normalized by
                # the engine); read it at send time from the engine.
                brightness = engine.output_brightness if engine else None
                transport.send(bulb_colors, brightness)

        try:
            transport = ScreenSyncTransport(
                self.controller,
                self._async_runner.submit,
                FastBulbSenderPool(
                    max_send_hz=min(28, max(12, settings.max_fps)), prewarm_ips=ips,
                ),
            )
            self._screen_sync_transport = transport
            self.screen_sync = OptimizedScreenSync(
                on_color_change=on_color_change,
                config=build_optimized_capture_config(settings, active_regions),
            )
            engine_holder["engine"] = self.screen_sync
            self.screen_sync.start()
        except Exception as exc:
            self._stop_screen_sync(update_status=False)
            self.screen_sync_switch.deselect()
            self.config.screen_sync.enabled = False
            self.config.save()
            self._set_status(f"Sync startup failed: {exc}")
            return
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
            + (f" | profile setup incomplete: {warning}" if warning else "")
        )
        missing = set(configured_ips) - set(ips)
        self._sync_active_ips = set(ips)
        self._sync_missing_ips = set(missing)
        if missing:
            self._recover_sync_bulbs(background=True, target_ips=missing)
        else:
            self._schedule_bulb_recovery_monitor()

    def _recover_sync_bulbs(self, *, background=False, target_ips=None):
        """Search in the background and verify matching saved devices."""
        token = object()
        self._bulb_recovery_token = token
        bulbs = tuple(self.config.bulbs)
        engine = self.screen_sync if background else None
        if not background:
            self._set_status("Searching for saved lights on the local network...")

        async def recover():
            try:
                result = await recover_saved_bulbs(self.controller, bulbs, target_ips=target_ips)
                message = None
            except Exception as exc:
                result, message = [], str(exc)
            if not self._closing:
                self.root.after(0, lambda: self._finish_bulb_recovery(token, result, message, engine))

        self._run_async(recover())

    def _finish_bulb_recovery(self, token, recovered, error, engine=None):
        if self._closing or getattr(self, "_bulb_recovery_token", None) is not token:
            return
        self._bulb_recovery_token = None
        if not self.screen_sync_switch.get():
            return
        if engine is not None and self.screen_sync is not engine:
            return
        moved = 0
        for item in recovered:
            existing = next((bulb for bulb in self.config.bulbs if bulb.ip == item.old_ip
                             and mac_identity(bulb.mac) == mac_identity(item.mac)), None)
            if existing is None or any(bulb.ip == item.new_ip for bulb in self.config.bulbs):
                continue
            self.config.add_bulb(item.new_ip, existing.name, existing.mac)
            moved += 1
        if moved:
            self._update_bulb_status()
            if engine is not None:
                self._stop_screen_sync(update_status=False)
            self._start_screen_sync()
            return
        if engine is not None:
            self._schedule_bulb_recovery_monitor()
            return
        self.screen_sync_switch.deselect()
        self._set_sync_running_ui(False)
        self.config.screen_sync.enabled = False
        self.config.save()
        self._set_status(f"Light search failed: {error}" if error else "No reachable saved lights found")

    def _schedule_bulb_recovery_monitor(self):
        if self._closing or not self.screen_sync or not self.screen_sync.is_running:
            return
        if getattr(self, "_bulb_recovery_monitor_id", None) is None:
            self._bulb_recovery_monitor_id = self.root.after(30000, self._poll_sync_bulbs)

    def _poll_sync_bulbs(self):
        self._bulb_recovery_monitor_id = None
        engine = self.screen_sync
        if self._closing or not engine or not engine.is_running:
            return
        bulbs = tuple(self.config.bulbs)

        async def poll():
            results = await asyncio.gather(
                *(fetch_fresh_identity(self.controller, bulb.ip) for bulb in bulbs),
                return_exceptions=True,
            )
            missing = {
                bulb.ip for bulb, result in zip(bulbs, results)
                if mac_identity(bulb.mac) and (
                    not isinstance(result, str) or result != mac_identity(bulb.mac)
                )
            }
            if not self._closing:
                self.root.after(0, lambda: self._finish_sync_bulb_poll(engine, missing))

        self._run_async(poll())

    def _finish_sync_bulb_poll(self, engine, missing):
        if self._closing or self.screen_sync is not engine or not engine.is_running:
            return
        previous_missing = getattr(self, "_sync_missing_ips", set())
        returned = previous_missing - missing
        self._sync_missing_ips = set(missing)
        if returned - getattr(self, "_sync_active_ips", set()):
            self._stop_screen_sync(update_status=False)
            self._start_screen_sync()
            return
        if returned:
            # A reconnected light may have reverted to its own saved scene.
            # Force a fresh update even when the screen colour has not changed.
            with engine._stats_lock:
                engine._last_sent_colors.clear()
        if missing and getattr(self, "_bulb_recovery_token", None) is None:
            self._recover_sync_bulbs(background=True, target_ips=missing)
        else:
            self._schedule_bulb_recovery_monitor()

    def _update_sync_preview(self, engine, preview):
        if not self._closing and self.screen_sync is engine:
            color = f"#{preview[0]:02x}{preview[1]:02x}{preview[2]:02x}"
            if self.color_preview.cget("fg_color") != color:
                self.color_preview.configure(fg_color=color)

    def _stop_screen_sync(self, update_status: bool = True):
        self._bulb_recovery_token = None
        future = getattr(self, "_sync_start_future", None)
        self._sync_start_future = None
        if future is not None:
            future.cancel()
        poll_id = getattr(self, "_sync_start_poll_id", None)
        if poll_id is not None:
            self.root.after_cancel(poll_id)
            self._sync_start_poll_id = None
        monitor = getattr(self, "_bulb_recovery_monitor_id", None)
        if monitor is not None:
            self.root.after_cancel(monitor)
            self._bulb_recovery_monitor_id = None
        engine, self.screen_sync = self.screen_sync, None
        transport, self._screen_sync_transport = self._screen_sync_transport, None
        try:
            try:
                if engine:
                    engine.stop()
            finally:
                if transport:
                    transport.close()
        finally:
            if self._screen_sync_debug_id:
                self.root.after_cancel(self._screen_sync_debug_id)
                self._screen_sync_debug_id = None
        if self._closing:
            return
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
        if ((self.screen_sync and self.screen_sync.is_running)
                or getattr(self, "_sync_start_future", None) is not None
                or getattr(self, "_bulb_recovery_token", None) is not None):
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
            try:
                self.clap_detector.start()
            except Exception as exc:
                self.clap_detector = None
                self.clap_switch.deselect()
                self.config.clap.enabled = False
                self.config.save()
                self._set_status(f"Microphone could not start: {exc}")
                return
            mode = "double clap" if self.config.clap.double_clap else "single clap"
            self._set_status(f"Clap detection ON ({mode})")
        else:
            if self.clap_detector:
                self.clap_detector.stop()
                self.clap_detector = None
            self._set_status("Clap detection OFF")
    
    # === View Switching ===
    
    def _scroll_to_sync_settings(self):
        self._switch_view("sync")
        self.root.update_idletasks()
        canvas = self._controls_frame._parent_canvas
        bounds = canvas.bbox("all")
        if bounds and bounds[3] > 0:
            canvas.yview_moveto(self._sync_settings_card.winfo_y() / bounds[3])
            self._controls_frame._queue_canvas_repaint()

    def _switch_view(self, view: str):
        """Switch between primary app sections."""
        if view == self._current_view:
            return

        for frame in self._view_frames.values():
            frame.pack_forget()
        self._current_view = view
        title, subtitle = self._page_labels[view]
        self._page_title_var.set(title)
        self._page_subtitle_var.set(subtitle)

        for name, button in self._view_buttons.items():
            active = name == view
            button.configure(
                fg_color="#244D7B" if active else "transparent",
                text_color="#FFFFFF" if active else "#A5B7CF",
                border_width=0,
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
            self._dashboard.start_auto_refresh(interval_ms=5000)
            self._dashboard.set_bulbs(bulbs)
    
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
        if self._closing:
            return
        # If minimized to tray, just hide
        if self._tray and self._tray.is_running and self._minimized_to_tray:
            return
        self._closing = True
        
        # Stop tray
        if self._tray:
            self._tray.stop()
        
        # Stop dashboard refresh
        if self._dashboard:
            self._dashboard.stop_auto_refresh()
        
        try:
            self._stop_screen_sync(update_status=False)
        except Exception as exc:
            print(f"Sync cleanup failed: {exc}")
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
            try:
                self._async_runner.shutdown()
            finally:
                self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    app = WizLightModernGUI()
    app.run()


if __name__ == "__main__":
    main()
