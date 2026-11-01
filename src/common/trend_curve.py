"""保留采样点与区间单调性的趋势曲线。"""

import numpy as np
from scipy.interpolate import PchipInterpolator


def smooth_trend(x, y):
    """使用一阶连续的保形插值，防止陡变处过冲及虚构极值。"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < 3:
        return x, y
    # 纳入全部原始节点，均匀绘图网格也能保留窄峰和不规则采样。
    samples = np.unique(np.concatenate((x, np.linspace(x[0], x[-1], 300))))
    return samples, PchipInterpolator(x, y, extrapolate=False)(samples)
