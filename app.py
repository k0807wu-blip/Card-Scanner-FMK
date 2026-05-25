import os
import base64
import json
import re
import threading
import requests
from flask import Flask, request, abort
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, ImageMessageContent, TextMessageContent
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexBubble,
    FlexBox,
    FlexText,
    FlexButton,
    FlexSeparator,
    MessageAction,
)
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

_config = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# In-memory session store
# user_id -> {"state": str, "card": dict, "editing_key": str|None}
user_sessions: dict = {}

CARD_FIELDS = [
    ("縣市",        "縣市"),
    ("公司/單位",   "公司_單位"),
    ("名字",        "名字"),
    ("名字羅馬拼音","名字_羅馬拼音"),
    ("部門",        "部門"),
    ("職稱",        "職稱"),
    ("Email",       "email"),
    ("電話",        "電話"),
    ("住所",        "住所"),
]

GHL_WEBHOOK_URL = (
    "https://services.leadconnectorhq.com/hooks/yizY5nSxtIyjnf8qVXIY"
    "/webhook-trigger/hXjEXArKSyC7fqdymDyH"
)


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
- 名字_羅馬拼音：姓名的羅馬字拼音（Romaji），名片上有標注時優先使用，否則依訓令式羅馬字推導
- 部門：完整組織層級，從最高層依序列出以空格連接，例如「事業部 営業企画課」
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


def merge_card_info(front: dict, back: dict) -> dict:
    """正面優先，正面空白的欄位用背面補入"""
    merged = dict(front)
    for key, value in back.items():
        if not merged.get(key) and value:
            merged[key] = value
    return merged


# ── Formatting ────────────────────────────────────────────────────────────────

def _name_with_romaji(info: dict) -> str:
    name = info.get("名字", "")
    romaji = info.get("名字_羅馬拼音", "")
    if name and romaji:
        return f"{name}（{romaji}）"
    return name


def build_card_flex(info: dict, title: str = "📇 名片資訊掃描結果") -> FlexMessage:
    """Flex Message：資料內嵌在訊息泡泡，按鈕永久顯示在底部"""

    def row(label: str, value: str) -> FlexText:
        display = value if value else "（無資料）"
        return FlexText(
            text=f"{label}：{display}",
            wrap=True,
            size="sm",
            color="#444444",
        )

    body_contents = [
        FlexText(text=title, weight="bold", size="md", color="#1a1a1a"),
        FlexSeparator(margin="sm"),
        row("🗺️  縣市",    info.get("縣市", "")),
        row("🏢 公司/單位", info.get("公司_單位", "")),
        row("👤 名字",      _name_with_romaji(info)),
        row("🏬 部門",      info.get("部門", "")),
        row("💼 職稱",      info.get("職稱", "")),
        row("📧 Email",     info.get("email", "")),
        row("📞 電話",      info.get("電話", "")),
        row("📍 住所",      info.get("住所", "")),
    ]

    footer_contents = [
        FlexButton(
            action=MessageAction(label="✅ 確認正確", text="確認正確"),
            style="primary",
        ),
        FlexButton(
            action=MessageAction(label="✏️ 修改資料", text="修改資料"),
            style="secondary",
        ),
        FlexButton(
            action=MessageAction(label="📷 上傳背面", text="上傳背面"),
            style="secondary",
        ),
    ]

    bubble = FlexBubble(
        body=FlexBox(layout="vertical", spacing="sm", contents=body_contents),
        footer=FlexBox(layout="vertical", spacing="sm", contents=footer_contents),
    )

    return FlexMessage(alt_text=title, contents=bubble)


def format_field_list(info: dict) -> str:
    lines = ["請直接回覆欄位編號選擇要修改的欄位：\n"]
    for i, (label, key) in enumerate(CARD_FIELDS, 1):
        value = info.get(key, "") or "（無資料）"
        lines.append(f"{i}. {label}：{value}")
    return "\n".join(lines)


# ── Messaging helpers ─────────────────────────────────────────────────────────

def push_flex(user_id: str, flex: FlexMessage) -> None:
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[flex])
        )


def push_text(user_id: str, text: str) -> None:
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
        )


def reply_text(reply_token: str, text: str) -> None:
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )


# ── GHL ───────────────────────────────────────────────────────────────────────

def send_to_ghl(card: dict) -> None:
    payload = {
        "prefecture":  card.get("縣市", ""),
        "company":     card.get("公司_單位", ""),
        "name":        card.get("名字", ""),
        "name_romaji": card.get("名字_羅馬拼音", ""),
        "department":  card.get("部門", ""),
        "title":       card.get("職稱", ""),
        "email":       card.get("email", ""),
        "phone":       card.get("電話", ""),
        "address":     card.get("住所", ""),
    }
    resp = requests.post(GHL_WEBHOOK_URL, json=payload, timeout=10)
    resp.raise_for_status()


# ── Background processing ─────────────────────────────────────────────────────

def process_front_async(user_id: str, image_data: bytes) -> None:
    try:
        card_info = analyze_business_card(image_data)
    except json.JSONDecodeError:
        push_text(user_id, "❌ 無法解析 OpenAI 回傳的格式，請再試一次。")
        return
    except Exception as e:
        push_text(user_id, f"❌ 分析失敗：{e}\n請確認上傳的是清晰的名片圖片。")
        return

    user_sessions[user_id] = {
        "state": "awaiting_confirm",
        "card": card_info,
        "editing_key": None,
    }
    push_flex(user_id, build_card_flex(card_info))


def process_back_async(user_id: str, image_data: bytes) -> None:
    session = user_sessions.get(user_id)
    if not session:
        push_text(user_id, "⚠️ 找不到正面資料，請重新傳送名片正面。")
        return

    try:
        back_info = analyze_business_card(image_data)
    except json.JSONDecodeError:
        push_text(user_id, "❌ 背面無法解析，請再試一次。")
        return
    except Exception as e:
        push_text(user_id, f"❌ 背面分析失敗：{e}")
        return

    merged = merge_card_info(session["card"], back_info)
    session["card"] = merged
    session["state"] = "awaiting_confirm"
    push_flex(user_id, build_card_flex(merged, title="📇 名片資訊（正面＋背面合併）"))


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
    session = user_sessions.get(user_id)

    with ApiClient(_config) as api_client:
        image_data = MessagingApiBlob(api_client).get_message_content(event.message.id)

    # 判斷：正在等背面 → 處理背面；否則當正面處理
    if session and session.get("state") == "awaiting_back_image":
        reply_text(event.reply_token, "🔍 正在分析背面，請稍候…")
        threading.Thread(
            target=process_back_async, args=(user_id, image_data), daemon=True
        ).start()
    else:
        reply_text(event.reply_token, "🔍 正在分析名片，請稍候…")
        threading.Thread(
            target=process_front_async, args=(user_id, image_data), daemon=True
        ).start()


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    session = user_sessions.get(user_id)

    # ── 確認正確 ──────────────────────────────────────────────
    if text == "確認正確":
        if not session:
            reply_text(event.reply_token, "⚠️ 找不到名片資料，請重新傳送名片圖片。")
            return
        card = session["card"]
        user_sessions.pop(user_id, None)
        try:
            send_to_ghl(card)
            reply_text(event.reply_token, "✅ 資料已確認並成功送出！")
        except Exception as e:
            reply_text(event.reply_token, f"✅ 資料已確認，但送出 GHL 時發生錯誤：{e}")
        return

    # ── 修改資料 ──────────────────────────────────────────────
    if text == "修改資料":
        if not session:
            reply_text(event.reply_token, "⚠️ 找不到名片資料，請重新傳送名片圖片。")
            return
        session["state"] = "awaiting_field_selection"
        reply_text(event.reply_token, format_field_list(session["card"]))
        return

    # ── 上傳背面 ──────────────────────────────────────────────
    if text == "上傳背面":
        if not session:
            reply_text(event.reply_token, "⚠️ 找不到名片資料，請重新傳送名片正面圖片。")
            return
        session["state"] = "awaiting_back_image"
        reply_text(event.reply_token, "請傳送名片背面圖片📷")
        return

    # ── 修改 [編號]：接受「修改 3」或直接輸入「3」─────────────
    match = re.fullmatch(r"修改\s*(\d+)", text) or (
        session and session["state"] == "awaiting_field_selection"
        and re.fullmatch(r"(\d+)", text)
    )
    if match and session and session["state"] == "awaiting_field_selection":
        idx = int(match.group(1)) - 1
        if not (0 <= idx < len(CARD_FIELDS)):
            reply_text(event.reply_token, f"⚠️ 請輸入 1 到 {len(CARD_FIELDS)} 之間的編號。")
            return
        label, key = CARD_FIELDS[idx]
        session["state"] = "awaiting_new_value"
        session["editing_key"] = key
        reply_text(event.reply_token, f"請輸入新的「{label}」：")
        return

    # ── 輸入新值 ──────────────────────────────────────────────
    if session and session["state"] == "awaiting_new_value":
        key = session["editing_key"]
        session["card"][key] = text
        session["state"] = "awaiting_confirm"
        session["editing_key"] = None
        reply_text(event.reply_token, "已更新，請確認以下資料：")
        push_flex(user_id, build_card_flex(session["card"]))
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
