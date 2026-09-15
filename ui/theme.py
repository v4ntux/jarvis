"""Design tokens for Jarvis 1.0 Beta.

The runtime UI consumes semantic tokens from this module instead of hard-coded
colors and measurements.  The visual philosophy is "Quiet Instrument": compact,
precise and mostly still until the assistant has been explicitly activated.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    bg_primary: str
    bg_secondary: str
    bg_tertiary: str
    bg_inverse: str
    text_primary: str
    text_secondary: str
    text_tertiary: str
    text_inverse: str
    text_link: str
    border_primary: str
    border_secondary: str
    border_focus: str
    accent_primary: str
    accent_hover: str
    accent_active: str
    accent_secondary: str
    success: str
    warning: str
    error: str
    info: str
    overlay: str


LIGHT = Palette(
    bg_primary="#f4f6f8",
    bg_secondary="#ffffff",
    bg_tertiary="#e9eef2",
    bg_inverse="#101419",
    text_primary="#111820",
    text_secondary="#4f5d6b",
    text_tertiary="#778492",
    text_inverse="#f5f8fa",
    text_link="#006f9e",
    border_primary="#cdd6de",
    border_secondary="#e2e8ed",
    border_focus="#008fbd",
    accent_primary="#007fa8",
    accent_hover="#006e93",
    accent_active="#005c7c",
    accent_secondary="#d9f3fa",
    success="#12805c",
    warning="#9a6400",
    error="#bd3a46",
    info="#2d6cdf",
    overlay="#111820",
)

DARK = Palette(
    bg_primary="#0b0f14",
    bg_secondary="#121820",
    bg_tertiary="#1a222c",
    bg_inverse="#f2f6f8",
    text_primary="#eef3f6",
    text_secondary="#a8b5c1",
    text_tertiary="#74818d",
    text_inverse="#10151a",
    text_link="#63d7f3",
    border_primary="#2a3541",
    border_secondary="#202a34",
    border_focus="#42c8e8",
    accent_primary="#43c7e6",
    accent_hover="#68d5ef",
    accent_active="#26aacb",
    accent_secondary="#153844",
    success="#42d39b",
    warning="#f0b95b",
    error="#ff6f7d",
    info="#79a6ff",
    overlay="#020407",
)

PALETTES = {"light": LIGHT, "dark": DARK}

SPACING = {
    "0": 0,
    "1": 2,
    "2": 4,
    "3": 6,
    "4": 8,
    "5": 12,
    "6": 16,
    "7": 24,
    "8": 32,
    "9": 48,
    "10": 64,
    "11": 96,
    "12": 128,
}

TYPE = {
    "family_display": "Segoe UI Variable Display",
    "family_body": "Segoe UI Variable Text",
    "family_fallback": "Segoe UI",
    "family_mono": "Cascadia Mono",
    "xs": 8,
    "sm": 9,
    "base": 10,
    "md": 11,
    "lg": 13,
    "xl": 16,
    "2xl": 20,
    "3xl": 26,
    "4xl": 32,
    "normal": "normal",
    "medium": "normal",
    "semibold": "bold",
    "bold": "bold",
}

RADIUS = {"sm": 6, "md": 10, "lg": 14, "xl": 18, "full": 999}

MOTION = {
    "instant": 50,
    "fast": 150,
    "normal": 250,
    "slow": 400,
    "slower": 600,
    "idle_tick": 750,
    "active_tick": 70,
}

BREAKPOINTS = {"compact": 760, "sidebar": 900, "wide": 1280, "ultrawide": 1536}

STATE_COLORS = {
    "idle": DARK.text_tertiary,
    "paused": DARK.warning,
    "starting": DARK.text_tertiary,
    "checking": DARK.info,
    "listening": DARK.accent_primary,
    "hearing": DARK.accent_hover,
    "thinking": DARK.info,
    "speaking": DARK.success,
    "asking": DARK.warning,
    "dictating": DARK.accent_primary,
    "reminder": DARK.warning,
    "error": DARK.error,
}


def palette(name="dark"):
    """Return a semantic palette, defaulting safely to dark mode."""
    return PALETTES.get((name or "dark").lower(), DARK)


def font(size="base", weight="normal", mono=False):
    """Return a Tk font tuple using installed Windows system families."""
    family = TYPE["family_mono"] if mono else TYPE["family_fallback"]
    return family, TYPE.get(size, TYPE["base"]), TYPE.get(weight, "normal")
