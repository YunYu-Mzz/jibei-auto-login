import importlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from playwright.async_api import async_playwright

module = importlib.import_module('power_login.login')

HTML = '''<!doctype html><html><body>
<input placeholder="请输入账号"><input placeholder="请输入密码" type="password">
<button>登 录</button>
<div class="el-dialog" style="display:none">
  <div role="tab">短信验证</div><input placeholder="短信验证码">
  <button>获取验证码</button>
  <button id="verify" onclick="document.querySelector('.el-dialog').style.display='none';
    document.querySelector('input[type=password]').style.display='none'; location.hash='/dashboard'">验 证</button>
</div></body></html>'''


class LoginFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.credentials = Path(self.temp.name) / 'credentials.json'
        self.credentials.write_text(json.dumps({'username': 'user', 'password': 'password'}))
        self.playwright = await async_playwright().start()
        endpoint = os.environ.get('POWER_LOGIN_TEST_CDP')
        self.browser = (await self.playwright.chromium.connect_over_cdp(endpoint, no_defaults=True)
                        if endpoint else await self.playwright.chromium.launch(channel='msedge', headless=True))
        self.owns_browser = not endpoint
        self.context = await self.browser.new_context()
        await self.context.route('**/*',
                                 lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=HTML))
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close()
        if self.owns_browser:
            await self.browser.close()
        await self.playwright.stop()
        self.temp.cleanup()

    async def _run(self, stop):
        async def fake_slider(page, **kwargs):
            await page.locator('.el-dialog').evaluate("e => e.style.display='block'")

        class FakeClient:
            def __init__(self, config): pass
            def wait_for_code(self, **kwargs):
                self.province = kwargs['province']
                return type('FakeCode', (), {'code': '123456'})()

        with patch.object(module, 'solve_slider', fake_slider), \
             patch.object(module, 'FeishuSmsClient', FakeClient), \
             patch.object(module, 'load_config', return_value={}):
            return await module.login(self.page, 'jibei', credentials_file=self.credentials,
                                      stop_after_code=stop, wait=3, sms_timeout=3)

    async def test_stop_after_code_does_not_submit_verification(self):
        result = await self._run(True)
        self.assertEqual(result.stage, 'code_filled')
        self.assertEqual(await self.page.get_by_placeholder('短信验证码').input_value(), '123456')
        self.assertTrue(await self.page.locator('.el-dialog').is_visible())
        self.assertNotIn('dashboard', self.page.url)

    async def test_normal_flow_confirms_logged_in_page(self):
        result = await self._run(False)
        self.assertEqual(result.stage, 'logged_in')
        self.assertTrue(self.page.url.endswith('#/dashboard'))

    async def test_http_error_stops_before_login(self):
        await self.context.unroute_all()
        await self.context.route('**/*', lambda route: route.fulfill(status=504, body='gateway timeout'))
        with patch.object(module, 'FeishuSmsClient'), patch.object(module, 'load_config', return_value={}):
            with self.assertRaisesRegex(module.LoginError, 'HTTP 504'):
                await module.login(self.page, 'jibei', credentials_file=self.credentials, wait=3)


if __name__ == '__main__':
    unittest.main()
