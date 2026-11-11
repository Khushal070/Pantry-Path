"""Static PNG charts for the Markdown reports (search, bias, sensitivity).

One look everywhere: the reference categorical palette in fixed order (slots 1-4 pass the
dataviz validator on the light surface; their contrast is low, so every bar carries a
value label and every chart sits next to a Markdown table), thin marks, hairline grid,
one axis, and a legend whenever there are two or more series.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402 - the backend must be set first
from matplotlib.ticker import PercentFormatter  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6",
          "#e34948"]
SURFACE, INK, INK_2, MUTED, GRID, AXIS = ("#fcfcfb", "#0b0b0b", "#52514e", "#898781",
                                          "#e1e0d9", "#c3c2b7")


def _style(ax: plt.Axes, title: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=11, pad=10)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def bar_chart(path: Path, title: str, labels: list[str], series: dict[str, list[float | None]],
              *, xlabel: str = "", xmax: float | None = 1.0, fmt: str = "{:.0%}") -> Path:
    """Horizontal grouped bars: one row per label, one bar per series, value at the end."""
    if len(series) > len(SERIES):
        raise ValueError(f"at most {len(SERIES)} series per chart; split it instead")
    n = len(series)
    height = 0.7 / n
    fig, ax = plt.subplots(figsize=(8, max(2.0, 0.42 * len(labels) * max(n, 1) + 1.2)),
                           facecolor=SURFACE)
    _style(ax, title)
    ax.grid(axis="y", visible=False)
    for i, (name, values) in enumerate(series.items()):
        ys = [row + (i - (n - 1) / 2) * height for row in range(len(labels))]
        xs = [v or 0 for v in values]
        ax.barh(ys, xs, height=height * 0.7, color=SERIES[i], label=name)
        for y, v in zip(ys, values, strict=True):
            ax.text((v or 0), y, " " + ("n/a" if v is None else fmt.format(v)), va="center",
                    ha="left", color=INK_2, fontsize=8)
    ax.set_yticks(range(len(labels)), labels)
    ax.invert_yaxis()
    if xmax is not None:
        ax.set_xlim(0, xmax * 1.12)
    if fmt.endswith("%}"):
        ax.xaxis.set_major_formatter(PercentFormatter(1.0))
    ax.set_xlabel(xlabel, color=MUTED, fontsize=9)
    if n >= 2:
        ax.legend(frameon=False, fontsize=8, loc="lower right", labelcolor=INK_2)
    return _save(fig, path)


def line_chart(path: Path, title: str, xs: list[float], series: dict[str, list[float | None]],
               *, xlabel: str, ylabel: str, ymax: float | None = 1.0) -> Path:
    """One line per series over a numeric x (a setting such as temperature)."""
    if len(series) > len(SERIES):
        raise ValueError(f"at most {len(SERIES)} series per chart; split it instead")
    fig, ax = plt.subplots(figsize=(7, 3.6), facecolor=SURFACE)
    _style(ax, title)
    for i, (name, ys) in enumerate(series.items()):
        points = [(x, y) for x, y in zip(xs, ys, strict=True) if y is not None]
        if points:
            ax.plot(*zip(*points, strict=True), color=SERIES[i], linewidth=2, marker="o",
                    markersize=6, markeredgecolor=SURFACE, markeredgewidth=1.5, label=name)
    ax.set_xticks(xs)
    if ymax is not None:
        ax.set_ylim(0, ymax * 1.05)
        ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.set_xlabel(xlabel, color=MUTED, fontsize=9)
    ax.set_ylabel(ylabel, color=MUTED, fontsize=9)
    if len(series) >= 2:
        ax.legend(frameon=False, fontsize=8, labelcolor=INK_2)
    return _save(fig, path)


def _save(fig: plt.Figure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return path


def pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.1%}"


def num(v: float | None, unit: str = "") -> str:
    return "n/a" if v is None else f"{v:.3g}{unit}"


def markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    """Cells are shown as given (format them with pct/num first); None shows as n/a."""
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join("n/a" if v is None else str(v) for v in row) + " |"
              for row in rows]
    return "\n".join(lines) + "\n"
