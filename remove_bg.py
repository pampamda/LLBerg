#!/usr/bin/env python3
"""
remove_bg.py — 批量背景去除

自己用 ffmpeg 抽完帧后，用这个脚本批量抠图，保存为透明 PNG。

用法:
    python remove_bg.py <输入文件夹> <输出文件夹> [模式]

模式:
    white      （默认）纯色/白色背景，速度快
    texture    白色背景 + 保留白色光线/发光效果（背景平坦、光线有纹理时用）
    proximity  白色背景 + 保留连接深色物体之间的光线（UFO 牵引光束等）★推荐
    grabcut    花哨/多色块背景，用 GrabCut 图割算法

示例:
    python remove_bg.py frames/chill    assets/sprites/chill
    python remove_bg.py frames/easter   assets/sprites/easter proximity
    python remove_bg.py frames/walk     assets/sprites/walk grabcut

输出文件命名：{前缀}_c_01.png, {前缀}_c_02.png …
依赖: pip install opencv-python pillow
"""

import sys
import cv2
import numpy as np
from pathlib import Path
from PIL import Image

# ── 通用可调参数 ──────────────────────────────────────────────────────────────

CLOSE_KSIZE   = 9     # 形态学闭运算核（填补前景内部小洞）
OPEN_KSIZE    = 5     # 形态学开运算核（去除边缘毛刺噪点）
MIN_BLOB_SIZE = 300   # 连通域最小面积，小于此的碎片直接删除
FEATHER       = 1     # 边缘羽化半径（像素）；0=完全锐利，1-2=轻微抗锯齿，5=明显模糊

# ── white 模式参数 ────────────────────────────────────────────────────────────

WHITE_S_MAX = 70      # 饱和度上限：低于此视为白/灰色
WHITE_V_MIN = 200     # 明度下限：高于此视为白色

# ── texture 模式参数 ──────────────────────────────────────────────────────────

VAR_WIN = 21    # 局部方差窗口大小（越大越平滑，建议 15-31）
VAR_MAX = 120   # 方差上限：低于此视为"平坦背景"，高于此视为"有纹理的前景"
                # 光线/发光区域方差通常 > 150，平坦背景 < 30，调小更严格

# ── proximity 模式参数 ────────────────────────────────────────────────────────

PROX_DARK_S_MIN = 30   # 饱和度下限：高于此视为"有色/深色前景"（猫毛、UFO 金属）
PROX_DARK_V_MAX = 180  # 明度上限：低于此视为"深色前景"
PROX_DIST       = 120  # 保护距离（像素）：距深色内容 ≤ 此值的亮色区域一律保留
                        # 光束越长、距离越大，调大此值即可（如 150、200）

# ── grabcut 模式参数 ──────────────────────────────────────────────────────────

GC_CORNER   = 0.04   # 四角小块作为"确定背景"的比例（4%），不用整圈边缘
GC_ITERS    = 8      # GrabCut 迭代次数，越大越精细越慢

IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


# ── 形态学后处理（两种模式共用） ──────────────────────────────────────────────

def _clean_mask(fg_mask: np.ndarray) -> np.ndarray:
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (CLOSE_KSIZE, CLOSE_KSIZE))
    fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, k_close)

    k_open  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (OPEN_KSIZE, OPEN_KSIZE))
    fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, k_open)

    # 连通域过滤：删除面积过小的碎片
    n, labels, stats, _ = cv2.connectedComponentsWithStats(fg_mask, connectivity=8)
    clean = np.zeros_like(fg_mask)
    for lbl in range(1, n):
        if stats[lbl, cv2.CC_STAT_AREA] >= MIN_BLOB_SIZE:
            clean[labels == lbl] = 255

    # 边缘羽化
    ksize = FEATHER * 2 + 1
    return cv2.GaussianBlur(clean, (ksize, ksize), 0)


def _to_rgba(img_bgr: np.ndarray, fg_mask: np.ndarray) -> Image.Image:
    rgb  = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    rgba = np.dstack([rgb, fg_mask])
    return Image.fromarray(rgba, "RGBA")


# ── white 模式 ────────────────────────────────────────────────────────────────

def remove_white(img_bgr: np.ndarray) -> Image.Image:
    """
    HSV 低饱和度检测 + 全边缘 Flood Fill。
    只删与图像边缘连通的白色区域，猫体内部白色毛发不受影响。
    """
    h, w = img_bgr.shape[:2]
    hsv  = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    s_ch = hsv[:, :, 1]
    v_ch = hsv[:, :, 2]

    white_candidate = ((s_ch < WHITE_S_MAX) & (v_ch > WHITE_V_MIN)).astype(np.uint8) * 255

    tmp     = white_candidate.copy()
    ff_mask = np.zeros((h + 2, w + 2), np.uint8)

    seeds  = []
    seeds += [(0,   int(x)) for x in np.where(white_candidate[0,   :] == 255)[0]]
    seeds += [(h-1, int(x)) for x in np.where(white_candidate[h-1, :] == 255)[0]]
    seeds += [(int(y),   0) for y in np.where(white_candidate[:,   0] == 255)[0]]
    seeds += [(int(y), w-1) for y in np.where(white_candidate[:, w-1] == 255)[0]]

    for (sy, sx) in seeds:
        if tmp[sy, sx] == 255:
            cv2.floodFill(tmp, ff_mask, (sx, sy), 128)

    bg_mask = (tmp == 128).astype(np.uint8) * 255
    fg_mask = cv2.bitwise_not(bg_mask)
    return _to_rgba(img_bgr, _clean_mask(fg_mask))


# ── texture 模式 ──────────────────────────────────────────────────────────────

def remove_texture(img_bgr: np.ndarray) -> Image.Image:
    """
    纹理感知白色背景去除。

    核心思路：
      平坦背景  = 白/灰色 + 局部方差极低（均匀无纹理）→ 标为背景
      白色光线  = 白/灰色 + 局部方差较高（有射线/粒子/渐变结构）→ 保留为前景

    适合：UFO 光线、发光效果、烟雾等"白色但有纹理"的前景元素。
    """
    h, w = img_bgr.shape[:2]
    hsv  = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    s_ch = hsv[:, :, 1]
    v_ch = hsv[:, :, 2]

    # 局部方差：E[x²] - E[x]²
    gray_f    = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mean_     = cv2.blur(gray_f,       (VAR_WIN, VAR_WIN))
    mean_sq_  = cv2.blur(gray_f ** 2,  (VAR_WIN, VAR_WIN))
    local_var = np.maximum(0.0, mean_sq_ - mean_ ** 2)

    # 背景候选 = 白/灰 AND 平坦（方差低）
    white_candidate = (
        (s_ch < WHITE_S_MAX) &
        (v_ch > WHITE_V_MIN) &
        (local_var < VAR_MAX)
    ).astype(np.uint8) * 255

    # 边缘全扫描 flood fill（同 white 模式）
    tmp     = white_candidate.copy()
    ff_mask = np.zeros((h + 2, w + 2), np.uint8)

    seeds  = []
    seeds += [(0,   int(x)) for x in np.where(white_candidate[0,   :] == 255)[0]]
    seeds += [(h-1, int(x)) for x in np.where(white_candidate[h-1, :] == 255)[0]]
    seeds += [(int(y),   0) for y in np.where(white_candidate[:,   0] == 255)[0]]
    seeds += [(int(y), w-1) for y in np.where(white_candidate[:, w-1] == 255)[0]]

    for (sy, sx) in seeds:
        if tmp[sy, sx] == 255:
            cv2.floodFill(tmp, ff_mask, (sx, sy), 128)

    bg_mask = (tmp == 128).astype(np.uint8) * 255
    fg_mask = cv2.bitwise_not(bg_mask)
    return _to_rgba(img_bgr, _clean_mask(fg_mask))


# ── proximity 模式 ───────────────────────────────────────────────────────────

def remove_proximity(img_bgr: np.ndarray) -> Image.Image:
    """
    近邻感知白色背景去除。

    核心思路：
      先定位图中"深色前景"（猫毛、UFO 金属等暗色/有色像素），
      然后计算每个像素到最近深色前景像素的距离。
      只有同时满足以下两条的像素才被标为背景：
        1. 颜色偏白/灰（低饱和度、高明度）
        2. 距最近深色像素 > PROX_DIST（即不在光束保护区内）

      光束夹在 UFO 和猫之间，两端紧靠深色物体，永远在保护区内 → 永远保留。

    适合：UFO 牵引光束、角色间连接的光线/烟雾等"连接两个深色物体的亮色元素"。
    """
    h, w = img_bgr.shape[:2]
    hsv  = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    s_ch = hsv[:, :, 1]
    v_ch = hsv[:, :, 2]

    # 1. 深色前景掩码：饱和度足够 OR 明度足够低（猫/UFO 主体）
    dark_mask = ((s_ch >= PROX_DARK_S_MIN) | (v_ch <= PROX_DARK_V_MAX)).astype(np.uint8) * 255

    # 2. 距离变换：非深色区域中每个像素到最近深色像素的欧氏距离
    non_dark       = cv2.bitwise_not(dark_mask)
    dist_from_dark = cv2.distanceTransform(non_dark, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)

    # 3. 背景候选 = 亮色 AND 离深色内容足够远
    light_candidate = ((s_ch < WHITE_S_MAX) & (v_ch > WHITE_V_MIN)).astype(np.uint8) * 255
    far_from_dark   = (dist_from_dark > PROX_DIST).astype(np.uint8) * 255
    bg_candidate    = cv2.bitwise_and(light_candidate, far_from_dark)

    # 4. 边缘 flood fill（只删与边缘连通的背景区域）
    tmp     = bg_candidate.copy()
    ff_mask = np.zeros((h + 2, w + 2), np.uint8)

    seeds  = []
    seeds += [(0,   int(x)) for x in np.where(bg_candidate[0,   :] == 255)[0]]
    seeds += [(h-1, int(x)) for x in np.where(bg_candidate[h-1, :] == 255)[0]]
    seeds += [(int(y),   0) for y in np.where(bg_candidate[:,   0] == 255)[0]]
    seeds += [(int(y), w-1) for y in np.where(bg_candidate[:, w-1] == 255)[0]]

    for (sy, sx) in seeds:
        if tmp[sy, sx] == 255:
            cv2.floodFill(tmp, ff_mask, (sx, sy), 128)

    bg_mask = (tmp == 128).astype(np.uint8) * 255
    fg_mask = cv2.bitwise_not(bg_mask)
    return _to_rgba(img_bgr, _clean_mask(fg_mask))


# ── grabcut 模式 ──────────────────────────────────────────────────────────────

def remove_grabcut(img_bgr: np.ndarray) -> Image.Image:
    """
    GrabCut 图割分割，适合花哨/多色块背景。

    初始化策略：
    - 只用四个角落小块标为"确定背景"，不用整圈边缘
    - 这样靠近边缘的白色光线等前景元素不会被误删
    """
    h, w  = img_bgr.shape[:2]
    c     = max(5, int(min(h, w) * GC_CORNER))   # 角落边长（像素）

    # 初始化：默认全图为"可能前景"，仅四角标为"确定背景"
    gc_mask = np.full((h, w), cv2.GC_PR_FGD, dtype=np.uint8)
    gc_mask[:c,   :c]    = cv2.GC_BGD   # 左上角
    gc_mask[:c,   w-c:]  = cv2.GC_BGD   # 右上角
    gc_mask[h-c:, :c]    = cv2.GC_BGD   # 左下角
    gc_mask[h-c:, w-c:]  = cv2.GC_BGD   # 右下角

    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)

    cv2.grabCut(img_bgr, gc_mask, None,
                bgd_model, fgd_model,
                GC_ITERS, cv2.GC_INIT_WITH_MASK)

    fg_mask = np.where(
        (gc_mask == cv2.GC_FGD) | (gc_mask == cv2.GC_PR_FGD), 255, 0
    ).astype(np.uint8)

    return _to_rgba(img_bgr, _clean_mask(fg_mask))


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(0)

    src_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    mode    = sys.argv[3].lower() if len(sys.argv) > 3 else "white"

    if mode not in ("white", "texture", "proximity", "grabcut"):
        print(f"[错误] 模式必须是 white、texture、proximity 或 grabcut，收到: {mode}")
        sys.exit(1)

    if not src_dir.exists():
        print(f"[错误] 输入文件夹不存在: {src_dir}")
        sys.exit(1)

    imgs = sorted(p for p in src_dir.iterdir() if p.suffix.lower() in IMG_EXTS)
    if not imgs:
        print(f"[错误] {src_dir} 中没有图片文件")
        sys.exit(1)

    first_parts = imgs[0].stem.split("_")
    prefix = "_".join(first_parts[:-1]) if len(first_parts) > 1 else first_parts[0]

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"模式: {mode}")
    print(f"输入: {src_dir}  ({len(imgs)} 张图片)")
    print(f"输出: {out_dir}  命名: {prefix}_c_%02d.png\n")

    if mode == "white":
        remove_fn = remove_white
    elif mode == "texture":
        remove_fn = remove_texture
    elif mode == "proximity":
        remove_fn = remove_proximity
    else:
        remove_fn = remove_grabcut

    for idx, img_path in enumerate(imgs, start=1):
        bgr = cv2.imread(str(img_path))
        if bgr is None:
            print(f"  [跳过] 读取失败: {img_path.name}")
            continue

        result   = remove_fn(bgr)
        out_path = out_dir / f"{prefix}_c_{idx:02d}.png"
        result.save(out_path)
        print(f"  [{idx:02d}/{len(imgs)}] {img_path.name} → {out_path.name}")

    print(f"\n完成！已保存到 {out_dir}")
    print("接着运行:  python normalize_sprites.py  统一猫的显示大小")


if __name__ == "__main__":
    main()
