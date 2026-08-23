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
    "U33dfd50f79bf0c4c44824ba2ab0622a8": "真季",
}

# 残高確認の対象項目と対応セルのマッピング（一括表示用）
BALANCE_CELL_LIST = [
    ("食費", "I41"),
    ("外食", "K41"),
    ("共用", "M41"),
    ("快海おこづかい", "O41"),
    ("真季おこづかい", "Q41"),
    ("全合計", "S43")
]

user_sessions = {}
# 直前に書き込みを行ったシート名を記録するグローバル変数
last_written_sheet = None

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

def get_target_sheet_name(specified_month=None, specified_day=None):
    """
    シート名と日付文字列を取得。
    指定の月があればその月シートを使用。
    月が未指定の場合は25日締めロジックで自動判定。
    """
    JST = timezone(timedelta(hours=+9))
    now = datetime.now(JST)
    
    if specified_month:
        target_sheet = f"{specified_month}月"
        day_str = str(specified_day) if specified_day else "1"
    else:
        day_num = int(specified_day) if specified_day else now.day
        if day_num >= 25:
            target_m = 1 if now.month == 12 else now.month + 1
        else:
            target_m = now.month
        target_sheet = f"{target_m}月"
        day_str = str(day_num)
        
    return target_sheet, day_str

def append_to_spreadsheet(month_str, day_str, amount, category, person, memo):
    """スプレッドシートへの書き込み共通処理"""
    global last_written_sheet
    sheet_name, _ = get_target_sheet_name(month_str, day_str)
    client = get_gspread_client()
    workbook = client.open_by_key(SPREADSHEET_KEY)
    sheet = workbook.worksheet(sheet_name)
    row_data = [day_str, int(amount), category, person, memo]
    sheet.append_row(row_data)
    
    # 削除時に直前の入力シートを正しく参照できるよう記録
    last_written_sheet = sheet_name
    return sheet_name

# --- Flex Message デザイン定義 ---

def get_main_menu_flex():
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
                            "action": {"type": "message", "label": "📝 データを入力する", "text": "入力"}
                        },
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#0288D1",
                            "action": {"type": "message", "label": "📅 日時を指定して入力", "text": "入力（日付指定）"}
                        },
                        {
                            "type": "button",
                            "style": "secondary",
                            "action": {"type": "message", "label": "🗑️ １つ前を削除", "text": "削除"}
                        },
                        {
                            "type": "button",
                            "style": "secondary",
                            "action": {"type": "message", "label": "📊 予算残高を確認", "text": "残高"}
                        }
                    ]
                }
            ]
        }
    }

def get_month_select_flex(prefix="指定月", title="対象の月を選択してください"):
    """月選択 Flex Message (1月〜12月)"""
    rows = []
    for r in range(4):
        cols = []
        for c in range(1, 4):
            m = r * 3 + c
            cols.append({
                "type": "button",
                "style": "secondary",
                "height": "sm",
                "action": {"type": "message", "label": f"{m}月", "text": f"{prefix}:{m}"}
            })
        rows.append({"type": "box", "layout": "horizontal", "spacing": "xs", "contents": cols})
    
    rows.append({
        "type": "box",
        "layout": "horizontal",
        "margin": "xs",
        "contents": [
            {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "message", "label": "キャンセル", "text": "キャンセル"}}
        ]
    })

    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": title, "weight": "bold", "size": "md", "align": "center", "color": "#555555"},
                {"type": "box", "layout": "vertical", "spacing": "xs", "contents": rows}
            ]
        }
    }

def get_day_input_flex(selected_month, current_day_str="1"):
    """日付入力用テンキーFlex Message"""
    def make_btn(label, action_text, style="secondary", color=None):
        btn = {
            "type": "button",
            "style": style,
            "height": "sm",
            "action": {"type": "message", "label": label, "text": action_text}
        }
        if color:
            btn["color"] = color
        return btn

    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": f"【{selected_month}月】の日（1〜31日）を入力",
                    "weight": "bold",
                    "size": "md",
                    "align": "center",
                    "color": "#555555"
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#F0F0F0",
                    "cornerRadius": "md",
                    "paddingAll": "md",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"{current_day_str} 日",
                            "weight": "bold",
                            "size": "xl",
                            "align": "end",
                            "color": "#111111"
                        }
                    ]
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "xs",
                    "contents": [
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "spacing": "xs",
                            "contents": [
                                make_btn("7", "日付電卓:7"),
                                make_btn("8", "日付電卓:8"),
                                make_btn("9", "日付電卓:9")
                            ]
                        },
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "spacing": "xs",
                            "contents": [
                                make_btn("4", "日付電卓:4"),
                                make_btn("5", "日付電卓:5"),
                                make_btn("6", "日付電卓:6")
                            ]
                        },
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "spacing": "xs",
                            "contents": [
                                make_btn("1", "日付電卓:1"),
                                make_btn("2", "日付電卓:2"),
                                make_btn("3", "日付電卓:3")
                            ]
                        },
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "spacing": "xs",
                            "contents": [
                                make_btn("0", "日付電卓:0"),
                                make_btn("⌫", "日付電卓:BS"),
                                make_btn("C クリア", "日付電卓:CLR")
                            ]
                        },
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "spacing": "xs",
                            "margin": "xs",
                            "contents": [
                                make_btn("キャンセル", "キャンセル")
                            ]
                        },
                        {
                            "type": "box",
                            "layout": "vertical",
                            "margin": "sm",
                            "contents": [
                                make_btn("✅ この日付で確定", "日付電卓:ENT", style="primary", color="#0288D1")
                            ]
                        }
                    ]
                }
            ]
        }
    }

def get_category_flex():
    categories = ["食費", "外食", "共用", "固定費", "収入", "快海おこづかい", "真季おこづかい", "臨時", "キャンセル"]
    buttons = [
        {
            "type": "button",
            "style": "secondary",
            "margin": "xs",
            "height": "sm",
            "action": {"type": "message", "label": cat, "text": f"分類:{cat}" if cat != "キャンセル" else "キャンセル"}
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

def get_fixed_cost_flex():
    items = ["電気代", "ガス代", "水道代", "スマホ", "ウォーターサーバー", "家賃", "保険", "キャンセル"]
    buttons = [
        {
            "type": "button",
            "style": "secondary",
            "margin": "xs",
            "height": "sm",
            "action": {"type": "message", "label": item, "text": f"固定費項目:{item}" if item != "キャンセル" else "キャンセル"}
        } for item in items
    ]
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "固定費の項目を選択してください", "weight": "bold", "size": "md", "align": "center"},
                {"type": "separator", "margin": "md"},
                {"type": "box", "layout": "vertical", "margin": "md", "spacing": "xs", "contents": buttons}
            ]
        }
    }

def get_income_flex():
    items = ["給料（快海）", "給料（真季）", "収入（その他）", "キャンセル"]
    buttons = [
        {
            "type": "button",
            "style": "secondary",
            "margin": "xs",
            "height": "sm",
            "action": {"type": "message", "label": item, "text": f"収入項目:{item}" if item != "キャンセル" else "キャンセル"}
        } for item in items
    ]
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "収入の項目を選択してください", "weight": "bold", "size": "md", "align": "center"},
                {"type": "separator", "margin": "md"},
                {"type": "box", "layout": "vertical", "margin": "md", "spacing": "xs", "contents": buttons}
            ]
        }
    }

def get_memo_option_flex():
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
    global last_written_sheet
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

    # ① 通常の入力開始（当日）
    if user_text in ["操作:入力開始", "入力"]:
        user_sessions[line_user_id] = {"step": "WAIT_CATEGORY", "month": None, "day": None}
        flex_msg = FlexSendMessage(alt_text="分類選択", contents=get_category_flex())
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # ①-2 日付指定入力の開始 (まず月を選択)
    if user_text in ["入力（日付指定）", "日付指定入力"]:
        user_sessions[line_user_id] = {"step": "WAIT_MONTH_SELECT"}
        flex_msg = FlexSendMessage(alt_text="月選択", contents=get_month_select_flex("指定月", "対象の月を選択してください"))
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # 既存セッションの取得
    session = user_sessions.get(line_user_id, {})
    step = session.get("step")

    # 月選択の受付 (日付指定入力用)
    if step == "WAIT_MONTH_SELECT" and user_text.startswith("指定月:"):
        selected_month = user_text.replace("指定月:", "")
        JST = timezone(timedelta(hours=+9))
        default_day = str(datetime.now(JST).day)
        
        session["month"] = selected_month
        session["day_str"] = default_day
        session["step"] = "WAIT_DAY_INPUT"
        user_sessions[line_user_id] = session
        
        flex_msg = FlexSendMessage(alt_text="日付選択", contents=get_day_input_flex(selected_month, default_day))
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # 日付指定電卓の受付
    if step == "WAIT_DAY_INPUT" and user_text.startswith("日付電卓:"):
        cmd = user_text.replace("日付電卓:", "")
        curr_day = session.get("day_str", "1")
        selected_month = session.get("month")

        if cmd in [str(i) for i in range(10)]:
            if curr_day == "0":
                curr_day = cmd
            else:
                if len(curr_day) < 2:
                    curr_day += cmd
        elif cmd == "BS":
            curr_day = curr_day[:-1]
            if not curr_day:
                curr_day = "0"
        elif cmd == "CLR":
            curr_day = "0"
        elif cmd == "ENT":
            if not (1 <= int(curr_day) <= 31):
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="日付は 1〜31 の範囲で入力してください。")
                )
                return
            
            session["day"] = curr_day
            session["step"] = "WAIT_CATEGORY"
            user_sessions[line_user_id] = session
            flex_msg = FlexSendMessage(alt_text="分類選択", contents=get_category_flex())
            line_bot_api.reply_message(event.reply_token, flex_msg)
            return

        session["day_str"] = curr_day
        user_sessions[line_user_id] = session
        flex_msg = FlexSendMessage(alt_text="日付指定入力", contents=get_day_input_flex(selected_month, curr_day))
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # ② 分類選択の受付
    if step == "WAIT_CATEGORY" and user_text.startswith("分類:"):
        category = user_text.replace("分類:", "")
        
        if category == "固定費":
            session["step"] = "WAIT_FIXED_COST_ITEM"
            user_sessions[line_user_id] = session
            flex_msg = FlexSendMessage(alt_text="固定費項目選択", contents=get_fixed_cost_flex())
            line_bot_api.reply_message(event.reply_token, flex_msg)
            return

        if category == "収入":
            session["step"] = "WAIT_INCOME_ITEM"
            user_sessions[line_user_id] = session
            flex_msg = FlexSendMessage(alt_text="収入項目選択", contents=get_income_flex())
            line_bot_api.reply_message(event.reply_token, flex_msg)
            return
        
        session["step"] = "WAIT_AMOUNT"
        session["category"] = category
        user_sessions[line_user_id] = session
        display_label = f"{session.get('month')}月分 {category}" if session.get('month') else category
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"【{display_label}】の金額（半角数字）を送信してください。"))
        return

    # ②-2 固定費項目選択の受付
    if step == "WAIT_FIXED_COST_ITEM" and user_text.startswith("固定費項目:"):
        item = user_text.replace("固定費項目:", "")
        session["category"] = item
        session["step"] = "WAIT_FIXED_COST_MONTH"
        user_sessions[line_user_id] = session
        flex_msg = FlexSendMessage(
            alt_text="固定費の対象月選択", 
            contents=get_month_select_flex("固定費月", f"【{item}】の対象月を選択してください")
        )
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # ②-2-2 固定費の対象月選択の受付
    if step == "WAIT_FIXED_COST_MONTH" and user_text.startswith("固定費月:"):
        selected_month = user_text.replace("固定費月:", "")
        session["month"] = selected_month
        session["day"] = "1"  # 固定費はデフォルトで1日に記録
        session["step"] = "WAIT_AMOUNT"
        user_sessions[line_user_id] = session
        
        category = session.get("category", "")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"【{selected_month}月分 {category}】の金額（半角数字）を送信してください。"))
        return

    # ②-3 収入項目選択の受付
    if step == "WAIT_INCOME_ITEM" and user_text.startswith("収入項目:"):
        item = user_text.replace("収入項目:", "")
        session["step"] = "WAIT_AMOUNT"
        session["category"] = item
        user_sessions[line_user_id] = session
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"【{item}】の金額（半角数字）を送信してください。"))
        return

    # ③ 金額入力（直接テキスト送信）の受付
    if step == "WAIT_AMOUNT":
        category = session.get("category", "")
        # 全角数字を半角に変換するなどの処理
        val_str = user_text.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
        if not val_str.isdigit() or int(val_str) == 0:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="金額は正の半角数字（例: 1200）で入力してください。")
            )
            return

        session["amount"] = val_str
        if category == "収入（その他）":
            session["step"] = "WAIT_MEMO_TEXT"
            user_sessions[line_user_id] = session
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="収入の内容（メモ）をテキストで入力してください。\n（例：宝くじ、メルカリ売上）")
            )
            return

        session["step"] = "WAIT_MEMO_OPTION"
        user_sessions[line_user_id] = session
        flex_msg = FlexSendMessage(alt_text="メモ選択", contents=get_memo_option_flex())
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # ④ メモ「あり」「なし」の選択受付
    if step == "WAIT_MEMO_OPTION":
        if user_text == "メモ:あり":
            session["step"] = "WAIT_MEMO_TEXT"
            user_sessions[line_user_id] = session
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="メモの内容をテキストで送信してください。")
            )
            return
        elif user_text == "メモ:なし":
            category = session.get("category")
            amount = session.get("amount")
            specified_month = session.get("month")
            specified_day = session.get("day")
            memo = "-"
            
            sheet_name, day_str = get_target_sheet_name(specified_month, specified_day)
            try:
                sheet_name = append_to_spreadsheet(specified_month, day_str, amount, category, person, memo)
                user_sessions.pop(line_user_id, None)
                reply_text = f"【記録完了 ({sheet_name})】\n日付: {day_str}日\n金額: {amount}円\n分類: {category}\n担当: {person}\nメモ: {memo}"
            except Exception as e:
                reply_text = f"⚠️ エラーが発生しました:\n{e}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            return

    # ⑤ メモテキストの受付＆確定処理
    if step == "WAIT_MEMO_TEXT":
        memo = user_text
        category = session.get("category")
        amount = session.get("amount")
        specified_month = session.get("month")
        specified_day = session.get("day")
        
        sheet_name, day_str = get_target_sheet_name(specified_month, specified_day)
        try:
            sheet_name = append_to_spreadsheet(specified_month, day_str, amount, category, person, memo)
            user_sessions.pop(line_user_id, None)
            reply_text = f"【記録完了 ({sheet_name})】\n日付: {day_str}日\n金額: {amount}円\n分類: {category}\n担当: {person}\nメモ: {memo}"
        except Exception as e:
            reply_text = f"⚠️ スプレッドシート保存エラー:\n{e}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # --- その他の固定コマンド（取り消し・残高など） ---

    # 直前の入力削除処理
    if user_text in ["取り消し", "削除", "とりけし"]:
        user_sessions.pop(line_user_id, None)
        try:
            # 直前に書き込んだシートがあれば優先、無ければ現在の締め日ロジックに基づく月シートを参照
            target_sheet = last_written_sheet if last_written_sheet else get_target_sheet_name()[0]
            
            client = get_gspread_client()
            workbook = client.open_by_key(SPREADSHEET_KEY)
            sheet = workbook.worksheet(target_sheet)
            all_rows = sheet.get_all_values()
            
            if len(all_rows) >= 4:
                # 削除対象の最終行データを取得
                deleted_row = all_rows[-1]
                del_day = deleted_row[0] if len(deleted_row) > 0 else "-"
                del_amount = deleted_row[1] if len(deleted_row) > 1 else "-"
                del_category = deleted_row[2] if len(deleted_row) > 2 else "-"
                del_person = deleted_row[3] if len(deleted_row) > 3 else "-"
                del_memo = deleted_row[4] if len(deleted_row) > 4 else "-"
                
                # 最終行を削除
                sheet.delete_rows(len(all_rows))
                
                reply_text = (
                    f"🗑️ 【{target_sheet}】直前の入力データを削除しました！\n"
                    f"--------------------\n"
                    f"・日付: {del_day}日\n"
                    f"・金額: {del_amount}円\n"
                    f"・分類: {del_category}\n"
                    f"・担当: {del_person}\n"
                    f"・メモ: {del_memo}"
                )
            else:
                reply_text = f"⚠️ ({target_sheet}) 削除できるデータがありません。"
        except Exception as e:
            reply_text = f"⚠️ 削除処理でエラーが発生しました:\n{e}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # 残高確認（ボタン1回で現在の対象月シートの全項目を即時表示）
    if user_text in ["残高メニュー", "残高確認", "残高"]:
        user_sessions.pop(line_user_id, None)
        sheet_name, _ = get_target_sheet_name()  # 25日締めロジックに基づき当月のシート名を取得
        
        try:
            client = get_gspread_client()
            workbook = client.open_by_key(SPREADSHEET_KEY)
            sheet = workbook.worksheet(sheet_name)
            
            # 各セルの値を順番に取得
            results = []
            for item_name, cell_addr in BALANCE_CELL_LIST:
                val = sheet.acell(cell_addr).value
                val_display = val if val is not None else "0"
                results.append(f"・{item_name}: {val_display}")
            
            lines = "\n".join(results)
            reply_text = f"📊 【{sheet_name} の予算残高一覧】\n--------------------\n{lines}"
        except Exception as e:
            reply_text = f"⚠️ 残高の取得に失敗しました ({sheet_name}):\n{e}"
            
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # バックアップ用処理
    user_sessions.pop(line_user_id, None)
    flex_msg = FlexSendMessage(alt_text="家計簿メニュー", contents=get_main_menu_flex())
    line_bot_api.reply_message(event.reply_token, flex_msg)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)