"""模型请求的紧凑表示；消息正文和本地证据ID保持可逆。"""

from __future__ import annotations

import copy


def wire_message(message, people):
    meta = {k: message[k] for k in ('name', 'bot_command', 'nearby_unread_image', 'images')
            if message.get(k) and not (k == 'name' and message.get('user') in people)}
    row = [message['id'], message.get('time', ''), message.get('user', ''), message['text']]
    if meta:
        row.append(meta)
    return row


def compact_payload(payload):
    """重复解释合并，相关指令才携带帮助，短编号在响应后还原。"""
    data = copy.deepcopy(payload)
    previous = data.get('previous_summary')
    if previous:
        for key in ('topics', 'terms_to_check'):
            for item in previous.get(key, []):
                item.pop('message_ids', None)
    ids = {}

    def encode_id(value):
        original = str(value)
        if original not in ids:
            ids[original] = f'm{len(ids) + 1}'
        return ids[original]

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'id':
                    value[key] = encode_id(item)
                elif key == 'message_ids':
                    value[key] = [encode_id(mid) for mid in item]
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    context = data.pop('context', {})
    grouped = {}
    for term in context.pop('terms', []):
        key = (term['meaning'], bool(term.get('keep_original')), bool(term.get('needs_context')))
        grouped.setdefault(key, []).append(term['term'])
    context['terms'] = [
        ['/'.join(names), meaning, *([{'keep_original': keep, 'needs_context': contextual}]
                                   if keep or contextual else [])]
        for (meaning, keep, contextual), names in grouped.items()
    ]
    corpus = str({k: v for k, v in data.items() if k not in {'people', 'period'}}).casefold()
    commands = []
    for command in context.pop('bot_commands', []):
        heads = command['usage'].split(' ', 1)[0].split('/')
        if any(head.casefold() in corpus for head in heads):
            commands.append([command['usage'], command['meaning']])
    if commands:
        context['bot_commands'] = commands
    people = data.pop('people', {})
    compact_people = {}
    for uid, person in people.items():
        confirmed = sorted(set(person.get('confirmed_aliases', [])) - {person['name']})
        observed = sorted(set(person.get('aliases', [])) - set(confirmed) - {person['name']})
        compact_people[uid] = [person['name'], confirmed, observed]
    messages = data.get('messages')
    if messages is not None:
        data['messages'] = [wire_message(message, people) for message in messages]
    # 稳定词表置于前缀，时间、消息等变动内容置于后方，便于服务端命中缓存。
    packed = {'context': context, 'people': compact_people, **data}
    return packed, {short: original for original, short in ids.items()}


def restore_evidence(result, identities):
    if isinstance(result, dict):
        for key in ('topics', 'terms_to_check'):
            for item in result.get(key, []):
                if isinstance(item, dict) and isinstance(item.get('message_ids'), list):
                    item['message_ids'] = [identities.get(str(mid), str(mid)) for mid in item['message_ids']]
    return result
