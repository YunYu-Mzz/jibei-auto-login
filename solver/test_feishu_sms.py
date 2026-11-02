from datetime import datetime, timedelta, timezone
import json
from urllib.parse import parse_qs, urlsplit
import unittest

from solver.feishu_sms import FeishuError, FeishuTransientError, FeishuSmsClient
from unittest.mock import patch


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self.value).encode()


class FakeOpener:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if request.full_url.endswith('/oauth/v3/token'):
            return FakeResponse({'code': 0, 'access_token': 'fake-token', 'expires_in': 7200})
        return FakeResponse(next(self.pages))


def item(chat_id, message_id, text):
    return {'chat_id': chat_id, 'message_id': message_id, 'msg_type': 'text',
            'body': {'content': json.dumps({'text': text})}}


class FeishuSmsTest(unittest.TestCase):
    def test_paginates_and_returns_matching_recent_code(self):
        now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
        stamp = now.strftime('%y/%m/%d,%H:%M:%S')
        unrelated = item('oc_group1', 'other', f'【其他省】短信验证码：111111\n接收时间：{stamp}')
        expected = item('oc_group1', 'target', f'【江苏电力】短信验证码：222222\n接收时间：{stamp}')
        opener = FakeOpener([
            {'code': 0, 'data': {'items': [unrelated], 'has_more': True, 'page_token': 'next'}},
            {'code': 0, 'data': {'items': [expected], 'has_more': False}},
        ])
        client = FeishuSmsClient({'app_id': 'cli_test1', 'app_secret': 'fake', 'chat_id': 'oc_group1'}, opener=opener)
        result = client.get_latest(since=now-timedelta(seconds=30), markers=['【江苏电力】'])
        self.assertEqual(result.code, '222222')
        self.assertEqual(result.message_id, 'target')
        self.assertEqual(len(opener.requests), 3)
        query = parse_qs(urlsplit(opener.requests[1].full_url).query)
        self.assertEqual(query['container_id'], ['oc_group1'])
        self.assertEqual(parse_qs(urlsplit(opener.requests[2].full_url).query)['page_token'], ['next'])

    def test_rejects_message_from_other_chat(self):
        opener = FakeOpener([{'code': 0, 'data': {'items': [item('oc_other', 'bad', 'text')], 'has_more': False}}])
        client = FeishuSmsClient({'app_id': 'cli_test1', 'app_secret': 'fake', 'chat_id': 'oc_group1'}, opener=opener)
        with self.assertRaises(FeishuError):
            client.messages_since(datetime.now(timezone.utc))

    def test_wait_retries_transient_connection_failure(self):
        client = FeishuSmsClient({'app_id': 'cli_test1', 'app_secret': 'fake', 'chat_id': 'oc_group1'})
        expected = object()
        with patch.object(client, 'get_latest', side_effect=[FeishuTransientError('temporary'), expected]), \
                patch('solver.feishu_sms.time.sleep'):
            self.assertIs(client.wait_for_code(since=datetime.now(timezone.utc), timeout=5), expected)

    def test_wait_does_not_retry_permission_errors(self):
        client = FeishuSmsClient({'app_id': 'cli_test1', 'app_secret': 'fake', 'chat_id': 'oc_group1'})
        with patch.object(client, 'get_latest', side_effect=FeishuError('permission denied')):
            with self.assertRaises(FeishuError):
                client.wait_for_code(since=datetime.now(timezone.utc), timeout=5)


if __name__ == '__main__':
    unittest.main()
