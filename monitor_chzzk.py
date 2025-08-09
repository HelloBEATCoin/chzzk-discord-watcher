#!/usr/bin/env python3
"""
Monitor multiple Chzzk channels and dispatch Discord notifications via webhooks.

This script reads a YAML configuration file (config.yaml) that describes
the channels to monitor, global polling interval and viewer threshold values.
It persists channel state in a JSON file (state.json) to avoid duplicate
notifications between runs.  The intended use is with GitHub Actions on a
schedule, but it also works when invoked locally.

Features:
  • Detects live start and end events.
  • Notifies on title changes and category changes while live.
  • Alerts when concurrent viewer counts cross configured thresholds.
  • Supports per‑streamer Discord role mentions via configuration.

The Chzzk API endpoints used here are unofficial and may change without
notice.  Multiple endpoints are queried with graceful fallbacks.  Headers
imitating a real browser are sent to avoid HTTP 403 errors.

Environment:
  Webhook URLs should be provided either directly in config.yaml or via
  environment variables.  Placeholders of the form ${VAR_NAME} will be
  expanded using os.environ at runtime.

Example config.yaml:

    poll_interval_seconds: 300
    viewer_thresholds: [100, 300, 500, 1000]
    streamers:
      - name: Mbeung
        channel_id: a872c0594e60f943748d76c565dd3a07
        chzzk_url: https://chzzk.naver.com/a872c0594e60f943748d76c565dd3a07
        webhook_url: ${WEBHOOK_MBEUNG}
        discord_role_id: null
      - name: Nari
        channel_id: 1755e5012c4dcd4eb94aec03205d6201
        chzzk_url: https://chzzk.naver.com/1755e5012c4dcd4eb94aec03205d6201
        webhook_url: ${WEBHOOK_NARI}
        discord_role_id: null

"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import yaml


CONFIG_FILE = Path(__file__).with_name("config.yaml")
STATE_FILE = Path(__file__).with_name("state.json")


async def fetch_live_info(session: aiohttp.ClientSession, channel_id: str) -> Dict[str, Any]:
    """Query Chzzk endpoints to obtain current live status and metadata.

    Returns a dictionary with at least the key `is_live`.  When available,
    additional keys include `title`, `category`, `viewers` (concurrent
    viewer count) and `status` (raw status string from API).
    """
    base = "https://api.chzzk.naver.com"
    # Standard headers to mimic a browser; Referer and Origin headers help avoid 403.
    headers = {
        "Referer": "https://chzzk.naver.com/",
        "Origin": "https://chzzk.naver.com",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }

    # First attempt: service/v1/channels/{id}/live-detail
    url_live_detail = f"{base}/service/v1/channels/{channel_id}/live-detail"
    try:
        async with session.get(url_live_detail, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data.get("content", {})
                status = content.get("status")
                # Determine live flag from status field.
                is_live = False
                if status:
                    is_live = status.upper() not in ("CLOSE", "ENDED", "IDLE")
                title = content.get("liveTitle")
                # Prefer localized category value, fall back to slug.
                category = content.get("liveCategoryValue") or content.get("liveCategory")
                viewers = content.get("concurrentUserCount") or content.get("watcherCount")
                return {
                    "is_live": bool(is_live),
                    "title": title,
                    "category": category,
                    "viewers": viewers,
                    "status": status,
                }
    except Exception:
        # ignore network/parsing errors in this attempt
        pass

    # Second attempt: service/v1/channels/{id}
    url_channel = f"{base}/service/v1/channels/{channel_id}"
    try:
        async with session.get(url_channel, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data.get("content", {})
                # openLive is True when channel is actively streaming
                is_live = bool(content.get("openLive"))
                return {
                    "is_live": is_live,
                    "title": None,
                    "category": None,
                    "viewers": None,
                    "status": None,
                }
    except Exception:
        pass

    # Third attempt: polling/v2/channels/{id}/live-status (may return just status)
    url_polling = f"{base}/polling/v2/channels/{channel_id}/live-status"
    try:
        async with session.get(url_polling, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data.get("content", {})
                # status may be ACTIVE / INACTIVE
                status = content.get("status")
                is_live = status and status.upper() == "ACTIVE"
                return {
                    "is_live": bool(is_live),
                    "title": None,
                    "category": None,
                    "viewers": None,
                    "status": status,
                }
    except Exception:
        pass

    # Default: offline
    return {"is_live": False}


def load_config(path: Path) -> Dict[str, Any]:
    """Load YAML configuration file and substitute environment variables."""
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if cfg is None:
        cfg = {}

    # Expand environment variables in webhook URLs
    for streamer in cfg.get("streamers", []):
        webhook = streamer.get("webhook_url")
        if isinstance(webhook, str) and "${" in webhook:
            # Simple variable expansion: ${VAR_NAME}
            key = webhook.strip()[2:-1]
            env_val = os.environ.get(key)
            if env_val:
                streamer["webhook_url"] = env_val
    return cfg


def load_state(path: Path) -> Dict[str, Any]:
    """Load persisted state from JSON; if absent, return empty dict."""
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            # ignore corrupt state
            return {}
    return {}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    """Persist state to disk."""
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


async def send_discord_message(session: aiohttp.ClientSession, webhook_url: str, content: str, embed: Optional[Dict[str, Any]]) -> None:
    """Send a message to Discord via webhook."""
    payload: Dict[str, Any] = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    try:
        async with session.post(webhook_url, json=payload, timeout=10) as resp:
            if resp.status >= 400:
                text = await resp.text()
                print(f"Discord webhook responded with status {resp.status}: {text}")
    except Exception as e:
        print(f"Failed to send Discord message: {e}")


def format_discord_message(event_type: str, streamer: Dict[str, Any], info: Dict[str, Any], last_state: Dict[str, Any], threshold: Optional[int] = None) -> Dict[str, Any]:
    """Create the content and embed for a Discord notification based on event type."""
    name = streamer.get("name")
    url = streamer.get("chzzk_url")
    role_id = streamer.get("discord_role_id")
    mention = f"<@&{role_id}> " if role_id else ""
    title = info.get("title") or last_state.get("title") or ""
    category = info.get("category") or last_state.get("category") or ""
    viewers = info.get("viewers")

    if event_type == "start":
        content = f"{mention}🔴 **{name}** 방송 시작!"
        description = f"**제목:** {title}\n**카테고리:** {category}"
        if viewers is not None:
            description += f"\n**시청자수:** {viewers}"
    elif event_type == "end":
        content = f"{mention}⚫️ **{name}** 방송 종료."
        description = f"**마지막 제목:** {title}\n**마지막 카테고리:** {category}"
    elif event_type == "title_change":
        old = last_state.get("title") or ""
        content = f"{mention}🔄 **{name}** 방송 제목 변경"
        description = f"**이전:** {old}\n**현재:** {title}"
    elif event_type == "category_change":
        old = last_state.get("category") or ""
        content = f"{mention}🔧 **{name}** 방송 카테고리 변경"
        description = f"**이전:** {old}\n**현재:** {category}"
    elif event_type == "threshold_cross":
        # threshold is guaranteed not None in this branch
        content = f"{mention}🎉 **{name}** 방송 시청자 {threshold}명 돌파!"
        description = f"현재 시청자수: {viewers}"
    else:
        # Unknown event
        content = f"{mention}{name}: {event_type}"
        description = ""

    embed: Dict[str, Any] = {
        "title": title if event_type != "threshold_cross" else None,
        "description": description,
        "url": url,
    }
    # Remove None entries
    embed = {k: v for k, v in embed.items() if v}
    return {"content": content, "embed": embed}


async def process_streamer(session: aiohttp.ClientSession, streamer: Dict[str, Any], thresholds: List[int], state: Dict[str, Any]) -> None:
    """Fetch current info for a streamer, detect events, send messages, and update state."""
    channel_id = streamer.get("channel_id")
    if not channel_id:
        return
    current = await fetch_live_info(session, channel_id)
    streamer_key = channel_id
    last = state.get(streamer_key, {})
    new_state: Dict[str, Any] = {
        "is_live": current.get("is_live", False),
        "title": current.get("title") or last.get("title"),
        "category": current.get("category") or last.get("category"),
        "viewers": current.get("viewers"),
        # Track which thresholds have been passed; store as list of ints
        "passed_thresholds": last.get("passed_thresholds", []),
    }
    events: List[Dict[str, Any]] = []

    # Determine start/end events
    if last.get("is_live"):
        if not new_state["is_live"]:
            # Ended
            events.append({"type": "end"})
    else:
        if new_state["is_live"]:
            # Started
            events.append({"type": "start"})

    # Title change detection (only when live)
    if new_state["is_live"] and last.get("is_live"):
        new_title = current.get("title")
        if new_title and new_title != last.get("title"):
            new_state["title"] = new_title
            events.append({"type": "title_change"})
        new_cat = current.get("category")
        if new_cat and new_cat != last.get("category"):
            new_state["category"] = new_cat
            events.append({"type": "category_change"})

    # If stream just started, update title/category from current
    if any(ev["type"] == "start" for ev in events):
        new_state["title"] = current.get("title") or new_state.get("title")
        new_state["category"] = current.get("category") or new_state.get("category")
        # Reset threshold list on each fresh start
        new_state["passed_thresholds"] = []

    # Viewer threshold detection
    if new_state["is_live"]:
        viewers = current.get("viewers")
        if viewers is not None and isinstance(viewers, (int, float)):
            passed = set(new_state.get("passed_thresholds", []))
            to_pass = []
            for th in thresholds:
                if viewers >= th and th not in passed:
                    to_pass.append(th)
            if to_pass:
                # Append to passed list
                new_state["passed_thresholds"] = sorted(passed.union(to_pass))
                for th in sorted(to_pass):
                    events.append({"type": "threshold_cross", "threshold": th})

    # Send notifications for each event
    webhook = streamer.get("webhook_url")
    if webhook and events:
        for ev in events:
            msg = format_discord_message(ev["type"], streamer, current, last, ev.get("threshold"))
            await send_discord_message(session, webhook, msg["content"], msg["embed"])
            # Slight delay between messages to avoid Discord rate limits
            await asyncio.sleep(1)

    # Persist updated state
    state[streamer_key] = new_state


async def main() -> None:
    cfg = load_config(CONFIG_FILE)
    if not cfg:
        print(f"Config file {CONFIG_FILE} not found or empty.")
        sys.exit(1)
    streamers = cfg.get("streamers", [])
    poll_interval = int(cfg.get("poll_interval_seconds", 300))
    thresholds = cfg.get("viewer_thresholds", [])
    # Ensure thresholds are sorted ascending and unique
    thresholds = sorted(set(int(x) for x in thresholds if isinstance(x, (int, float))))

    state = load_state(STATE_FILE)

    async with aiohttp.ClientSession() as session:
        # Single run fetch (GitHub Actions will schedule periodically)
        tasks = [process_streamer(session, s, thresholds, state) for s in streamers]
        await asyncio.gather(*tasks)
    # Save state after run
    save_state(STATE_FILE, state)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
