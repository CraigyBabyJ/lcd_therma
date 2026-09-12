# Trofeo Vision LCD Stats Display

Python service for a Thermalright Trofeo Vision 9.16" USB-C LCD panel (1920x480). It renders system stats and live service counters into a full-frame image, JPEG-encodes it, and pushes it to the panel over `pyusb` using the vendor's "LY" bulk transport protocol.

Spun off from the sibling [`LCD`](../LCD) project, which drives Craig's original 3.5" panel — the data-fetching logic (crypto prices, BeatMyLanding, Discord, YouTube, TikTok, system stats) is shared/ported from there, but the rendering is a fresh landscape layout designed for this panel's much larger, wider screen.

## Panel / Protocol

- **Panel**: Thermalright Trofeo Vision 9.16" ultrawide, 1920x480, USB-C.
- **USB ID**: `0416:5408` (Winbond Electronics Corp., product string "USBDISPLAY"). Vendor-specific USB class with two bulk endpoints — **not** a CDC/serial device, no `/dev/ttyACM*` node. Uses `pyusb` directly.
- **Endpoints**: `0x81` (bulk IN), `0x09` (bulk OUT).
- **Transport**: reverse-engineered from the open-source [`thermalright-lcd-control`](https://github.com/rejeb/thermalright-lcd-control) project. One-time handshake (fixed 2048-byte packet, expects a specific 512-byte ACK pattern), then every frame is JPEG-encoded, split into 512-byte chunks (16-byte header + 496 bytes data), and sent as 4096-byte USB bulk writes.
- **No partial/region updates** — unlike the `LCD` project's panels, this protocol has no "set window" command. Every update replaces the entire screen, so the code only re-encodes and sends when the rendered state actually changes (`state != last_sent_state` guard in the main loop).

## Layout

Four columns across the full 1920x480 canvas, each column's rows vertically centered to use the whole height rather than clustering at the top:

- **System**: CPU/Load, SWAP/MEM, TEMP/DISK, Ping (8.8.8.8)/Uptime, LAN/WAN throughput
- **Crypto**: BTC/ETH (GBP, with green/red trend arrows on price moves), Crypto Fear & Greed Index, weather for Playa del Ingles and Newton Aycliffe
- **BeatMyLanding**: total users/online/currently flying, new users/landings today, latest traffic event, Search Console clicks/impressions (7d)
- **Social**: Discord online/members, YouTube subscribers/total views, TikTok new/old account followers, AdSense earnings (today/yesterday)

Background is a subtle diagonal gradient with a soft corner vignette, built once and cached rather than recomputed per frame.

## Project Files

- `lcd_therma_stats.py` - main display renderer, USB transport (`LyPanel`), and data fetchers
- `lcd_therma.service` - systemd unit for running the display at boot
- `lcd_therma.rules` - udev rule so the panel is writable by a normal user, not just root
- `scripts/lcd-network-counters-setup` - live nftables LAN/WAN counter setup used by the shared writer service
- `*_logo.png` - icons rendered on the display (`bml_logo.png` rasterized from BeatMyLanding's favicon SVG via `cairosvg`, one-time build step, not a runtime dependency)
- `.env.example` - optional environment variables
- `requirements.txt` - Python dependencies
- `HANDOVER.md` - detailed original build notes/context

Runtime files such as `lcd_therma.log`, the virtualenv, and Python cache files are intentionally ignored by git.

## Setup

```bash
cd /home/craig/projects/lcd_therma
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Optional local config:

```bash
cp .env.example .env
```

`.env` only needs `YOUTUBE_API_KEY`/`YOUTUBE_CHANNEL_ID` and TikTok usernames (which already have sane defaults) — Discord and BeatMyLanding credentials are read directly from the sibling projects' own `.env` files (see External Integrations below).

### udev rule (required for non-root access)

The panel is owned `root:root` by default. Install the udev rule once so a normal user can open it:

```bash
sudo cp lcd_therma.rules /etc/udev/rules.d/99-lcd-therma.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Run Manually

```bash
cd /home/craig/projects/lcd_therma
./venv/bin/python lcd_therma_stats.py
```

(Needs `sudo` if the udev rule above hasn't been installed yet.)

## Install As A Service

```bash
sudo cp lcd_therma.service /etc/systemd/system/lcd_therma.service
sudo systemctl daemon-reload
sudo systemctl enable --now lcd_therma.service
```

Runs as `User=craig` — requires the udev rule to be installed first.

The service also runs `scripts/lcd-wait-for-wan` before the display process.
This checks real DNS and outbound HTTPS connectivity because
`network-online.target` can be reached while the router/WAN is still coming
back after a reboot. Without the gate, long-refresh sources such as YouTube,
TikTok, weather, Bitcoin, AdSense, and Search Console can stay blank until
their next scheduled refresh. The check host, URL, and retry delay can be
overridden with `LCD_WAN_DNS_HOST`, `LCD_WAN_CHECK_URL`, and
`LCD_WAN_RETRY_SECONDS`.

Logs are written to:

```text
/home/craig/projects/lcd_therma/lcd_therma.log
```

Useful checks:

```bash
systemctl status lcd_therma.service --no-pager
tail -80 /home/craig/projects/lcd_therma/lcd_therma.log
lsusb -d 0416:5408
```

## External Integrations

- **BeatMyLanding API** at `http://localhost:4001` (admin secret from `/home/craig/projects/beatmylanding/api/.env`)
- **Discord bot token** from `/home/craig/projects/hanger-bot/.env`
- **YouTube Data API** key/channel from this project's own `.env`
- **TikTok** — public profile page scraping, no auth
- **CoinGecko** — BTC/ETH prices, no auth
- **Crypto Fear & Greed Index** ([alternative.me](https://alternative.me/crypto/fear-and-greed-index/)) — no auth
- **Open-Meteo** — weather, no auth, no API key
- **Google AdSense Management API** and **Search Console API** — OAuth via `~/.config/gsc/oauth-client.json`, refresh tokens in `~/.config/gsc/adsense-token.json` and `~/.config/gsc/token.json` (shared with the `seo-tools` project's `adsense-check.mjs`/`seo-check.mjs`). The OAuth app is published to production so these refresh tokens don't expire on Google's 7-day Testing-mode timer.

If any of these services or credentials are unavailable, the display keeps running and shows `--`/stale values for the affected rows rather than crashing.

## LAN/WAN Counters

The display reads split LAN/WAN traffic counters from `/run/lcd-network-counters.json`, written by `lcd-network-counters-writer.service`.

The live setup script is mirrored in `scripts/lcd-network-counters-setup`. It classifies both direct host traffic and Docker-forwarded traffic, so LAN clients using Docker-published services count against LAN rather than WAN.

## Troubleshooting

If the screen stays blank:

1. Check the device is visible and has the right permissions:

   ```bash
   lsusb -d 0416:5408
   ls -l /dev/bus/usb/*/*   # find the matching node, should be crw-rw-rw-
   ```

   If it's not `crw-rw-rw-`, the udev rule likely isn't installed — see Setup above.

2. Check the service log:

   ```bash
   tail -80 /home/craig/projects/lcd_therma/lcd_therma.log
   ```

3. USB errors mid-run (unplug/replug, power event) are caught and retried automatically — the service closes and reopens the panel handle every 2s until it reconnects, rather than crash-looping. If the panel is wedged rather than just momentarily gone (soft reopen keeps failing), after `LCD_RESET_AFTER_FAILURES` consecutive failures (default 5) it escalates to an actual USB port reset (`USBDEVFS_RESET`, the same recovery a physical unplug/replug gives you) before continuing to retry.
