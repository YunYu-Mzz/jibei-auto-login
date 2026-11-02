#!/usr/bin/env python3
"""
本地滑块验证（index.html 内嵌实例）的自动识别：Playwright 控制浏览器 + OpenCV 找缺口

流程：导出两个 canvas 的像素 → 在拼图块所在的横条里做模板匹配 → 用鼠标把滑块拖过去
每轮同时计算两种识别方法，便于对比：
  gray  灰度模板匹配：只比较拼图内部的纹理（真缺口是原图压暗 50%，归一化相关不受亮度影响）
  edge  边缘匹配：拼图轮廓 vs 背景 Canny 边缘（经典教程做法）

用法（先在项目目录运行 python3 -m http.server 8765）：
  .venv/bin/python solver/solve.py                          # 有界面，跑 1 轮
  .venv/bin/python solver/solve.py --rounds 50 --headless   # 批量测试
  .venv/bin/python solver/solve.py --method edge --decoys 5
  .venv/bin/python solver/solve.py --duration 2             # 拖得更慢，方便演示
  .venv/bin/python solver/solve.py --drag smooth            # 不用拟人轨迹，回到匀滑直线
  .venv/bin/python solver/solve.py --seed 7                 # 固定随机种子，拟人轨迹可复现
"""
import argparse
import base64
import csv
import math
import random
import time
from pathlib import Path

import cv2
import numpy as np
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ROOT = '#embed .sc-root'
COLORS = {'gray': (60, 180, 40), 'edge': (200, 50, 200)}  # BGR

GRAB_JS = """
root => {
  const bg = root.querySelector('.sc-bg');
  const piece = root.querySelector('.sc-piece');
  const panel = root.querySelector('.sc-panel').getBoundingClientRect();
  const p = piece.getBoundingClientRect();
  return {
    bg: bg.toDataURL('image/png'),
    piece: piece.toDataURL('image/png'),
    scale: bg.width / bg.getBoundingClientRect().width,
    left: p.left - panel.left,
    top: p.top - panel.top,
  };
}
"""


def decode(data_url):
    raw = base64.b64decode(data_url.split(',', 1)[1])
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)


# ---------- 识别 ----------

def detect(bg_bgra, piece_bgra, top, scale):
    """返回 {方法: 识别结果}，坐标单位是 canvas 像素（= CSS 像素 × scale）"""
    bg = cv2.cvtColor(bg_bgra, cv2.COLOR_BGRA2GRAY)
    tpl = cv2.cvtColor(piece_bgra, cv2.COLOR_BGRA2GRAY)
    h, w = tpl.shape

    # 拼图形状来自透明通道；往里腐蚀几个像素，去掉白色描边和阴影
    shape = np.where(piece_bgra[:, :, 3] >= 250, 255, 0).astype(np.uint8)
    inner = cv2.erode(shape, np.ones((3, 3), np.uint8), iterations=max(1, round(2 * scale)))

    # 只在拼图块那一行上下留几像素的横条里搜
    margin = round(3 * scale)
    y0 = max(0, round(top * scale) - margin)
    y1 = min(bg.shape[0], round(top * scale) + h + margin)
    band = bg[y0:y1]

    scores = {
        'gray': cv2.matchTemplate(band, tpl, cv2.TM_CCOEFF_NORMED, mask=inner),
        'edge': cv2.matchTemplate(
            cv2.Canny(cv2.GaussianBlur(band, (3, 3), 0), 80, 200),
            cv2.Canny(shape, 50, 150),
            cv2.TM_CCOEFF_NORMED,
        ),
    }

    result = {}
    for name, res in scores.items():
        res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
        curve = res.max(axis=0)  # 每个 x 取最佳的 y
        x = int(curve.argmax())
        # 峰值优势：最高峰减去离它半个拼图以外的次高峰，越大说明越不容易被干扰块骗
        rest = curve.copy()
        rest[max(0, x - w // 2): x + w // 2 + 1] = -1
        result[name] = {
            'x': x,
            'y': y0 + int(res[:, x].argmax()),
            'score': float(curve[x]),
            'margin': float(curve[x] - rest.max()),
            'curve': curve,
        }
    return result, {'inner': inner, 'edge_tpl': cv2.Canny(shape, 50, 150), 'band': (y0, y1)}


# ---------- 标注图 ----------

def fit(img, width):
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:  # 透明处垫白底
        a = img[:, :, 3:] / 255.0
        img = (img[:, :, :3] * a + 255 * (1 - a)).astype(np.uint8)
    h = round(img.shape[0] * width / img.shape[1])
    return cv2.resize(img, (width, h), interpolation=cv2.INTER_NEAREST)


def annotate(bg_bgra, piece_bgra, det, extra, path, title):
    img = cv2.cvtColor(bg_bgra, cv2.COLOR_BGRA2BGR)
    H, W = img.shape[:2]
    h, w = piece_bgra.shape[:2]

    # 搜索横条 + 两种方法的识别框
    y0, y1 = extra['band']
    shade = img.copy()
    cv2.rectangle(shade, (0, y0), (W - 1, y1 - 1), (0, 215, 255), -1)
    img = cv2.addWeighted(shade, 0.18, img, 0.82, 0)
    for yy in (y0, y1 - 1):
        cv2.line(img, (0, yy), (W - 1, yy), (0, 165, 255), 1)
    for name, d in det.items():
        cv2.rectangle(img, (d['x'], d['y']), (d['x'] + w - 1, d['y'] + h - 1), COLORS[name], 2)

    # 分数曲线：横轴与上图对齐到拼图中心
    ph = 130
    plot = np.full((ph, W, 3), 255, np.uint8)
    for v in (0.0, 0.5, 1.0):
        gy = int(ph - 12 - (v + 0.2) / 1.2 * (ph - 24))
        cv2.line(plot, (0, gy), (W - 1, gy), (225, 225, 225), 1)
        cv2.putText(plot, f'{v:.1f}', (2, gy - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (150, 150, 150), 1, cv2.LINE_AA)
    for i, (name, d) in enumerate(det.items()):
        c = np.clip(d['curve'], -0.2, 1.0)
        ys = (ph - 12 - (c + 0.2) / 1.2 * (ph - 24)).astype(np.int32)
        pts = np.stack([np.arange(len(c)) + w // 2, ys], axis=1)
        cv2.polylines(plot, [pts], False, COLORS[name], 1, cv2.LINE_AA)
        cv2.circle(plot, (d['x'] + w // 2, int(ys[d['x']])), 3, COLORS[name], -1)
        label = f"{name}: x={d['x']} score={d['score']:.2f} margin={d['margin']:.2f}"
        cv2.putText(plot, label, (W - 230, 14 + i * 14), cv2.FONT_HERSHEY_SIMPLEX, 0.35, COLORS[name], 1, cv2.LINE_AA)
    left = np.vstack([img, plot])

    # 右侧：拼图块 / 内部掩膜 / 轮廓模板
    side_w = 70
    tiles = [fit(piece_bgra, side_w), fit(extra['inner'], side_w), fit(extra['edge_tpl'], side_w)]
    side = np.full((left.shape[0], side_w + 12, 3), 255, np.uint8)
    y = 6
    for t in tiles:
        if y + t.shape[0] > side.shape[0]:
            break
        side[y:y + t.shape[0], 6:6 + side_w] = t
        y += t.shape[0] + 6
    body = np.hstack([left, side])

    head = np.full((22, body.shape[1], 3), 255, np.uint8)
    cv2.putText(head, title, (6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (40, 40, 40), 1, cv2.LINE_AA)
    out = np.vstack([head, body])
    out = cv2.resize(out, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST) if W < 500 else out
    cv2.imwrite(str(path), out)


# ---------- 浏览器操作 ----------

def new_puzzle(page):
    # demo 页在参数变化时会重建内嵌实例，借它换一张新图
    page.eval_on_selector('#decoys', 'el => el.dispatchEvent(new Event("change"))')
    page.wait_for_function(f"document.querySelector('{ROOT}')?.dataset.state === 'ready'")


def set_control(page, sel, value):
    page.eval_on_selector(sel, '(el, v) => { el.value = v; }', str(value))


def min_jerk(u):
    """最小加加速度曲线：0→1，起止速度为 0，速度呈钟形，接近人手的瞄准运动"""
    return u ** 3 * (10 - 15 * u + 6 * u * u)


def smooth_noise(rng, terms=3):
    """一条平滑的随机曲线 f(秒)，幅度约 ±1：几个低频正弦叠加，用来模拟手抖"""
    comps = [(rng.uniform(0.4, 2.5), rng.uniform(0, 2 * math.pi), rng.uniform(0.4, 1)) for _ in range(terms)]
    norm = sum(a for _, _, a in comps)
    return lambda t: sum(a * math.sin(2 * math.pi * f * t + ph) for f, ph, a in comps) / norm


def human_path(distance, rng, duration=None):
    """生成一次拟人拖动的轨迹，返回 ([(t, dx, dy)], 松手时刻)

    t 是按下鼠标之后的秒数，dx/dy 相对按下的位置。人拖滑块的特点：
      1. 速度先快后慢，减速段比加速段长（目标越近越小心）
      2. 手不稳：y 方向有几像素的缓慢漂移，采样间隔也不均匀（8~20ms）
      3. 多数时候会冲过头一点，停顿一下再往回修正
      4. 偶尔在途中顿一下，到位后停一会儿才松手
    """
    sign = 1 if distance >= 0 else -1
    T = duration * rng.uniform(0.85, 1.15) if duration else rng.uniform(0.9, 1.7)
    overshoot = sign * rng.uniform(1.5, 5) if rng.random() < 0.7 else 0.0
    reach = distance + overshoot
    amp = rng.uniform(1.0, 3.0)
    wobble = smooth_noise(rng)
    hesitate_at = rng.uniform(0.5, 0.75) if rng.random() < 0.5 else None
    dwell = 0.0  # 途中停顿累计的时间

    pts, t, y = [], 0.0, 0.0
    while t < T:
        t = min(T, t + rng.uniform(0.008, 0.02))
        u = t / T
        if hesitate_at is not None and dwell == 0 and u >= hesitate_at:
            dwell = rng.uniform(0.06, 0.16)  # 这段时间不产生移动事件
        # u**0.8 让速度峰值提前到约 42% 处，后半程是更长的减速
        x = reach * min_jerk(u ** 0.8)
        y = amp * math.sin(math.pi * u) ** 0.6 * wobble(t)
        pts.append((t + dwell, x, y))
    t_now = pts[-1][0]

    if overshoot:  # 发现过头了：停一下，再慢慢拉回来
        t_now += rng.uniform(0.08, 0.2)
        back = rng.uniform(0.12, 0.28)
        y_end, c = y, 0.0
        while c < back:
            c = min(back, c + rng.uniform(0.008, 0.02))
            pts.append((t_now + c, reach - overshoot * min_jerk(c / back), y_end + rng.gauss(0, 0.25)))
        t_now += back
    return pts, t_now + rng.uniform(0.1, 0.3)  # 到位后再停一会儿才松手


def drag_human(page, distance, rng, duration, pos):
    """拟人拖动：先弧线靠近滑块，按下前后各停顿，再按 human_path 的轨迹拖。返回松手时的鼠标位置"""
    box = page.locator(f'{ROOT} .sc-handle').bounding_box()
    # 抓取点不在正中心，而是围绕中心的一个小范围
    gx = box['x'] + box['width'] * min(max(rng.gauss(0.5, 0.12), 0.2), 0.8)
    gy = box['y'] + box['height'] * min(max(rng.gauss(0.5, 0.12), 0.2), 0.8)

    # 靠近：二次贝塞尔弧线 + 最小加加速度节奏；第一次没有历史位置，从滑块左上/左下一段距离外出发
    if pos is None:
        pos = (gx - rng.uniform(120, 260), gy + rng.uniform(-140, 140))
        page.mouse.move(*pos)
    d = math.hypot(gx - pos[0], gy - pos[1])
    bend = rng.uniform(-0.15, 0.15) * d
    cx, cy = (pos[0] + gx) / 2 - (gy - pos[1]) / max(d, 1) * bend, (pos[1] + gy) / 2 + (gx - pos[0]) / max(d, 1) * bend
    T = min(0.9, 0.25 + d / 1500) * rng.uniform(0.85, 1.2)
    start = time.perf_counter()
    while (u := min(1.0, (time.perf_counter() - start) / T)) < 1:
        s = min_jerk(u)
        page.mouse.move((1 - s) ** 2 * pos[0] + 2 * (1 - s) * s * cx + s * s * gx,
                        (1 - s) ** 2 * pos[1] + 2 * (1 - s) * s * cy + s * s * gy)
        time.sleep(rng.uniform(0.008, 0.016))
    page.mouse.move(gx, gy)

    time.sleep(rng.uniform(0.12, 0.35))  # 对准滑块，停一下
    page.mouse.down()
    time.sleep(rng.uniform(0.06, 0.18))  # 反应时间

    path, t_release = human_path(distance, rng, duration)
    start = time.perf_counter()
    for t, dx, dy in path:
        wait = t - (time.perf_counter() - start)
        if wait > 0:
            time.sleep(wait)
        page.mouse.move(gx + dx, gy + dy)
    wait = t_release - (time.perf_counter() - start)
    if wait > 0:
        time.sleep(wait)
    page.mouse.up()
    return gx + path[-1][1], gy + path[-1][2]


def drag(page, distance, steps, duration):
    box = page.locator(f'{ROOT} .sc-handle').bounding_box()
    x0, y0 = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    page.mouse.move(x0, y0)
    page.mouse.down()
    if duration <= 0:
        page.mouse.move(x0 + distance, y0, steps=steps)
    else:
        # 放慢成一段平滑的减速动画，只是为了演示时看得清；按实际经过的时间算位置，总时长才准
        start = time.perf_counter()
        while True:
            t = min(1.0, (time.perf_counter() - start) / duration)
            page.mouse.move(x0 + distance * (1 - (1 - t) ** 3), y0)
            if t >= 1:
                break
            page.wait_for_timeout(16)
    page.mouse.up()


def main():
    ap = argparse.ArgumentParser(description='本地滑块验证自动识别（Playwright + OpenCV）')
    ap.add_argument('--url', default='http://localhost:8765/')
    ap.add_argument('--rounds', type=int, default=1)
    ap.add_argument('--method', choices=['gray', 'edge'], default='gray', help='实际用来拖动的方法')
    ap.add_argument('--decoys', type=int, help='干扰块数量，不填用页面默认')
    ap.add_argument('--tolerance', type=int, help='容差 px，不填用页面默认')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--dpr', type=float, default=1, help='设备像素比，2 可模拟 Retina 屏')
    ap.add_argument('--drag', choices=['human', 'smooth'],
                    help='拖动方式：human = 拟人轨迹（有界面默认），smooth = 匀滑直线（无界面默认，批量跑更快）')
    ap.add_argument('--seed', type=int, help='随机种子，固定后拟人轨迹可复现')
    ap.add_argument('--steps', type=int, default=20, help='smooth 模式下，拖动过程的鼠标移动次数')
    ap.add_argument('--duration', type=float,
                    help='拖动主段用时（秒）。human 默认随机 0.9~1.7；smooth 有界面默认 1.2，无界面默认 0')
    ap.add_argument('--save', choices=['auto', 'all', 'fail', 'none'], default='auto',
                    help='保存标注图：auto = 5 轮以内全存，否则只存失败的')
    args = ap.parse_args()
    if args.drag is None:
        args.drag = 'smooth' if args.headless else 'human'
    if args.drag == 'smooth' and args.duration is None:
        args.duration = 0 if args.headless else 1.2
    rng = random.Random(args.seed)
    pos = None  # 拟人模式下鼠标上一次停的位置

    out_dir = Path(__file__).with_name('out') / time.strftime('%Y%m%d-%H%M%S')
    out_dir.mkdir(parents=True, exist_ok=True)
    save_all = args.save == 'all' or (args.save == 'auto' and args.rounds <= 5)
    rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page(viewport={'width': 1100, 'height': 800}, device_scale_factor=args.dpr)
        page.add_init_script(
            "window.__sc = []; document.addEventListener('sc:verify', e => window.__sc.push(e.detail));")
        try:
            page.goto(args.url)
        except PlaywrightError:
            raise SystemExit(f'打不开 {args.url}，先在项目目录运行：python3 -m http.server 8765')

        if args.decoys is not None:
            set_control(page, '#decoys', args.decoys)
        if args.tolerance is not None:
            set_control(page, '#tolerance', args.tolerance)
        tolerance = int(page.input_value('#tolerance'))

        for i in range(1, args.rounds + 1):
            new_puzzle(page)
            info = page.locator(ROOT).evaluate(GRAB_JS)
            bg, piece = decode(info['bg']), decode(info['piece'])
            scale = info['scale']

            t0 = time.perf_counter()
            det, extra = detect(bg, piece, info['top'], scale)
            detect_ms = (time.perf_counter() - t0) * 1000

            # 拼图块与滑块 1:1 移动：拖动距离 = 匹配位置 - 拼图块当前位置
            distance = det[args.method]['x'] / scale - info['left']
            n = page.evaluate('window.__sc.length')
            if args.drag == 'human':
                pos = drag_human(page, distance, rng, args.duration, pos)
            else:
                drag(page, distance, args.steps, args.duration)
            page.wait_for_function('n => window.__sc.length > n', arg=n, timeout=5000)
            res = page.evaluate('window.__sc[window.__sc.length - 1]')

            # 用组件回报的偏差反推真实距离，给两种方法都算误差
            truth = round(res['distance'] - res['offset'])
            row = {
                'round': i,
                'success': res['success'],
                'offset': res['offset'],
                'distance': round(distance, 1),
                'truth': truth,
                'detect_ms': round(detect_ms, 2),
                'drag_ms': res['duration'],
                'track_pts': len(res['track']),
            }
            for name, d in det.items():
                row[f'{name}_err'] = round(d['x'] / scale - info['left'] - truth, 1)
                row[f'{name}_score'] = round(d['score'], 3)
                row[f'{name}_margin'] = round(d['margin'], 3)
            rows.append(row)

            mark = '通过' if res['success'] else '失败'
            print(f"[{i:>3}/{args.rounds}] {mark}  偏差 {res['offset']:>6}px  "
                  f"gray 误差 {row['gray_err']:>6}  edge 误差 {row['edge_err']:>6}  识别 {detect_ms:.1f}ms")

            if save_all or (args.save in ('auto', 'fail') and not res['success']):
                title = f"round {i}  {'PASS' if res['success'] else 'FAIL'}  method={args.method}  offset={res['offset']}px"
                annotate(bg, piece, det, extra, out_dir / f"round_{i:03d}_{'ok' if res['success'] else 'fail'}.png", title)

            if not args.headless:
                page.wait_for_timeout(800)

        browser.close()

    with open(out_dir / 'results.csv', 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    passed = sum(r['success'] for r in rows)
    print(f'\n共 {total} 轮，拖动方法 {args.method}，容差 {tolerance}px')
    print(f'  通过 {passed}/{total}（{passed / total:.1%}），'
          f'平均 |偏差| {np.mean([abs(r["offset"]) for r in rows]):.2f}px，'
          f'平均识别耗时 {np.mean([r["detect_ms"] for r in rows]):.1f}ms')
    print('  两种方法的识别误差（|误差| ≤ 容差算对）：')
    for name in COLORS:
        errs = np.abs([r[f'{name}_err'] for r in rows])
        ok = int((errs <= tolerance).sum())
        print(f'    {name:<4}  {ok}/{total}（{ok / total:.1%}）  平均 {errs.mean():.2f}px  最差 {errs.max():.1f}px')
    print(f'  明细和标注图：{out_dir}')


if __name__ == '__main__':
    main()
