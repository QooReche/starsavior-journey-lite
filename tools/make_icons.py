# -*- coding: utf-8 -*-
"""여정 내비게이터 아이콘 시안 3종 (Pillow, 4배 슈퍼샘플링)"""
import math
import os
import sys

from PIL import Image, ImageDraw

S = 4                      # 슈퍼샘플 배율
N = 256                    # 기준 크기
NAVY1, NAVY2 = (10, 22, 38), (22, 52, 82)
CYAN = (25, 227, 255)
WHITE = (240, 252, 255)


def bg(size):
    """둥근 사각형 + 세로 그라데이션 바탕"""
    w = size * S
    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    grad = Image.new("RGBA", (w, w))
    gd = ImageDraw.Draw(grad)
    for y in range(w):
        t = y / (w - 1)
        gd.line([(0, y), (w, y)], fill=tuple(int(NAVY1[i] + (NAVY2[i] - NAVY1[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (w, w), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, w - 1], radius=int(w * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def star(d, cx, cy, r, fill, inner=0.30):
    pts = []
    for k in range(8):
        ang = math.radians(-90 + k * 45)
        rr = r if k % 2 == 0 else r * inner
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    d.polygon(pts, fill=fill)


def pin(d, cx, cy, r, fill):
    """위치 핀: 원 + 아래로 뾰족한 꼬리"""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
    d.polygon([(cx - r * 0.82, cy + r * 0.55), (cx + r * 0.82, cy + r * 0.55), (cx, cy + r * 2.0)], fill=fill)


def variant_a(size=N):
    """A: 청록 위치 핀 안에 남색 4각 별"""
    img = bg(size)
    d = ImageDraw.Draw(img)
    w = size * S
    pin(d, w * 0.5, w * 0.42, w * 0.25, CYAN)
    star(d, w * 0.5, w * 0.42, w * 0.15, NAVY1, 0.32)
    return img


def variant_b(size=N):
    """B: 나침반 고리 + 4각 별 바늘"""
    img = bg(size)
    d = ImageDraw.Draw(img)
    w = size * S
    c = w * 0.5
    d.ellipse([c - w * 0.33, c - w * 0.33, c + w * 0.33, c + w * 0.33], outline=CYAN, width=int(w * 0.035))
    for k in range(4):                                         # 동서남북 눈금
        a = math.radians(k * 90)
        d.line([(c + w * 0.33 * math.cos(a), c + w * 0.33 * math.sin(a)),
                (c + w * 0.27 * math.cos(a), c + w * 0.27 * math.sin(a))], fill=CYAN, width=int(w * 0.035))
    star(d, c, c, w * 0.24, CYAN, 0.26)
    d.ellipse([c - w * 0.035, c - w * 0.035, c + w * 0.035, c + w * 0.035], fill=NAVY1)
    return img


def variant_c(size=N):
    """C: 왼쪽 위 별에서 점선 경로가 오른쪽 아래 핀까지 (길 안내)"""
    img = bg(size)
    d = ImageDraw.Draw(img)
    w = size * S
    star(d, w * 0.30, w * 0.30, w * 0.17, WHITE, 0.30)
    # 점선 곡선 경로
    pts = []
    for i in range(0, 41):
        t = i / 40
        x = w * (0.30 + 0.40 * t)
        y = w * (0.30 + 0.34 * t) + math.sin(t * math.pi) * w * 0.16
        pts.append((x, y))
    for k, (x, y) in enumerate(pts):
        if k % 4 == 1 and 4 < k < 34:
            r = w * 0.018
            d.ellipse([x - r, y - r, x + r, y + r], fill=CYAN)
    pin(d, w * 0.70, w * 0.62, w * 0.12, CYAN)
    d.ellipse([w * 0.70 - w * 0.045, w * 0.62 - w * 0.045, w * 0.70 + w * 0.045, w * 0.62 + w * 0.045], fill=NAVY1)
    return img


def small(fn, px):
    return fn(N).resize((px, px), Image.LANCZOS)


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    fns = [("A", variant_a), ("B", variant_b), ("C", variant_c)]
    sheet = Image.new("RGB", (3 * 300 + 20, 560), (32, 33, 36))
    light = Image.new("RGB", (3 * 300 + 20, 150), (238, 240, 243))      # 밝은 작업표시줄에서의 모습
    for i, (name, fn) in enumerate(fns):
        big = fn(N).resize((N, N), Image.LANCZOS)
        big.save(os.path.join(out_dir, f"icon_{name}_256.png"))
        x = 20 + i * 300
        sheet.paste(big, (x, 20), big)
        for j, px in enumerate((64, 32, 24, 16)):
            s = small(fn, px)
            sheet.paste(s, (x + [0, 80, 130, 170][j], 300), s)
            light.paste(s, (x + [0, 80, 130, 170][j], 40), s)
    sheet.paste(light, (0, 400))
    sheet.save(os.path.join(out_dir, "icon_sheet.png"))
    print("ok")


if __name__ == "__main__":
    main(sys.argv[1])


def export_variant_a(assets_dir):
    """A안을 앱 리소스로 내보낸다: assets/icon.ico(exe용, 여러 크기), assets/icon_<px>.png(Qt 창/트레이용)"""
    os.makedirs(assets_dir, exist_ok=True)
    sizes = (16, 24, 32, 48, 64, 128, 256)
    frames = []
    for px in sizes:
        im = variant_a(256).resize((px, px), Image.LANCZOS) if px < 256 else variant_a(256).resize((256, 256), Image.LANCZOS)
        im.save(os.path.join(assets_dir, f"icon_{px}.png"))
        frames.append(im)
    frames[-1].save(os.path.join(assets_dir, "icon.ico"), format="ICO", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    print("exported", assets_dir)
