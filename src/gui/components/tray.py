"""System tray integration for WizLight."""

import threading
from pathlib import Path
from typing import Callable, Optional

try:
    import pystray
    from PIL import Image
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False


def create_default_icon(size: int = 64, color: tuple = (59, 142, 208)) -> "Image.Image":
    """Create a simple bulb icon for the tray."""
    from PIL import Image, ImageDraw
    
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    
    # Bulb body (circle)
    margin = size // 8
    bulb_top = margin
    bulb_bottom = int(size * 0.65)
    draw.ellipse(
        [margin, bulb_top, size - margin, bulb_bottom],
        fill=color,
        outline=(255, 255, 255, 200),
        width=2
    )
    
    # Bulb base (small rectangle)
    base_width = size // 3
    base_left = (size - base_width) // 2
    base_top = bulb_bottom - 4
    base_bottom = int(size * 0.85)
    draw.rectangle(
        [base_left, base_top, base_left + base_width, base_bottom],
        fill=(80, 80, 80),
        outline=(60, 60, 60),
        width=1
    )
    
    # Screw threads (lines)
    for i in range(3):
        y = base_top + 4 + i * 4
        if y < base_bottom - 2:
            draw.line(
                [base_left + 2, y, base_left + base_width - 2, y],
                fill=(100, 100, 100),
                width=1
            )
    
    return image


class SystemTrayManager:
    """
    Manages system tray icon and menu for WizLight.
    
    Features:
    - Quick on/off control
    - Preset submenu
    - Show/hide main window
    - Exit application
    """
    
    def __init__(
        self,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        on_toggle: Callable[[], None],
        on_toggle_screen_sync: Callable[[], None],
        on_toggle_clap: Callable[[], None],
        on_brightness: Callable[[int], None],
        on_color_temp: Callable[[int], None],
        on_monitor: Callable[[int], None],
        on_manage_monitors: Callable[[], None],
        on_preset: Callable[[str], None],
        presets: list[str],
        monitors: list[dict[str, int | str]],
        icon_path: Optional[Path] = None,
    ):
        """
        Initialize system tray manager.
        
        Args:
            on_show: Callback to show main window
            on_quit: Callback to quit application
            on_toggle: Callback to toggle lights
            on_turn_on: Callback to turn lights on
            on_turn_off: Callback to turn lights off
            on_preset: Callback to apply preset (receives preset name)
            presets: List of available preset names
            icon_path: Optional path to custom icon image
        """
        if not TRAY_AVAILABLE:
            raise ImportError("pystray and Pillow are required for system tray support")
        
        self.on_show = on_show
        self.on_quit = on_quit
        self.on_toggle = on_toggle
        self.on_toggle_screen_sync = on_toggle_screen_sync
        self.on_toggle_clap = on_toggle_clap
        self.on_brightness = on_brightness
        self.on_color_temp = on_color_temp
        self.on_monitor = on_monitor
        self.on_manage_monitors = on_manage_monitors
        self.on_preset = on_preset
        self.presets = presets
        self.monitors = monitors
        
        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        
        # Load or create icon
        if icon_path and icon_path.exists():
            self._image = Image.open(icon_path)
        else:
            self._image = create_default_icon()
    
    def _create_menu(self) -> "pystray.Menu":
        """Create the tray menu."""
        def preset_action(name: str):
            return lambda _icon, _item: self.on_preset(name)

        def monitor_action(index: int):
            return lambda _icon, _item: self.on_monitor(index)

        # Preset submenu items
        preset_items = [
            pystray.MenuItem(
                name.title(),
                preset_action(name),
            )
            for name in self.presets
        ]

        monitor_items = [
            pystray.MenuItem(
                str(monitor["label"]),
                monitor_action(int(monitor["index"])),
            )
            for monitor in self.monitors
        ]
        has_extended_display = len(self.monitors) > 1
        
        return pystray.Menu(
            pystray.MenuItem("Show WizLight", self._on_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Toggle Lights", self._on_toggle),
            pystray.MenuItem("Screen Sync", self._on_toggle_screen_sync),
            pystray.MenuItem("Clap Detection", self._on_toggle_clap),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Brightness", pystray.Menu(
                pystray.MenuItem("30%", lambda _icon, _item: self.on_brightness(77)),
                pystray.MenuItem("60%", lambda _icon, _item: self.on_brightness(153)),
                pystray.MenuItem("100%", lambda _icon, _item: self.on_brightness(255)),
            )),
            pystray.MenuItem("Color Temperature", pystray.Menu(
                pystray.MenuItem("Warm 2700K", lambda _icon, _item: self.on_color_temp(2700)),
                pystray.MenuItem("Neutral 4000K", lambda _icon, _item: self.on_color_temp(4000)),
                pystray.MenuItem("Cool 6500K", lambda _icon, _item: self.on_color_temp(6500)),
            )),
            pystray.MenuItem(
                "Monitor",
                pystray.Menu(*monitor_items) if monitor_items else pystray.Menu(),
                enabled=has_extended_display,
            ),
            pystray.MenuItem("Manage Monitors", self._on_manage_monitors, enabled=has_extended_display),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Presets", pystray.Menu(*preset_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._on_quit),
        )
    
    def _on_show(self, icon, item):
        """Handle show menu item."""
        self.on_show()
    
    def _on_quit(self, icon, item):
        """Handle quit menu item."""
        self.stop()
        self.on_quit()
    
    def _on_toggle(self, icon, item):
        """Handle toggle menu item."""
        self.on_toggle()
    
    def _on_toggle_screen_sync(self, icon, item):
        """Handle screen sync menu item."""
        self.on_toggle_screen_sync()

    def _on_toggle_clap(self, icon, item):
        """Handle clap detection menu item."""
        self.on_toggle_clap()

    def _on_manage_monitors(self, icon, item):
        """Handle monitor manager menu item."""
        self.on_manage_monitors()
    
    def start(self):
        """Start the system tray icon."""
        if self._running:
            return
        
        self._running = True
        self._icon = pystray.Icon(
            "WizLight",
            self._image,
            "WizLight",
            self._create_menu()
        )
        
        # Run in background thread
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Stop the system tray icon."""
        self._running = False
        if self._icon:
            self._icon.stop()
            self._icon = None
    
    def update_icon(self, color: Optional[tuple[int, int, int]] = None):
        """Update the tray icon color to reflect bulb state."""
        if not self._icon:
            return
        self._image = create_default_icon(color=color) if color else create_default_icon()
        self._icon.icon = self._image

    def show_notification(self, title: str, message: str):
        """Show a system notification."""
        if self._icon:
            self._icon.notify(message, title)
    
    @property
    def is_running(self) -> bool:
        return self._running


def is_tray_available() -> bool:
    """Check if system tray support is available."""
    return TRAY_AVAILABLE
