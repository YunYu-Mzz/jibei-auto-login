# 冀北电力交易平台自动登录模块

从冀北采集机 `mz` 的现用部署中整理的登录代码。模块接收调用方提供的 Playwright 页面，打开冀北登录页，填写账号密码，识别滑块，从飞书读取短信验证码，填写验证码后可选择停下或提交最终验证。

此仓库包含共享的 `power_login` / `solver` 源码，因此代码中也有其他省份的站点映射；**这些映射不代表本仓库已完成其他省份的接入或线上验收**。冀北生产用封装 `integration/automatic_login.py` 同包保留，但它依赖原冀北采集项目的队列、保活、上下文封闭证据和状态目录，并不能从本仓库单独启动生产恢复流程。不要把该封装复制到另一套环境后绕过这些保护。

## 运行环境

- Python 3.9 及以上；线上部署曾使用 3.10。
- Playwright、NumPy、OpenCV：见 `pyproject.toml`。
- 可以连接目标站的网络、浏览器和飞书短信来源；这些环境与登录信息由使用方自行准备。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
```

账号文件包含 `username` 和 `password`；飞书配置包含 `app_id`、`app_secret` 和 `chat_id`。请将这些文件保存在仓库外的私有目录，设置为当前用户独占读取（例如 `chmod 600`），并通过下例参数传入。**不要提交真实账号、密码、飞书配置、短信、Cookie、浏览器 profile 或采集数据。** 飞书读取器要求配置文件不是符号链接且权限不向组或其他用户开放；生产封装对两个文件均有私有性检查。

## 调用示例

下面的例子停在填写短信验证码之后，供人工检查；只有得到明确授权后，才应选择提交最终验证。

```python
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from power_login import login

async def main():
    private = Path.home() / ".config" / "jibei-login"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        page = await browser.new_page()
        result = await login(
            page,
            "jibei",
            credentials_file=private / "credentials.json",
            feishu_config=private / "feishu_config.json",
            stop_after_code=True,
            wait=75,
            sms_timeout=150,
        )
        print(result.stage)  # code_filled
        # 调用方在这里负责人工检查及浏览器生命周期。
        await page.wait_for_timeout(60_000)
        await browser.close()

asyncio.run(main())
```

`stop_after_code=False` 会点击最终验证并等待进入登录后的页面；**该参数在原版代码中默认为 False**。返回 `logged_in` 表示该次流程观察到登录后的页面，并不替代采集项目要求的独立认证、保活、任务锁和原件校验。账号与浏览器可能正由采集使用，调用前应确认 owner 和授权，避免并发登录。自动滑块有可能失败，不能保证每次成功。

## 冀北生产接入说明

`integration/automatic_login.py` 是从生产机保留的调用封装副本，要求原项目的 `adapters`、`state` 和现有 CDP 页面，以及与认证失败和旧上下文关闭关联的证据。它保留单次领取、防止旧请求重发、私有配置文件检查等机制。源码中的 `/home/mz/...` 路径是当时部署路径，迁移时需审查并按目标机配置；本仓库没有生产状态或凭据。

该封装调用原版 `login` 时未设置 `stop_after_code=True`，因此会提交最终验证，不适用于“填完验证码停止”的测试。2026-11-01 冀北旧流程曾实际完成登录；这里的源码打包未进行新的线上登录或网站请求。新版人工停点流程属于另一套独立代码，不包含在此生产旧版包中。

## 本地测试

```bash
python -m unittest power_login.test_login solver.test_feishu_sms solver.test_sms_code
```

登录流程测试使用拦截的本地模拟页面与伪短信客户端，默认需要系统安装 Microsoft Edge；可通过 `POWER_LOGIN_TEST_CDP` 指定专门用于测试的浏览器。不要把测试指向生产浏览器。无需访问真实冀北网站。
