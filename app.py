import os
import base64
import json
import re
import threading
import requests
from flask import Flask, request, abort
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, ImageMessageContent, TextMessageContent, FollowEvent
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    QuickReply,
    QuickReplyItem,
    MessageAction,
)
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

_config = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# In-memory session store: user_id -> {"state": str, "card": dict, "editing_key": str|None}
user_sessions: dict = {}

# Ordered editable fields: (display_label, dict_key)
CARD_FIELDS = [
    ("縣市",       "縣市"),
    ("公司/單位",  "公司_單位"),
    ("名字",       "名字"),
    ("名字羅馬拼音","名字_羅馬拼音"),
    ("部門",       "部門"),
    ("職稱",       "職稱"),
    ("Email",      "email"),
    ("電話",       "電話"),
    ("住所",       "住所"),
]

CONFIRM_QR = QuickReply(items=[
    QuickReplyItem(action=MessageAction(label="✅ 確認正確", text="確認正確")),
    QuickReplyItem(action=MessageAction(label="✏️ 修改資料", text="修改資料")),
])


# ── OpenAI ────────────────────────────────────────────────────────────────────

def analyze_business_card(image_content: bytes) -> dict:
    image_base64 = base64.b64encode(image_content).decode("utf-8")

    prompt = """請分析這張名片圖片，提取以下資訊，以 JSON 格式回傳：
{
  "縣市": "",
  "公司_單位": "",
  "名字": "",
  "名字_羅馬拼音": "",
  "部門": "",
  "職稱": "",
  "email": "",
  "電話": "",
  "住所": ""
}

說明：
- 縣市：日本都道府縣或城市，例如青森、福島、秋田、三重縣、北海道等
- 名字_羅馬拼音：姓名的羅馬字拼音（Romaji），例如 Kawada Ryota，名片上有標注時優先使用，否則依標準訓令式羅馬字推導
- 部門：完整組織層級，從最高層依序列出，以空格連接，例如「事業部 営業企画課」、「営業本部 第一営業部 営業二課」
- 找不到的欄位請留空字串
- 只回傳 JSON，不要其他說明文字"""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        max_tokens=600,
    )

    result_text = response.choices[0].message.content.strip()
    if "```" in result_text:
        parts = result_text.split("```")
        result_text = parts[1]
        if result_text.startswith("json"):
            result_text = result_text[4:]

    return json.loads(result_text.strip())


# ── Formatting ────────────────────────────────────────────────────────────────

def _name_with_romaji(info: dict) -> str:
    name = info.get("名字", "")
    romaji = info.get("名字_羅馬拼音", "")
    if name and romaji:
        return f"{name}（{romaji}）"
    return name


def format_card_info(info: dict) -> str:
    lines = ["📇 名片資訊掃描結果\n"]
    rows = [
        ("🗺️  縣市",    info.get("縣市", "")),
        ("🏢 公司/單位", info.get("公司_單位", "")),
        ("👤 名字",      _name_with_romaji(info)),
        ("🏬 部門",      info.get("部門", "")),
        ("💼 職稱",      info.get("職稱", "")),
        ("📧 Email",     info.get("email", "")),
        ("📞 電話",      info.get("電話", "")),
        ("📍 住所",      info.get("住所", "")),
    ]
    for label, value in rows:
        display = value if value else "（無資料）"
        lines.append(f"{label}：{display}")
    return "\n".join(lines)


def format_field_list(info: dict) -> str:
    lines = ["請回覆「修改 [編號]」選擇要修改的欄位：\n"]
    for i, (label, key) in enumerate(CARD_FIELDS, 1):
        value = info.get(key, "") or "（無資料）"
        lines.append(f"{i}. {label}：{value}")
    return "\n".join(lines)


# ── Messaging helpers ─────────────────────────────────────────────────────────

def push(user_id: str, text: str, quick_reply: QuickReply | None = None) -> None:
    msg = TextMessage(text=text, quick_reply=quick_reply)
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[msg])
        )


def reply(reply_token: str, text: str, quick_reply: QuickReply | None = None) -> None:
    msg = TextMessage(text=text, quick_reply=quick_reply)
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[msg])
        )


GHL_WEBHOOK_URL = "https://services.leadconnectorhq.com/hooks/zY8JbOOVat7EkBLQyIkB/webhook-trigger/aeae4188-2510-4be6-b946-2b9968a7067c"


def send_to_ghl(card: dict) -> None:
    payload = {
        "prefecture":   card.get("縣市", ""),
        "company":      card.get("公司_單位", ""),
        "name":         card.get("名字", ""),
        "name_romaji":  card.get("名字_羅馬拼音", ""),
        "department":   card.get("部門", ""),
        "title":        card.get("職稱", ""),
        "email":        card.get("email", ""),
        "phone":        card.get("電話", ""),
        "address":      card.get("住所", ""),
    }
    resp = requests.post(GHL_WEBHOOK_URL, json=payload, timeout=10)
    resp.raise_for_status()


# ── Background card processing ────────────────────────────────────────────────

def process_card_async(user_id: str, image_data: bytes) -> None:
    try:
        card_info = analyze_business_card(image_data)
    except json.JSONDecodeError:
        push(user_id, "❌ 無法解析 OpenAI 回傳的格式，請再試一次。")
        return
    except Exception as e:
        push(user_id, f"❌ 分析失敗：{e}\n請確認上傳的是清晰的名片圖片。")
        return

    user_sessions[user_id] = {"state": "awaiting_confirm", "card": card_info, "editing_key": None}
    push(user_id, format_card_info(card_info), quick_reply=CONFIRM_QR)


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id

    with ApiClient(_config) as api_client:
        image_data = MessagingApiBlob(api_client).get_message_content(event.message.id)

    reply(event.reply_token, "🔍 正在分析名片，請稍候…")

    threading.Thread(
        target=process_card_async, args=(user_id, image_data), daemon=True
    ).start()


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    session = user_sessions.get(user_id)

    # ── 確認正確 ──────────────────────────────────────────────
    if text == "確認正確":
        if not session:
            reply(event.reply_token, "⚠️ 找不到名片資料，請重新傳送名片圖片。")
            return
        card = session["card"]
        user_sessions.pop(user_id, None)
        try:
            send_to_ghl(card)
            reply(event.reply_token, "✅ 資料已確認並成功送出！")
        except Exception as e:
            reply(event.reply_token, f"✅ 資料已確認，但送出 GHL 時發生錯誤：{e}")
        return

    # ── 修改資料（顯示欄位清單）────────────────────────────────
    if text == "修改資料":
        if not session:
            reply(event.reply_token, "⚠️ 找不到名片資料，請重新傳送名片圖片。")
            return
        session["state"] = "awaiting_field_selection"
        reply(event.reply_token, format_field_list(session["card"]))
        return

    # ── 修改 [編號]：接受「修改 3」或直接輸入「3」─────────────
    match = re.fullmatch(r"修改\s*(\d+)", text) or (
        session and session["state"] == "awaiting_field_selection" and re.fullmatch(r"(\d+)", text)
    )
    if match and session and session["state"] == "awaiting_field_selection":
        idx = int(match.group(1)) - 1
        if not (0 <= idx < len(CARD_FIELDS)):
            reply(event.reply_token, f"⚠️ 請輸入 1 到 {len(CARD_FIELDS)} 之間的編號。")
            return
        label, key = CARD_FIELDS[idx]
        session["state"] = "awaiting_new_value"
        session["editing_key"] = key
        reply(event.reply_token, f"請輸入新的「{label}」：")
        return

    # ── 輸入新值 ──────────────────────────────────────────────
    if session and session["state"] == "awaiting_new_value":
        key = session["editing_key"]
        session["card"][key] = text
        session["state"] = "awaiting_confirm"
        session["editing_key"] = None
        push(user_id, format_card_info(session["card"]), quick_reply=CONFIRM_QR)
        # reply token used to ack immediately
        reply(event.reply_token, "已更新，請確認以下資料：")
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
