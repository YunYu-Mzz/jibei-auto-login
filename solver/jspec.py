#!/usr/bin/env python3
"""通过已有的 Edge 调试连接测试江苏电力交易平台官网的滑块。

需要 Edge 提供 Chrome DevTools Protocol 地址，并在该窗口中打开验证码。
这个脚本不创建或重启浏览器，也不填写登录信息。
"""
import argparse
import asyncio
import math
from pathlib import Path

import cv2
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

if __package__:
    from .solve import decode, detect
else:
    from solve import decode, detect


def as_bgra(image):
    if image is None:
        raise ValueError('验证码图片解码失败')
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGRA)
    if image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    return image


def find_distance(background, piece, bg_box, piece_box, method):
    """返回应从滑块当前位置拖动的 CSS 像素数。"""
    bg = as_bgra(background)
    piece = as_bgra(piece)
    scale = bg.shape[1] / bg_box['width']
    vertical_scale = bg.shape[0] / bg_box['height']
    width = max(1, round(piece_box['width'] * scale))
    height = max(1, round(piece_box['height'] * vertical_scale))
    piece = cv2.resize(piece, (width, height), interpolation=cv2.INTER_AREA)
    if width > bg.shape[1] or height > bg.shape[0]:
        raise ValueError('拼图尺寸大于背景，无法匹配')
    # 网站可以分别缩放图像宽和高；detect 的 top 由水平 scale 换算。
    top = (piece_box['y'] - bg_box['y']) * vertical_scale / scale
    matches, _ = detect(bg, piece, top, scale)
    match = matches[method]
    current_left = piece_box['x'] - bg_box['x']
    return match['x'] / scale - current_left, match


async def main():
    parser = argparse.ArgumentParser(description='在已打开的 Edge 中测试江苏电力交易平台滑块')
    endpoint_args = parser.add_mutually_exclusive_group(required=True)
    endpoint_args.add_argument('--cdp', help='已有 Edge 的 CDP 地址')
    endpoint_args.add_argument('--devtools-file', type=Path, help='已有 Edge 的 DevToolsActivePort 文件')
    parser.add_argument('--method', choices=('gray', 'edge'), default='edge')
    parser.add_argument('--wait', type=int, default=120, help='等待用户打开滑块的秒数')
    args = parser.parse_args()
    endpoint = args.cdp
    if args.devtools_file:
        try:
            port, path = args.devtools_file.read_text().splitlines()[:2]
            endpoint = f'ws://127.0.0.1:{int(port)}{path}'
        except (OSError, ValueError) as exc:
            raise SystemExit(f'无法读取 Edge 的调试连接信息：{exc}') from exc

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(endpoint, timeout=10000, no_defaults=True)
        except PlaywrightError as exc:
            raise SystemExit(f'无法连接已有 Edge 的调试端口：{exc}') from exc

        pages = [page for context in browser.contexts for page in context.pages
                 if page.url.startswith('https://www.jspec.com.cn/')]
        if len(pages) != 1:
            raise SystemExit(f'需要恰好一个江苏官网标签页，当前找到 {len(pages)} 个')
        page = pages[0]
        print('请在当前页面打开滑块验证，等待程序识别……', flush=True)
        await solve_slider(page, method=args.method, wait=args.wait)


async def solve_slider(page, *, method='edge', wait=120):
    """识别并拖动指定页面上的江苏滑块，以官网响应判断结果。"""
    panel = page.locator('.verify-img-panel:visible').first
    await panel.wait_for(state='visible', timeout=wait * 1000)
    root = panel.locator('..').locator('..')
    bg_img = panel.locator('img').first
    piece_img = root.locator('.verify-sub-block img').first
    handle = root.locator('.verify-move-block').first
    await piece_img.wait_for(state='visible', timeout=10000)

    # 弹窗可能先出现，图片随后才由验证码接口异步填入。
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        bg_source = await bg_img.get_attribute('src') or ''
        piece_source = await piece_img.get_attribute('src') or ''
        if all(s.startswith('data:image/') and ',' in s and len(s.split(',', 1)[1]) > 20
               for s in (bg_source, piece_source)):
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise ValueError('验证码图片加载超时，或官网的图片格式已变化')
        await asyncio.sleep(0.2)
    bg_box, piece_box, handle_box = await asyncio.gather(
        *(el.bounding_box() for el in (bg_img, piece_img, handle)))
    if not all((bg_box, piece_box, handle_box)):
        raise ValueError('无法确定验证码的位置')
    distance, match = find_distance(decode(bg_source), decode(piece_source), bg_box, piece_box, method)
    print(f'识别到的拖动距离 {distance:.1f}px，匹配分数 {match["score"]:.3f}', flush=True)
    if not math.isfinite(distance) or distance <= 0 or distance > bg_box['width']:
        raise ValueError('识别结果超出滑动范围，停止测试')

    x = handle_box['x'] + handle_box['width'] / 2
    y = handle_box['y'] + handle_box['height'] / 2
    # 验证结果以服务端实际响应为准，不依赖缺口识别分数。
    async with page.expect_response(lambda r: '/captcha/check' in r.url, timeout=20000) as response:
        await page.mouse.move(x, y)
        await page.mouse.down()
        try:
            for i in range(1, 61):
                u = i / 60
                ease = u * u * (3 - 2 * u)
                await page.mouse.move(x + distance * ease, y)
                await asyncio.sleep(0.02)
        finally:
            await page.mouse.up()
    response = await response.value
    result = await response.json()
    code = result.get('repCode') if isinstance(result, dict) else None
    if code is None and isinstance(result, dict) and isinstance(result.get('data'), dict):
        code = result['data'].get('repCode')
    if response.status != 200 or code != '0000':
        raise ValueError(f'官网未返回验证成功（HTTP {response.status}，状态码：{code!r}）')
    print('官网返回滑块验证成功', flush=True)
    return {'success': True, 'distance': distance, 'score': match['score'], 'method': method}


if __name__ == '__main__':
    asyncio.run(main())
