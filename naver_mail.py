import os
import poplib
import email
import html
import urllib.parse
import urllib.request
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

# ============================================================
# 네이버 메일 → Telegram 새 메일 알림
# ============================================================

NAVER_EMAIL = os.environ["NAVER_EMAIL"]
NAVER_APP_PASSWORD = os.environ["NAVER_APP_PASSWORD"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

POP_SERVER = "pop.naver.com"
POP_PORT = 995

KST = timezone(timedelta(hours=9))


# ------------------------------------------------------------
# 메일 제목/보낸사람 한글 디코딩
# ------------------------------------------------------------
def decode_text(value):
    if not value:
        return ""

    result = ""

    for text, charset in decode_header(value):
        if isinstance(text, bytes):
            try:
                result += text.decode(charset or "utf-8", errors="replace")
            except Exception:
                result += text.decode("utf-8", errors="replace")
        else:
            result += text

    return result


# ------------------------------------------------------------
# Telegram 메시지 전송
# ------------------------------------------------------------
def send_telegram(text):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true"
    }).encode("utf-8")

    request = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


# ------------------------------------------------------------
# 메일 날짜 가져오기
# ------------------------------------------------------------
def get_mail_datetime(msg):
    try:
        dt = parsedate_to_datetime(msg.get("Date"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(KST)

    except Exception:
        return None


# ------------------------------------------------------------
# 메인
# ------------------------------------------------------------
def main():

    print("네이버 메일 서버에 접속합니다.")

    pop = poplib.POP3_SSL(
        POP_SERVER,
        POP_PORT,
        timeout=30
    )

    try:
        pop.user(NAVER_EMAIL)
        pop.pass_(NAVER_APP_PASSWORD)

        message_count, mailbox_size = pop.stat()

        print(f"현재 메일 수: {message_count}")

        if message_count == 0:
            print("메일이 없습니다.")
            return

        # ----------------------------------------------------
        # GitHub Actions 실행 시점 기준 최근 메일 확인
        # ----------------------------------------------------
        now = datetime.now(KST)

        # GitHub Actions를 5분마다 실행할 예정이므로
        # 약간의 지연을 고려하여 최근 10분 메일을 확인
        cutoff = now - timedelta(minutes=10)

        # 너무 많은 메일을 읽지 않도록 최근 20개만 검사
        start = max(1, message_count - 19)

        found = 0

        for i in range(start, message_count + 1):

            response, lines, octets = pop.retr(i)

            raw_email = b"\r\n".join(lines)

            msg = email.message_from_bytes(raw_email)

            mail_datetime = get_mail_datetime(msg)

            if mail_datetime is None:
                continue

            # 최근 10분 이전 메일은 제외
            if mail_datetime < cutoff:
                continue

            subject = decode_text(msg.get("Subject"))
            sender = decode_text(msg.get("From"))

            subject = html.escape(subject)
            sender = html.escape(sender)

            date_text = mail_datetime.strftime(
                "%Y-%m-%d %H:%M"
            )

            telegram_message = (
                "📩 <b>네이버 새 메일</b>\n\n"
                f"👤 <b>보낸사람</b>\n{sender}\n\n"
                f"📌 <b>제목</b>\n{subject}\n\n"
                f"🕐 <b>수신시간</b>\n{date_text}"
            )

            send_telegram(telegram_message)

            print(
                f"Telegram 전송 완료: "
                f"{decode_text(msg.get('Subject'))}"
            )

            found += 1

        if found == 0:
            print("최근 새 메일이 없습니다.")

        else:
            print(f"총 {found}개의 메일을 전송했습니다.")

    finally:
        try:
            pop.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
