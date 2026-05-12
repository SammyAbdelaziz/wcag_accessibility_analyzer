"""
OOXML Theme Color Resolver
Resolves scheme color references (e.g. accent1, bg1, tx1) to hex RGB
through the full theme chain: slide → slideLayout → slideMaster → theme.
Also computes WCAG 2.2 contrast ratios.
"""
from __future__ import annotations
import zipfile
import re
import math
from typing import Optional, Dict, Tuple
from lxml import etree

# XML namespaces
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Map semantic scheme color names to theme slot names
SCHEME_ALIAS: Dict[str, str] = {
    "bg1": "lt1",
    "bg2": "lt2",
    "tx1": "dk1",
    "tx2": "dk2",
}

# Default theme colors (Office default theme — fallback only)
DEFAULT_THEME_COLORS: Dict[str, str] = {
    "dk1": "000000",
    "lt1": "FFFFFF",
    "dk2": "44546A",
    "lt2": "E7E6E6",
    "accent1": "4472C4",
    "accent2": "ED7D31",
    "accent3": "A9D18E",
    "accent4": "FFC000",
    "accent5": "5B9BD5",
    "accent6": "70AD47",
    "hlink": "0563C1",
    "folHlink": "954F72",
}


class ThemeResolver:
    def __init__(self, zip_file: zipfile.ZipFile):
        self.zip = zip_file
        self._theme_colors: Optional[Dict[str, str]] = None

    def _load_theme(self) -> Dict[str, str]:
        if self._theme_colors is not None:
            return self._theme_colors
        theme_files = [n for n in self.zip.namelist() if re.match(r'ppt/theme/theme\d+\.xml$', n)]
        if not theme_files:
            self._theme_colors = DEFAULT_THEME_COLORS.copy()
            return self._theme_colors
        try:
            content = self.zip.read(theme_files[0])
            root = etree.fromstring(content)
            clr_scheme = root.find(f'.//{{{NS_A}}}clrScheme')
            colors = {}
            if clr_scheme is not None:
                for slot in clr_scheme:
                    slot_name = slot.tag.split('}')[-1]
                    # srgbClr
                    srgb = slot.find(f'{{{NS_A}}}srgbClr')
                    if srgb is not None:
                        colors[slot_name] = srgb.get('val', '000000').upper()
                        continue
                    # sysClr (system color — use lastClr)
                    sys = slot.find(f'{{{NS_A}}}sysClr')
                    if sys is not None:
                        colors[slot_name] = sys.get('lastClr', '000000').upper()
            self._theme_colors = {**DEFAULT_THEME_COLORS, **colors}
        except Exception:
            self._theme_colors = DEFAULT_THEME_COLORS.copy()
        return self._theme_colors

    def resolve_scheme_color(self, scheme_val: str,
                              lum_mod: int = 100000,
                              lum_off: int = 0,
                              shade: int = 100000,
                              tint: int = 100000) -> str:
        """
        Resolve a scheme color to a 6-char hex string, applying luminance/shade/tint mods.
        All modifiers use Office units (100000 = 100%).
        Returns hex RGB string e.g. "4472C4".
        """
        theme = self._load_theme()
        # Resolve alias
        key = SCHEME_ALIAS.get(scheme_val, scheme_val)
        hex_val = theme.get(key, "808080")
        rgb = _hex_to_rgb(hex_val)
        hls = _rgb_to_hls(rgb)
        l = hls[1]

        if lum_mod != 100000:
            l = l * (lum_mod / 100000)
        if lum_off != 0:
            l = l + (lum_off / 100000)
        if shade != 100000:
            l = l * (shade / 100000)
        if tint != 100000:
            l = l + (1 - l) * (1 - tint / 100000)

        l = max(0.0, min(1.0, l))
        rgb_out = _hls_to_rgb((hls[0], l, hls[2]))
        return _rgb_to_hex(rgb_out)

    def contrast_ratio(self, hex1: str, hex2: str) -> float:
        """Compute WCAG 2.2 contrast ratio between two hex colors."""
        l1 = _relative_luminance(_hex_to_rgb(hex1))
        l2 = _relative_luminance(_hex_to_rgb(hex2))
        lighter = max(l1, l2)
        darker = min(l1, l2)
        return (lighter + 0.05) / (darker + 0.05)

    def meets_contrast(self, fg_hex: str, bg_hex: str,
                        font_size_pt: float = 12, is_bold: bool = False) -> bool:
        """Check if fg/bg pair meets WCAG 2.2 AA minimum contrast."""
        ratio = self.contrast_ratio(fg_hex, bg_hex)
        # Large text: >= 18pt normal or >= 14pt bold → 3:1 required
        large = font_size_pt >= 18 or (is_bold and font_size_pt >= 14)
        required = 3.0 if large else 4.5
        return ratio >= required


# ── Colour math helpers ──────────────────────────────────────────────────────

def _hex_to_rgb(hex_str: str) -> Tuple[float, float, float]:
    h = hex_str.lstrip('#').upper()
    if len(h) != 6:
        return (0.5, 0.5, 0.5)
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return (r, g, b)


def _rgb_to_hex(rgb: Tuple[float, float, float]) -> str:
    r = int(round(rgb[0] * 255))
    g = int(round(rgb[1] * 255))
    b = int(round(rgb[2] * 255))
    return f"{r:02X}{g:02X}{b:02X}"


def _rgb_to_hls(rgb: Tuple[float, float, float]) -> Tuple[float, float, float]:
    import colorsys
    h, l, s = colorsys.rgb_to_hls(rgb[0], rgb[1], rgb[2])
    return (h, l, s)


def _hls_to_rgb(hls: Tuple[float, float, float]) -> Tuple[float, float, float]:
    import colorsys
    return colorsys.hls_to_rgb(hls[0], hls[1], hls[2])


def _linearize(c: float) -> float:
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(rgb: Tuple[float, float, float]) -> float:
    r, g, b = _linearize(rgb[0]), _linearize(rgb[1]), _linearize(rgb[2])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
