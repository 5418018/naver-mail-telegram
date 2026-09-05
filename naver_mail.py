import hashlib
import json
import os
import poplib
import ssl
import sys
import time

from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PROCESSED_FILE = Path("processed_uidls.txt")
PROGRESS_FILE = Path("telegram_progress.json")

# 한 번에 오래 실행되지 않도록 제한합니다.
# 남은 메일은 다음 실행에서 이어서 처리합니다.
RUN_SECONDS = 360

# 이모지까지 고려하여 Telegram 길이 제한보다 여유 있게 분할
CHUNK_UTF16_UNITS = 3400


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"GitHub Secret이 비어 있습니다: {name}")
    return value


def atomic_write(path, text):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def save_state(processed, progress):
    atomic_write(
        PROCESSED_FILE,
        "\n".join(sorted(processed)) + "\n",
    )
    atomic_write(
        PROGRESS_FILE,
        json.dumps(progress, ensure_ascii=False, indent=2),
    )


class HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag in ("script", "style", "head"):
            self.hidden_depth += 1
            return

        if self.hidden_depth:
            return

        if tag in ("br", "p", "div", "tr", "li", "hr", "h1", "h2", "h3"):
            self.parts.append("\n")

        if tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in ("script", "style", "head"):
            self.hidden_depth = max(0, self.hidden_depth - 1)
            return

        if not self.hidden_depth and tag in (
            "p", "div", "tr", "li", "h1", "h2", "h3"
        ):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)

    def text(self):
        lines = [
            line.strip()
            for line in "".join(self.parts).splitlines()
        ]

        result = []
        for line in lines:
            if line or (result and result[-1]):
                result.append(line)

        return "\n".join(result).strip()


def decode_part(part):
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except (LookupError, UnicodeError):
        pass

    raw = part.get_payload(decode=True)
    if raw is None:
        content = part.get_payload()
        return content if isinstance(content, str) else ""

    charset = part.get_content_charset() or "utf-8"

    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def mail_body(message):
    part = message.get_body(preferencelist=("plain", "html"))

    if part is None:
        return "(텍스트 본문이 없습니다. 이미지나 첨부파일을 확인하세요.)"

    content = decode_part(part)

    if part.get_content_type() == "text/html":
        parser = HTMLToText()
        parser.feed(content)
        parser.close()
        content = parser.text()

    content = content.replace("\x00", "").strip()

    return content or "(텍스트 본문이 없습니다.)"


def mail_text(message):
    attachments = []

    for part in message.walk():
        filename = part.get_filename()
        if filename:
            attachments.append(str(filename))

    attachment_text = ""
    if attachments:
        attachment_text = (
            "\n\n📎 첨부파일 이름\n"
            + "\n".join("- " + name for name in attachments)
            + "\n※ 첨부파일 내용은 네이버 메일에서 확인하세요."
        )

    return (
        "📩 네이버 새 메일\n\n"
        f"👤 보낸 사람: {message.get('From', '(알 수 없음)')}\n"
        f"📝 제목: {message.get('Subject', '(제목 없음)')}\n"
        f"🕒 메일 날짜: {message.get('Date', '(알 수 없음)')}\n\n"
        "──────────────\n"
        f"{mail_body(message)}"
        f"{attachment_text}\n\n"
        "📬 네이버 메일 열기\n"
        "https://mail.naver.com/"
    )


def split_text(text):
    chunks = []
    current = []
    units = 0

    for character in text:
        size = 2 if ord(character) > 0xFFFF else 1

        if current and units + size > CHUNK_UTF16_UNITS:
            chunks.append("".join(current))
            current = []
            units = 0

        current.append(character)
        units += size

    if current:
        chunks.append("".join(current))

    return chunks


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    data = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "link_preview_options": {
            "is_disabled": True
        },
    }).encode("utf-8")

    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=40) as response:
            result = json.loads(response.read().decode("utf-8"))

    except HTTPError as error:
        # 오류 URL에 봇 토큰이 포함되므로 원문 오류를 출력하지 않습니다.
        raise RuntimeError(
            f"텔레그램 전송 실패: HTTP {error.code}"
        ) from None

    except Exception:
        raise RuntimeError(
            "텔레그램 연결 실패. 다음 실행에서 재시도합니다."
        ) from None

    if not result.get("ok"):
        raise RuntimeError("텔레그램이 메시지를 접수하지 않았습니다.")


def main():
    email_address = required_env("NAVER_EMAIL")
    app_password = required_env("NAVER_APP_PASSWORD")
    telegram_token = required_env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = required_env("TELEGRAM_CHAT_ID")

    deadline = time.monotonic() + RUN_SECONDS

    # 기존 파일은 UIDL이 한 줄에 하나씩 저장되어 있다고 가정합니다.
    had_state = PROCESSED_FILE.exists()

    processed = set()
    if had_state:
        processed = {
            line.strip()
            for line in PROCESSED_FILE.read_text(
                encoding="utf-8-sig"
            ).splitlines()
            if line.strip()
        }

    progress = {}
    if PROGRESS_FILE.exists():
        progress = json.loads(
            PROGRESS_FILE.read_text(encoding="utf-8")
        )
        if not isinstance(progress, dict):
            raise RuntimeError("전송 진행 기록 형식이 올바르지 않습니다.")

    client = None

    try:
        client = poplib.POP3_SSL(
            "pop.naver.com",
            995,
            timeout=40,
            context=ssl.create_default_context(),
        )

        client.user(email_address)
        client.pass_(app_password)

        _, lines, _ = client.uidl()

        mailbox = []
        for line in lines:
            number, uid = line.decode("ascii").split(maxsplit=1)
            mailbox.append((int(number), uid))

        # 처리 이력이 없는 최초 설치:
        # 기존 메일을 대량 전송하지 않고 현재 시점부터 시작합니다.
        if not had_state:
            send_telegram(
                telegram_token,
                telegram_chat_id,
                "✅ 네이버 메일 알림 연결 완료\n\n"
                "현재 메일은 기존 메일로 등록했습니다.\n"
                "다음 실행부터 새 메일의 제목과 본문을 전송합니다.\n\n"
                "확인 주기: 약 5분\n"
                "GitHub 실행 상황에 따라 지연될 수 있습니다.",
            )

            processed.update(uid for _, uid in mailbox)
            save_state(processed, progress)

            print("최초 등록 완료. 다음 실행부터 새 메일을 알립니다.")
            return

        sent_count = 0

        for number, uid in mailbox:
            if uid in processed:
                continue

            if time.monotonic() >= deadline:
                print("이번 실행을 마칩니다. 남은 메일은 다음에 처리합니다.")
                break

            _, raw_lines, _ = client.retr(number)
            raw_message = b"\r\n".join(raw_lines)

            message = BytesParser(
                policy=policy.default
            ).parsebytes(raw_message)

            text = mail_text(message)
            chunks = split_text(text)

            # 코드 변경 등으로 내용이 달라졌으면 처음부터 전송합니다.
            digest = hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest()

            record = progress.get(uid, {})
            start = 0

            if record.get("digest") == digest:
                start = int(record.get("next", 0))

            for index in range(start, len(chunks)):
                if time.monotonic() >= deadline:
                    print("분할 전송을 다음 실행에서 이어갑니다.")
                    return

                prefix = ""
                if len(chunks) > 1:
                    prefix = (
                        f"📄 메일 본문 "
                        f"({index + 1}/{len(chunks)})\n\n"
                    )

                send_telegram(
                    telegram_token,
                    telegram_chat_id,
                    prefix + chunks[index],
                )

                # 성공한 조각을 기록하여 다음 실행에서 이어서 전송
                progress[uid] = {
                    "digest": digest,
                    "next": index + 1,
                }
                save_state(processed, progress)

                time.sleep(1.1)

            processed.add(uid)
            progress.pop(uid, None)
            save_state(processed, progress)
            sent_count += 1

        print(f"새 메일 {sent_count}통 전송 완료")

    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # 계정 정보나 토큰이 로그에 노출되지 않도록 제한
        if isinstance(error, RuntimeError):
            print(f"오류: {error}", file=sys.stderr)
        else:
            print(
                f"메일 처리 실패: {type(error).__name__}. "
                "메일 설정 및 연결 상태를 확인하세요.",
                file=sys.stderr,
            )
        sys.exit(1)
