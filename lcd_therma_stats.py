#!/usr/bin/env python3

import json
import os
import re
import struct
import subprocess
import threading
import time

import psutil
import requests
import usb.core
import usb.util
from PIL import Image, ImageDraw, ImageFilter, ImageFont


def log(msg):
    """StandardOutput=append in the systemd unit writes raw stdout straight
    to a file, bypassing journald's own timestamps - so without this, the
    log has no timing info at all, making it impossible to correlate a
    problem with anything else going on at the same moment."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# --- Panel / transport ---
# Thermalright Trofeo Vision 9.16" ultrawide (VID:PID 0416:5408), "LY" USB bulk
# transport. Protocol ported from the open-source thermalright-lcd-control
# project (https://github.com/rejeb/thermalright-lcd-control), which
# reverse-engineered it from the vendor's TRCC USBLCDNEW software.
#
# Unlike the 3.5" panels in the sibling `LCD` project, this transport has NO
# partial/region update command - every send pushes a full-frame JPEG. See
# HANDOVER.md for the full protocol writeup.

VID = 0x0416
PID = 0x5408
EP_OUT = 0x09
EP_IN = 0x81

WIDTH = 1920
HEIGHT = 480
SAMPLE_SECONDS = float(os.environ.get("LCD_SAMPLE_SECONDS", "2.0"))
JPEG_QUALITY = int(os.environ.get("LCD_JPEG_QUALITY", "85"))

_CHUNK_SIZE = 512
_CHUNK_HEADER_SIZE = 16
_CHUNK_DATA_SIZE = 496  # _CHUNK_SIZE - _CHUNK_HEADER_SIZE
_USB_WRITE_SIZE = 4096  # bytes per USB bulk write
_ACK_SIZE = 512
_CMD_LY = 1
_PAD_MULTIPLE_LY = 4

_HANDSHAKE = bytes([
    0x02, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
]) + bytes(2032)


class LyPanel:
    """Minimal LY-transport driver for the 0416:5408 panel. Sends a full JPEG
    frame per update; there is no way to redraw just part of the screen."""

    def __init__(self, timeout_ms=5000):
        self.timeout_ms = timeout_ms
        self.dev = None
        self._handshaked = False

    def open(self):
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise RuntimeError(f"Panel {VID:04x}:{PID:04x} not found")
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        dev.set_configuration()
        usb.util.claim_interface(dev, 0)
        self.dev = dev
        self._handshaked = False
        self._recover_endpoints()

    def close(self):
        if self.dev is not None:
            try:
                usb.util.release_interface(self.dev, 0)
            except usb.core.USBError:
                pass
            try:
                usb.util.dispose_resources(self.dev)
            except usb.core.USBError:
                pass
            self.dev = None

    def _recover_endpoints(self):
        for ep in (EP_OUT, EP_IN):
            try:
                self.dev.clear_halt(ep)
            except usb.core.USBError:
                pass
        self._drain_in()

    def _drain_in(self, max_reads=8, timeout_ms=20):
        for _ in range(max_reads):
            try:
                self.dev.read(EP_IN, _ACK_SIZE, timeout=timeout_ms)
            except usb.core.USBTimeoutError:
                break
            except usb.core.USBError:
                break

    def _handshake(self):
        self._drain_in()
        self.dev.write(EP_OUT, _HANDSHAKE, timeout=self.timeout_ms)
        resp = bytes(self.dev.read(EP_IN, _ACK_SIZE, timeout=self.timeout_ms))
        ok = len(resp) >= 9 and resp[0] == 3 and resp[1] == 0xFF and resp[8] == 1
        if not ok:
            log(
                f"LY handshake unexpected response (len={len(resp)}, "
                f"[0]={resp[0] if resp else None}, "
                f"[1]={resp[1] if len(resp) > 1 else None}, "
                f"[8]={resp[8] if len(resp) > 8 else None})"
            )
        else:
            log("LY handshake OK")
        self._handshaked = True

    def _build_chunks(self, payload: bytes) -> bytes:
        total_size = len(payload)
        num_chunks = total_size // _CHUNK_DATA_SIZE + 1
        last_chunk_data = total_size % _CHUNK_DATA_SIZE

        chunks = bytearray(num_chunks * _CHUNK_SIZE)
        for i in range(num_chunks):
            off = i * _CHUNK_SIZE
            is_last = i == num_chunks - 1
            data_len = last_chunk_data if is_last else _CHUNK_DATA_SIZE

            chunks[off] = 0x01
            chunks[off + 1] = 0xFF
            struct.pack_into("<I", chunks, off + 2, total_size)
            struct.pack_into("<H", chunks, off + 6, data_len)
            chunks[off + 8] = _CMD_LY
            struct.pack_into("<H", chunks, off + 9, num_chunks)
            struct.pack_into("<H", chunks, off + 11, i)

            src = i * _CHUNK_DATA_SIZE
            payload_start = off + _CHUNK_HEADER_SIZE
            chunks[payload_start:payload_start + data_len] = payload[src:src + data_len]

        # pad chunk count to a multiple of 4 (LY, as opposed to LY1 which pads to 1)
        remainder = num_chunks % _PAD_MULTIPLE_LY
        if remainder:
            chunks.extend(bytes((_PAD_MULTIPLE_LY - remainder) * _CHUNK_SIZE))

        return bytes(chunks)

    def send_image(self, img: Image.Image):
        if not self._handshaked:
            self._handshake()

        import io
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
        jpeg_bytes = buf.getvalue()

        send_buf = self._build_chunks(jpeg_bytes)
        t0 = time.time()
        for i in range(0, len(send_buf), _USB_WRITE_SIZE):
            self.dev.write(EP_OUT, send_buf[i:i + _USB_WRITE_SIZE], timeout=self.timeout_ms)
        resp = self.dev.read(EP_IN, _ACK_SIZE, timeout=self.timeout_ms)
        if len(resp) != _ACK_SIZE:
            log(f"LY frame ACK unexpected length: {len(resp)} (expected {_ACK_SIZE})")
        log(f"Frame sent OK ({len(jpeg_bytes)} bytes JPEG, {len(send_buf)} bytes wire, {time.time() - t0:.2f}s)")


# --- Data sources (ported unchanged from ../LCD/server_lcd_stats.py) ---

BG_COLOR = (10, 12, 18)
LABEL_COLOR = (150, 160, 175)
VALUE_COLOR = (255, 255, 255)

BTC_CURRENCY = "gbp"
BTC_REFRESH_SECONDS = 600

AGENTS_ONLINE_URL = "http://localhost:4001/v1/agents/online"
AGENTS_ONLINE_REFRESH_SECONDS = 15

BML_DEPARTURES_URL = "http://localhost:4001/v1/departures?limit=1"
TRAFFIC_REFRESH_SECONDS = 30

TIKTOK_NEW_USERNAME = os.environ.get("TIKTOK_NEW_USERNAME", "craigybabyj_new")
TIKTOK_MAIN_USERNAME = os.environ.get("TIKTOK_MAIN_USERNAME", "craigybabyj")
TIKTOK_REFRESH_SECONDS = 3600

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "UCjMx6UPBC8NXfbhfiCIDIoA")
YOUTUBE_REFRESH_SECONDS = 3600

LCD_COUNTERS_PATH = os.environ.get("LCD_COUNTERS_PATH", "/run/lcd-network-counters.json")


def _read_env_file(path):
    result = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    result[k.strip()] = v.strip()
    except OSError:
        pass
    return result


_hanger_env = _read_env_file("/home/craig/projects/hanger-bot/.env")
DISCORD_BOT_TOKEN = _hanger_env.get("DISCORD_TOKEN", "")

_bml_env = _read_env_file("/home/craig/projects/beatmylanding/api/.env")
BML_ADMIN_SECRET = _bml_env.get("API_ADMIN_INTERNAL_SECRET", "")
BML_DASHBOARD_URL = "http://localhost:4001/v1/internal/admin/dashboard?days=1"
BML_DASHBOARD_REFRESH_SECONDS = 60
DISCORD_GUILD_ID = "1047463607010603058"
DISCORD_API_URL = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}?with_counts=true"
DISCORD_REFRESH_SECONDS = 60


def get_net_counters():
    import json
    try:
        with open(LCD_COUNTERS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        counters = {"lan_rx": 0, "lan_tx": 0, "wan_rx": 0, "wan_tx": 0}
        for item in data.get("nftables", []):
            counter = item.get("counter")
            if not counter:
                continue
            name = counter.get("name")
            if name in counters:
                counters[name] = int(counter.get("bytes") or 0)
        counters["split"] = True
        return counters
    except Exception:
        total = psutil.net_io_counters()
        return {
            "lan_rx": 0,
            "lan_tx": 0,
            "wan_rx": int(total.bytes_recv),
            "wan_tx": int(total.bytes_sent),
            "split": False,
        }


def _mbps_delta(current, previous, key, elapsed):
    return max(0, current.get(key, 0) - previous.get(key, 0)) * 8 / elapsed / 1_000_000


def get_temp_c():
    try:
        temps = psutil.sensors_temperatures()
        preferred_names = ("coretemp", "k10temp", "cpu_thermal", "acpitz")
        for name in preferred_names:
            if name in temps:
                for entry in temps[name]:
                    current = getattr(entry, "current", None)
                    if current is not None:
                        return float(current)
        for _, entries in temps.items():
            for entry in entries:
                current = getattr(entry, "current", None)
                if current is not None:
                    return float(current)
    except Exception:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r", encoding="utf-8") as f:
            return float(f.read().strip()) / 1000.0
    except Exception:
        return None


PING_TARGET = "8.8.8.8"
PING_REFRESH_SECONDS = 5


def fetch_ping():
    """Returns round-trip ms, or -1 to mean "no reply" (distinct from None,
    which _schedule_fetch treats as "fetch failed, keep showing stale value")."""
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "1", PING_TARGET],
            capture_output=True, text=True, timeout=3,
        )
        match = re.search(r"time=([\d.]+)", r.stdout)
        return float(match.group(1)) if match else -1
    except Exception as e:
        print(f"Failed to ping {PING_TARGET}: {e}")
        return -1


FEAR_GREED_REFRESH_SECONDS = 1800


def fetch_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        r.raise_for_status()
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception as e:
        print(f"Failed to fetch Fear & Greed index: {e}")
        return None


# (name, latitude, longitude)
WEATHER_LOCATIONS = [
    ("Playa del Ingles", 27.7567, -15.5787),
    ("Newton Aycliffe", 54.61842, -1.5719),
]
WEATHER_REFRESH_SECONDS = 1800

WEATHER_CODES = {
    0: ("Clear", "☀"), 1: ("Mainly clear", "☀"), 2: ("Partly cloudy", "☁"), 3: ("Overcast", "☁"),
    45: ("Fog", "☁"), 48: ("Fog", "☁"),
    51: ("Light drizzle", "☔"), 53: ("Drizzle", "☔"), 55: ("Heavy drizzle", "☔"),
    61: ("Light rain", "☔"), 63: ("Rain", "☔"), 65: ("Heavy rain", "☔"),
    71: ("Light snow", "❄"), 73: ("Snow", "❄"), 75: ("Heavy snow", "❄"),
    80: ("Showers", "☔"), 81: ("Showers", "☔"), 82: ("Heavy showers", "☔"),
    95: ("Thunderstorm", "⚡"), 96: ("Thunderstorm", "⚡"), 99: ("Thunderstorm", "⚡"),
}


def fetch_weather():
    try:
        results = {}
        for name, lat, lon in WEATHER_LOCATIONS:
            url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code"
            )
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            current = r.json()["current"]
            temp = current["temperature_2m"]
            desc, icon = WEATHER_CODES.get(current["weather_code"], ("--", ""))
            results[name] = f"{icon} {temp:.0f}C {desc}"
        return results
    except Exception as e:
        print(f"Failed to fetch weather: {e}")
        return None


ADSENSE_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "gsc")
ADSENSE_OAUTH_CLIENT_PATH = os.path.join(ADSENSE_CONFIG_DIR, "oauth-client.json")
ADSENSE_TOKEN_PATH = os.path.join(ADSENSE_CONFIG_DIR, "adsense-token.json")
ADSENSE_ACCOUNT = "accounts/pub-6345720202915573"
ADSENSE_REFRESH_SECONDS = 900

GSC_TOKEN_PATH = os.path.join(ADSENSE_CONFIG_DIR, "token.json")
GSC_PROPERTY = "sc-domain:beatmyland.ing"
GSC_REFRESH_SECONDS = 1800


def _gsc_oauth_access_token(token_path):
    """Shared refresh-token -> access-token exchange for both the AdSense
    and Search Console tokens, which use the same OAuth client but separate
    per-scope token files."""
    with open(ADSENSE_OAUTH_CLIENT_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    client = raw.get("installed") or raw.get("web") or raw
    with open(token_path, "r", encoding="utf-8") as f:
        token = json.load(f)
    r = requests.post(
        client["token_uri"],
        data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _adsense_access_token():
    return _gsc_oauth_access_token(ADSENSE_TOKEN_PATH)


def fetch_seo_stats():
    try:
        access_token = _gsc_oauth_access_token(GSC_TOKEN_PATH)
        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
        import urllib.parse
        url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{urllib.parse.quote(GSC_PROPERTY, safe='')}/searchAnalytics/query"
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"startDate": start, "endDate": end, "dimensions": []},
            timeout=10,
        )
        r.raise_for_status()
        rows = r.json().get("rows") or []
        row = rows[0] if rows else {"clicks": 0, "impressions": 0}
        return {"clicks": int(row.get("clicks", 0)), "impressions": int(row.get("impressions", 0))}
    except Exception as e:
        print(f"Failed to fetch SEO stats: {e}")
        return None


def _adsense_day_earnings(access_token, day):
    url = f"https://adsense.googleapis.com/v2/{ADSENSE_ACCOUNT}/reports:generate"
    params = {
        "startDate.year": day.year, "startDate.month": day.month, "startDate.day": day.day,
        "endDate.year": day.year, "endDate.month": day.month, "endDate.day": day.day,
        "metrics": "ESTIMATED_EARNINGS",
        "reportingTimeZone": "ACCOUNT_TIME_ZONE",
    }
    r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    cells = (data.get("totals") or {}).get("cells") or []
    return float(cells[0]["value"]) if cells else 0.0


def fetch_adsense_earnings():
    try:
        access_token = _adsense_access_token()
        today = time.strftime("%Y-%m-%d")
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        import datetime
        today_d = datetime.date.fromisoformat(today)
        yesterday_d = datetime.date.fromisoformat(yesterday)
        return {
            "today": _adsense_day_earnings(access_token, today_d),
            "yesterday": _adsense_day_earnings(access_token, yesterday_d),
        }
    except Exception as e:
        print(f"Failed to fetch AdSense earnings: {e}")
        return None


def fetch_btc_price():
    url = f"https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies={BTC_CURRENCY}"
    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        return r.json()["bitcoin"][BTC_CURRENCY.lower()]
    except Exception as e:
        print(f"Failed to fetch BTC: {e}")
        return None


def fetch_eth_price():
    url = f"https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies={BTC_CURRENCY}"
    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        return r.json()["ethereum"][BTC_CURRENCY.lower()]
    except Exception as e:
        print(f"Failed to fetch ETH: {e}")
        return None


def fetch_latest_traffic_event():
    try:
        dep_r = requests.get(BML_DEPARTURES_URL, timeout=10)
        dep_r.raise_for_status()
        dep_items = dep_r.json().get("items") or []

        land_r = requests.get("http://localhost:4001/v1/landings?limit=1", timeout=10)
        land_r.raise_for_status()
        land_items = land_r.json().get("items") or []

        dep = dep_items[0] if dep_items else None
        land = land_items[0] if land_items else None

        dep_ts = dep.get("departedAt", "") if dep else ""
        land_ts = land.get("timestampZulu") or land.get("createdAt", "") if land else ""

        if dep and (not land or dep_ts >= land_ts):
            return {
                "pilot": (dep.get("pilotName") or "Unknown")[:10],
                "icao": dep.get("departureAirportIcao") or "????",
                "status": "Dept",
            }
        elif land:
            fpm = land.get("touchdownFpm")
            return {
                "pilot": (land.get("pilotName") or "Unknown")[:10],
                "icao": land.get("airportIcao") or "????",
                "status": str(int(fpm)) if fpm is not None else "--",
            }
        return None
    except Exception as e:
        print(f"Failed to fetch latest traffic: {e}")
        return None


def fetch_agents_online():
    try:
        r = requests.get(AGENTS_ONLINE_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        online = int(data.get("online_agents") or 0)
        entries = data.get("online_pilot_entries") or []
        sim = sum(1 for e in entries if e.get("latestSimConnected") is True)
        return {"online": online, "sim": sim}
    except Exception as e:
        print(f"Failed to fetch agents online: {e}")
        return None


def fetch_bml_today():
    try:
        headers = {"Authorization": f"Bearer {BML_ADMIN_SECRET}"}
        r = requests.get(BML_DASHBOARD_URL, headers=headers, timeout=10)
        r.raise_for_status()
        summary = r.json().get("summary") or {}
        return {
            "users_today": int(summary.get("newUsersToday") or 0),
            "landings_today": int(summary.get("landingsToday") or 0),
            "total_users": int(summary.get("totalUsers") or 0),
        }
    except Exception as e:
        print(f"Failed to fetch BML today stats: {e}")
        return None


def fetch_discord_stats():
    try:
        headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
        r = requests.get(DISCORD_API_URL, headers=headers, timeout=5)
        r.raise_for_status()
        data = r.json()
        return {
            "online": int(data.get("approximate_presence_count") or 0),
            "total": int(data.get("approximate_member_count") or 0),
        }
    except Exception as e:
        print(f"Failed to fetch Discord stats: {e}")
        return None


def fetch_youtube_stats():
    """One channels.list call (1 quota unit) returns both subscriberCount
    and viewCount, so subs and total views come from the same fetch."""
    if not YOUTUBE_API_KEY:
        return None
    try:
        url = (
            "https://www.googleapis.com/youtube/v3/channels"
            f"?part=statistics&id={YOUTUBE_CHANNEL_ID}&key={YOUTUBE_API_KEY}"
        )
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        items = r.json().get("items") or []
        if not items:
            return None
        stats = items[0]["statistics"]
        return {
            "subs": int(stats["subscriberCount"]),
            "views": int(stats["viewCount"]),
        }
    except Exception as e:
        print(f"Failed to fetch YouTube stats: {e}")
        return None


def format_yt_subs(count):
    if count is None:
        return "--"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


TIKTOK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def fetch_tiktok_followers(username):
    try:
        url = f"https://www.tiktok.com/@{username}"
        r = requests.get(url, headers=TIKTOK_HEADERS, timeout=10)
        r.raise_for_status()
        match = __import__("re").search(r'"followerCount":(\d+)', r.text)
        return int(match.group(1)) if match else None
    except Exception as e:
        print(f"Failed to fetch TikTok followers for {username}: {e}")
        return None


def format_btc_price(price):
    if price is None:
        return "--"
    currency_symbols = {"usd": "$", "gbp": "£", "eur": "€"}
    sym = currency_symbols.get(BTC_CURRENCY.lower(), "")
    return f"{sym}{int(price)}"


def sample_metrics(prev_net, prev_time):
    now = time.time()
    elapsed = max(now - prev_time, 0.001)

    cpu = round(psutil.cpu_percent(interval=None))
    mem = round(psutil.virtual_memory().percent)
    swap = round(psutil.swap_memory().percent)

    net = get_net_counters()
    lan_rx_mbps = _mbps_delta(net, prev_net, "lan_rx", elapsed)
    lan_tx_mbps = _mbps_delta(net, prev_net, "lan_tx", elapsed)
    wan_rx_mbps = _mbps_delta(net, prev_net, "wan_rx", elapsed)
    wan_tx_mbps = _mbps_delta(net, prev_net, "wan_tx", elapsed)

    import shutil
    du = shutil.disk_usage("/")
    disk = round(du.used / du.total * 100)
    temp_c = get_temp_c()

    uptime_s = int(now - psutil.boot_time())
    days, rem = divmod(uptime_s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    uptime = f"{days}d {hours}h" if days else f"{hours}h {minutes}m"

    load1, _, _ = os.getloadavg()

    values = {
        "cpu": f"{cpu}%",
        "mem": f"{mem}%",
        "swap": f"{swap}%",
        "disk": f"{disk}%",
        "temp": f"{temp_c:.1f}C" if temp_c is not None else "--",
        "lan": f"{lan_rx_mbps:.1f}/{lan_tx_mbps:.1f}",
        "wan": f"{wan_rx_mbps:.1f}/{wan_tx_mbps:.1f}",
        "uptime": uptime,
        "load": f"{load1:.2f}",
    }
    return values, net, now


# --- Rendering ---
# Same stats, groupings, and label-above-value convention as the LCD
# project's 320x480 layout, laid out across four columns and spread to fill
# the full 480px height instead of the 3.5" panel's tightly stacked rows.

def load_font(paths, size):
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


LABEL_FONT = load_font(["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"], 26)
VALUE_FONT = load_font(["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"], 42)
SMALL_LABEL_FONT = load_font(["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"], 24)
HERO_VALUE_FONT = load_font(["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"], 54)
MEDIUM_VALUE_FONT = load_font(["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"], 30)

ASSET_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_logo(path, height):
    """Kept as RGBA (not flattened onto a solid background) so it composites
    cleanly over the gradient background instead of showing a solid block."""
    try:
        raw = Image.open(path).convert("RGBA")
        w = int(raw.width * height / raw.height)
        return raw.resize((w, height), Image.LANCZOS)
    except Exception as e:
        print(f"Could not load logo {path}: {e}")
        return None


BTC_LOGO = _load_logo(os.path.join(ASSET_DIR, "btc_logo.png"), 34)
ETH_LOGO = _load_logo(os.path.join(ASSET_DIR, "eth_logo.png"), 34)
DISCORD_LOGO = _load_logo(os.path.join(ASSET_DIR, "discord_logo.png"), 32)
YOUTUBE_LOGO = _load_logo(os.path.join(ASSET_DIR, "youtube_logo.png"), 32)
TIKTOK_LOGO = _load_logo(os.path.join(ASSET_DIR, "tiktok_logo.png"), 32)
BML_LOGO = _load_logo(os.path.join(ASSET_DIR, "bml_logo.png"), 32)

COL_W = WIDTH // 4


TREND_UP_COLOR = (46, 204, 113)
TREND_DOWN_COLOR = (231, 76, 60)


def _stat(draw, img, x, y, label, value, logo=None, label_font=LABEL_FONT, value_font=VALUE_FONT, trend=None):
    """Same label-above-value convention as the LCD project, just bigger.
    trend, if given, is "up" or "down" and draws a small coloured arrow
    after the value."""
    lx = x
    if logo:
        img.paste(logo, (lx, y - 2), logo)
        lx += logo.width + 10
    draw.text((lx, y), label, font=label_font, fill=LABEL_COLOR)
    value_y = y + label_font.size + 6
    draw.text((x, value_y), value, font=value_font, fill=VALUE_COLOR)
    if trend in ("up", "down"):
        value_w = draw.textlength(value, font=value_font)
        arrow = "▲" if trend == "up" else "▼"
        color = TREND_UP_COLOR if trend == "up" else TREND_DOWN_COLOR
        draw.text((x + value_w + 14, value_y), arrow, font=value_font, fill=color)


def _build_background():
    """Subtle diagonal gradient + soft vignette, built once and reused every
    frame rather than a flat fill - a bit of depth without being distracting
    or interfering with text legibility."""
    TOP_LEFT = (44, 50, 72)
    BOTTOM_RIGHT = (6, 7, 10)

    tiny = Image.new("RGB", (2, 2), BG_COLOR)
    tiny.putpixel((0, 0), TOP_LEFT)
    tiny.putpixel((1, 0), tuple((a + b) // 2 for a, b in zip(TOP_LEFT, BOTTOM_RIGHT)))
    tiny.putpixel((0, 1), tuple((a + b) // 2 for a, b in zip(TOP_LEFT, BOTTOM_RIGHT)))
    tiny.putpixel((1, 1), BOTTOM_RIGHT)
    bg = tiny.resize((WIDTH, HEIGHT), Image.BICUBIC)

    vignette = Image.new("L", (WIDTH, HEIGHT), 0)
    vdraw = ImageDraw.Draw(vignette)
    margin = -40
    vdraw.ellipse((margin, margin, WIDTH - margin, HEIGHT - margin), fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(160))

    darker = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    return Image.composite(bg, darker, vignette)


BACKGROUND = _build_background()


def render_frame(state):
    img = BACKGROUND.copy()
    draw = ImageDraw.Draw(img)

    for i in range(1, 4):
        x = i * COL_W
        draw.line([(x, 20), (x, HEIGHT - 20)], fill=LABEL_COLOR, width=1)

    pad = 24
    right_off = COL_W // 2 + 4

    # Column 0: system — CPU/Load, SWAP/MEM, TEMP/DISK, Ping/Uptime, LAN/WAN,
    # paired two-per-line and spread to fill the full height.
    x0 = pad
    row_h = 88
    y = (HEIGHT - 5 * row_h) // 2
    _stat(draw, img, x0, y, "CPU", state.get("cpu", "--"))
    _stat(draw, img, x0 + right_off, y, "Load", state.get("load", "--"))
    y += row_h
    _stat(draw, img, x0, y, "SWAP", state.get("swap", "--"))
    _stat(draw, img, x0 + right_off, y, "MEM", state.get("mem", "--"))
    y += row_h
    _stat(draw, img, x0, y, "TEMP", state.get("temp", "--"))
    _stat(draw, img, x0 + right_off, y, "DISK", state.get("disk", "--"))
    y += row_h
    _stat(draw, img, x0, y, "Ping 8.8.8.8", state.get("ping", "--"))
    _stat(draw, img, x0 + right_off, y, "Uptime", state.get("uptime", "--"))
    y += row_h
    _stat(draw, img, x0, y, "LAN Mbps", state.get("lan", "--"))
    _stat(draw, img, x0 + right_off, y, "WAN Mbps", state.get("wan", "--"))

    # Column 1: crypto — BTC/ETH up top, Fear & Greed index below, a
    # separator, then weather for Playa del Ingles and Newton Aycliffe.
    x1 = COL_W + pad
    hero_row_h = 100
    small_row_h = 66
    sep_gap = 16
    y = (HEIGHT - (2 * hero_row_h + 3 * small_row_h + sep_gap)) // 2
    _stat(draw, img, x1, y, "BTC", state.get("btc", "--"), logo=BTC_LOGO, value_font=HERO_VALUE_FONT, trend=state.get("btc_trend"))
    y += hero_row_h
    _stat(draw, img, x1, y, "ETH", state.get("eth", "--"), logo=ETH_LOGO, value_font=HERO_VALUE_FONT, trend=state.get("eth_trend"))
    y += hero_row_h
    _stat(draw, img, x1, y, "Fear & Greed", state.get("fear_greed", "--"), value_font=MEDIUM_VALUE_FONT)
    y += small_row_h
    draw.line([(x1, y + sep_gap // 2), (2 * COL_W - pad, y + sep_gap // 2)], fill=LABEL_COLOR, width=1)
    y += sep_gap
    _stat(draw, img, x1, y, "Playa del Ingles", state.get("weather_playa", "--"), value_font=MEDIUM_VALUE_FONT)
    y += small_row_h
    _stat(draw, img, x1, y, "Newton Aycliffe", state.get("weather_newton", "--"), value_font=MEDIUM_VALUE_FONT)

    # Column 2: BeatMyLanding — total users/online/flying split out, new
    # users/landings today paired, latest traffic alongside landings,
    # SEO clicks/impressions.
    x2 = 2 * COL_W + pad
    row_h = 90
    last_row_content_h = LABEL_FONT.size + 6 + VALUE_FONT.size
    y = (HEIGHT - (3 * row_h + last_row_content_h)) // 2
    _stat(draw, img, x2, y, "Total Users", state.get("bml_total", "--"), logo=BML_LOGO)
    _stat(draw, img, x2 + right_off, y, "Online", state.get("bml_online", "--"))
    y += row_h
    _stat(draw, img, x2, y, "Flying", state.get("bml_flying", "--"))
    _stat(draw, img, x2 + right_off, y, "New users", state.get("bml_usrs", "--"))
    y += row_h
    _stat(draw, img, x2, y, "Landings", state.get("bml_lnds", "--"))
    tx = x2 + right_off
    draw.text((tx, y), "Latest traffic", font=LABEL_FONT, fill=LABEL_COLOR)
    traffic_line_y = y + LABEL_FONT.size + 6
    draw.text((tx, traffic_line_y), state.get("traffic_who", "--"), font=SMALL_LABEL_FONT, fill=VALUE_COLOR)
    draw.text((tx, traffic_line_y + SMALL_LABEL_FONT.size + 4), state.get("traffic_where", "--"), font=SMALL_LABEL_FONT, fill=VALUE_COLOR)
    traffic_block_h = LABEL_FONT.size + 6 + 2 * SMALL_LABEL_FONT.size + 4
    y += max(row_h - 20, traffic_block_h + 10)
    draw.line([(x2, y), (3 * COL_W - pad, y)], fill=LABEL_COLOR, width=1)
    y += 20
    _stat(draw, img, x2, y, "Clicks (7d)", state.get("seo_clicks", "--"))
    _stat(draw, img, x2 + right_off, y, "Impressions", state.get("seo_impressions", "--"))

    # Column 3: social — Discord, YouTube, TikTok, AdSense, each a paired row.
    x3 = 3 * COL_W + pad
    row_h = 90
    last_row_content_h = LABEL_FONT.size + 6 + VALUE_FONT.size
    y = (HEIGHT - (3 * row_h + last_row_content_h)) // 2
    _stat(draw, img, x3, y, "Online", state.get("discord_online", "--"), logo=DISCORD_LOGO)
    _stat(draw, img, x3 + right_off, y, "Members", state.get("discord_total", "--"), logo=DISCORD_LOGO)
    y += row_h
    _stat(draw, img, x3, y, "Subscribers", state.get("yt", "--"), logo=YOUTUBE_LOGO)
    _stat(draw, img, x3 + right_off, y, "Total Views", state.get("yt_views", "--"), logo=YOUTUBE_LOGO)
    y += row_h
    _stat(draw, img, x3, y, "New", state.get("tiktok_new", "--"), logo=TIKTOK_LOGO)
    _stat(draw, img, x3 + right_off, y, "Old", state.get("tiktok_main", "--"), logo=TIKTOK_LOGO)
    y += row_h
    _stat(draw, img, x3, y, "Today", state.get("adsense_today", "--"))
    _stat(draw, img, x3 + right_off, y, "Yesterday", state.get("adsense_yesterday", "--"))

    return img


# --- Background fetch scheduling (same pattern as ../LCD/server_lcd_stats.py) ---

_pending = {}
_pending_lock = threading.Lock()


def _schedule_fetch(key, fn):
    def worker():
        result = fn()
        if result is not None:
            with _pending_lock:
                _pending[key] = result
    threading.Thread(target=worker, daemon=True).start()


def main():
    log(f"Opening Trofeo Vision panel {VID:04x}:{PID:04x}...")
    panel = LyPanel()
    panel.open()
    log("Panel opened.")

    psutil.cpu_percent(interval=None)
    prev_net = get_net_counters()
    prev_time = time.time()

    state = {}
    last_btc_fetch_time = 0
    last_agents_fetch_time = 0
    last_bml_today_fetch_time = 0
    last_traffic_fetch_time = 0
    last_discord_fetch_time = 0
    last_youtube_fetch_time = 0
    last_tiktok_fetch_time = 0
    last_ping_fetch_time = 0
    last_adsense_fetch_time = 0
    last_seo_fetch_time = 0
    last_fear_greed_fetch_time = 0
    last_weather_fetch_time = 0

    last_bml_today = None
    last_agents_online = None
    last_btc_price = None
    last_eth_price = None

    last_sent_state = None

    while True:
        try:
            now = time.time()

            with _pending_lock:
                pending = dict(_pending)
                _pending.clear()
            if "btc" in pending:
                price = pending["btc"]
                if last_btc_price is not None and price != last_btc_price:
                    state["btc_trend"] = "up" if price > last_btc_price else "down"
                last_btc_price = price
                state["btc"] = format_btc_price(price)
            if "eth" in pending:
                price = pending["eth"]
                if last_eth_price is not None and price != last_eth_price:
                    state["eth_trend"] = "up" if price > last_eth_price else "down"
                last_eth_price = price
                state["eth"] = format_btc_price(price)
            if "agents" in pending:
                last_agents_online = pending["agents"]
            if "traffic" in pending:
                t = pending["traffic"]
                state["traffic_who"] = t["pilot"]
                state["traffic_where"] = f"{t['icao']} {t['status']}"
            if "bml_today" in pending:
                last_bml_today = pending["bml_today"]
                state["bml_usrs"] = str(last_bml_today["users_today"])
                state["bml_lnds"] = str(last_bml_today["landings_today"])
            if "discord" in pending:
                d = pending["discord"]
                state["discord_online"] = str(d["online"])
                state["discord_total"] = str(d["total"])
            if "youtube" in pending:
                yt = pending["youtube"]
                state["yt"] = format_yt_subs(yt["subs"])
                state["yt_views"] = format_yt_subs(yt["views"])
            if "tiktok_new" in pending:
                state["tiktok_new"] = format_yt_subs(pending["tiktok_new"])
            if "tiktok_main" in pending:
                state["tiktok_main"] = format_yt_subs(pending["tiktok_main"])
            if "ping" in pending:
                ms = pending["ping"]
                state["ping"] = f"{ms:.0f}ms" if ms >= 0 else "Timeout"
            if "adsense" in pending:
                a = pending["adsense"]
                state["adsense_today"] = f"£{a['today']:.2f}"
                state["adsense_yesterday"] = f"£{a['yesterday']:.2f}"
            if "seo" in pending:
                s = pending["seo"]
                state["seo_clicks"] = str(s["clicks"])
                state["seo_impressions"] = str(s["impressions"])
            if "fear_greed" in pending:
                fg = pending["fear_greed"]
                state["fear_greed"] = f"{fg['value']} {fg['label']}"
            if "weather" in pending:
                w = pending["weather"]
                state["weather_playa"] = w.get("Playa del Ingles", "--")
                state["weather_newton"] = w.get("Newton Aycliffe", "--")

            if last_agents_online is not None:
                state["bml_online"] = str(last_agents_online["online"])
                state["bml_flying"] = str(last_agents_online["sim"])
            if last_bml_today is not None:
                state["bml_total"] = str(last_bml_today["total_users"])

            if now - last_btc_fetch_time >= BTC_REFRESH_SECONDS or last_btc_fetch_time == 0:
                last_btc_fetch_time = now
                log("Scheduling BTC/ETH price fetch (10-min interval)")
                _schedule_fetch("btc", fetch_btc_price)
                _schedule_fetch("eth", fetch_eth_price)
            if now - last_agents_fetch_time >= AGENTS_ONLINE_REFRESH_SECONDS or last_agents_fetch_time == 0:
                last_agents_fetch_time = now
                _schedule_fetch("agents", fetch_agents_online)
            if now - last_traffic_fetch_time >= TRAFFIC_REFRESH_SECONDS or last_traffic_fetch_time == 0:
                last_traffic_fetch_time = now
                _schedule_fetch("traffic", fetch_latest_traffic_event)
            if now - last_bml_today_fetch_time >= BML_DASHBOARD_REFRESH_SECONDS or last_bml_today_fetch_time == 0:
                last_bml_today_fetch_time = now
                _schedule_fetch("bml_today", fetch_bml_today)
            if now - last_discord_fetch_time >= DISCORD_REFRESH_SECONDS or last_discord_fetch_time == 0:
                last_discord_fetch_time = now
                _schedule_fetch("discord", fetch_discord_stats)
            if now - last_youtube_fetch_time >= YOUTUBE_REFRESH_SECONDS or last_youtube_fetch_time == 0:
                last_youtube_fetch_time = now
                _schedule_fetch("youtube", fetch_youtube_stats)
            if now - last_tiktok_fetch_time >= TIKTOK_REFRESH_SECONDS or last_tiktok_fetch_time == 0:
                last_tiktok_fetch_time = now
                _schedule_fetch("tiktok_new", lambda: fetch_tiktok_followers(TIKTOK_NEW_USERNAME))
                _schedule_fetch("tiktok_main", lambda: fetch_tiktok_followers(TIKTOK_MAIN_USERNAME))
            if now - last_ping_fetch_time >= PING_REFRESH_SECONDS or last_ping_fetch_time == 0:
                last_ping_fetch_time = now
                _schedule_fetch("ping", fetch_ping)
            if now - last_adsense_fetch_time >= ADSENSE_REFRESH_SECONDS or last_adsense_fetch_time == 0:
                last_adsense_fetch_time = now
                _schedule_fetch("adsense", fetch_adsense_earnings)
            if now - last_seo_fetch_time >= GSC_REFRESH_SECONDS or last_seo_fetch_time == 0:
                last_seo_fetch_time = now
                _schedule_fetch("seo", fetch_seo_stats)
            if now - last_fear_greed_fetch_time >= FEAR_GREED_REFRESH_SECONDS or last_fear_greed_fetch_time == 0:
                last_fear_greed_fetch_time = now
                _schedule_fetch("fear_greed", fetch_fear_greed)
            if now - last_weather_fetch_time >= WEATHER_REFRESH_SECONDS or last_weather_fetch_time == 0:
                last_weather_fetch_time = now
                _schedule_fetch("weather", fetch_weather)

            values, prev_net, prev_time = sample_metrics(prev_net, prev_time)
            state.update(values)

            # Full-frame-only protocol: only bother re-encoding/sending when
            # something actually changed, since every send is comparatively
            # expensive (JPEG encode + full 1920x480 push).
            if state != last_sent_state:
                frame = render_frame(state)
                panel.send_image(frame)
                last_sent_state = dict(state)

            time.sleep(SAMPLE_SECONDS)

        except KeyboardInterrupt:
            log("Stopped.")
            break
        except (usb.core.USBError, RuntimeError, AttributeError) as e:
            # RuntimeError: panel.open() raises this when the device isn't
            # found (e.g. mid unplug/replug). AttributeError: panel.dev is
            # None because a prior open() failed - without catching these
            # too, only a clean USBError would trigger a reopen attempt,
            # and any other failure mode would just log forever without
            # ever retrying (observed when the panel moved USB ports).
            log(f"Panel unusable, reopening: {e!r}")
            panel.close()
            time.sleep(2.0)
            try:
                panel.open()
                log("Panel reopened OK.")
            except Exception as e2:
                log(f"Reopen failed: {e2!r}")
                time.sleep(2.0)
        except Exception as e:
            log(f"Update failed: {e!r}")
            time.sleep(1.0)

    panel.close()


if __name__ == "__main__":
    main()
