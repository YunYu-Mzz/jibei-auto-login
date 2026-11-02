"""使用调用方提供的 Playwright 页面完成登录或安全地停在短信输入。"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeout

from solver.feishu_sms import FeishuError, FeishuSmsClient, load_config
from solver.jspec import solve_slider

SITES = {
    'jiangsu': ('江苏', 'https://www.jspec.com.cn/#/outNet'),
    'jibei': ('冀北', 'https://pmos.jibei.sgcc.com.cn/#/outNet'),
    'anhui': ('安徽', 'https://pmos.ah.sgcc.com.cn/#/outNet'),
    'liaoning': ('辽宁', 'https://pmos.ln.sgcc.com.cn/#/outNet'),
    'shanxi': ('山西', 'https://pmos.sx.sgcc.com.cn/#/outNet'),
    'shaanxi': ('陕西', 'https://pmos.sn.sgcc.com.cn/#/outNet'),
}
DEFAULT_ROOT = Path(__file__).resolve().parent.parent


class LoginError(Exception):
    """登录未能完成；不包含账号、密码或验证码。"""


@dataclass(frozen=True)
class LoginResult:
    province: str
    stage: str  # code_filled 或 logged_in


def _credentials(path):
    try:
        credentials = json.loads(Path(path).read_text(encoding='utf-8'))
        username, password = credentials['username'], credentials['password']
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise LoginError('无法读取账号密码文件') from exc
    if not all(isinstance(v, str) and v for v in (username, password)):
        raise LoginError('账号或密码为空')
    return username, password


async def login(page, province, *, credentials_file=None, feishu_config=None,
                stop_after_code=False, wait=75, sms_timeout=150, method='edge',
                receiver=None, progress=None):
    """在调用方提供的独立 Page 上执行；stop_after_code=True 时填码后绝不点击验证。

    所有浏览器生命周期和网络配置由调用方管理。只在最后观察到 dashboard
    后返回 logged_in；验证码、手机号及凭证不会出现在返回值或日志中。
    """
    if province not in SITES:
        raise ValueError('不支持的省份')
    if wait <= 0 or sms_timeout <= 0:
        raise ValueError('等待时间必须大于零')
    if method not in ('edge', 'gray'):
        raise ValueError('滑块识别方法必须是 edge 或 gray')
    if receiver is not None and (not isinstance(receiver, str) or not re.fullmatch(r'\d{11}', receiver)):
        raise ValueError('接收号码必须为 11 位数字')
    username, password = _credentials(credentials_file or DEFAULT_ROOT / 'credentials.json')
    try:
        client = FeishuSmsClient(load_config(feishu_config or DEFAULT_ROOT / 'feishu_config.json'))
    except FeishuError as exc:
        raise LoginError(f'飞书配置错误：{exc}') from exc
    _, url = SITES[province]
    current_stage = '初始化'
    def report(stage):
        nonlocal current_stage
        current_stage = stage
        if progress:
            progress(stage)

    try:
        report('opening_page')
        response = await page.goto(url, wait_until='commit', timeout=wait * 1000)
        if response is not None and response.status >= 400:
            raise LoginError(f'登录页面返回 HTTP {response.status}，未执行登录')
        if urlsplit(page.url).hostname != urlsplit(url).hostname:
            raise LoginError('网站跳转到了非预期域名，已停止')
        account = page.get_by_placeholder('请输入账号', exact=True)
        secret = page.get_by_placeholder('请输入密码', exact=True)
        submit = page.get_by_role('button', name=re.compile(r'^登\s*录$'))
        await account.wait_for(state='visible', timeout=wait * 1000)
        await secret.wait_for(state='visible', timeout=wait * 1000)
        if await account.count() != 1 or await secret.count() != 1 or await submit.count() != 1:
            raise LoginError('登录控件不唯一，已停止')
        await account.fill(username)
        await secret.fill(password)
        await submit.click(timeout=wait * 1000)
        report('credentials_submitted')
        try:
            await solve_slider(page, method=method, wait=wait)
        except ValueError as exc:
            raise LoginError(f'滑块验证失败：{exc}') from exc
        report('slider_passed')

        dialog = page.locator('.el-dialog:visible').filter(has=page.get_by_role('tab', name='短信验证', exact=True))
        await dialog.wait_for(state='visible', timeout=wait * 1000)
        if await dialog.count() != 1:
            raise LoginError('短信验证窗口不唯一，已停止')
        await dialog.get_by_role('tab', name='短信验证', exact=True).click(timeout=wait * 1000)
        code_input = dialog.get_by_placeholder('短信验证码', exact=True)
        send = dialog.get_by_role('button', name=re.compile(r'^(获取验证码|重新获取)$'))
        confirm = dialog.get_by_role('button', name=re.compile(r'^验\s*证$'))
        await code_input.wait_for(state='visible', timeout=wait * 1000)
        await send.wait_for(state='visible', timeout=max(wait, 190) * 1000)
        if await code_input.count() != 1 or await send.count() != 1 or await confirm.count() != 1:
            raise LoginError('短信验证控件不唯一，已停止')
        if not await send.is_enabled():
            raise LoginError('短信发送按钮不可用')
        since = datetime.now(timezone.utc).replace(microsecond=0)
        await send.click(timeout=wait * 1000)
        report('sms_requested')
        result = await asyncio.to_thread(client.wait_for_code, since=since, province=province,
                                         receiver=receiver, timeout=sms_timeout,
                                         max_age_seconds=sms_timeout + 30)
        if result is None:
            raise LoginError('没有收到符合省份条件的新短信验证码')
        await code_input.fill(result.code)
        report('code_filled')
        if stop_after_code:
            return LoginResult(province, 'code_filled')
        await confirm.click(timeout=wait * 1000)
        await dialog.wait_for(state='hidden', timeout=wait * 1000)
        await page.wait_for_function("() => location.hash === '#/dashboard' || location.hash.startsWith('#/dashboard?')",
                                     timeout=wait * 1000)
        if await secret.is_visible():
            raise LoginError('登录密码框仍可见，无法确认登录成功')
        return LoginResult(province, 'logged_in')
    except PlaywrightTimeout as exc:
        raise LoginError(f'阶段 {current_stage} 等待页面或验证控件超时') from exc
    except FeishuError as exc:
        raise LoginError(f'飞书读取失败：{exc}') from exc
