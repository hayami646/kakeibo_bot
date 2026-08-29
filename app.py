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

@app.route("/", methods=["GET"])
def health_check():
    return "OK", 200


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

# 残高確認の対象項目と対応セルのマッピング
BALANCE_CELL_LIST = [
    ("食費", "I41"),
    ("外食", "K41"),
    ("共用", "M41"),
    ("快海おこづかい", "O41"),
    ("真季おこづかい", "Q41"),
    ("全合計", "S43")
]

user_sessions = {}
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
    global last_written_sheet
    sheet_name, _ = get_target_sheet_name(month_str, day_str)
    client = get_gspread_client()
    workbook = client.open_by_key(SPREADSHEET_KEY)
    sheet = workbook.worksheet(sheet_name)
    
    col_a_values = sheet.col_values(1)  # A列の値をすべて取得
    
    # 目印とみなす文字リスト（前後スペース無視）
    END_MARKERS = ["*", "以下余白", "END", "---", "合計"]
    
    target_row = None
    
    # 上から順にスキャンして目印を探す
    for idx, val in enumerate(col_a_values):
        # .strip() で前後の半角・全角スペースを除去
        val_str = str(val).replace('　', ' ').strip()
        
        if val_str in END_MARKERS:
            # 目印のある行より上の中で、本当にデータが入っている最後の行を探す
            last_data_idx = -1
            for upper_idx in range(idx - 1, -1, -1):
                upper_val = str(col_a_values[upper_idx]).replace('　', ' ').strip()
                # 空白以外の文字が入っている場合のみデータとみなす
                if upper_val != "":
                    last_data_idx = upper_idx
                    break
            
            # データが入っている行の「すぐ下」を指定（1-indexed補正で +2）
            target_row = last_data_idx + 2
            break
            
    # もしシート内に目印が見つからなかった場合のフォールバック処理
    if target_row is None:
        last_data_idx = -1
        for idx in range(len(col_a_values) - 1, -1, -1):
            val_clean = str(col_a_values[idx]).replace('　', ' ').strip()
            if val_clean != "":
                last_data_idx = idx
                break
        target_row = last_data_idx + 2

    # 指定したピンポイントの行（A~E列）に書き込み
    cell_range = f"A{target_row}:E{target_row}"
    row_data = [[day_str, int(amount), category, person, memo]]
    
    sheet.update(cell_range, row_data)
    
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
                            "action": {"type": "message", "label": "🗑️ 自分の最後の入力を削除", "text": "削除確認"}
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

def get_category_flex():
    """分類選択（固定費・収入を控えめなデザインに変更）"""
    main_categories = ["食費", "外食", "共用", "快海おこづかい", "真季おこづかい", "臨時"]
    
    main_buttons = [
        {
            "type": "button",
            "style": "primary",
            "color": "#4A90E2",
            "margin": "xs",
            "height": "sm",
            "action": {"type": "message", "label": cat, "text": f"分類:{cat}"}
        } for cat in main_categories
    ]
    
    # 控えめな配置（横並びで小さめのグレーボタン）
    sub_buttons = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "xs",
        "margin": "md",
        "contents": [
            {
                "type": "button",
                "style": "secondary",
                "height": "sm",
                "action": {"type": "message", "label": "固定費", "text": "分類:固定費"}
            },
            {
                "type": "button",
                "style": "secondary",
                "height": "sm",
                "action": {"type": "message", "label": "収入", "text": "分類:収入"}
            }
        ]
    }
    
    cancel_btn = {
        "type": "button",
        "style": "secondary",
        "margin": "sm",
        "height": "sm",
        "action": {"type": "message", "label": "キャンセル", "text": "キャンセル"}
    }

    contents = main_buttons + [sub_buttons, cancel_btn]

    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "分類を選択してください", "weight": "bold", "size": "lg", "align": "center"},
                {"type": "separator", "margin": "md"},
                {"type": "box", "layout": "vertical", "margin": "md", "spacing": "xs", "contents": contents}
            ]
        }
    }

def get_delete_confirm_flex(sheet_name, row_num, day_str, amount, category, memo):
    """削除前の確認画面 Flex Message"""
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "⚠️ 以下のデータを削除しますか？", "weight": "bold", "size": "md", "color": "#D32F2F", "align": "center"},
                {"type": "separator", "margin": "md"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "md",
                    "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": f"対象シート: {sheet_name}", "size": "sm", "color": "#666666"},
                        {"type": "text", "text": f"日付: {day_str}日", "size": "sm"},
                        {"type": "text", "text": f"金額: {amount}円", "weight": "bold", "size": "md"},
                        {"type": "text", "text": f"分類: {category}", "size": "sm"},
                        {"type": "text", "text": f"メモ: {memo}", "size": "sm", "color": "#666666"}
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "md",
                    "margin": "lg",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#D32F2F",
                            "action": {"type": "message", "label": "はい（削除）", "text": f"実行削除:{sheet_name}:{row_num}"}
                        },
                        {
                            "type": "button",
                            "style": "secondary",
                            "action": {"type": "message", "label": "いいえ", "text": "キャンセル"}
                        }
                    ]
                }
            ]
        }
    }

def get_month_select_flex(prefix="指定月", title="対象の月を選択してください"):
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

    person = USER_MAP.get(line_user_id, None)
    if not person:
        reply_text = f"ユーザーID未登録です。\napp.pyのUSER_MAPに以下を登録してください：\n\n\"{line_user_id}\": \"お名前\""
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    if user_text in ["キャンセル", "中止"]:
        user_sessions.pop(line_user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="操作をキャンセルしました。"))
        return

    if user_text in ["メニュー", "記録", "家計簿", "スタート"]:
        user_sessions.pop(line_user_id, None)
        flex_msg = FlexSendMessage(alt_text="家計簿メニュー", contents=get_main_menu_flex())
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # ① 入力開始
    if user_text in ["操作:入力開始", "入力"]:
        user_sessions[line_user_id] = {"step": "WAIT_CATEGORY", "month": None, "day": None}
        flex_msg = FlexSendMessage(alt_text="分類選択", contents=get_category_flex())
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # ①-2 日付指定入力
    if user_text in ["入力（日付指定）", "日付指定入力"]:
        user_sessions[line_user_id] = {"step": "WAIT_MONTH_SELECT"}
        flex_msg = FlexSendMessage(alt_text="月選択", contents=get_month_select_flex("指定月", "対象の月を選択してください"))
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    session = user_sessions.get(line_user_id, {})
    step = session.get("step")

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

    # ② 分類選択
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

    if step == "WAIT_FIXED_COST_MONTH" and user_text.startswith("固定費月:"):
        selected_month = user_text.replace("固定費月:", "")
        session["month"] = selected_month
        session["day"] = "1"
        session["step"] = "WAIT_AMOUNT"
        user_sessions[line_user_id] = session
        
        category = session.get("category", "")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"【{selected_month}月分 {category}】の金額（半角数字）を送信してください。"))
        return

    if step == "WAIT_INCOME_ITEM" and user_text.startswith("収入項目:"):
        item = user_text.replace("収入項目:", "")
        session["step"] = "WAIT_AMOUNT"
        session["category"] = item
        user_sessions[line_user_id] = session
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"【{item}】の金額（半角数字）を送信してください。"))
        return

# ③ 金額入力（四則演算・全角記号対応版）
    if step == "WAIT_AMOUNT":
        category = session.get("category", "")
        
        # 全角数字・「＋ － ＊ ／ × ÷」などの記号をすべて半角・プログラム用に変換
        trans_table = str.maketrans({
            '０':'0', '１':'1', '２':'2', '３':'3', '４':'4',
            '５':'5', '６':'6', '７':'7', '８':'8', '９':'9',
            '＋':'+', '－':'-', '＊':'*', '／':'/', '×':'*', '÷':'/'
        })
        raw_text = user_text.translate(trans_table)
        
        # 式または数字のみか判定（数字, +, -, *, /, カッコ, スペースを許可）
        import re
        if re.match(r'^[0-9+\-*/()\s]+$', raw_text):
            try:
                # 安全な計算処理
                calc_val = eval(raw_text, {"__builtins__": None}, {})
                val_num = int(calc_val)
                
                if val_num <= 0:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text="計算結果が0以下になりました。正の金額を入力してください。")
                    )
                    return
                
                val_str = str(val_num)
                
            except Exception:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="計算式にエラーがあります。例: 500+200 や 100x3 のように入力してください。")
                )
                return
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="金額または計算式（例: 500+200 / 100×3）を送信してください。")
            )
            return

        session["amount"] = val_str
        
        # 計算式（記号）が含まれていた場合、計算結果の案内メッセージを用意
        has_operator = any(op in raw_text for op in ['+', '-', '*', '/'])
        calc_note = f"（計算結果: {val_str}円）\n" if has_operator else ""

        if category == "収入（その他）":
            session["step"] = "WAIT_MEMO_TEXT"
            user_sessions[line_user_id] = session
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"{calc_note}収入の内容（メモ）をテキストで入力してください。")
            )
            return

        session["step"] = "WAIT_MEMO_OPTION"
        user_sessions[line_user_id] = session
        
        # 計算式が使われた場合は通知メッセージを添えてメモ選択を表示
        if has_operator:
            line_bot_api.reply_message(
                event.reply_token,
                [
                    TextSendMessage(text=f"💡 計算結果: {val_str}円 で受け付けました！"),
                    FlexSendMessage(alt_text="メモ選択", contents=get_memo_option_flex())
                ]
            )
        else:
            flex_msg = FlexSendMessage(alt_text="メモ選択", contents=get_memo_option_flex())
            line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # ④ メモ選択
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

    # ⑤ メモ確定
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

    # --- 削除処理（改善版：ユーザー判定 ＋ 削除前確認） ---

    # 削除ボタン押下時（削除対象を探索して確認画面を提示）
    if user_text in ["削除確認", "削除", "とりけし"]:
        user_sessions.pop(line_user_id, None)
        try:
            target_sheet = last_written_sheet if last_written_sheet else get_target_sheet_name()[0]
            client = get_gspread_client()
            workbook = client.open_by_key(SPREADSHEET_KEY)
            sheet = workbook.worksheet(target_sheet)
            all_rows = sheet.get_all_values()
            
            target_row_idx = None
            target_row_data = None
            
            # 下の行から順に探して、担当者（D列: インデックス3）が自分（person）のものを検出
            for idx in range(len(all_rows) - 1, 3, -1):
                row = all_rows[idx]
                if len(row) > 3 and row[3] == person:
                    target_row_idx = idx + 1  # 1-indexedの行番号
                    target_row_data = row
                    break

            if target_row_data:
                day_str = target_row_data[0] if len(target_row_data) > 0 else "-"
                amount = target_row_data[1] if len(target_row_data) > 1 else "-"
                category = target_row_data[2] if len(target_row_data) > 2 else "-"
                memo = target_row_data[4] if len(target_row_data) > 4 else "-"
                
                flex_msg = FlexSendMessage(
                    alt_text="削除確認",
                    contents=get_delete_confirm_flex(target_sheet, target_row_idx, day_str, amount, category, memo)
                )
                line_bot_api.reply_message(event.reply_token, flex_msg)
            else:
                line_bot_api.reply_message(
                    event.reply_token, 
                    TextSendMessage(text=f"⚠️ 【{target_sheet}】に{person}さんが入力した削除対象のデータが見つかりませんでした。")
                )
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ エラーが発生しました:\n{e}"))
        return

        # 削除の「はい」を押したときの実行処理（行ごとではなく A〜E列のデータをクリアする仕様）
    if user_text.startswith("実行削除:"):
        try:
            _, sheet_name, row_num_str = user_text.split(":")
            row_num = int(row_num_str)
            
            client = get_gspread_client()
            workbook = client.open_by_key(SPREADSHEET_KEY)
            sheet = workbook.worksheet(sheet_name)
            
            # A〜E列のセルを空文字（""）で上書きしてクリアする
            cell_range = f"A{row_num}:E{row_num}"
            empty_data = [["", "", "", "", ""]]
            sheet.update(cell_range, empty_data)
            
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🗑️ 【{sheet_name}】の指定データをクリアしました！"))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 削除に失敗しました:\n{e}"))
        return

    

    # 残高確認
    if user_text in ["残高メニュー", "残高確認", "残高"]:
        user_sessions.pop(line_user_id, None)
        sheet_name, _ = get_target_sheet_name()
        
        try:
            client = get_gspread_client()
            workbook = client.open_by_key(SPREADSHEET_KEY)
            sheet = workbook.worksheet(sheet_name)
            
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

    user_sessions.pop(line_user_id, None)
    flex_msg = FlexSendMessage(alt_text="家計簿メニュー", contents=get_main_menu_flex())
    line_bot_api.reply_message(event.reply_token, flex_msg)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
