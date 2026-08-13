import os
import poplib
import email
import html
import urllib.parse
import urllib.request
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import timezone, timedelta

# ============================================================
# 네이버 메일 → Telegram 알림
# POP3 UIDL을 이용한 중복 알림 방지
# ============================================================

NAVER_EMAIL = os.environ["NAVER_EMAIL"]
NAVER_APP_PASSWORD = os.environ["NAVER_APP_PASSWORD"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

POP_SERVER = "pop.naver.com"
POP_PORT = 995

STATE_FILE = "processed_uidls.txt"

KST = timezone(timedelta(hours=9))


# ============================================================
# 한글 제목 / 발신자 디코딩
# ============================================================

def decode_text(value):
    if not value:
        return ""

    result = ""

    for text, charset in decode_header(value):

        if isinstance(text, bytes):
            try:
                result += text.decode(
                    charset or "utf-8",
                    errors="replace"
                )

            except Exception:
                result += text.decode(
                    "utf-8",
                    errors="replace"
                )

        else:
            result += text

    return result


# ============================================================
# Telegram 전송
# ============================================================

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

    request = urllib.request.Request(
        url,
        data=data
    )

    with urllib.request.urlopen(
        request,
        timeout=30
    ) as response:

        response.read()


# ============================================================
# 처리한 UIDL 불러오기
# ============================================================

def load_processed_uidls():

    if not os.path.exists(STATE_FILE):
        return set()

    with open(
        STATE_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return {
            line.strip()
            for line in f
            if line.strip()
        }


# ============================================================
# 처리한 UIDL 저장
# ============================================================

def save_processed_uidls(uidls):

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        for uidl in sorted(uidls):
            f.write(uidl + "\n")


# ============================================================
# 메일 수신시간
# ============================================================

def get_mail_date(msg):

    try:

        dt = parsedate_to_datetime(
            msg.get("Date")
        )

        if dt is None:
            return "시간 정보 없음"

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        dt = dt.astimezone(KST)

        return dt.strftime(
            "%Y-%m-%d %H:%M"
        )

    except Exception:

        return "시간 정보 없음"


# ============================================================
# 메인
# ============================================================

def main():

    print("네이버 메일 서버에 접속합니다.")

    pop = poplib.POP3_SSL(
        POP_SERVER,
        POP_PORT,
        timeout=30
    )

    try:

        # ----------------------------------------------------
        # 네이버 로그인
        # ----------------------------------------------------

        pop.user(NAVER_EMAIL)
        pop.pass_(NAVER_APP_PASSWORD)

        message_count, mailbox_size = pop.stat()

        print(
            f"현재 POP3 메일 수: "
            f"{message_count}"
        )

        # ----------------------------------------------------
        # POP3 UIDL 목록 가져오기
        # ----------------------------------------------------

        response, uidl_lines, octets = pop.uidl()

        current_messages = []

        for line in uidl_lines:

            parts = line.decode(
                "utf-8",
                errors="replace"
            ).split()

            if len(parts) >= 2:

                message_number = int(parts[0])
                uidl = parts[1]

                current_messages.append(
                    (
                        message_number,
                        uidl
                    )
                )

        current_uidls = {
            uidl
            for _, uidl in current_messages
        }

        # ----------------------------------------------------
        # 이전 처리 기록
        # ----------------------------------------------------

        processed_uidls = (
            load_processed_uidls()
        )

        # ----------------------------------------------------
        # 최초 실행
        # ----------------------------------------------------

        if not processed_uidls:

            print(
                "최초 실행입니다."
            )

            print(
                "현재 존재하는 메일은 "
                "기준점으로만 저장합니다."
            )

            processed_uidls.update(
                current_uidls
            )

            save_processed_uidls(
                processed_uidls
            )

            print(
                f"기존 메일 "
                f"{len(current_uidls)}개를 "
                "기준점으로 등록했습니다."
            )

            print(
                "이제부터 새로 도착하는 "
                "메일만 Telegram으로 전송됩니다."
            )

            return

        # ----------------------------------------------------
        # 새 메일 확인
        # ----------------------------------------------------

        new_messages = [
            (number, uidl)
            for number, uidl
            in current_messages
            if uidl not in processed_uidls
        ]

        if not new_messages:

            print(
                "새 메일이 없습니다."
            )

            return

        print(
            f"새 메일 {len(new_messages)}개 발견"
        )

        # ----------------------------------------------------
        # 새 메일 Telegram 전송
        # ----------------------------------------------------

        for message_number, uidl in new_messages:

            response, lines, octets = (
                pop.retr(message_number)
            )

            raw_email = b"\r\n".join(
                lines
            )

            msg = email.message_from_bytes(
                raw_email
            )

            subject = decode_text(
                msg.get("Subject")
            )

            sender = decode_text(
                msg.get("From")
            )

            date_text = get_mail_date(
                msg
            )

            safe_subject = html.escape(
                subject
            )

            safe_sender = html.escape(
                sender
            )

            telegram_message = (
                "📩 <b>네이버 새 메일</b>\n\n"
                f"👤 <b>보낸사람</b>\n"
                f"{safe_sender}\n\n"
                f"📌 <b>제목</b>\n"
                f"{safe_subject}\n\n"
                f"🕐 <b>수신시간</b>\n"
                f"{date_text}"
            )

            # Telegram 전송 성공 후에만
            # UIDL을 처리 완료로 기록
            send_telegram(
                telegram_message
            )

            processed_uidls.add(
                uidl
            )

            save_processed_uidls(
                processed_uidls
            )

            print(
                f"Telegram 전송 완료: "
                f"{subject}"
            )

        print(
            f"총 {len(new_messages)}개의 "
            f"새 메일을 처리했습니다."
        )

    finally:

        try:
            pop.quit()

        except Exception:
            pass


if __name__ == "__main__":
    main()
