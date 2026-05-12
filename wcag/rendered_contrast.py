from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

RGBA_PATTERN = re.compile(r"rgba?\(([^)]+)\)", re.IGNORECASE)


def parse_css_rgba(value: str) -> Optional[Tuple[float, float, float, float]]:
    if not value:
        return None
    match = RGBA_PATTERN.search(value.strip())
    if not match:
        return None
    parts = [part.strip() for part in match.group(1).split(",")]
    if len(parts) < 3:
        return None
    try:
        red = float(parts[0])
        green = float(parts[1])
        blue = float(parts[2])
        alpha = float(parts[3]) if len(parts) >= 4 else 1.0
        return red, green, blue, max(0.0, min(alpha, 1.0))
    except ValueError:
        return None


def blend_rgba_over_background(
    foreground: Tuple[float, float, float, float],
    background: Tuple[float, float, float],
) -> Tuple[float, float, float]:
    red, green, blue, alpha = foreground
    bg_red, bg_green, bg_blue = background
    return (
        red * alpha + bg_red * (1.0 - alpha),
        green * alpha + bg_green * (1.0 - alpha),
        blue * alpha + bg_blue * (1.0 - alpha),
    )


def _linearize_channel(channel_255: float) -> float:
    channel = channel_255 / 255.0
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: Tuple[float, float, float]) -> float:
    red, green, blue = rgb
    return (
        0.2126 * _linearize_channel(red)
        + 0.7152 * _linearize_channel(green)
        + 0.0722 * _linearize_channel(blue)
    )


def contrast_ratio_rgb(
    foreground: Tuple[float, float, float],
    background: Tuple[float, float, float],
) -> float:
    l1 = relative_luminance(foreground)
    l2 = relative_luminance(background)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def extract_background_candidates(
    background_color: str,
    background_image: str,
    fallback: Tuple[float, float, float] = (255.0, 255.0, 255.0),
) -> List[Tuple[float, float, float]]:
    candidates: List[Tuple[float, float, float]] = []

    color_rgba = parse_css_rgba(background_color or "")
    if color_rgba and color_rgba[3] > 0:
        candidates.append(blend_rgba_over_background(color_rgba, fallback))

    gradient_rgba_values = [
        parse_css_rgba(match.group(0))
        for match in RGBA_PATTERN.finditer(background_image or "")
    ]
    for rgba in gradient_rgba_values:
        if rgba and rgba[3] > 0:
            candidates.append(blend_rgba_over_background(rgba, fallback))

    if not candidates:
        candidates.append(fallback)

    deduped: List[Tuple[float, float, float]] = []
    for candidate in candidates:
        normalized = tuple(round(channel, 2) for channel in candidate)
        if normalized not in deduped:
            deduped.append(normalized)
    return deduped


def minimum_css_contrast(
    foreground_color: str,
    background_color: str,
    background_image: str,
) -> Optional[float]:
    foreground_rgba = parse_css_rgba(foreground_color or "")
    if not foreground_rgba:
        return None

    candidates = extract_background_candidates(background_color, background_image)
    ratios = [
        contrast_ratio_rgb(blend_rgba_over_background(foreground_rgba, background), background)
        for background in candidates
    ]
    return min(ratios) if ratios else None


def estimate_bbox_contrast(image, bbox: Sequence[int]) -> Optional[float]:
    if image is None:
        return None

    left, top, right, bottom = [int(value) for value in bbox]
    if right <= left or bottom <= top:
        return None

    width, height = image.size
    left = max(0, min(left, width - 1))
    top = max(0, min(top, height - 1))
    right = max(left + 1, min(right, width))
    bottom = max(top + 1, min(bottom, height))
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    pixel_access = crop.load()
    pixels = [pixel_access[x, y] for y in range(crop.height) for x in range(crop.width)]
    if len(pixels) < 25:
        return None

    luminances = [relative_luminance((float(red), float(green), float(blue))) for red, green, blue in pixels]
    sorted_luminances = sorted(luminances)
    # OCR line crops are mostly background pixels, so broad 20/80 percentiles
    # collapse on the background and miss faint text. Use tighter tails instead.
    low_index = max(0, int(len(sorted_luminances) * 0.05) - 1)
    high_index = min(len(sorted_luminances) - 1, int(len(sorted_luminances) * 0.95))
    low_cutoff = sorted_luminances[low_index]
    high_cutoff = sorted_luminances[high_index]

    if high_cutoff - low_cutoff < 0.08:
        return None

    dark_pixels = [pixel for pixel, lum in zip(pixels, luminances) if lum <= low_cutoff]
    light_pixels = [pixel for pixel, lum in zip(pixels, luminances) if lum >= high_cutoff]
    if not dark_pixels or not light_pixels:
        return None

    def average_rgb(group):
        return (
            sum(pixel[0] for pixel in group) / len(group),
            sum(pixel[1] for pixel in group) / len(group),
            sum(pixel[2] for pixel in group) / len(group),
        )

    dark_rgb = average_rgb(dark_pixels)
    light_rgb = average_rgb(light_pixels)
    return contrast_ratio_rgb(dark_rgb, light_rgb)