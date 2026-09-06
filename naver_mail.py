"""네이버 IMAP -> Telegram. Marks a message read only after delivery."""
import hashlib
import imaplib
import json
import os
import re
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

STATE = Path('naver_imap_state.json')
KST = timezone(timedelta(hours=9))


def save(state):
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    temp.replace(STATE)


def api(url, payload, headers=None):
    request = Request(url, data=json.dumps(payload).encode(), headers={
        'Content-Type': 'application/json', **(headers or {})})
    try:
        with urlopen(request, timeout=45) as response:
            return json.load(response)
    except Exception as exc:
        # Do not print URLs containing tokens or email contents.
        raise RuntimeError('API 요청 실패: ' + str(getattr(exc, 'code', type(exc).__name__))) from None


def telegram(text):
    result = api('https://api.telegram.org/bot' + os.environ['TELEGRAM_TOKEN'] + '/sendMessage', {
        'chat_id': os.environ['TELEGRAM_CHAT_ID'], 'text': text,
        'link_preview_options': {'is_disabled': True}})
    if not result.get('ok'):
        raise RuntimeError('Telegram 전송 실패')


def chunks(text):
    result, part, size = [], '', 0
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > 3400:
            result.append(part)
            part, size = '', 0
        part += char
        size += units
    if part:
        result.append(part)
    return result


def deliver(state, key, text, deadline):
    record = state['sent'].get(key, {})
    if record.get('done'):
        return True
    digest = hashlib.sha256(text.encode()).hexdigest()
    start = record.get('next', 0) if record.get('hash') == digest else 0
    pieces = chunks(text)
    for index in range(start, len(pieces)):
        if time.monotonic() > deadline:
            return False
        telegram(f'({index + 1}/{len(pieces)})\n' + pieces[index])
        state['sent'][key] = {'hash': digest, 'next': index + 1}
        save(state)
        time.sleep(1.1)
    state['sent'][key] = {'done': True}
    save(state)
    return True


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self.hidden += 1
        if not self.hidden and tag in ('br', 'p', 'div', 'li', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head'):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ('p', 'div', 'li', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def body(message):
    part = message.get_body(preferencelist=('plain', 'html'))
    if part is None:
        return '(텍스트 본문 없음)'
    try:
        text = part.get_content()
    except (LookupError, UnicodeError):
        text = (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
    if part.get_content_type() == 'text/html':
        parser = PlainHTML()
        parser.feed(text)
        text = '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())
    return text.replace('\x00', '').strip() or '(텍스트 본문 없음)'



LABEL = '네이버'
HOST = 'imap.naver.com'
EMAIL_SECRET = 'NAVER_EMAIL'
PASSWORD_SECRET = 'NAVER_APP_PASSWORD'
TOKEN_SECRET = 'TELEGRAM_BOT_TOKEN'
MAIL_URL = 'https://mail.naver.com/'

def mark_read(client, uid, state, key):
    record = state['sent'].get(key, {})
    if not record.get('done'):
        raise RuntimeError('전송 완료 전 읽음 처리는 허용하지 않습니다.')
    if record.get('read'):
        return
    status, _ = client.uid('STORE', uid, '+FLAGS.SILENT', '(\\Seen)')
    if status != 'OK':
        raise RuntimeError('알림은 전송했지만 읽음 처리 실패. 다음 실행에서 재시도합니다.')
    record['read'] = True
    save(state)



def main():
    for name in (EMAIL_SECRET, PASSWORD_SECRET, TOKEN_SECRET, 'TELEGRAM_CHAT_ID'):
        if not os.environ.get(name, '').strip():
            raise RuntimeError('Secret 누락: ' + name)
    os.environ['TELEGRAM_TOKEN'] = os.environ[TOKEN_SECRET]
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else None
    deadline = time.monotonic() + 360
    with imaplib.IMAP4_SSL(HOST, 993, ssl_context=ssl.create_default_context(), timeout=45) as client:
        client.login(os.environ[EMAIL_SECRET], os.environ[PASSWORD_SECRET])
        if client.select('INBOX', readonly=False)[0] != 'OK':
            raise RuntimeError('받은메일함 열기 실패. IMAP 사용 설정을 확인하세요.')
        values = client.response('UIDVALIDITY')[1]
        if not values or values[0] is None:
            raise RuntimeError('메일함 UIDVALIDITY 확인 실패')
        validity = values[0].decode()
        status, data = client.uid('SEARCH', None, 'ALL')
        if status != 'OK':
            raise RuntimeError('메일 검색 실패')
        uids = data[0].split() if data[0] else []
        if state is None:
            telegram(f'✅ {LABEL} IMAP 전환 완료\n현재 받은메일함은 기준으로 등록했습니다.\n이후 새 메일은 텔레그램 전송 성공 후 읽음 처리합니다.')
            state = {'validity': validity, 'baseline': max((int(u) for u in uids), default=0), 'sent': {}}
            save(state)
            print('IMAP 최초 등록 완료. 이후 새 메일부터 알림 및 읽음 처리합니다.')
            return
        if state['validity'] != validity:
            raise RuntimeError('메일함 UIDVALIDITY가 변경됐습니다. 중복/누락 방지를 위해 처리를 중단했습니다. 기록을 삭제하지 말고 점검하세요.')
        count = 0
        for uid in uids:
            if int(uid) <= state['baseline']:
                continue
            key = uid.decode()
            record = state['sent'].get(key, {})
            if record.get('read'):
                continue
            if time.monotonic() > deadline:
                break
            # 전송 성공 후 읽음 변경만 실패했으면 재전송하지 않습니다.
            if not record.get('done'):
                status, data = client.uid('FETCH', uid, '(BODY.PEEK[])')
                if status != 'OK':
                    raise RuntimeError('메일 본문 조회 실패')
                item = next((x for x in data if isinstance(x, tuple)), None)
                if item is None:
                    continue  # 검색 후 다른 클라이언트에서 이동/삭제한 메일
                message = BytesParser(policy=policy.default).parsebytes(item[1])
                names = [str(p.get_filename()) for p in message.walk() if p.get_filename()]
                attachment = '\n\n📎 첨부파일 이름\n' + '\n'.join(names) if names else ''
                text = (f"📩 {LABEL} 새 메일\n\n보낸 사람: {message.get('From', '')}\n"
                        f"제목: {message.get('Subject', '(제목 없음)')}\n메일 날짜: {message.get('Date', '')}\n\n"
                        + body(message) + attachment + '\n\n메일함 열기: ' + MAIL_URL)
                if not deliver(state, key, text, deadline):
                    break
            mark_read(client, uid, state, key)
            count += 1
        print(f'{LABEL}: 알림 전송 및 읽음 처리 {count}통 완료')


if __name__ == '__main__':
    try:
        main()
    except imaplib.IMAP4.error as exc:
        detail = str(exc)
        for name in (EMAIL_SECRET, PASSWORD_SECRET, TOKEN_SECRET):
            value = os.environ.get(name, '').strip()
            if value:
                detail = detail.replace(value, '[숨김]')
        print('IMAP 연결/인증 오류: ' + detail, file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else '실행 실패: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
