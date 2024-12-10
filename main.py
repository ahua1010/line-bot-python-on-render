from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    TextMessage,
    ReplyMessageRequest,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    CameraAction,
    CameraRollAction,
    LocationAction,
    DatetimePickerAction,
)
from linebot.v3 import (
    WebhookHandler,
)
from linebot.v3.webhooks import (
    MessageEvent,
    FollowEvent
)
from linebot.exceptions import InvalidSignatureError
from linebot.models import ( 
    QuickReply, 
)

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError

from flask import Flask, request, abort

from datetime import datetime
import requests
import sqlite3
import logging
import re

import os
from dotenv import load_dotenv

app = Flask(__name__)
user_settings = {}
scheduler = BackgroundScheduler()
scheduler.start()

load_dotenv()

# 設定日誌
logging.basicConfig(level=logging.INFO)  # 設置日誌級別為INFO

# LINE Bot API 設定
configuration = Configuration(access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

### SQLite數據庫邏輯 -----------------------------------------------------
def init_db():
    conn = sqlite3.connect('user_settings.db')
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id TEXT PRIMARY KEY,
            send_time TEXT,
            location TEXT,
            rain_alert TEXT,
            uv_alert TEXT
        )
    """)
    conn.commit()
    conn.close()

def load_user_settings():
    try:
        conn = sqlite3.connect('user_settings.db')
        cursor = conn.cursor()
        
        # 獲取所有設定
        cursor.execute("SELECT user_id, send_time, location, rain_alert,  uv_alert FROM settings")
        rows = cursor.fetchall()

        logging.info(f"從資料庫載入設定: {rows}")

        return {row[0]: {"send_time": row[1].replace("：", ":"), "location": row[2], "rain_alert": row[3],  'uv_alert': row[4],  'awaiting_input': None} for row in rows}
    except Exception as e:
        logging.error(f"載入用戶設定時發生錯誤: {e}")
        return {}
    finally:
        conn.close()

def update_user_settings_batch(user_id, updates):
    for key, value in updates.items():
        user_settings[user_id][key] = value
    save_user_settings_batch(user_id, updates)


def save_user_settings_batch(user_id, updates):
    try:
        conn = sqlite3.connect('user_settings.db')
        cursor = conn.cursor()

        # 使用資料庫批量更新
        set_clause = ", ".join([f"{key} = ?" for key in updates.keys()])
        values = list(updates.values())
        cursor.execute(f"""
            UPDATE settings
            SET {set_clause}
            WHERE user_id = ?
        """, (*values, user_id))

        conn.commit()
        logging.info(f"成功批量更新設定至資料庫 {user_id}: {updates}")
    except Exception as e:
        logging.error(f"儲存至資料庫失敗 {user_id}: {e}")
    finally:
        conn.close()

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']

    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

### 監聽函式主邏輯以及子函式---------------------------
# 處理 FollowEvent：用戶首次新增好友或解除封鎖後重新加好友時觸發
@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id

    # 檢查用戶是否已經存在設定
    if user_id not in user_settings:
        user_settings[user_id] = {
            'send_time': '08:00',
            'location': '臺北市',
            'rain_alert': True,
            'uv_alert': True,
            'awaiting_input': None,
        }
        update_user_settings_batch(user_id, list(user_settings[user_id].items())[:-1])
    else:
        # 如果用戶已存在，歡迎再次加入
        set_awaiting_input(user_id, 'send_time', event, "歡迎回來！\n"
            "期待繼續為您提供服務~：")
    
# 用戶傳送訊息
@handler.add(MessageEvent)
def handle_message(event):
    '''
    主邏輯，負責監聽用戶對話，並依照輸入類型處理，分成首次註冊、修改設定
    '''
    user_id = event.source.user_id
    logging.info(f"成功進入用戶對話 {user_id}")
    user_message = event.message.text.strip()
    logging.info(f"收到來自 {user_id} 的訊息: {user_message}")

    if user_message == "/cancel":
        user_settings[user_id]['awaiting_input'] = None
        send_reply(event, "已取消設定操作。。")
        return

    # 根據用戶指令執行功能
    if user_message == "/setTime":
        logging.info(f"成功進入功能函數")
        set_awaiting_input(user_id, 'send_time', event, f"請輸入新的發送時間，格式為 HH:MM（目前時間為 {user_settings[user_id]['send_time']}\n取消設定請輸入/cancel）")
    elif user_message == "/setLocation":
        set_awaiting_input(user_id, 'location', event, "請輸入新的地點，例如：臺北市。\n取消設定請輸入/cancel")
    elif user_message == "/setContent":
        set_awaiting_input(user_id, 'content', event, "請問您希望接收哪些資訊？\n1. 下雨預告\n2. 紫外線警報\n請回覆「1」或「2」，或回覆「3」。\n取消設定請輸入/cancel")
    elif user_message == "/currentWeather":
        send_weather_info(user_id)
    else:
        # 根據當前等待輸入的類型進行處理
        awaiting_input = user_settings[user_id]['awaiting_input']
        if awaiting_input == 'send_time':
            process_time_input(user_id, user_message, event)
        elif awaiting_input == 'location':
            process_location_input(user_id, user_message, event)
        elif awaiting_input == 'content':
            process_content_input(user_id, user_message, event)
        
def process_time_input(user_id, message, event):
    message = message.replace("：", ":")

    is_time_format_illegal = not re.match(r'^(?:0\d|1\d|2[0-3]):[0-5]\d$', message) is not None
    if not is_time_format_illegal:
        send_reply(event, "時間格式不正確，請使用 HH:MM 格式（例如：14:30）。\n取消設定請輸入/cancel")
        return
    
    user_settings[user_id]['send_time'] = message
    update_user_settings_batch(user_id, {'send_time': message})

    schedule_weather_task(user_id, message)
    send_reply(event, f"已更新發送時間為：{message}")
    user_settings[user_id]['awaiting_input'] = None

def process_location_input(user_id, message, event):
    if message not in ['臺北市','新北市','桃園市','臺中市','臺南市','高雄市', '宜蘭縣','新竹縣','苗栗縣','彰化縣','南投縣','雲林縣','嘉義縣','屏東縣','花蓮縣','臺東縣','澎湖縣', '基隆市','新竹市','嘉義市']:
        send_reply(event, "地點格式不正確，請輸入存在的行政區。\n取消設定請輸入/cancel")
        return
    user_settings[user_id]['location'] = message
    update_user_settings_batch(user_id, {'location': message})

    send_reply(event, f"已更新地點為：{message}")
    user_settings[user_id]['awaiting_input'] = None

def process_content_input(user_id, message, event):
    if message not in ["1", "2", "3"]:
        send_reply(event, "無效選擇，請回覆「1」、「2」，或「3」。\n取消設定請輸入/cancel")
        return
    
    updates = {}
    
    updates['rain_alert'] = True if message in ["1", "3"] else False
    updates['uv_alert'] = True if message in ["2", "3"] else False

    update_user_settings_batch(user_id, updates)
    send_reply(event, "設定成功！")
    user_settings[user_id]['awaiting_input'] = None

# 設定等待輸入狀態並傳送提示訊息
def set_awaiting_input(user_id, input_type, event, prompt_message):
    user_settings[user_id]['awaiting_input'] = input_type
    logging.info(f"準備發送訊息")
    send_reply(event, prompt_message)
    logging.info(f"成功發送對話")

# 回覆訊息
def send_reply(event, message):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=message)]
            )
        )

### 執行邏輯函式----------------------------------------------------------
# 獲取天氣資訊的函數
def get_weather(user_id, location):
    api_key = os.environ.get("CWA_API_KEY")
    api_url_1 = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-D0047-089?Authorization={api_key}&locationName={location}"
    response_1 = requests.get(api_url_1)
    
    if response_1.status_code != 200:
        return "無法獲取天氣資訊，請檢查地點名稱或授權碼。"

    try:
        weather_data = response_1.json()
        data = weather_data['records']['Locations'][0]["Location"][0]
    except IndexError:
        logging.error(f"index超出json格式 location:{location}")
    

    # 計算最高溫和最低溫
    temps = []
    for time_data in data["WeatherElement"][0]["Time"]:
        temp = int(time_data["ElementValue"][0]["Temperature"])  # 獲取氣溫值
        temps.append(temp)

    max_temp = max(temps)
    min_temp = min(temps)

    # 尋找逐三小時紀錄中最接近現在溫度之紀錄
    current_time = datetime.now()
    current_temp = None

    
    for time_data in data["WeatherElement"][0]["Time"]:
        start_time = datetime.strptime(time_data["DataTime"], "%Y-%m-%d %H:%M:%S")

        if current_time <= start_time:
            current_temp = int(time_data["ElementValue"][0]["Temperature"])
            break 
    
    if current_temp is None:
        print("無法找到當前氣溫，請檢查時間或 API 資料是否正確。")  


    # 下雨預測
    threshold = 50
    rain_alert_message = ""
    if user_settings[user_id]['rain_alert']:
        for forecast in data['WeatherElement'][7]['Time']:
            start_time = datetime.strptime(forecast['StartTime'], '%Y-%m-%d %H:%M:%S')
            rain_probability = int(forecast['ElementValue'][0]['ProbabilityOfPrecipitation'])

            if start_time > current_time and rain_probability > threshold:
                # 計算距離`send_time`的時間差
                time_difference = (start_time - current_time).total_seconds() / 3600  # 轉換為小時
                print(start_time)
                print(current_time)
                rain_alert_message =  f"{int(time_difference)}小時後高機率下雨"
                break
        else:
            rain_alert_message =  "未來12小時無降雨風險"
        
    station_id = get_uv_station_by_city(location)
    api_url_2 = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0005-001?Authorization={api_key}&StationID={station_id}"
    response_2 = requests.get(api_url_2)
    print(response_2.json(), station_id)

    if response_2.status_code != 200:
        return "無法獲取觀測資訊，請檢查地點名稱或授權碼。"

    uv_alert_message = ""
    if user_settings[user_id]['uv_alert']:
        uv_level = response_2.json()['records']['weatherElement']['location'][0]['UVIndex']
        uv_alert_message = get_uv_warning(uv_level)

    return (f"當前溫度: {current_temp}°C。 {rain_alert_message}"
            f"\n{user_settings[user_id]['location']}最高/最低溫度:{max_temp}°C / {min_temp}°C。"
            f"\n{uv_alert_message}")

def send_weather_info(user_id):
    location = user_settings.get(user_id, {}).get('location', '臺北市')
    weather_info = get_weather(user_id, location)
    
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message_with_http_info(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=weather_info)]
                )
            )
        logging.info(f"成功傳送天氣資訊給用戶 {user_id}")
    except Exception as e:
        logging.error(f"無法傳送天氣資訊給用戶 {user_id}：{e}")

def get_uv_warning(uv_index):
    """
    根據紫外線指數回傳等級及建議訊息
    """
    if uv_index <= 2:
        level = "低量級"
        advice = "紫外線弱，建議適度戶外活動，但仍需注意防曬。"
    elif 3 <= uv_index <= 5:
        level = "中量級"
        advice = "紫外線中等，建議戴帽子、太陽眼鏡，並塗抹防曬乳。"
    elif 6 <= uv_index <= 7:
        level = "高量級"
        advice = "紫外線高，避免長時間曝曬，建議撐傘或尋找遮蔽處。"
    elif 8 <= uv_index <= 10:
        level = "過量級"
        advice = "紫外線非常強烈，建議減少戶外活動，並全方位做好防曬措施。"
    else:  # uv_index >= 11
        level = "危險級"
        advice = "紫外線極危險，避免任何直接曝曬，務必留在室內或做好防護。"

    return f"目前紫外線指數：{uv_index} {level}\n建議：{advice}"

def get_uv_station_by_city(user_city):
    uv_stations = [
        {"station_id": "466850", "city": "新北市"},
        {"station_id": "466910", "city": "臺北市"},
        {"station_id": "466940", "city": "基隆市"},
        {"station_id": "466990", "city": "花蓮縣"},
        {"station_id": "467050", "city": "桃園市"},
        {"station_id": "467080", "city": "宜蘭縣"},
        {"station_id": "467110", "city": "金門縣"},
        {"station_id": "467270", "city": "彰化縣"},
        {"station_id": "467280", "city": "苗栗縣"},
        {"station_id": "467290", "city": "雲林縣"},
        {"station_id": "467300", "city": "澎湖縣"},
        {"station_id": "467410", "city": "臺南市"},
        {"station_id": "467441", "city": "高雄市"},
        {"station_id": "467480", "city": "嘉義市"},
        {"station_id": "467490", "city": "臺中市"},
        {"station_id": "467530", "city": "嘉義縣"},
        {"station_id": "467540", "city": "臺東縣"},
        {"station_id": "467550", "city": "南投縣"},
        {"station_id": "467571", "city": "新竹縣"},
        {"station_id": "467590", "city": "屏東縣"},
        {"station_id": "467990", "city": "連江縣"},
        {"station_id": "C0D660", "city": "新竹市"},
        {"station_id": "G2AI50",  "city":"台北市"}
    ]
    matched_stations = [station["station_id"] for station in uv_stations if station["city"] == user_city]
    if matched_stations:
        return matched_stations[0]
    else:
        return None

def schedule_weather_task(user_id, send_time):
    """為用戶設定定時發送天氣資訊的任務"""
    hour, minute = map(int, send_time.split(":"))
    
    job_id = f"weather_task_{user_id}"
    try:
        scheduler.remove_job(job_id=job_id)
        logging.info(f"已移除舊的定時任務: {job_id}")
    except JobLookupError:
        logging.info(f"未找到舊的定時任務: {job_id}，跳過移除")
    
    scheduler.add_job(
        func=send_weather_info, 
        trigger="cron",
        hour=hour,
        minute=minute,
        args=[user_id],
        id=job_id
    )
    logging.info(f"已為用戶 {user_id} 排程每日 {send_time} 的天氣資訊推送")

# if __name__ == "__main__":
init_db()
user_settings = load_user_settings()

for user_id, settings in user_settings.items():
    if settings["send_time"] and settings["location"]:
        schedule_weather_task(user_id, settings["send_time"])
send_weather_info("Ua06c92cabcc3df6268665d6c944e877a")
app.run()
