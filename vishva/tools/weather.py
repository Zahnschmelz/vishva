"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
import re
import json
import time
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from ..paths import p, cfg_path, BASE_DIR
try:
    import requests
except ImportError:
    requests = None

def _weather(self, args: Dict[str, Any]) -> Dict[str, Any]:
    mode = args.get("mode", "today")
    if mode not in ("today", "tomorrow", "week"):
        return {"error": f"Invalid mode: {mode}. Use 'today', 'tomorrow', or 'week'."}
    lat = self.config.get("weather_latitude")
    lon = self.config.get("weather_longitude")
    location_name = self.config.get("weather_location_name", "Unknown")
    timezone = self.config.get("weather_timezone", "Europe/Berlin")
    if not lat or not lon:
        return {
            "error": "Weather location not configured. Add weather_latitude, weather_longitude, weather_location_name, and weather_timezone to config.json"}
    try:
        lat = float(lat)
        lon = float(lon)
    except (ValueError, TypeError):
        return {"error": f"Invalid lat/lon values: {lat}, {lon}"}

    def get_weathercode_desc(code):
        code = int(code)
        if code == 0:
            return "Sonnig"
        elif code == 1:
            return "Hauptsächlich klar"
        elif code == 2:
            return "Teilweise bewölkt"
        elif code == 3:
            return "Bewölkt"
        elif code in (45, 48):
            return "Nebel"
        elif code in (51, 53, 55):
            return "Nieselregen"
        elif code in (56, 57):
            return "Gefrierender Niesel"
        elif code in (61, 63, 65):
            return "Regen"
        elif code in (66, 67):
            return "Gefrierender Regen"
        elif code in (71, 73, 75, 77):
            return "Schnee"
        elif code in (80, 81, 82):
            return "Regenschauer"
        elif code in (85, 86):
            return "Schneeschauer"
        elif code == 95:
            return "Gewitter"
        elif code in (96, 99):
            return "Gewitter mit Hagel"
        else:
            return "Unbekannt"
    try:
        if mode == "today":
            url = (f"https://api.open-meteo.com/v1/forecast?"
                   f"latitude={lat}&longitude={lon}"
                   f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
                   f"&timezone={timezone}&forecast_days=1")
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            code = data["daily"]["weathercode"][0]
            max_temp = data["daily"]["temperature_2m_max"][0]
            min_temp = data["daily"]["temperature_2m_min"][0]
            precip_prob = data["daily"]["precipitation_probability_max"][0]
            prefix = ""
            if 0 < precip_prob < 50:
                prefix = "Isoliertes "
            desc = get_weathercode_desc(code)
            message = (f"Wetter HEUTE in {location_name}: "
                      f"{prefix}{desc} (Max: {max_temp}°C, Min: {min_temp}°C). "
                      f"Regen-Wahrscheinlichkeit: {precip_prob}%.")
            return {
                "success": True,
                "mode": "today",
                "location": location_name,
                "message": message,
                "data": {
                    "code": code,
                    "description": desc,
                    "max_temp": max_temp,
                    "min_temp": min_temp,
                    "precipitation_probability": precip_prob}}
        elif mode == "tomorrow":
            url = (f"https://api.open-meteo.com/v1/forecast?"
                   f"latitude={lat}&longitude={lon}"
                   f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
                   f"&timezone={timezone}&forecast_days=2")
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            code = data["daily"]["weathercode"][1]
            max_temp = data["daily"]["temperature_2m_max"][1]
            min_temp = data["daily"]["temperature_2m_min"][1]
            precip_prob = data["daily"]["precipitation_probability_max"][1]
            prefix = ""
            if 0 < precip_prob < 50:
                prefix = "Isoliertes "
            desc = get_weathercode_desc(code)
            message = (f"Wetter MORGEN in {location_name}: "
                      f"{prefix}{desc} (Max: {max_temp}°C, Min: {min_temp}°C). "
                      f"Regen-Wahrscheinlichkeit: {precip_prob}%.")
            return {
                "success": True,
                "mode": "tomorrow",
                "location": location_name,
                "message": message,
                "data": {
                    "code": code,
                    "description": desc,
                    "max_temp": max_temp,
                    "min_temp": min_temp,
                    "precipitation_probability": precip_prob}}
        elif mode == "week":
            url = (f"https://api.open-meteo.com/v1/forecast?"
                   f"latitude={lat}&longitude={lon}"
                   f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum"
                   f"&timezone={timezone}&forecast_days=7")
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            table_lines = ["Datum       | Wetter             | Min | Max | Niederschlag"]
            table_lines.append("------------+--------------------+-----+-----+-------------")
            for i in range(len(data["daily"]["time"])):
                date = data["daily"]["time"][i]
                code = data["daily"]["weathercode"][i]
                min_temp = int(data["daily"]["temperature_2m_min"][i])
                max_temp = int(data["daily"]["temperature_2m_max"][i])
                precip = data["daily"]["precipitation_sum"][i]
                desc = get_weathercode_desc(code)
                table_lines.append(f"{date} | {desc:<18} | {min_temp:2}° | {max_temp:2}° | {precip} mm")
            return {
                "success": True,
                "mode": "week",
                "location": location_name,
                "message": f"Wetter WOCHENANSICHT ({location_name}):\n" + "\n".join(table_lines),
                "data": data["daily"]}
    except requests.exceptions.RequestException as e:
        return {"error": f"Network error: {e}"}
    except Exception as e:
        return {"error": f"Error fetching weather: {type(e).__name__}: {e}"}
