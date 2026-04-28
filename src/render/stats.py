import io
import json
import os
import datetime
import numpy as np
from PIL import Image
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
from scipy.interpolate import make_interp_spline

from pathlib import Path

from src.db.stats import list_stats
from src.common.utils import ROOT, load_env_file

matplotlib.use('Agg')
load_env_file()

try:
    fallback_fonts_json = os.environ.get('DEFAULT_FALLBACK_FONTS', '[]')
    font_names = json.loads(fallback_fonts_json)
except Exception:
    font_names = []

fonts_dir = ROOT / "fonts"
if fonts_dir.exists():
    for ext in ("*.ttf", "*.otf", "*.ttc"):
        for font_path in fonts_dir.rglob(ext):
            try:
                fm.fontManager.addfont(str(font_path))
            except Exception:
                continue

plt.rcParams['font.sans-serif'] = font_names
plt.rcParams['axes.unicode_minus'] = False

def _base_render(times: list[datetime.datetime], values: list[int], label: str, color: str, title: str) -> Image.Image | None:
    # 过滤无效数据 (-1)
    clean_t = []
    clean_v = []
    for t, v in zip(times, values):
        if v == -1:
            continue
        if not clean_t or t > clean_t[-1]:
            clean_t.append(t)
            clean_v.append(v)

    if len(clean_t) < 2:
        return None

    fig, ax = plt.subplots(figsize=(10, 4))
    x = mdates.date2num(clean_t)
    y = np.array(clean_v)

    # 动态计算 Y 轴的合理上下限
    y_min, y_max = y.min(), y.max()
    y_range = y_max - y_min
    
    # 设定一个“最小视觉跨度”，防止变化只有 1 的时候被无限放大
    # 如果最大值和最小值的差不到 10，我们就强行按照 10 的跨度来画，让微小的变化看起来更平稳
    eff_range = max(y_range, 10)
    
    # 上下各留出 20% 的视觉呼吸空间
    padding = eff_range * 0.2
    y_bottom = max(0, y_min - padding)  # 数据底部不能穿透 0
    y_top = y_max + padding

    # 平滑插值与绘图
    if len(x) > 3:
        x_smooth = np.linspace(x.min(), x.max(), 300)
        k = min(3, len(x) - 1)
        try:
            spl = make_interp_spline(x, y, k=k)
            y_smooth = spl(x_smooth)
            ax.plot(x_smooth, y_smooth, color=color, linewidth=2)
            # 填充到底部 limit，而不是随意的 0.99
            ax.fill_between(x_smooth, y_smooth, y_bottom, color=color, alpha=0.1)
        except:
            ax.plot(x, y, color=color, linewidth=2)
            ax.fill_between(x, y, y_bottom, color=color, alpha=0.1)
    else:
        ax.plot(x, y, color=color, linewidth=2)
        ax.fill_between(x, y, y_bottom, color=color, alpha=0.1)

    ax.set_ylim(y_bottom, y_top)
    ax.yaxis.set_major_locator(plt.MaxNLocator(integer=True))

    ax.set_title(title, fontsize=14)
    ax.grid(True, linestyle=':', alpha=0.6)

    ax.set_xlim(x.min(), x.max())

    uniform_ticks = np.linspace(x.min(), x.max(), 5)
    ax.set_xticks(uniform_ticks)

    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))

    ticks = ax.xaxis.get_major_ticks()
    if ticks:
        ticks[0].label1.set_horizontalalignment('left')
        ticks[-1].label1.set_horizontalalignment('right')
    
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=120)
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")

async def render_fans_trend(room_id: int, days: int, room_name: str) -> tuple[Image.Image, int, int] | None:
    data = list_stats(room_id, days)
    if not data:
        return None
    times = [datetime.datetime.strptime(d['created_at'], "%Y-%m-%d %H:%M:%S") for d in data]
    values = [d['fans_num'] for d in data]
    return _base_render(times, values, "粉丝数", "#E54D4D", f"{room_name} 粉丝数趋势 (近{days}天)"), values[0], values[-1]

async def render_guards_trend(room_id: int, days: int, room_name: str) -> tuple[Image.Image, int, int] | None:
    data = list_stats(room_id, days)
    if not data:
        return None
    times = [datetime.datetime.strptime(d['created_at'], "%Y-%m-%d %H:%M:%S") for d in data]
    values = [d['guard_num'] for d in data]
    return _base_render(times, values, "舰长数", "#E2B52B", f"{room_name} 大航海数趋势 (近{days}天)"), values[0], values[-1]

async def render_fan_club_trend(room_id: int, days: int, room_name: str) -> tuple[Image.Image, int, int] | None:
    data = list_stats(room_id, days)
    if not data:
        return None
    times = [datetime.datetime.strptime(d['created_at'], "%Y-%m-%d %H:%M:%S") for d in data]
    values = [d['fan_club_num'] for d in data]
    return _base_render(times, values, "粉丝团", "#427D9E", f"{room_name} 粉丝团人数趋势 (近{days}天)"), values[0], values[-1]