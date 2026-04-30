"""Compatibility fixes for CustomTkinter runtime quirks."""

from __future__ import annotations

import tkinter


def install_customtkinter_dropdown_destroy_patch() -> None:
    """Ensure CTk option-menu dropdowns unregister DPI-scaling callbacks."""

    try:
        from customtkinter.windows.widgets.appearance_mode.appearance_mode_base_class import (
            CTkAppearanceModeBaseClass,
        )
        from customtkinter.windows.widgets.core_widget_classes.dropdown_menu import DropdownMenu
        from customtkinter.windows.widgets.ctk_optionmenu import CTkOptionMenu
        from customtkinter.windows.widgets.font import CTkFont
        from customtkinter.windows.widgets.scaling.scaling_base_class import CTkScalingBaseClass
    except Exception:
        return

    if not getattr(DropdownMenu.destroy, "_wizlight_safe_destroy", False):
        original_dropdown_destroy = DropdownMenu.destroy

        def safe_dropdown_destroy(self):
            if getattr(self, "_wizlight_destroyed", False):
                return
            self._wizlight_destroyed = True

            try:
                font = getattr(self, "_font", None)
                update_font = getattr(self, "_update_font", None)
                if isinstance(font, CTkFont) and update_font is not None:
                    font.remove_size_configure_callback(update_font)
            except Exception:
                pass

            try:
                tkinter.Menu.destroy(self)
            except tkinter.TclError:
                pass

            try:
                CTkAppearanceModeBaseClass.destroy(self)
            except Exception:
                pass

            try:
                CTkScalingBaseClass.destroy(self)
            except Exception:
                pass

        safe_dropdown_destroy._wizlight_safe_destroy = True
        safe_dropdown_destroy._wizlight_original_destroy = original_dropdown_destroy
        DropdownMenu.destroy = safe_dropdown_destroy

    if not getattr(CTkOptionMenu.destroy, "_wizlight_safe_destroy", False):
        original_option_destroy = CTkOptionMenu.destroy

        def safe_option_destroy(self):
            dropdown = getattr(self, "_dropdown_menu", None)
            if dropdown is not None:
                try:
                    dropdown.destroy()
                except Exception:
                    pass
                self._dropdown_menu = None
            original_option_destroy(self)

        safe_option_destroy._wizlight_safe_destroy = True
        safe_option_destroy._wizlight_original_destroy = original_option_destroy
        CTkOptionMenu.destroy = safe_option_destroy

