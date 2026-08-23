from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    FlexSendMessage
)
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

# LINE API & スプレッドシート設定
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ユーザーIDと表示名のマッピング
USER_MAP = {
    "U7ec4e3142dbdca5e58341bac3264e8d4": "快海",
    "YOUR_LINE_USER_ID_2": "Maki",
}

# 残高確認の対象項目と対応セルのマッピング
BALANCE_CELL_MAP = {
    "残高:食費": ("食費", "I41"),
    "残高:外食": ("外食", "K41"),
    "残高:共用": ("共用", "M41"),
    "残高:快海": ("快海おこづかい", "O41"),
    "残高:真季": ("真季おこづかい", "Q41"),
    "残高:全合計": ("全合計", "S43")
}

user_sessions = {}

def get_gspread_client():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    google_key_env = os.getenv("GOOGLE_KEY_JSON")
    if google_key_env:
        creds_dict = json.loads(google_key_env)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("google_key.json", scope)
    return gspread.authorize(creds)

def get_target_sheet_name():
    """25日締めロジックに基づいたシート名を取得"""
    JST = timezone(timedelta(hours=+9))
    now = datetime.now(JST)
    if now.day >= 25:
        target_month = 1 if now.month == 12 else now.month + 1
    else:
        target_month = now.month
    return f"{target_month}月", str(now.day)

# --- Flex Message レスポンス定義 ---

def get_main_menu_flex():
    """メインメニュー"""
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "家計簿 メニュー", "weight": "bold", "size": "xl", "align": "center", "color": "#1DB446"},
                {"type": "separator", "margin": "md"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "lg",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#1DB446",
                            "action": {"type": "message", "label": "📝 データを入力する", "text": "操作:入力開始"}
                        },
                        {
                            "type": "button",
                            "style": "secondary",
                            "action": {"type": "message", "label": "🗑️ １つ前を削除", "text": "取り消し"}
                        },
                        {
                            "type": "button",
                            "style": "secondary",
                            "action": {"type": "message", "label": "📊 今月の予算残高を確認", "text": "残高メニュー"}
                        }
                    ]
                }
            ]
        }
    }

def get_balance_menu_flex():
    """残高確認カテゴリ選択メニュー"""
    items = [
        ("🍔 食費", "残高:食費"),
        ("🍺 外食", "残高:外食"),
        ("🏠 共用", "残高:共用"),
        ("👨 快海おこづかい", "残高:快海"),
        ("👩 真季おこづかい", "残高:真季"),
        ("💰 全合計", "残高:全合計")
    ]
    buttons = [
        {
            "type": "button",
            "style": "secondary",
            "margin": "xs",
            "height": "sm",
            "action": {"type": "message", "label": label, "text": cmd}
        } for label, cmd in items
    ]
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "確認したい項目を選択してください", "weight": "bold", "size": "md", "align": "center"},
                {"type": "separator", "margin": "md"},
                {"type": "box", "layout": "vertical", "margin": "md", "spacing": "xs", "contents": buttons}
            ]
        }
    }

def get_category_flex():
    """分類選択"""
    categories = ["食費", "日用品", "外食", "交通費", "娯楽", "固定費", "特別費"]
    buttons = [
        {
            "type": "button",
            "style": "secondary",
            "margin": "xs",
            "height": "sm",
            "action": {"type": "message", "label": cat, "text": f"分類:{cat}"}
        } for cat in categories
    ]
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "分類を選択してください", "weight": "bold", "size": "lg", "align": "center"},
                {"type": "separator", "margin": "md"},
                {"type": "box", "layout": "vertical", "margin": "md", "spacing": "xs", "contents": buttons}
            ]
        }
    }

def get_memo_option_flex():
    """メモ入力選択"""
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "メモを追加しますか？", "weight": "bold", "size": "lg", "align": "center"},
                {"type": "separator", "margin": "md"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "md",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#1DB446",
                            "action": {"type": "message", "label": "⚡ メモなしで確定", "text": "メモ:なし"}
                        },
                        {
                            "type": "button",
                            "style": "secondary",
                            "action": {"type": "message", "label": "📝 メモを入力する", "text": "メモ:あり"}
                        }
                    ]
                }
            ]
        }
    }

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    line_user_id = event.source.user_id

    # ユーザー判定
    person = USER_MAP.get(line_user_id, None)
    if not person:
        reply_text = f"ユーザーID未登録です。\napp.pyのUSER_MAPに以下を登録してください：\n\n\"{line_user_id}\": \"お名前\""
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # キャンセル処理
    if user_text in ["キャンセル", "中止"]:
        user_sessions.pop(line_user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="入力操作をキャンセルしました。"))
        return

    # メインメニュー表示
    if user_text in ["メニュー", "記録", "家計簿", "スタート"]:
        user_sessions.pop(line_user_id, None)
        flex_msg = FlexSendMessage(alt_text="家計簿メニュー", contents=get_main_menu_flex())
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    sheet_name, day_str = get_target_sheet_name()
    client = get_gspread_client()
    workbook = client.open_by_key(SPREADSHEET_KEY)

    try:
        sheet = workbook.worksheet(sheet_name)
    except Exception:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"エラー: シート「{sheet_name}」が見つかりませんでした。")
        )
        return

    # 取り消し機能
    if user_text in ["取り消し", "削除", "とりけし"]:
        user_sessions.pop(line_user_id, None)
        all_rows = sheet.get_all_values()
        if len(all_rows) >= 4:
            sheet.delete_rows(len(all_rows))
            reply_text = f"🗑️ ({sheet_name}) 直前の入力データを取り消しました！"
        else:
            reply_text = f"⚠️ ({sheet_name}) 削除できるデータがありません。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # 残高確認メニューの表示
    if user_text in ["残高メニュー", "残高確認", "残高"]:
        user_sessions.pop(line_user_id, None)
        flex_msg = FlexSendMessage(alt_text="残高確認メニュー", contents=get_balance_menu_flex())
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # 指定セルの残高取得処理
    if user_text in BALANCE_CELL_MAP:
        user_sessions.pop(line_user_id, None)
        item_name, cell_address = BALANCE_CELL_MAP[user_text]
        try:
            val = sheet.acell(cell_address).value
            val_display = val if val is not None else "0"
            reply_text = f"📊 【{sheet_name} / {item_name}】\n残高 (セル {cell_address}): {val_display}"
        except Exception as e:
            reply_text = f"⚠️ 残高の取得に失敗しました ({cell_address})"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # --- 対話型入力フロー ---

    session = user_sessions.get(line_user_id, {})

    if user_text == "操作:入力開始":
        user_sessions[line_user_id] = {"step": "WAIT_CATEGORY"}
        flex_msg = FlexSendMessage(alt_text="分類選択", contents=get_category_flex())
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    if user_text.startswith("分類:") and session.get("step") == "WAIT_CATEGORY":
        category = user_text.replace("分類:", "")
        user_sessions[line_user_id] = {"step": "WAIT_AMOUNT", "category": category}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"【{category}】ですね！\n金額を半角数字で入力してください（例: 1500）")
        )
        return

    if session.get("step") == "WAIT_AMOUNT":
        if user_text.isdigit():
            session["amount"] = user_text
            session["step"] = "WAIT_MEMO_OPTION"
            user_sessions[line_user_id] = session
            flex_msg = FlexSendMessage(alt_text="メモ選択", contents=get_memo_option_flex())
            line_bot_api.reply_message(event.reply_token, flex_msg)
            return
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="金額は半角数字のみで入力してください（例: 1500）")
            )
            return

    if session.get("step") == "WAIT_MEMO_OPTION":
        if user_text == "メモ:なし":
            memo = "-"
        elif user_text == "メモ:あり":
            session["step"] = "WAIT_MEMO_TEXT"
            user_sessions[line_user_id] = session
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="メモの内容をテキストで送信してください。")
            )
            return
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ボタンを選択してください。"))
            return

        category = session.get("category")
        amount = session.get("amount")
        row_data = [day_str, int(amount), category, person, memo]
        sheet.append_row(row_data)
        user_sessions.pop(line_user_id, None)

        reply_text = f"【記録完了 ({sheet_name})】\n日付: {day_str}日\n金額: {amount}円\n分類: {category}\n担当: {person}\nメモ: {memo}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    if session.get("step") == "WAIT_MEMO_TEXT":
        memo = user_text
        category = session.get("category")
        amount = session.get("amount")
        row_data = [day_str, int(amount), category, person, memo]
        sheet.append_row(row_data)
        user_sessions.pop(line_user_id, None)

        reply_text = f"【記録完了 ({sheet_name})】\n日付: {day_str}日\n金額: {amount}円\n分類: {category}\n担当: {person}\nメモ: {memo}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # バックアップ用直接入力
    parts = user_text.split()
    category = "食費"
    amount = ""
    memo = "-"

    if len(parts) == 1 and parts[0].isdigit():
        amount = parts[0]
    elif len(parts) == 2 and parts[1].isdigit():
        category, amount = parts[0], parts[1]
    elif len(parts) >= 2 and any(p.isdigit() for p in parts):
        for i, p in enumerate(parts):
            if p.isdigit():
                amount = p
                if i > 0:
                    category = parts[0]
                remains = parts[i+1:]
                if remains:
                    memo = " ".join(remains)
                break
    else:
        flex_msg = FlexSendMessage(alt_text="家計簿メニュー", contents=get_main_menu_flex())
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    row_data = [day_str, int(amount), category, person, memo]
    sheet.append_row(row_data)
    reply_text = f"【記録完了 ({sheet_name})】\n日付: {day_str}日\n金額: {amount}円\n分類: {category}\n担当: {person}\nメモ: {memo}"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)