from datetime import datetime, timedelta, timezone
import unittest

from solver.sms_code import SmsCode, latest_code

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 31, 20, 10, 30, tzinfo=TZ)
SINCE = NOW - timedelta(seconds=70)


def message(province, code, timestamp, message_id):
    return {'message_id': message_id, 'text': (
        f'【{province}】新一代电力交易平台登录短信验证码：{code}，请尽快填写。\n'
        f'接收时间：{timestamp}\n设备号1')}


def anhui_message(body, timestamp, message_id, *, phone='13900000000', source='10690000000', device='1'):
    return {'message_id': message_id, 'text': (f'{body}\n\n短信来自：{source}\n'
            f'接收号SIM-1：\n{phone}\n接收时间：{timestamp}\n设备号{device}')}


class SmsCodeTest(unittest.TestCase):
    def test_selects_latest_matching_recent_message(self):
        messages = [
            message('江苏电力', '111111', '26/10/31,20:09:30', 'old'),
            message('国网辽宁电力', '222222', '26/10/31,20:10:20', 'other'),
            message('江苏电力', '333333', '26/10/31,20:10:10', 'new'),
        ]
        result = latest_code(messages, since=SINCE, markers=['【江苏电力】'], now=NOW)
        self.assertEqual(result.code, '333333')
        self.assertEqual(result.message_id, 'new')
        self.assertNotIn('333333', repr(result))

    def test_rejects_before_request_and_expired(self):
        messages = [message('江苏电力', '111111', '26/10/31,20:09:00', 'old')]
        self.assertIsNone(latest_code(messages, since=SINCE, markers=['【江苏电力】'], now=NOW))
        self.assertIsNone(latest_code(messages, since=NOW-timedelta(minutes=10), markers=['【江苏电力】'],
                                      now=NOW, max_age_seconds=30))

    def test_rejects_ambiguous_generic_and_missing_timestamp(self):
        messages = [
            message('国家电网', '111111', '26/10/31,20:10:10', 'generic'),
            {'message_id': 'no-time', 'text': '【江苏电力】短信验证码：111111'},
            message('江苏电力', '111111', '26/15/31,20:10:10', 'invalid-date'),
        ]
        self.assertIsNone(latest_code(messages, since=SINCE, markers=['【江苏电力】'], now=NOW))

    def test_rejects_invalid_marker_and_naive_time(self):
        with self.assertRaises(ValueError):
            latest_code([], since=SINCE, markers=[''], now=NOW)
        with self.assertRaises(ValueError):
            latest_code([], since=SINCE.replace(tzinfo=None), markers=['江苏'], now=NOW)

    def test_latest_from_any_source_when_no_markers(self):
        messages = [message('江苏电力', '111111', '26/10/31,20:10:10', 'first'),
                    message('国家电网', '222222', '26/10/31,20:10:20', 'latest')]
        result = latest_code(messages, since=SINCE, now=NOW)
        self.assertEqual(result.message_id, 'latest')

    def test_excludes_future_messages(self):
        messages = [message('江苏电力', '444444', '26/10/31,20:10:31', 'future')]
        self.assertIsNone(latest_code(messages, since=SINCE, markers=['江苏'], now=NOW))

    def test_filters_receiver(self):
        messages = [message('江苏电力', '111111', '26/10/31,20:10:10', 'wrong')]
        messages[0]['text'] += '\n接收号SIM-2：\n13900000001'
        messages.append(message('江苏电力', '222222', '26/10/31,20:10:11', 'right'))
        messages[1]['text'] += '\n接收号SIM-1：\n13900000000'
        result = latest_code(messages, since=SINCE, markers=['江苏'], receiver='13900000000', now=NOW)
        self.assertEqual(result.message_id, 'right')

    def test_province_filters_use_observed_signatures(self):
        provinces = {'jiangsu': '江苏电力', 'jibei': '冀北电力', 'liaoning': '国网辽宁电力',
                     'shaanxi': '国网陕西电力', 'shanxi': '国家电网'}
        messages = [message(sig, f'{i:06d}', '26/10/31,20:10:10', province)
                    for i, (province, sig) in enumerate(provinces.items(), 1)]
        for province in provinces:
            with self.subTest(province=province):
                self.assertEqual(latest_code(messages, since=SINCE, province=province, now=NOW).message_id, province)
        with self.assertRaises(ValueError):
            latest_code(messages, since=SINCE, province='unknown', now=NOW)

    def test_anhui_waits_for_paired_signature_and_handles_duplicate_forwarding(self):
        body = '新一代电力交易平台登录短信验证码：123456，请尽快填写。'
        code = anhui_message(body, '26/10/31,20:10:10', 'code')
        signature = anhui_message('【安徽电力】', '26/10/31,20:10:11', 'signature')
        self.assertIsNone(latest_code([code], since=SINCE, province='anhui', now=NOW))
        result = latest_code([code, signature, dict(code, message_id='duplicate')],
                             since=SINCE, province='anhui', now=NOW)
        self.assertEqual(result.code, '123456')
        self.assertEqual(result.message_id, 'code')

    def test_anhui_rejects_unmatched_or_ambiguous_pairs(self):
        body = '新一代电力交易平台登录短信验证码：123456，请尽快填写。'
        code = anhui_message(body, '26/10/31,20:10:10', 'code')
        for changes in ({'phone': '13900000001'}, {'source': '10690000001'}, {'device': '2'}):
            signature = anhui_message('【安徽电力】', '26/10/31,20:10:10', 'signature', **changes)
            with self.subTest(changes=changes):
                self.assertIsNone(latest_code([code, signature], since=SINCE, province='anhui', now=NOW))
        signature = anhui_message('【安徽电力】', '26/10/31,20:10:10', 'signature')
        second = anhui_message(body.replace('123456', '654321'), '26/10/31,20:10:11', 'other-code')
        self.assertIsNone(latest_code([code, second, signature], since=SINCE, province='anhui', now=NOW))
        far_signature = anhui_message('【安徽电力】', '26/10/31,20:10:20', 'far')
        self.assertIsNone(latest_code([code, far_signature], since=SINCE, province='anhui', now=NOW))


if __name__ == '__main__':
    unittest.main()
