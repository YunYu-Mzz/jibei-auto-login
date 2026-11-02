"""从已授权读取到的飞书群消息中筛选对应的新验证码。"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

LOCAL_TZ = timezone(timedelta(hours=8))
CODE_PATTERN = re.compile(r'(?:登录\s*)?(?:短信\s*)?验证码\s*[:：]\s*(\d{4,8})(?!\d)')
SMS_TIME_PATTERN = re.compile(
    r'接收时间\s*[:：]\s*(\d{2}|\d{4})/(\d{1,2})/(\d{1,2})\s*[,， ]\s*(\d{1,2}):(\d{2}):(\d{2})')
RECEIVER_PATTERN = re.compile(r'接收号\s*SIM\s*[-－]?\s*\d+\s*[:：]\s*(\d{11})(?!\d)', re.I)
SOURCE_PATTERN = re.compile(r'短信来自\s*[:：]\s*([^\s]+)')
DEVICE_PATTERN = re.compile(r'设备号\s*(\d+)')
LOGIN_TEXT = '新一代电力交易平台登录短信验证码'
PROVINCE_MARKERS = {
    'jiangsu': '【江苏电力】',
    'jibei': '【冀北电力】',
    'liaoning': '【国网辽宁电力】',
    'shaanxi': '【国网陕西电力】',
    # 山西使用通用签名；仅凭短信正文无法与其他使用同签名的省份区分。
    'shanxi': '【国家电网】',
}
PROVINCES = (*PROVINCE_MARKERS, 'anhui')
ANHUI_PAIR_SECONDS = 3


@dataclass(frozen=True, repr=False)
class SmsCode:
    code: str
    received_at: datetime
    message_id: str

    def __repr__(self):
        return f'SmsCode(code=<hidden>, received_at={self.received_at!r}, message_id={self.message_id!r})'


def sms_time(text):
    """读取短信猫消息中的实际接收时间，缺失或不合法时返回 None。"""
    match = SMS_TIME_PATTERN.search(text)
    if not match:
        return None
    year, month, day, hour, minute, second = map(int, match.groups())
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def latest_code(messages, *, since, markers=(), receiver=None, province=None, now=None, max_age_seconds=180):
    """只接受收到时间在本次请求之后且满足筛选条件的短信。

    messages 是包含 text 和 message_id 的字典序列；不读取或输出消息内容。
    province 可指定省份；不指定时兼容从所有来源选取最新验证码。
    安徽需要同接收号、来源和设备的验证码正文与签名在短时间内唯一配对。
    """
    if since.tzinfo is None or since.utcoffset() is None:
        raise ValueError('since 必须带时区')
    now = now or datetime.now(LOCAL_TZ)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now 必须带时区')
    if not isinstance(markers, (list, tuple)) or any(not isinstance(m, str) or not m.strip() for m in markers):
        raise ValueError('短信来源标识必须是非空字符串的列表')
    if max_age_seconds <= 0:
        raise ValueError('max_age_seconds 必须为正数')
    if receiver is not None and (not isinstance(receiver, str) or not re.fullmatch(r'\d{11}', receiver)):
        raise ValueError('接收号码必须为 11 位数字')
    if province is not None and province not in PROVINCES:
        raise ValueError('不支持的省份')
    records = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = message.get('text')
        if not isinstance(text, str):
            continue
        receiver_match = RECEIVER_PATTERN.search(text)
        if receiver is not None:
            if not receiver_match or receiver_match.group(1) != receiver:
                continue
        received = sms_time(text)
        if received is None:
            continue
        if received < since or received > now or now - received > timedelta(seconds=max_age_seconds):
            continue
        message_id = message.get('message_id')
        if not isinstance(message_id, str) or not message_id:
            continue
        first_line = text.splitlines()[0].strip() if text.splitlines() else ''
        source_match = SOURCE_PATTERN.search(text)
        device_match = DEVICE_PATTERN.search(text)
        source = source_match.group(1) if source_match else None
        device = device_match.group(1) if device_match else None
        phone = receiver_match.group(1) if receiver_match else None
        code_match = CODE_PATTERN.search(first_line)
        code = code_match.group(1) if code_match else None
        # 短信猫可能重复转发同一条短信，不让重复副本造成配对歧义。
        key = (received, phone, source, device, first_line)
        records.setdefault(key, (text, first_line, received, phone, source, device, code, message_id))

    entries = list(records.values())
    candidates = []
    for text, first_line, received, phone, source, device, code, message_id in entries:
        if not code or (markers and not any(marker in text for marker in markers)):
            continue
        if province in PROVINCE_MARKERS:
            if not first_line.startswith(PROVINCE_MARKERS[province]) or LOGIN_TEXT not in first_line:
                continue
        elif province == 'anhui':
            if not first_line.startswith(LOGIN_TEXT) or not all((phone, source, device)):
                continue
            signatures = [r for r in entries if r[1] == '【安徽电力】' and
                          r[3:6] == (phone, source, device) and
                          abs((r[2] - received).total_seconds()) <= ANHUI_PAIR_SECONDS]
            if len(signatures) != 1:
                continue
            signature_time = signatures[0][2]
            possible_codes = [r for r in entries if r[6] and r[1].startswith(LOGIN_TEXT) and
                              r[3:6] == (phone, source, device) and
                              abs((r[2] - signature_time).total_seconds()) <= ANHUI_PAIR_SECONDS]
            if len(possible_codes) != 1:
                continue
        candidates.append(SmsCode(code, received, message_id))
    return max(candidates, key=lambda item: item.received_at) if candidates else None
