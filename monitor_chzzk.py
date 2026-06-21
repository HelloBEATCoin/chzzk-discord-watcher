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

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import yaml


CONFIG_FILE = Path(__file__).with_name("config.yaml")
STATE_FILE = Path(__file__).with_name("state.json")
LOGGER = logging.getLogger("chzzk_watcher")


def setup_logging(verbose: bool = False) -> None:
    """Configure process-wide logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = time.gmtime


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
        "Accept-Language": "ko,en;q=0.8",
        "Cache-Control": "no-cache",
    }

    # First attempt: service/v1/channels/{id}/live-detail
    url_live_detail = f"{base}/service/v1/channels/{channel_id}/live-detail"
    try:
        async with session.get(url_live_detail, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data.get("content") or {}
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
            LOGGER.debug("live-detail returned HTTP %s for channel=%s", resp.status, channel_id)
    except Exception as exc:
        LOGGER.warning("live-detail request failed for channel=%s: %s", channel_id, exc)

    # Second attempt: service/v1/channels/{id}
    url_channel = f"{base}/service/v1/channels/{channel_id}"
    try:
        async with session.get(url_channel, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data.get("content") or {}
                # openLive is True when channel is actively streaming
                is_live = bool(content.get("openLive"))
                return {
                    "is_live": is_live,
                    "title": None,
                    "category": None,
                    "viewers": None,
                    "status": None,
                }
            LOGGER.debug("channel endpoint returned HTTP %s for channel=%s", resp.status, channel_id)
    except Exception as exc:
        LOGGER.warning("channel request failed for channel=%s: %s", channel_id, exc)

    # Third attempt: polling/v2/channels/{id}/live-status (may return just status)
    url_polling = f"{base}/polling/v2/channels/{channel_id}/live-status"
    try:
        async with session.get(url_polling, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data.get("content") or {}
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
            LOGGER.debug("live-status returned HTTP %s for channel=%s", resp.status, channel_id)
    except Exception as exc:
        LOGGER.warning("live-status request failed for channel=%s: %s", channel_id, exc)

    # Avoid turning transient API/network failures into false "stream ended" events.
    return {"is_live": False, "fetch_error": True}


async def enrich_live_meta(
    session: aiohttp.ClientSession,
    channel_id: str,
    tries: int = 4,
    delay: float = 2.0,
):
    """
    title/category가 비어 있을 때 짧게 재시도하며 채워 넣는다.
    live-detail 우선, 없으면 live-status 폴백. (점증 백오프)
    """
    base = "https://api.chzzk.naver.com"
    headers = {
        "Referer": "https://chzzk.naver.com/",
        "Origin": "https://chzzk.naver.com",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "ko,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    for i in range(tries):
        title = None
        category = None
        # 1) live-detail
        try:
            async with session.get(f"{base}/service/v1/channels/{channel_id}/live-detail", headers=headers, timeout=10) as r:
                if r.status == 200:
                    d = await r.json()
                    c = d.get("content") or {}
                    title = c.get("liveTitle") or c.get("title")
                    category = c.get("liveCategoryValue") or c.get("videoCategoryValue") or c.get("categoryType") or c.get("liveCategory")
        except Exception as exc:
            LOGGER.debug("metadata enrich live-detail failed for channel=%s: %s", channel_id, exc)
        # 2) live-status (보강)
        if not title or not category:
            try:
                async with session.get(f"{base}/polling/v2/channels/{channel_id}/live-status", headers=headers, timeout=10) as r:
                    if r.status == 200:
                        d = await r.json()
                        c = d.get("content") or {}
                        title = title or c.get("liveTitle")
                        category = category or c.get("liveCategoryValue") or c.get("liveCategory")
            except Exception as exc:
                LOGGER.debug("metadata enrich live-status failed for channel=%s: %s", channel_id, exc)
        if title or category:
            return title, category
        await asyncio.sleep(delay * (i + 1))  # 2s, 4s, 6s...
    return None, None


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
            else:
                LOGGER.warning("Webhook environment variable %s is not set for %s", key, streamer.get("name"))
                streamer["webhook_url"] = None
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


def save_state(path: Path, state: Dict[str, Any]) -> bool:
    """Persist state to disk."""
    old_data = None
    if path.exists():
        old_data = path.read_text(encoding="utf-8")
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    new_data = tmp.read_text(encoding="utf-8")
    if old_data == new_data:
        tmp.unlink()
        return False
    tmp.replace(path)
    return True


async def send_discord_message(
    session: aiohttp.ClientSession,
    webhook_url: str,
    content: str,
    embed: Optional[Dict[str, Any]],
    dry_run: bool = False,
) -> bool:
    """Send a message to Discord via webhook."""
    if dry_run:
        LOGGER.info(
            "[dry-run] Discord message skipped: content=%r embed_title=%r",
            content,
            embed.get("title") if embed else None,
        )
        return True

    payload: Dict[str, Any] = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    try:
        async with session.post(webhook_url, json=payload, timeout=10) as resp:
            if resp.status >= 400:
                LOGGER.error("Discord webhook returned HTTP %s", resp.status)
                return False
            LOGGER.info("Discord webhook delivered: status=%s", resp.status)
            return True
    except Exception as e:
        LOGGER.error("Discord webhook request failed: %s", e)
        return False


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


async def process_streamer(
    session: aiohttp.ClientSession,
    streamer: Dict[str, Any],
    thresholds: List[int],
    state: Dict[str, Any],
    dry_run: bool = False,
    persist_viewer_count: bool = False,
) -> bool:
    """Fetch current info for a streamer, detect events, send messages, and update state."""
    channel_id = streamer.get("channel_id")
    if not channel_id:
        LOGGER.error("Skipping streamer without channel_id: %s", streamer.get("name"))
        return False

    name = streamer.get("name") or channel_id
    LOGGER.info("Checking streamer=%s channel=%s", name, channel_id)
    current = await fetch_live_info(session, channel_id)
    if current.get("fetch_error"):
        LOGGER.error("Skipping state update because all Chzzk API attempts failed: streamer=%s", name)
        return False

    streamer_key = channel_id
    last = state.get(streamer_key, {})
    new_state: Dict[str, Any] = {
        "is_live": current.get("is_live", False),
        "title": current.get("title") or last.get("title"),
        "category": current.get("category") or last.get("category"),
        "viewers": current.get("viewers") if persist_viewer_count else None,
        # Track which thresholds have been passed; store as list of ints
        "passed_thresholds": last.get("passed_thresholds", []),
    }
    events: List[Dict[str, Any]] = []

    # --- 마지막 비어있지 않은 메타 보존 ---
    last_title_nonempty = last.get("last_nonempty_title")
    last_cat_nonempty = last.get("last_nonempty_category")
    cur_title = current.get("title")
    cur_cat = current.get("category")
    if cur_title:
        last_title_nonempty = cur_title
    if cur_cat:
        last_cat_nonempty = cur_cat
    new_state["last_nonempty_title"] = last_title_nonempty
    new_state["last_nonempty_category"] = last_cat_nonempty

    # Determine start/end events
    if last.get("is_live"):
        if not new_state["is_live"]:
            # Ended
            events.append({"type": "end"})
    else:
        if new_state["is_live"]:
            # Started
            events.append({"type": "start"})

    # Title/category change detection (only when live)
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

    # --- 알림 메시지에 쓸 메타 보강 ---
    # 시작 이벤트가 있으면 즉시 보강 시도 (짧은 재시도)
    has_start = any(ev["type"] == "start" for ev in events)
    if has_start and (not current.get("title") or not current.get("category")):
        t, c = await enrich_live_meta(session, channel_id)
        if t and not current.get("title"):
            current["title"] = t
        if c and not current.get("category"):
            current["category"] = c

    def build_current_for_msg(ev_type: str) -> Dict[str, Any]:
        """이벤트별로 표시용 메타를 채워 넣는다."""
        cur = dict(current)  # shallow copy
        if ev_type == "end":
            # 종료 시 실시간 API는 비어있을 수 있으니 마지막 비어있지 않은 값 사용
            cur["title"] = new_state.get("last_nonempty_title") or new_state.get("title")
            cur["category"] = new_state.get("last_nonempty_category") or new_state.get("category")
        else:
            # 시작/변경 이벤트: 없으면 상태에 남아있는 값으로라도 채움
            if not cur.get("title"):
                cur["title"] = new_state.get("last_nonempty_title") or new_state.get("title")
            if not cur.get("category"):
                cur["category"] = new_state.get("last_nonempty_category") or new_state.get("category")
        return cur

    # Send notifications for each event (with enriched meta)
    webhook = streamer.get("webhook_url")
    if webhook and events:
        for ev in events:
            info_for_msg = build_current_for_msg(ev["type"])
            msg = format_discord_message(ev["type"], streamer, info_for_msg, last, ev.get("threshold"))
            ok = await send_discord_message(session, webhook, msg["content"], msg["embed"], dry_run=dry_run)
            if not ok:
                return False
            # Slight delay between messages to avoid Discord rate limits
            await asyncio.sleep(1)
    elif events and dry_run:
        LOGGER.info(
            "[dry-run] Events detected but webhook is missing: streamer=%s events=%s",
            name,
            [ev["type"] for ev in events],
        )
    elif events:
        LOGGER.error("Events detected but webhook is missing: streamer=%s events=%s", name, [ev["type"] for ev in events])
        return False
    else:
        LOGGER.info(
            "No notification events: streamer=%s is_live=%s title=%r category=%r viewers=%r",
            name,
            new_state["is_live"],
            new_state.get("title"),
            new_state.get("category"),
            current.get("viewers"),
        )

    # Persist updated state
    state[streamer_key] = new_state
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Chzzk Discord watcher cycle.")
    parser.add_argument("config", nargs="?", default=os.environ.get("CONFIG_PATH", str(CONFIG_FILE)))
    parser.add_argument("--state", default=os.environ.get("STATE_PATH", str(STATE_FILE)))
    parser.add_argument("--dry-run", action="store_true", default=env_bool("DRY_RUN", False))
    parser.add_argument("--verbose", action="store_true", default=env_bool("VERBOSE", False))
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)

    config_path = Path(args.config)
    state_path = Path(args.state)
    cfg = load_config(config_path)
    if not cfg:
        LOGGER.error("Config file %s not found or empty.", config_path)
        return 1
    streamers = cfg.get("streamers", [])
    poll_interval = int(cfg.get("poll_interval_seconds", 300))
    thresholds = cfg.get("viewer_thresholds", [])
    persist_viewer_count = bool(cfg.get("persist_viewer_count", False))
    # Ensure thresholds are sorted ascending and unique
    thresholds = sorted(set(int(x) for x in thresholds if isinstance(x, (int, float))))

    LOGGER.info(
        "Starting watcher cycle: streamers=%s thresholds=%s poll_interval_seconds=%s dry_run=%s persist_viewer_count=%s",
        len(streamers),
        thresholds,
        poll_interval,
        args.dry_run,
        persist_viewer_count,
    )
    state = load_state(state_path)

    async with aiohttp.ClientSession() as session:
        # Single run fetch (GitHub Actions will schedule periodically)
        tasks = [
            process_streamer(
                session,
                s,
                thresholds,
                state,
                dry_run=args.dry_run,
                persist_viewer_count=persist_viewer_count,
            )
            for s in streamers
        ]
        results = await asyncio.gather(*tasks)
    # Save state after run
    if args.dry_run:
        changed = False
        LOGGER.info("[dry-run] State write skipped: %s", state_path)
    else:
        changed = save_state(state_path, state)
        LOGGER.info("State %s: %s", "updated" if changed else "unchanged", state_path)
    return 0 if all(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        LOGGER.info("Interrupted")
        sys.exit(130)
