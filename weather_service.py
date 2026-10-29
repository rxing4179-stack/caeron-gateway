import os
import json
import time
import logging
import asyncio
import httpx
from datetime import datetime

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LOCATION_FILE = os.path.join(DATA_DIR, "last_location.json")
CACHE_TTL = 1800  # 30 分钟缓存

# WMO 标准天气代码对照字典 (Open-Meteo 使用 WMO Code 0-99)
WMO_CODE_MAP = {
    0: ("晴朗", "Clear sky"),
    1: ("大部晴朗", "Mainly clear"),
    2: ("多云", "Partly cloudy"),
    3: ("阴天", "Overcast"),
    45: ("雾", "Fog"),
    48: ("冻雾", "Depositing rime fog"),
    51: ("毛毛细雨", "Light drizzle"),
    53: ("中度细雨", "Moderate drizzle"),
    55: ("密集细雨", "Dense drizzle"),
    56: ("轻微冻细雨", "Light freezing drizzle"),
    57: ("密集冻细雨", "Dense freezing drizzle"),
    61: ("微弱小雨", "Slight rain"),
    63: ("中雨", "Moderate rain"),
    65: ("大雨", "Heavy rain"),
    66: ("轻微冻雨", "Light freezing rain"),
    67: ("大冻雨", "Heavy freezing rain"),
    71: ("小雪", "Slight snow fall"),
    73: ("中雪", "Moderate snow fall"),
    75: ("大雪", "Heavy snow fall"),
    77: ("雪粒", "Snow grains"),
    80: ("微弱阵雨", "Slight rain showers"),
    81: ("中度阵雨", "Moderate rain showers"),
    82: ("强阵雨", "Violent rain showers"),
    85: ("微弱阵雪", "Slight snow showers"),
    86: ("强阵雪", "Heavy snow showers"),
    95: ("雷阵雨", "Thunderstorm"),
    96: ("雷阵雨伴有微弱冰雹", "Thunderstorm with slight hail"),
    99: ("雷阵雨伴有强冰雹", "Thunderstorm with heavy hail"),
}

# wttr.in 英文天气描述翻译映射
WTTR_DESC_MAP = {
    "sunny": ("晴天", "Sunny"),
    "clear": ("晴朗", "Clear"),
    "partly cloudy": ("多云", "Partly cloudy"),
    "cloudy": ("多云", "Cloudy"),
    "overcast": ("阴天", "Overcast"),
    "mist": ("薄雾", "Mist"),
    "fog": ("大雾", "Fog"),
    "freezing fog": ("冻雾", "Freezing fog"),
    "patchy rain nearby": ("附近零星小雨", "Patchy rain nearby"),
    "patchy rain possible": ("局部可能有小雨", "Patchy rain possible"),
    "patchy snow possible": ("局部可能有小雪", "Patchy snow possible"),
    "patchy sleet possible": ("局部可能有雨夹雪", "Patchy sleet possible"),
    "patchy freezing drizzle possible": ("局部可能有冻细雨", "Patchy freezing drizzle possible"),
    "thundery outbreaks possible": ("局部可能有雷阵雨", "Thundery outbreaks possible"),
    "light drizzle": ("毛毛细雨", "Light drizzle"),
    "freezing drizzle": ("冻细雨", "Freezing drizzle"),
    "heavy freezing drizzle": ("稠密冻细雨", "Heavy freezing drizzle"),
    "patchy light drizzle": ("零星毛毛雨", "Patchy light drizzle"),
    "light rain": ("小雨", "Light rain"),
    "moderate rain at times": ("时有中雨", "Moderate rain at times"),
    "moderate rain": ("中雨", "Moderate rain"),
    "heavy rain at times": ("时有大雨", "Heavy rain at times"),
    "heavy rain": ("大雨", "Heavy rain"),
    "light freezing rain": ("小冻雨", "Light freezing rain"),
    "moderate or heavy freezing rain": ("中到大冻雨", "Moderate or heavy freezing rain"),
    "light sleet": ("小雨夹雪", "Light sleet"),
    "moderate or heavy sleet": ("中到大雨夹雪", "Moderate or heavy sleet"),
    "patchy light snow": ("零星小雪", "Patchy light snow"),
    "light snow": ("小雪", "Light snow"),
    "patchy moderate snow": ("零星中雪", "Patchy moderate snow"),
    "moderate snow": ("中雪", "Moderate snow"),
    "patchy heavy snow": ("零星大雪", "Patchy heavy snow"),
    "heavy snow": ("大雪", "Heavy snow"),
    "ice pellets": ("冰粒", "Ice pellets"),
    "light rain shower": ("小阵雨", "Light rain shower"),
    "moderate or heavy rain shower": ("中到大阵雨", "Moderate or heavy rain shower"),
    "torrential rain shower": ("暴雨阵雨", "Torrential rain shower"),
    "light sleet showers": ("小雨夹雪阵雨", "Light sleet showers"),
    "moderate or heavy sleet showers": ("中到大雨夹雪阵雨", "Moderate or heavy sleet showers"),
    "light snow showers": ("小阵雪", "Light snow showers"),
    "moderate or heavy snow showers": ("中到大阵雪", "Moderate or heavy snow showers"),
    "patchy light rain with thunder": ("雷阵雨伴零星小雨", "Patchy light rain with thunder"),
    "moderate or heavy rain with thunder": ("雷阵雨伴大雨", "Moderate or heavy rain with thunder"),
    "patchy light snow with thunder": ("雷阵雪伴零星小雪", "Patchy light snow with thunder"),
    "moderate or heavy snow with thunder": ("雷阵雪伴大雪", "Moderate or heavy snow with thunder"),
}

# 内存缓存: {(lat_grid, lon_grid): {"data": dict, "timestamp": float}}
_WEATHER_CACHE = {}


def _get_grid_key(lat: float, lon: float) -> tuple[float, float]:
    """将经纬度保留2位小数作为网格缓存键（约 1.1km 精度）"""
    return (round(float(lat), 2), round(float(lon), 2))


def save_last_location(lat: float, lon: float, address: str = None):
    """持久化保存最新坐标"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        data = {
            "lat": float(lat),
            "lon": float(lon),
            "address": address or "重庆市",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": time.time()
        }
        with open(LOCATION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f"[WEATHER] 已更新最后位置: {lat}, {lon} ({address})")
    except Exception as e:
        logger.error(f"[WEATHER] 保存位置失败: {e}")


def get_last_location() -> dict:
    """获取最后已知坐标（默认重庆市区坐标）"""
    default_loc = {
        "lat": 29.867242,
        "lon": 107.752625,
        "address": "重庆市",
        "updated_at": ""
    }
    if os.path.exists(LOCATION_FILE):
        try:
            with open(LOCATION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "lat" in data and "lon" in data:
                    return data
        except Exception as e:
            logger.error(f"[WEATHER] 读取位置文件失败: {e}")
    return default_loc


def translate_weather_desc(desc_en: str) -> str:
    """将英文天气描述翻译为规范中英双语格式"""
    if not desc_en:
        return "未知 (Unknown)"
    key = desc_en.strip().lower()
    if key in WTTR_DESC_MAP:
        zh, en = WTTR_DESC_MAP[key]
        return f"{zh} ({en})"
    # 模糊包含匹配
    for k, (zh, en) in WTTR_DESC_MAP.items():
        if k in key:
            return f"{zh} ({desc_en.strip()})"
    return f"{desc_en.strip()}"


async def _fetch_from_open_meteo(lat: float, lon: float) -> dict | None:
    """从 Open-Meteo 高精度接口获取实时天气"""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&"
        f"current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m&"
        f"timezone=Asia%2FShanghai"
    )
    headers = {"User-Agent": "CaeronGateway/2.0 (WeatherService)"}
    timeout = httpx.Timeout(connect=3.0, read=4.0, write=3.0, pool=3.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            curr = data.get("current", {})
            w_code = curr.get("weather_code", 0)
            zh_desc, en_desc = WMO_CODE_MAP.get(w_code, ("多云", "Cloudy"))
            weather_desc = f"{zh_desc} ({en_desc})"

            temp = curr.get("temperature_2m")
            apparent_temp = curr.get("apparent_temperature")
            humidity = curr.get("relative_humidity_2m")
            wind_speed = curr.get("wind_speed_10m")
            precip = curr.get("precipitation", 0.0)

            return {
                "success": True,
                "weather_desc": weather_desc,
                "temp": f"{temp:.1f}°C" if temp is not None else "未知",
                "feels_like": f"{apparent_temp:.1f}°C" if apparent_temp is not None else "未知",
                "humidity": f"{humidity}%" if humidity is not None else "未知",
                "wind": f"{wind_speed:.1f} km/h" if wind_speed is not None else "未知",
                "precipitation": f"{precip:.1f} mm" if precip is not None else "0 mm",
                "source": "Open-Meteo 实时气象",
                "timestamp": time.time(),
            }
    return None


async def _fetch_from_wttr(lat: float, lon: float) -> dict | None:
    """从 wttr.in 兜底拉取天气数据"""
    url = f"https://wttr.in/{lat},{lon}?format=j1"
    headers = {"User-Agent": "curl/7.68.0"}
    timeout = httpx.Timeout(connect=3.0, read=4.0, write=3.0, pool=3.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            curr_cond = data.get("current_condition", [])
            if curr_cond:
                curr = curr_cond[0]
                raw_desc = curr.get("weatherDesc", [{}])[0].get("value", "")
                weather_desc = translate_weather_desc(raw_desc)
                temp = curr.get("temp_C", "")
                feels_like = curr.get("FeelsLikeC", "")
                humidity = curr.get("humidity", "")
                wind = curr.get("windspeedKmph", "")

                return {
                    "success": True,
                    "weather_desc": weather_desc,
                    "temp": f"{temp}°C" if temp else "未知",
                    "feels_like": f"{feels_like}°C" if feels_like else "未知",
                    "humidity": f"{humidity}%" if humidity else "未知",
                    "wind": f"{wind} km/h" if wind else "未知",
                    "precipitation": "暂无",
                    "source": "wttr.in (备选兜底)",
                    "timestamp": time.time(),
                }
    return None


async def get_weather(lat: float = None, lon: float = None, address: str = None) -> dict:
    """
    获取实时天气，带 30 分钟网格缓存与自动降级机制
    """
    if lat is None or lon is None:
        last_loc = get_last_location()
        lat = last_loc["lat"]
        lon = last_loc["lon"]
        if not address:
            address = last_loc.get("address", "重庆市")

    grid_key = _get_grid_key(lat, lon)
    now_ts = time.time()

    # 1. 检查缓存 (30分钟以内有效)
    if grid_key in _WEATHER_CACHE:
        entry = _WEATHER_CACHE[grid_key]
        if now_ts - entry["timestamp"] < CACHE_TTL:
            res = dict(entry["data"])
            res["cached"] = True
            return res

    # 2. 尝试从 Open-Meteo 主数据源拉取
    try:
        data = await _fetch_from_open_meteo(lat, lon)
        if data:
            _WEATHER_CACHE[grid_key] = {"data": data, "timestamp": now_ts}
            return data
    except Exception as e:
        logger.warning(f"[WEATHER] Open-Meteo 抓取失败，尝试 wttr.in 兜底: {e}")

    # 3. 尝试从 wttr.in 备选数据源拉取
    try:
        data = await _fetch_from_wttr(lat, lon)
        if data:
            _WEATHER_CACHE[grid_key] = {"data": data, "timestamp": now_ts}
            return data
    except Exception as e:
        logger.error(f"[WEATHER] wttr.in 抓取亦失败: {e}")

    # 4. 若之前有过期缓存，继续返回过期缓存避免空置
    if grid_key in _WEATHER_CACHE:
        res = dict(_WEATHER_CACHE[grid_key]["data"])
        res["cached"] = True
        res["stale"] = True
        return res

    # 5. 极端异常情况：返回明确的防幻觉占位
    return {
        "success": False,
        "weather_desc": "天气数据暂不可用",
        "temp": "未知",
        "feels_like": "未知",
        "humidity": "未知",
        "wind": "未知",
        "source": "离线状态",
        "note": "气象接口暂时离线，请勿臆造天气数据",
        "timestamp": now_ts,
    }


async def get_weather_formatted(lat: float = None, lon: float = None, address: str = None) -> str:
    """
    格式化为统一的【当前天气】文本块
    """
    if lat is None or lon is None:
        last_loc = get_last_location()
        lat = last_loc["lat"]
        lon = last_loc["lon"]
        if not address:
            address = last_loc.get("address", "重庆市")

    w = await get_weather(lat, lon, address)

    if not w.get("success", False):
        return (
            f"【当前天气】\n"
            f"状态: 天气数据暂不可用\n"
            f"说明: 气象服务连接超时，请勿臆造天气数据"
        )

    loc_str = f"{lat:.6f}, {lon:.6f}"
    if address:
        loc_str += f" ({address})"

    return (
        f"【当前天气】\n"
        f"地点: {loc_str}\n"
        f"天气: {w['weather_desc']}\n"
        f"温度: {w['temp']} (体感: {w['feels_like']})\n"
        f"湿度: {w['humidity']}\n"
        f"风速: {w['wind']}\n"
        f"来源: {w['source']}"
    )


def get_weather_sync(lat: float = None, lon: float = None, address: str = None) -> str:
    """同步阻塞获取（用于模版变量替换）"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            grid_key = _get_grid_key(lat or 29.867, lon or 107.752)
            if grid_key in _WEATHER_CACHE:
                w = _WEATHER_CACHE[grid_key]["data"]
                return f"{w['weather_desc']} {w['temp']} (体感: {w['feels_like']})"
            return "天气数据加载中..."
        else:
            return loop.run_until_complete(get_weather_formatted(lat, lon, address))
    except Exception:
        return "天气数据暂不可用"
