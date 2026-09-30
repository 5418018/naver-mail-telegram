"""네이버 IMAP -> Telegram.
Telegram에는 제목 + 2줄 요약 + 메일함 링크만 전송합니다.
Telegram 전송 성공 후에만 메일을 읽음 처리합니다.
"""

import hashlib
import imaplib
import json
import os
import re
import ssl
import sys
import time

from datetime import timedelta, timezone
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from html import escape, unescape
from urllib.parse import urlsplit
from pathlib import Path
from urllib.request import Request, urlopen


# --------------------------------------------------
# 기본 설정
# --------------------------------------------------

STATE = Path('naver_imap_state.json')

KST = timezone(
    timedelta(hours=9)
)

TELEGRAM_MESSAGE_LIMIT = 3400

MAX_TELEGRAM_LINK_LENGTH = 900

# 요약 한 줄 최대 길이
SUMMARY_LINE_LENGTH = 120


# --------------------------------------------------
# 상태 저장
# --------------------------------------------------

def save(state):

    temp = STATE.with_suffix(
        '.tmp'
    )

    temp.write_text(
        json.dumps(
            state,
            ensure_ascii=False
        ),
        encoding='utf-8'
    )

    temp.replace(
        STATE
    )


# --------------------------------------------------
# API 호출
# --------------------------------------------------

def api(
    url,
    payload,
    headers=None
):

    request = Request(
        url,
        data=json.dumps(
            payload
        ).encode(),
        headers={
            'Content-Type':
                'application/json',

            **(headers or {})
        }
    )

    try:

        with urlopen(
            request,
            timeout=45
        ) as response:

            return json.load(
                response
            )

    except Exception as exc:

        code = getattr(
            exc,
            'code',
            type(exc).__name__
        )

        detail = ''

        try:

            detail = exc.read().decode(
                'utf-8',
                errors='replace'
            )

        except Exception:
            pass

        # Telegram Token 노출 방지
        token = os.environ.get(
            'TELEGRAM_TOKEN',
            ''
        ).strip()

        if token:

            detail = detail.replace(
                token,
                '[TELEGRAM_TOKEN 숨김]'
            )

        if detail:

            raise RuntimeError(
                f'API 요청 실패: {code}\n'
                f'API 응답: {detail}'
            ) from None

        raise RuntimeError(
            'API 요청 실패: '
            + str(code)
        ) from None


# --------------------------------------------------
# Telegram
# --------------------------------------------------

def telegram(
    text,
    *,
    html=False
):

    payload = {

        'chat_id':
            os.environ[
                'TELEGRAM_CHAT_ID'
            ],

        'text':
            text,

        'link_preview_options': {
            'is_disabled': True
        }
    }

    if html:

        payload[
            'parse_mode'
        ] = 'HTML'

    result = api(
        'https://api.telegram.org/bot'
        + os.environ[
            'TELEGRAM_TOKEN'
        ]
        + '/sendMessage',

        payload
    )

    if not result.get(
        'ok'
    ):

        raise RuntimeError(
            'Telegram 전송 실패: '
            + str(
                result.get(
                    'description',
                    '알 수 없는 오류'
                )
            )
        )


# --------------------------------------------------
# HTML → 일반 텍스트 fallback
# --------------------------------------------------

def html_to_plain(text):

    # Telegram HTML 링크 제거
    text = re.sub(
        r'<a\s+href="[^"]*">(.*?)</a>',
        r'\1',
        text,
        flags=(
            re.IGNORECASE
            | re.DOTALL
        )
    )

    # 남아있는 HTML 태그 제거
    text = re.sub(
        r'<[^>]+>',
        '',
        text
    )

    return unescape(
        text
    )


def telegram_safe(
    text,
    *,
    html=False
):

    if not html:

        telegram(
            text,
            html=False
        )

        return

    try:

        telegram(
            text,
            html=True
        )

    except RuntimeError as exc:

        error_text = str(
            exc
        )

        html_errors = (
            'ENTITIES_TOO_LONG',
            "can't parse entities",
            'entity',
            'ENTITY',
        )

        if not any(
            marker in error_text
            for marker in html_errors
        ):

            raise

        print(
            'Telegram HTML 전송 실패 → '
            '일반 텍스트로 재전송합니다.',
            file=sys.stderr
        )

        plain = html_to_plain(
            text
        )

        telegram(
            plain,
            html=False
        )


# --------------------------------------------------
# URL 단축 표시
# --------------------------------------------------

def linked_chunks(text):

    result = []

    part = ''

    size = 0

    links = 0


    def append(
        rendered,
        visible_size,
        is_link=False
    ):

        nonlocal part
        nonlocal size
        nonlocal links

        if part and (

            size + visible_size
            >
            TELEGRAM_MESSAGE_LIMIT

            or

            (
                is_link
                and
                links >= 50
            )
        ):

            result.append(
                part
            )

            part = ''

            size = 0

            links = 0

        part += rendered

        size += visible_size

        links += int(
            is_link
        )


    def plain(value):

        for char in value:

            append(
                escape(
                    char,
                    quote=False
                ),

                2
                if ord(char) > 0xFFFF
                else 1
            )


    cursor = 0

    for match in re.finditer(
        r'https?://[^\s<>\x22]+',
        text,
        re.IGNORECASE
    ):

        # URL 앞 일반 텍스트
        plain(
            text[
                cursor:
                match.start()
            ]
        )

        url = match.group()

        suffix = ''

        # URL 뒤 문장부호 분리
        while url and (

            url[-1] in '.,;!'

            or

            (
                url[-1] == ')'
                and
                url.count(')')
                >
                url.count('(')
            )

            or

            (
                url[-1] == ']'
                and
                url.count(']')
                >
                url.count('[')
            )
        ):

            suffix = (
                url[-1]
                + suffix
            )

            url = url[:-1]


        try:

            host = urlsplit(
                url
            ).hostname

        except ValueError:

            host = None


        if host:

            display_host = (

                host

                if len(host) <= 32

                else

                host[:29]
                + '…'
            )


            # 너무 긴 URL은 링크 entity를
            # 만들지 않습니다.
            if (
                len(url)
                >
                MAX_TELEGRAM_LINK_LENGTH
            ):

                label = (
                    '🔗 긴 링크 생략 · '
                    + display_host
                )

                plain(
                    label
                )

            else:

                label = (
                    '🔗 링크 열기 · '
                    + display_host
                )

                rendered = (
                    '<a href="'
                    + escape(
                        url,
                        quote=True
                    )
                    + '">'
                    + escape(
                        label
                    )
                    + '</a>'
                )

                visible_size = (
                    len(
                        label.encode(
                            'utf-16-le'
                        )
                    )
                    // 2
                )

                append(
                    rendered,
                    visible_size,
                    True
                )


            plain(
                suffix
            )

        else:

            plain(
                match.group()
            )


        cursor = (
            match.end()
        )


    # 마지막 텍스트
    plain(
        text[cursor:]
    )

    if part:

        result.append(
            part
        )

    return result


# --------------------------------------------------
# Telegram 전달
# --------------------------------------------------

def deliver(
    state,
    key,
    text,
    deadline
):

    record = state[
        'sent'
    ].get(
        key,
        {}
    )


    if record.get(
        'done'
    ):

        return True


    # 메시지 포맷 버전
    digest = hashlib.sha256(
        (
            'naver-two-line-summary-v1\n'
            + text
        ).encode()
    ).hexdigest()


    start = (

        record.get(
            'next',
            0
        )

        if record.get(
            'hash'
        ) == digest

        else 0
    )


    pieces = linked_chunks(
        text
    )


    for index in range(
        start,
        len(pieces)
    ):

        if (
            time.monotonic()
            >
            deadline
        ):

            return False


        # 메시지가 한 개라면
        # (1/1) 표시 생략
        if len(pieces) == 1:

            message_text = (
                pieces[index]
            )

        else:

            message_text = (
                f'({index + 1}/'
                f'{len(pieces)})\n'
                + pieces[index]
            )


        telegram_safe(
            message_text,
            html=True
        )


        state[
            'sent'
        ][key] = {

            'hash':
                digest,

            'next':
                index + 1
        }


        save(
            state
        )

        time.sleep(
            1.1
        )


    state[
        'sent'
    ][key] = {
        'done':
            True
    }


    save(
        state
    )

    return True


# --------------------------------------------------
# HTML 메일 → 일반 텍스트
# --------------------------------------------------

class PlainHTML(
    HTMLParser
):

    def __init__(self):

        super().__init__()

        self.parts = []

        self.hidden = 0

        self.anchor = None


    def handle_starttag(
        self,
        tag,
        attrs
    ):

        if tag in (
            'script',
            'style',
            'head'
        ):

            self.hidden += 1


        if (
            not self.hidden

            and

            tag in (
                'br',
                'p',
                'div',
                'li',
                'tr'
            )
        ):

            self.parts.append(
                '\n'
            )


        if (
            not self.hidden
            and
            tag == 'a'
        ):

            href = dict(
                attrs
            ).get(
                'href',
                ''
            )

            self.anchor = (

                (
                    href,
                    len(
                        self.parts
                    )
                )

                if href.lower().startswith(
                    (
                        'https://',
                        'http://'
                    )
                )

                else None
            )


    def handle_endtag(
        self,
        tag
    ):

        if (
            tag == 'a'
            and
            self.anchor
        ):

            href, start = (
                self.anchor
            )


            if href not in ''.join(
                self.parts[
                    start:
                ]
            ):

                self.parts.append(
                    ' <'
                    + href
                    + '>'
                )


            self.anchor = None


        if tag in (
            'script',
            'style',
            'head'
        ):

            self.hidden = max(
                0,
                self.hidden - 1
            )


        if (
            not self.hidden

            and

            tag in (
                'p',
                'div',
                'li',
                'tr'
            )
        ):

            self.parts.append(
                '\n'
            )


    def handle_data(
        self,
        data
    ):

        if not self.hidden:

            self.parts.append(
                data
            )


# --------------------------------------------------
# 메일 본문 추출
# --------------------------------------------------

def body(message):

    part = message.get_body(
        preferencelist=(
            'plain',
            'html'
        )
    )


    if part is None:

        return (
            '(텍스트 본문 없음)'
        )


    try:

        text = (
            part.get_content()
        )

    except (
        LookupError,
        UnicodeError
    ):

        text = (
            part.get_payload(
                decode=True
            )
            or b''
        ).decode(
            'utf-8',
            errors='replace'
        )


    if (
        part.get_content_type()
        ==
        'text/html'
    ):

        parser = PlainHTML()

        parser.feed(
            text
        )

        text = '\n'.join(

            line.strip()

            for line

            in ''.join(
                parser.parts
            ).splitlines()

            if line.strip()
        )


    return (

        text
        .replace(
            '\x00',
            ''
        )
        .strip()

        or

        '(텍스트 본문 없음)'
    )


# --------------------------------------------------
# ★ 요약용 텍스트 정리
# --------------------------------------------------

def clean_summary_text(text):

    if not text:

        return ''


    text = unescape(
        text
    )


    # URL 제거
    text = re.sub(
        r'https?://\S+',
        ' ',
        text,
        flags=re.IGNORECASE
    )


    # <URL> 제거
    text = re.sub(
        r'<https?://[^>]+>',
        ' ',
        text,
        flags=re.IGNORECASE
    )


    # 이메일 주소 제거
    text = re.sub(
        r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b',
        ' ',
        text
    )


    # 반복 공백 정리
    text = re.sub(
        r'[ \t]+',
        ' ',
        text
    )


    # 과도한 줄바꿈 정리
    text = re.sub(
        r'\n{3,}',
        '\n\n',
        text
    )


    return text.strip()


# --------------------------------------------------
# ★ 의미 없는 문장 제외
# --------------------------------------------------

def useful_summary_line(text):

    value = text.strip()

    if not value:

        return False


    if len(value) < 5:

        return False


    lower = value.lower()


    ignore_words = (

        'unsubscribe',

        '수신거부',

        '수신 거부',

        '메일 수신 거부',

        '광고입니다',

        '본 메일은 발신전용',

        '본 메일은 발신 전용',

        '발신전용',

        '발신 전용',

        '개인정보처리방침',

        '개인정보 처리방침',

        'copyright',

        'all rights reserved',

        'view in browser',

        '웹에서 보기',

        '이메일을 표시할 수 없습니다',

        '메일 수신을 원하지',

        '본 메일은 회원님',

        '본 메일은 정보통신망',
    )


    if any(
        word in lower
        for word in ignore_words
    ):

        return False


    return True


# --------------------------------------------------
# ★ 요약 문장 길이 제한
# --------------------------------------------------

def shorten_summary_line(
    text,
    max_length=SUMMARY_LINE_LENGTH
):

    text = re.sub(
        r'\s+',
        ' ',
        text
    ).strip()


    if len(text) <= max_length:

        return text


    shortened = (

        text[
            :max_length
        ]

        .rsplit(
            ' ',
            1
        )[0]

        .strip()
    )


    # 한국어 문장처럼
    # 띄어쓰기가 적은 경우
    if len(shortened) < (
        max_length // 2
    ):

        shortened = (
            text[
                :max_length
            ]
            .strip()
        )


    return (
        shortened.rstrip(
            '.,;: '
        )
        + '…'
    )


# --------------------------------------------------
# ★ 메일 본문 2줄 요약
# --------------------------------------------------

def summarize_two_lines(text):

    text = clean_summary_text(
        text
    )


    if (
        not text

        or

        text
        ==
        '(텍스트 본문 없음)'
    ):

        return (
            '메일 본문을 확인해 주세요.',
            '자세한 내용은 아래 링크에서 볼 수 있습니다.'
        )


    candidates = []


    # --------------------------------------------------
    # 1차: 줄 단위로 후보 생성
    # --------------------------------------------------

    for line in text.splitlines():

        line = re.sub(
            r'\s+',
            ' ',
            line
        ).strip()


        if not useful_summary_line(
            line
        ):

            continue


        # 긴 줄을 문장 단위로 분리
        sentences = re.split(
            r'(?<=[.!?。！？])\s+',
            line
        )


        for sentence in sentences:

            sentence = (
                sentence.strip()
            )


            if useful_summary_line(
                sentence
            ):

                candidates.append(
                    sentence
                )


    # --------------------------------------------------
    # 후보가 부족하면 본문 전체에서 추가 추출
    # --------------------------------------------------

    if len(candidates) < 2:

        sentences = re.split(
            r'(?<=[.!?。！？])\s+|\n+',
            text
        )


        for sentence in sentences:

            sentence = re.sub(
                r'\s+',
                ' ',
                sentence
            ).strip()


            if not useful_summary_line(
                sentence
            ):

                continue


            if sentence not in candidates:

                candidates.append(
                    sentence
                )


            if len(candidates) >= 4:

                break


    # --------------------------------------------------
    # 최종 두 문장 선택
    # --------------------------------------------------

    summary = []


    for candidate in candidates:

        candidate = shorten_summary_line(
            candidate
        )


        if candidate in summary:

            continue


        summary.append(
            candidate
        )


        if len(summary) == 2:

            break


    if not summary:

        summary = [

            '새 메일이 도착했습니다.',

            '자세한 내용은 아래 링크에서 확인해 주세요.'
        ]


    elif len(summary) == 1:

        summary.append(
            '자세한 내용은 아래 링크에서 확인해 주세요.'
        )


    return (
        summary[0],
        summary[1]
    )


# --------------------------------------------------
# 네이버 메일 설정
# --------------------------------------------------

LABEL = '네이버'

HOST = 'imap.naver.com'

EMAIL_SECRET = (
    'NAVER_EMAIL'
)

PASSWORD_SECRET = (
    'NAVER_APP_PASSWORD'
)

TOKEN_SECRET = (
    'TELEGRAM_BOT_TOKEN'
)

MAIL_URL = (
    'https://mail.naver.com/'
)


# --------------------------------------------------
# 읽음 처리
# --------------------------------------------------

def mark_read(
    client,
    uid,
    state,
    key
):

    record = state[
        'sent'
    ].get(
        key,
        {}
    )


    if not record.get(
        'done'
    ):

        raise RuntimeError(
            '전송 완료 전 읽음 처리는 '
            '허용하지 않습니다.'
        )


    if record.get(
        'read'
    ):

        return


    status, _ = client.uid(
        'STORE',
        uid,
        '+FLAGS.SILENT',
        '(\\Seen)'
    )


    if status != 'OK':

        raise RuntimeError(
            '알림은 전송했지만 읽음 처리 실패. '
            '다음 실행에서 재시도합니다.'
        )


    record[
        'read'
    ] = True


    save(
        state
    )


# --------------------------------------------------
# 메인
# --------------------------------------------------

def main():

    # --------------------------------------------------
    # Secret 확인
    # --------------------------------------------------

    for name in (

        EMAIL_SECRET,

        PASSWORD_SECRET,

        TOKEN_SECRET,

        'TELEGRAM_CHAT_ID'
    ):

        if not os.environ.get(
            name,
            ''
        ).strip():

            raise RuntimeError(
                'Secret 누락: '
                + name
            )


    # 기존 환경변수 이름 유지
    os.environ[
        'TELEGRAM_TOKEN'
    ] = os.environ[
        TOKEN_SECRET
    ]


    # --------------------------------------------------
    # 상태 파일
    # --------------------------------------------------

    state = (

        json.loads(
            STATE.read_text(
                encoding='utf-8'
            )
        )

        if STATE.exists()

        else None
    )


    # GitHub Actions 실행 제한 대비
    deadline = (
        time.monotonic()
        + 360
    )


    # --------------------------------------------------
    # 네이버 IMAP 접속
    # --------------------------------------------------

    with imaplib.IMAP4_SSL(

        HOST,

        993,

        ssl_context=
            ssl.create_default_context(),

        timeout=45

    ) as client:


        client.login(

            os.environ[
                EMAIL_SECRET
            ],

            os.environ[
                PASSWORD_SECRET
            ]
        )


        if (
            client.select(
                'INBOX',
                readonly=False
            )[0]
            != 'OK'
        ):

            raise RuntimeError(
                '받은메일함 열기 실패. '
                '네이버 IMAP 사용 설정을 확인하세요.'
            )


        # --------------------------------------------------
        # UIDVALIDITY
        # --------------------------------------------------

        values = client.response(
            'UIDVALIDITY'
        )[1]


        if (
            not values

            or

            values[0] is None
        ):

            raise RuntimeError(
                '메일함 UIDVALIDITY 확인 실패'
            )


        validity = (
            values[0].decode()
        )


        # --------------------------------------------------
        # 모든 UID 검색
        # --------------------------------------------------

        status, data = client.uid(
            'SEARCH',
            None,
            'ALL'
        )


        if status != 'OK':

            raise RuntimeError(
                '메일 검색 실패'
            )


        uids = (

            data[0].split()

            if data[0]

            else []
        )


        # --------------------------------------------------
        # 최초 실행
        # --------------------------------------------------

        if state is None:

            telegram(
                f'✅ {LABEL} IMAP 전환 완료\n'
                '현재 받은메일함은 기준으로 등록했습니다.\n'
                '이후 새 메일은 제목과 2줄 요약으로 '
                '알림을 보내고 전송 성공 후 읽음 처리합니다.'
            )


            state = {

                'validity':
                    validity,

                'baseline':
                    max(
                        (
                            int(u)

                            for u
                            in uids
                        ),

                        default=0
                    ),

                'sent':
                    {}
            }


            save(
                state
            )


            print(
                'IMAP 최초 등록 완료. '
                '이후 새 메일부터 '
                '알림 및 읽음 처리합니다.'
            )


            return


        # --------------------------------------------------
        # UIDVALIDITY 확인
        # --------------------------------------------------

        if (
            state[
                'validity'
            ]
            != validity
        ):

            raise RuntimeError(
                '메일함 UIDVALIDITY가 변경됐습니다. '
                '중복/누락 방지를 위해 처리를 중단했습니다. '
                '기록을 삭제하지 말고 점검하세요.'
            )


        count = 0


        # --------------------------------------------------
        # 신규 메일 처리
        # --------------------------------------------------

        for uid in uids:


            if (
                int(uid)
                <=
                state[
                    'baseline'
                ]
            ):

                continue


            key = (
                uid.decode()
            )


            record = state[
                'sent'
            ].get(
                key,
                {}
            )


            # 이미 읽음 완료
            if record.get(
                'read'
            ):

                continue


            # 실행시간 제한
            if (
                time.monotonic()
                >
                deadline
            ):

                break


            # --------------------------------------------------
            # Telegram 전송이 아직 안 된 경우
            # --------------------------------------------------

            if not record.get(
                'done'
            ):


                status, data = client.uid(
                    'FETCH',
                    uid,
                    '(BODY.PEEK[])'
                )


                if status != 'OK':

                    raise RuntimeError(
                        '메일 본문 조회 실패'
                    )


                item = next(

                    (
                        x

                        for x in data

                        if isinstance(
                            x,
                            tuple
                        )
                    ),

                    None
                )


                # 검색 이후 다른 클라이언트에서
                # 이동/삭제된 경우
                if item is None:

                    continue


                message = BytesParser(
                    policy=
                        policy.default
                ).parsebytes(
                    item[1]
                )


                # --------------------------------------------------
                # 제목
                # --------------------------------------------------

                subject = str(
                    message.get(
                        'Subject',
                        '(제목 없음)'
                    )
                ).strip()


                # 제목 안의 줄바꿈 제거
                subject = re.sub(
                    r'\s+',
                    ' ',
                    subject
                )


                # --------------------------------------------------
                # 본문
                # --------------------------------------------------

                mail_body = body(
                    message
                )


                # --------------------------------------------------
                # ★ 2줄 요약
                # --------------------------------------------------

                summary1, summary2 = (
                    summarize_two_lines(
                        mail_body
                    )
                )


                # --------------------------------------------------
                # ★ Telegram 최종 메시지
                # --------------------------------------------------

                text = (

                    f"📩 {LABEL} 새 메일\n\n"

                    f"📌 제목\n"
                    f"{subject}\n\n"

                    f"📝 요약\n"
                    f"1. {summary1}\n"
                    f"2. {summary2}\n\n"

                    f"🔗 자세히 보기\n"
                    f"{MAIL_URL}"
                )


                # --------------------------------------------------
                # Telegram 전송
                # --------------------------------------------------

                if not deliver(
                    state,
                    key,
                    text,
                    deadline
                ):

                    break


            # --------------------------------------------------
            # Telegram 전송 성공 후에만 읽음 처리
            # --------------------------------------------------

            mark_read(
                client,
                uid,
                state,
                key
            )


            count += 1


        print(
            f'{LABEL}: '
            f'알림 전송 및 읽음 처리 '
            f'{count}통 완료'
        )


# --------------------------------------------------
# 실행
# --------------------------------------------------

if __name__ == '__main__':

    try:

        main()


    except imaplib.IMAP4.error as exc:

        detail = str(
            exc
        )


        # Secret 노출 방지
        for name in (

            EMAIL_SECRET,

            PASSWORD_SECRET,

            TOKEN_SECRET
        ):

            value = os.environ.get(
                name,
                ''
            ).strip()


            if value:

                detail = detail.replace(
                    value,
                    '[숨김]'
                )


        print(
            'IMAP 연결/인증 오류: '
            + detail,
            file=sys.stderr
        )


        sys.exit(
            1
        )


    except Exception as exc:

        print(

            (
                str(exc)

                if isinstance(
                    exc,
                    RuntimeError
                )

                else

                '실행 실패: '
                + type(
                    exc
                ).__name__
            ),

            file=sys.stderr
        )


        sys.exit(
            1
        )
