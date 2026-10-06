import os
import sys
import json
import requests

def send_file_to_telegram(file_path):
    base_dir = os.path.expanduser("~/ai/vishva")
    config_path = os.path.join(base_dir, "config/config.json")
    if not os.path.exists(config_path):
        return {"error": f"Error: Configuration file not found at {config_path}"}
    with open(config_path, 'r') as f:
        config = json.load(f)
    token = config.get("bot_token")
    chat_id = config.get("chat_id")
    if not token or not chat_id:
        return {"error": f"Error: Telegram token or chat_id not found in config.json"}
    if not os.path.isfile(file_path):
        return {"error": f"Error: File '{file_path}' does not exist."}
    print(f"Streaming '{file_path}' to Telegram...")
    url = f"https://api.telegram.org/bot{token}/senddocument"
    try:
        with open(file_path, 'rb') as f:
            files = {'document': f}
            data = {'chat_id': chat_id}
            response = requests.post(url, data=data, files=files)
        if response.status_code == 200:
            return "Successfully sent!"
        else:
            print(f"Failed to send. Telegram API response: {response.status_code} - {response.text}")
    except Exception as e:
        return {"error": f"An error occurred: {e}"}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 tg_send_file.py <file_path>")
    else:
        send_file_to_telegram(sys.argv[1])
