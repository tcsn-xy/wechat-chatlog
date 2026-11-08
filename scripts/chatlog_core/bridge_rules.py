"""Identity and trigger rules. Message text never grants access."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

KEYWORD = '豆包豆包'


def wechat_mentions(source):
    """Only the separately captured source column is authoritative, never body XML."""
    if not source:
        return None
    try:
        root = ET.fromstring(source)
    except (ET.ParseError, ValueError):
        return None
    if root.tag != 'msgsource':
        return None
    node = root.find('atuserlist')
    return set(re.split(r'[,;\s]+', node.text or '')) - {''} if node is not None else set()


def wecom_segments(raw):
    """5.0.7 text elements: repeated field 1; type 5 is an actual mention.

    Read only the top-level body, never quoted content in extra_content. Unknown
    element schemas are rejected rather than searching arbitrary protobuf IDs.
    """
    from wecom import pbwire
    try:
        root = pbwire.parse(bytes(raw or b''))
        if not root.fields or any(f.number != 1 for f in root.fields):
            return None, None
        text, ids = [], set()
        for field in root.fields:
            part = field.value
            if not isinstance(part, pbwire.Message):
                return None, None
            typ, payload = part.first(1), part.sub(2)
            if typ is None or typ.wire != 0 or payload is None:
                return None, None
            if typ.value == 0:
                value = payload.first(1)
                if value is None or not isinstance(value.value, (str, bytes)):
                    return None, None
                text.append(value.value if isinstance(value.value, str) else value.value.decode('utf-8'))
            elif typ.value == 5:
                uid = payload.first(1)
                if uid is None or uid.wire != 0 or not isinstance(uid.value, int) or uid.value <= 0:
                    return None, None
                ids.add(str(uid.value))
                text.append(' ')
            else:
                return None, None
        return ''.join(text), ids
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None, None


def question(message, self_id):
    if message.get('kind') != 'text':
        return None
    text = message.get('text') or ''
    room = (bool(message.get('is_room')) or
            (message['platform'] == 'wechat' and message.get('chat_id', '').endswith('@chatroom')) or
            (message['platform'] == 'wecom' and message.get('chat_id', '').startswith('R:')))
    own = message.get('sender_id') == self_id
    if message['platform'] == 'wecom':
        reconstructed, mentions = wecom_segments(message.get('raw_blob'))
        if reconstructed is not None:
            text = reconstructed
    else:
        mentions = wechat_mentions(message.get('mention_source'))
        # Strip only leading actual display mentions after verifying the source.
        if room and mentions and self_id in mentions:
            text = re.sub(r'^(?:\s*@[^\u2005\n]+\u2005\s*)+', '', text)
    if room and not own and (mentions is None or self_id not in mentions):
        return None
    text = text.lstrip()
    if KEYWORD not in text:
        return None
    body = text.replace(KEYWORD, '', 1).strip(' \t\r\n，,:：')
    return body if body.strip() else None
