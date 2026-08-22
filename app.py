import os
from datetime import datetime
from flask import Flask, request, abort
from dotenv import load_dotenv

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# .env ファイルの読み込み
load_dotenv()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY")

app = Flask(__name__)

# LINE APIの初期設定
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheetsの初期設定
import json

def get_gspread_client():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    # 環境変数からJSON文字列を読み込む（ローカルならファイルから読み込む）
    google_key_env = os.getenv("GOOGLE_KEY_JSON")
    if google_key_env:
        creds_dict = json.loads(google_key_env)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("google_key.json", scope)
    return gspread.authorize(creds)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    
    # 全角スペースを半角スペースに統一して分割
    items = text.replace(' ', ' ').split()
    
    # LINEの送信者（ユーザー）のお名前を取得
    user_id = event.source.user_id
    sender_name = "不明"
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            profile = line_bot_api.get_profile(user_id)
            sender_name = profile.display_name  # LINEの表示名を取得
        except Exception:
            sender_name = "未設定"

    # 1. 数字だけが送られてきた場合（例: "3000"）
    if len(items) == 1 and items[0].isdigit():
        category = "食費"
        amount = int(items[0])
        payer = sender_name                  # デフォルトで送信者の名前
        method = "カード"
        memo = "-"
        
    # 2. 分類や金額などが送られてきた場合（例: "日用品 1500"）
    elif len(items) >= 2 and items[1].isdigit():
        category = items[0]
        amount = int(items[1])
        payer = items[2] if len(items) > 2 else sender_name  # 省略時は送信者の名前
        method = items[3] if len(items) > 3 else "カード"
        memo = items[4] if len(items) > 4 else "-"
        
    else:
        reply_text = (
            "入力フォーマットが正しくないよ！\n\n"
            "【入力例】\n"
            "・3000（食費・カード・あなたのお名前で記録）\n"
            "・日用品 1500\n"
            "・外食 5000 共通 現金 居酒屋"
        )
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
        return

    # スプレッドシートに記録
    try:
        gc = get_gspread_client()
        sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
        
        # 日時, 分類, 金額, 担当者, 決済方法, メモ の順で追加
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        sheet.append_row([now, category, amount, payer, method, memo])
        
        reply_text = (
            f"記録したよ！\n"
            f"【分類】{category}\n"
            f"【金額】{amount:,}円\n"
            f"【担当】{payer}\n"
            f"【決済】{method}\n"
            f"【メモ】{memo}"
        )
    except Exception as e:
        reply_text = f"エラーが発生して記録できなかった…: {e}"

    # 返信処理
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    app.run(port=5000, debug=True)