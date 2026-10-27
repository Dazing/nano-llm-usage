"""The design tokens are a contract: one dark palette, complete, and readable.

Parses `frontend/src/styles/tokens.css` (the single colour source, Delivery 3 D8,
dark-only since the HUD plan D1) and checks that the one `:root` block defines every
token the UI consumes and that the text and series pairs clear WCAG contrast on the
surfaces they are drawn over. A token renamed or dropped, a second theme block, or a
foreground darkened below AA fails here, long before the browser.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TOKENS = Path(__file__).resolve().parent.parent / "frontend" / "src" / "styles" / "tokens.css"

COLOR_TOKENS = (
    "--bg",
    "--panel",
    "--panel-2",
    "--sunk",
    "--fg",
    "--fg-muted",
    "--border",
    "--line",
    "--accent",
    "--series-input",
    "--series-output",
    "--warning",
    "--error",
    "--cyan",
    "--magenta",
    "--green",
    "--teal",
    "--ok",
    "--caution",
    "--cost",
)
SPACING_TOKENS = ("--sp-1", "--sp-2", "--sp-3", "--sp-4", "--sp-5", "--sp-6")

TEXT_PAIRS = (
    ("--fg", "--bg"),
    ("--fg", "--panel"),
    ("--fg", "--sunk"),
    ("--fg-muted", "--bg"),
    ("--fg-muted", "--panel"),
    ("--fg-muted", "--sunk"),
    ("--warning", "--panel"),
    ("--error", "--panel"),
    ("--ok", "--panel"),
    ("--cost", "--panel"),
    ("--cyan", "--panel"),
)
SERIES_PAIRS = (
    ("--series-input", "--panel"),
    ("--series-output", "--panel"),
    ("--series-input", "--sunk"),
    ("--series-output", "--sunk"),
)


@pytest.fixture(scope="module")
def css() -> str:
    assert TOKENS.is_file(), f"missing token source: {TOKENS}"
    return TOKENS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tokens(css: str) -> dict[str, str]:
    match = re.search(r":root\s*\{(.*?)\}", css, re.DOTALL)
    assert match, "tokens.css defines no `:root` block"
    return dict(
        (name, value.split("/*")[0].strip())
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", match.group(1))
    )


def rgb(value: str) -> tuple[float, float, float]:
    match = re.fullmatch(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})", value.strip())
    assert match, f"{value!r} is not a 3- or 6-digit hex colour"
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return tuple(int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def luminance(color: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(tokens: dict[str, str], foreground: str, background: str) -> float:
    light_value = luminance(rgb(tokens[foreground]))
    dark_value = luminance(rgb(tokens[background]))
    higher, lower = max(light_value, dark_value), min(light_value, dark_value)
    return (higher + 0.05) / (lower + 0.05)


def test_the_palette_is_dark_and_unique(css, tokens):
    assert "color-scheme: dark" in css, "the HUD palette declares color-scheme: dark"
    assert "[data-theme" not in css, "there is one palette: no theme selector in tokens.css"
    assert len(re.findall(r":root\s*\{", css)) == 1, "tokens.css defines exactly one :root block"
    assert tokens["--bg"] != tokens["--panel"], "the page ground and the panel are distinct"


def test_every_colour_token_is_defined(tokens):
    missing = [name for name in COLOR_TOKENS if name not in tokens]
    assert not missing, f"the palette is missing {missing}"


def test_the_mono_font_and_chamfer_are_defined(tokens):
    assert "monospace" in tokens.get("--font-mono", ""), "--font-mono must be a mono stack"
    assert re.fullmatch(r"\d+px", tokens.get("--cut", "")), "--cut must be a px chamfer"


def test_spacing_scale_is_defined(tokens):
    missing = [name for name in SPACING_TOKENS if not re.fullmatch(r"\d+px", tokens.get(name, ""))]
    assert not missing, f"spacing tokens missing or not in px: {missing}"


@pytest.mark.parametrize("foreground,background", TEXT_PAIRS)
def test_text_contrast(tokens, foreground, background):
    ratio = contrast(tokens, foreground, background)
    assert ratio >= 4.5, f"{foreground} on {background} is {ratio:.2f}, needs 4.5"


@pytest.mark.parametrize("foreground,background", SERIES_PAIRS)
def test_series_contrast(tokens, foreground, background):
    ratio = contrast(tokens, foreground, background)
    assert ratio >= 3.0, f"{foreground} on {background} is {ratio:.2f}, needs 3.0"


def test_the_two_series_are_distinguishable(tokens):
    assert tokens["--series-input"] != tokens["--series-output"]


def test_no_raw_hex_colours_outside_the_token_source():
    styles = TOKENS.parent
    offenders = []
    for sheet in sorted(styles.glob("*.css")):
        if sheet == TOKENS:
            continue
        for number, line in enumerate(sheet.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"#[0-9a-fA-F]{3,6}\b", line):
                offenders.append(f"{sheet.name}:{number}: {line.strip()}")
    assert not offenders, "components must use tokens, not raw hex: " + "; ".join(offenders)
