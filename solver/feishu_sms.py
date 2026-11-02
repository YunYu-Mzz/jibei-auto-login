"""使用飞书开放平台只读接口，读取指定群中的新短信验证码。"""
import argparse
import json
from pathlib import Path
import re
import stat
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

if __package__:
    from .sms_code import PROVINCES, latest_code
else:
    from sms_code import PROVINCES, latest_code

TOKEN_URL = 'https://accounts.feishu.cn/oauth/v3/token'
MESSAGES_URL = 'https://open.feishu.cn/open-apis/im/v1/messages'


class FeishuError(Exception):
    pass


class FeishuTransientError(FeishuError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_config(path):
    path = Path(path)
    try:
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise FeishuError('飞书配置文件必须由当前用户独占读取（权限 0600），且不能是符号链接')
        config = json.loads(path.read_text(encoding='utf-8'))
        app_id, app_secret, chat_id = (config[key] for key in ('app_id', 'app_secret', 'chat_id'))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise FeishuError('无法读取飞书配置文件，请检查文件与必要字段') from exc
    if not all(isinstance(v, str) and v.strip() for v in (app_id, app_secret, chat_id)):
        raise FeishuError('飞书配置文件中的 app_id、app_secret 和 chat_id 不能为空')
    if not re.fullmatch(r'cli_[A-Za-z0-9]+', app_id) or not re.fullmatch(r'oc_[A-Za-z0-9]+', chat_id):
        raise FeishuError('飞书应用 ID 或群 ID 格式不正确')
    return config


class FeishuSmsClient:
    def __init__(self, config, *, opener=None):
        self.config = config
        self.opener = opener or build_opener(NoRedirect())
        self._token = None
        self._expires_at = 0.0

    def _json_request(self, url, *, payload=None, token=None):
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        request = Request(url, data=data, headers=headers, method='POST' if data else 'GET')
        try:
            with self.opener.open(request, timeout=15) as response:
                result = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            # 不输出响应体、请求头、令牌或群消息正文。
            if exc.code == 429 or 500 <= exc.code < 600:
                raise FeishuTransientError(f'飞书接口暂时不可用（HTTP {exc.code}）') from exc
            raise FeishuError(f'飞书接口返回 HTTP {exc.code}；请检查应用权限与群配置') from exc
        except (URLError, OSError) as exc:
            raise FeishuTransientError('飞书接口连接暂时失败') from exc
        except ValueError as exc:
            raise FeishuError('飞书接口返回格式异常') from exc
        if not isinstance(result, dict):
            raise FeishuError('飞书接口返回格式异常')
        if result.get('code', 0) != 0:
            code = result.get('code')
            raise FeishuError(f'飞书接口错误（代码 {code}）；请检查应用权限与群配置')
        return result

    def _access_token(self):
        if not self._token or time.monotonic() >= self._expires_at:
            result = self._json_request(TOKEN_URL, payload={
                'grant_type': 'client_credentials',
                'client_id': self.config['app_id'],
                'client_secret': self.config['app_secret'],
            })
            token, expires = result.get('access_token'), result.get('expires_in')
            if not isinstance(token, str) or not token or not isinstance(expires, (int, float)) or expires <= 0:
                raise FeishuError('飞书未返回有效的应用凭证')
            self._token = token
            self._expires_at = time.monotonic() + max(0, expires - 120)
        return self._token

    def messages_since(self, since):
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError('since 必须带时区')
        token = self._access_token()
        params = {'container_id_type': 'chat', 'container_id': self.config['chat_id'],
                  'start_time': str(max(0, int(since.timestamp()) - 1)),
                  'sort_type': 'ByCreateTimeDesc', 'page_size': '50'}
        messages = []
        for _ in range(5):
            response = self._json_request(MESSAGES_URL + '?' + urlencode(params), token=token)
            data = response.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('items', []), list):
                raise FeishuError('飞书群消息格式异常')
            for item in data.get('items', []):
                if not isinstance(item, dict) or item.get('deleted') or item.get('msg_type') != 'text':
                    continue
                if item.get('chat_id') != self.config['chat_id']:
                    raise FeishuError('飞书返回了非目标群消息，停止读取')
                sender_id = self.config.get('sender_id')
                if sender_id and (not isinstance(item.get('sender'), dict)
                                  or item['sender'].get('id') != sender_id):
                    continue
                body = item.get('body') or {}
                try:
                    content = json.loads(body.get('content', ''))
                except (ValueError, TypeError, AttributeError):
                    continue
                if isinstance(content, dict) and isinstance(content.get('text'), str):
                    messages.append({'text': content['text'], 'message_id': item.get('message_id')})
            if not data.get('has_more'):
                return messages
            page_token = data.get('page_token')
            if not isinstance(page_token, str) or not page_token:
                raise FeishuError('飞书群消息分页信息缺失')
            params['page_token'] = page_token
        raise FeishuError('短时间内群消息超过读取上限，请缩短等待时间后检查')

    def get_latest(self, *, since, markers=(), receiver=None, province=None, max_age_seconds=180):
        return latest_code(self.messages_since(since), since=since, markers=markers,
                           receiver=receiver, province=province, max_age_seconds=max_age_seconds)

    def wait_for_code(self, *, since, markers=(), receiver=None, province=None, timeout=120, interval=3, max_age_seconds=180):
        if timeout <= 0 or interval < 1:
            raise ValueError('等待时长必须为正，查询间隔不得小于 1 秒')
        deadline = time.monotonic() + timeout
        last_error = None
        while True:
            try:
                result = self.get_latest(since=since, markers=markers, receiver=receiver,
                                         province=province, max_age_seconds=max_age_seconds)
                last_error = None
            except FeishuTransientError as exc:
                last_error = exc
                result = None
            if result:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last_error:
                    raise FeishuTransientError('等待期间飞书接口持续不可用') from last_error
                return None
            time.sleep(min(interval, remaining))


def main():
    parser = argparse.ArgumentParser(description='只读检查飞书指定群中新接收的验证码')
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parent.parent / 'feishu_config.json')
    parser.add_argument('--marker', default=[], action='append', help='可选的短信来源标识，可以重复指定')
    parser.add_argument('--province', choices=PROVINCES, help='按省份匹配验证码短信')
    parser.add_argument('--receiver', help='按短信接收手机号码筛选（11 位）')
    parser.add_argument('--since-seconds', type=int, default=180, help='仅检查最近多少秒的消息')
    args = parser.parse_args()
    if args.since_seconds <= 0:
        parser.error('--since-seconds 必须为正数')
    try:
        client = FeishuSmsClient(load_config(args.config))
        since = datetime.now(timezone.utc) - timedelta(seconds=args.since_seconds)
        result = client.get_latest(since=since, markers=args.marker, receiver=args.receiver,
                                   province=args.province, max_age_seconds=args.since_seconds)
        print('已找到有效验证码（内容不在终端显示）' if result else '没有找到符合条件的新验证码')
    except (FeishuError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == '__main__':
    main()
