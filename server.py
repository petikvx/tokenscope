#!/usr/bin/env python3
"""Tableau de bord local : tokens Grok, Gemini et ChatGPT.

Lit uniquement les journaux déjà présents sur la machine.
N'appelle aucune API et n'expose aucun contenu de conversation.
"""

from __future__ import annotations

import errno
import json
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parent
HOME = Path.home()
INDEX = ROOT / "web" / "index.html"
PRICES = ROOT / "prices.json"
_lock = threading.Lock()
# chemin -> (mtime_ns, taille, valeur). La valeur n'est recalculée que si le fichier change.
_file_cache: dict[str, tuple] = {}
_grok_model_cache: tuple | None = None


def _int(value) -> int:
    try:
        if value is None or value is False:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _cached_sig(key: str, signature, loader):
    hit = _file_cache.get(key)
    if hit and hit[0] == signature:
        return hit[1]
    value = loader()
    _file_cache[key] = (signature, value)
    return value


def _cached(path: Path, loader):
    """Retourne le résultat de loader(path), relu seulement si mtime ou taille change."""
    signature = _signature(path)
    if signature is None:
        return loader(path)
    return _cached_sig(str(path), signature, lambda: loader(path))


def _forget_unseen(seen: set[str]) -> None:
    for key in list(_file_cache):
        if key not in seen:
            del _file_cache[key]


_slot_cache: dict[str, tuple[str, int] | None] = {}


def _slot(timestamp) -> tuple[str, int] | None:
    """(jour, heure) dans le fuseau local de la machine. Un horodatage sans fuseau est lu comme UTC."""
    if not isinstance(timestamp, str) or len(timestamp) < 10:
        return None
    hit = _slot_cache.get(timestamp, False)
    if hit is not False:
        return hit
    text = timestamp.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    result = None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = None
    if moment is not None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone()
        result = (moment.strftime("%Y-%m-%d"), moment.hour)
    if len(_slot_cache) > 200_000:
        _slot_cache.clear()
    _slot_cache[timestamp] = result
    return result


def _day(timestamp) -> str | None:
    slot = _slot(timestamp)
    return slot[0] if slot else None


def _aggregate(calls: list[dict]) -> list[dict]:
    buckets: dict[tuple, dict] = {}
    for call in calls:
        day = _day(call.get("t"))
        if day is None:
            continue
        key = (day, call["provider"], call["model"])
        row = buckets.get(key)
        if row is None:
            row = {
                "date": day,
                "provider": call["provider"],
                "model": call["model"],
                "calls": 0,
                "input": 0,
                "new_input": 0,
                "resent": 0,
                "cached": 0,
                "cache_write": 0,
                "output": 0,
                "reasoning": 0,
                "cost": 0.0,
                "unpriced": 0,
            }
            buckets[key] = row
        row["calls"] += call.get("n", 1)
        row["input"] += call["input"]
        row["new_input"] += call.get("new_input", call["input"])
        row["resent"] += call.get("resent", 0)
        row["cached"] += call["cached"]
        row["cache_write"] += call.get("cache_write", 0)
        row["output"] += call["output"]
        row["reasoning"] += call["reasoning"]
        _add_cost(row, call)
    return [buckets[key] for key in sorted(buckets)]


def _add_cost(row: dict, call: dict) -> None:
    cost = call.get("cost")
    if cost is None:
        row["unpriced"] += 1
    else:
        row["cost"] += cost


def _aggregate_hours(calls: list[dict]) -> list[dict]:
    """Par jour, heure locale et fournisseur : de quoi tracer une carte jour × heure."""
    buckets: dict[tuple, dict] = {}
    for call in calls:
        slot = _slot(call.get("t"))
        if slot is None:
            continue
        key = (slot[0], slot[1], call["provider"])
        row = buckets.get(key)
        if row is None:
            row = {"date": slot[0], "hour": slot[1], "provider": call["provider"],
                   "calls": 0, "input": 0, "new_input": 0, "output": 0, "reasoning": 0,
                   "cost": 0.0, "unpriced": 0}
            buckets[key] = row
        row["calls"] += call.get("n", 1)
        row["input"] += call["input"]
        row["new_input"] += call.get("new_input", call["input"])
        row["output"] += call["output"]
        row["reasoning"] += call["reasoning"]
        _add_cost(row, call)
    return [buckets[key] for key in sorted(buckets)]


def _annotate_growth(calls: list[dict]) -> None:
    """Dans une session, le prompt contient le tour précédent. Seul l'écart est nouveau."""
    previous = None
    for call in sorted(calls, key=lambda item: item.get("t") or ""):
        prompt = call["input"]
        if previous is None or prompt < previous:
            call["new_input"] = prompt
            call["resent"] = 0
        else:
            call["new_input"] = prompt - previous
            call["resent"] = previous
        previous = prompt


def _short_project(path: str | None) -> str:
    if not isinstance(path, str):
        return "inconnu"
    path = path.strip()
    if not path:
        return "inconnu"
    home = str(HOME)
    if path == home:
        return "~"
    prefix = home + os.sep
    if path.startswith(prefix):
        return "~/" + path[len(prefix):]
    return path


def _stamp_project(calls: list[dict], project: str) -> None:
    for call in calls:
        call["project"] = project


def _gemini_project(path: Path) -> str:
    for parent in path.parents:
        marker = parent / ".project_root"
        if marker.is_file():
            try:
                return _short_project(marker.read_text(encoding="utf-8"))
            except OSError:
                return "gemini"
        if parent.name == ".gemini":
            break
    return "gemini"


def _aggregate_projects(calls: list[dict]) -> list[dict]:
    buckets: dict[tuple, dict] = {}
    for call in calls:
        day = _day(call.get("t"))
        if day is None:
            continue
        project = call.get("project") or "inconnu"
        key = (day, call["provider"], project)
        row = buckets.get(key)
        if row is None:
            row = {
                "date": day,
                "provider": call["provider"],
                "project": project,
                "calls": 0,
                "new_input": 0,
                "resent": 0,
                "output": 0,
                "reasoning": 0,
                "cost": 0.0,
                "unpriced": 0,
            }
            buckets[key] = row
        row["calls"] += call.get("n", 1)
        row["new_input"] += call.get("new_input", 0)
        row["resent"] += call.get("resent", 0)
        row["output"] += call["output"]
        row["reasoning"] += call["reasoning"]
        _add_cost(row, call)
    return [buckets[key] for key in sorted(buckets)]


def _call(when, provider, model, source, inp, cached, out, reasoning, cache_write=0) -> dict:
    return {
        "t": when,
        "provider": provider,
        "model": model or "inconnu",
        "source": source,
        "input": _int(inp),
        "cached": _int(cached),
        "cache_write": _int(cache_write),
        "output": _int(out),
        "reasoning": _int(reasoning),
    }


def _grok_sessions() -> tuple[dict[str, str], dict[str, str]]:
    global _grok_model_cache
    root = HOME / ".grok" / "sessions"
    paths = sorted(root.glob("*/*/summary.json")) if root.is_dir() else []
    signature = tuple((str(path),) + (_signature(path) or (0, 0)) for path in paths)
    if _grok_model_cache and _grok_model_cache[0] == signature:
        return _grok_model_cache[1], _grok_model_cache[2]
    models: dict[str, str] = {}
    folders: dict[str, str] = {}
    for summary in paths:
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        model = data.get("current_model_id")
        if isinstance(model, str) and model:
            models[summary.parent.name] = model
        cwd = (data.get("info") or {}).get("cwd") if isinstance(data.get("info"), dict) else None
        if isinstance(cwd, str) and cwd:
            folders[summary.parent.name] = _short_project(cwd)
    _grok_model_cache = (signature, models, folders)
    return models, folders


def _parse_grok_log(path: Path) -> dict:
    calls: list[dict] = []
    latest = None
    history = []
    seen_quota: set[str] = set()
    with path.open(encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if "shell.turn.inference_done" not in line and "billing: fetched credits config" not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("msg")
            if message == "shell.turn.inference_done":
                ctx = event.get("ctx") or {}
                calls.append(
                    _call(
                        event.get("ts"),
                        "grok",
                        event.get("sid") or "",
                        "grok",
                        ctx.get("prompt_tokens"),
                        ctx.get("cached_prompt_tokens"),
                        ctx.get("completion_tokens"),
                        ctx.get("reasoning_tokens"),
                    )
                )
                continue
            if message != "billing: fetched credits config":
                continue
            ctx = event.get("ctx") or {}
            config = ctx.get("config") or {}
            if config.get("creditUsagePercent") is None:
                continue
            period = config.get("currentPeriod") or {}
            sample = {
                "t": event.get("ts"),
                "percent": config.get("creditUsagePercent"),
                "tier": ctx.get("subscriptionTier"),
                "period_start": period.get("start"),
                "period_end": period.get("end"),
                "period_type": period.get("type"),
            }
            if sample["t"] not in seen_quota:
                seen_quota.add(sample["t"])
                history.append({"t": sample["t"], "percent": sample["percent"]})
            latest = sample
    quota = None
    if latest is not None:
        quota = dict(latest)
        quota["history"] = history[-80:]
    return {"calls": calls, "quota": quota}


def _grok_model(name) -> str:
    if not isinstance(name, str) or not name:
        return "grok"
    return name[:-len("-build")] if name.endswith("-build") else name


def _parse_grok_updates(path: Path) -> list[dict]:
    """Historique d'une session Grok : un enregistrement par tour et par modèle.

    Le tour donne la somme de ses appels (modelCalls) et le coût calculé par xAI.
    """
    calls: list[dict] = []
    seen: set[str] = set()
    for event in _iter_jsonl(path):
        params = event.get("params") if isinstance(event, dict) else None
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict) or update.get("sessionUpdate") != "turn_completed":
            continue
        usage = update.get("usage")
        if not isinstance(usage, dict):
            continue
        prompt_id = str(update.get("prompt_id") or "")
        if prompt_id:
            if prompt_id in seen:
                continue
            seen.add(prompt_id)
        stamp = event.get("timestamp")
        when = None
        if isinstance(stamp, (int, float)) and stamp > 0:
            when = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        per_model = usage.get("modelUsage")
        if not isinstance(per_model, dict) or not per_model:
            per_model = {"grok": usage}
        complete = not usage.get("usageIsIncomplete")
        for model, block in per_model.items():
            if not isinstance(block, dict):
                continue
            output = _int(block.get("outputTokens"))
            reasoning = min(_int(block.get("reasoningTokens")), output)
            call = _call(
                when,
                "grok",
                _grok_model(model),
                "grok-session",
                block.get("inputTokens"),
                block.get("cachedReadTokens"),
                output - reasoning,  # outputTokens inclut le raisonnement
                reasoning,
                block.get("cacheCreationTokens"),
            )
            call["n"] = max(1, _int(block.get("modelCalls")))
            ticks = block.get("costUsdTicks")
            if complete and isinstance(ticks, (int, float)) and ticks >= 0:
                call["cost_official"] = ticks / 1e10  # 1 tick = 1e-10 dollar
            # Un tour additionne plusieurs appels : la part non lue depuis le cache tient lieu de « nouveau ».
            call["new_input"] = max(0, call["input"] - call["cached"])
            call["resent"] = call["cached"]
            calls.append(call)
    return calls


def collect_grok(seen: set[str]) -> tuple[list[dict], dict | None]:
    """Appels Grok depuis l'historique des sessions ; le journal unifié sert au quota
    et, à défaut d'historique, au détail des appels."""
    models, folders = _grok_sessions()
    calls: list[dict] = []
    covered: set[str] = set()
    root = HOME / ".grok" / "sessions"
    if root.is_dir():
        for path in root.glob("*/*/updates.jsonl"):
            seen.add(str(path))
            turns = _cached(path, _parse_grok_updates)
            if not turns:
                continue
            session = path.parent.name
            covered.add(session)
            project = folders.get(session) or _short_project(unquote(path.parent.parent.name))
            _stamp_project(turns, project)
            calls.extend(turns)
    quota = None
    path = HOME / ".grok" / "logs" / "unified.jsonl"
    if path.is_file():
        seen.add(str(path))
        parsed = _cached(path, _parse_grok_log)
        quota = parsed["quota"]
        groups: dict[str, list[dict]] = {}
        for raw in parsed["calls"]:
            session = raw["model"]
            if session in covered:
                continue
            call = dict(raw)
            call["model"] = models.get(session, "grok")
            call["project"] = folders.get(session, "inconnu")
            groups.setdefault(session, []).append(call)
        for group in groups.values():
            _annotate_growth(group)
            calls.extend(group)
    return calls, quota


def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            line = line.strip()
            if not line or line[0] not in "{[":
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_gemini_file(path: Path) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for event in _iter_jsonl(path):
        tokens = event.get("tokens") if isinstance(event, dict) else None
        if not isinstance(tokens, dict):
            continue
        message_id = str(event.get("id") or "")
        if message_id and message_id in seen:
            continue
        if message_id:
            seen.add(message_id)
        rows.append((
            message_id,
            _call(
                event.get("timestamp"),
                "gemini",
                event.get("model") or "gemini",
                "gemini-cli",
                tokens.get("input"),
                tokens.get("cached"),
                tokens.get("output"),
                tokens.get("thoughts"),
            ),
        ))
    calls = [call for _, call in rows]
    _stamp_project(calls, _gemini_project(path))
    _annotate_growth(calls)
    return rows


def collect_gemini_cli(seen_files: set[str]) -> list[dict]:
    root = HOME / ".gemini" / "tmp"
    if not root.is_dir():
        return []
    calls: list[dict] = []
    seen_ids: set[str] = set()
    for path in root.glob("**/chats/**/*.jsonl"):
        seen_files.add(str(path))
        for message_id, call in _cached(path, _parse_gemini_file):
            if message_id and message_id in seen_ids:
                continue
            if message_id:
                seen_ids.add(message_id)
            calls.append(call)
    return calls


def _parse_antigravity(path: Path) -> list[dict]:
    calls: list[dict] = []
    for event in _iter_jsonl(path):
        if not isinstance(event, dict) or "input_tokens" not in event:
            continue
        if _int(event.get("input_tokens")) == 0 and _int(event.get("output_tokens")) == 0:
            continue
        calls.append(
            _call(
                event.get("created_at"),
                "gemini",
                "antigravity",
                "antigravity",
                event.get("input_tokens"),
                event.get("cache_read_tokens"),
                event.get("output_tokens"),
                0,
            )
        )
    _stamp_project(calls, "Antigravity")
    _annotate_growth(calls)
    return calls


def collect_antigravity(seen_files: set[str]) -> list[dict]:
    root = HOME / ".gemini" / "antigravity-cli" / "brain"
    if not root.is_dir():
        return []
    calls: list[dict] = []
    for path in root.glob("*/.system_generated/logs/transcript.jsonl"):
        seen_files.add(str(path))
        calls.extend(_cached(path, _parse_antigravity))
    return calls


def _usage_from_obj(obj: dict) -> dict | None:
    """Retourne un bloc de tokens s'il ressemble à un usage d'appel, pas à un cumul."""
    if not isinstance(obj, dict):
        return None
    last = obj.get("last_token_usage")
    if isinstance(last, dict):
        obj = last
    keys = ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens")
    if not any(k in obj for k in keys):
        return None
    # Un total de session est cumulatif : on ne l'additionne pas ici.
    if "total_token_usage" in obj and "last_token_usage" not in obj:
        return None
    return {
        "input": obj.get("input_tokens", obj.get("prompt_tokens")),
        "cached": obj.get("cached_input_tokens", obj.get("cache_read_input_tokens", obj.get("cached_tokens"))),
        "output": obj.get("output_tokens", obj.get("completion_tokens")),
        "reasoning": obj.get("reasoning_tokens", obj.get("reasoning_output_tokens")),
    }


def _walk_usage(node, found: list[dict]) -> None:
    if isinstance(node, dict):
        usage = node.get("usage") if "usage" in node else None
        parsed = _usage_from_obj(usage) if isinstance(usage, dict) else None
        if parsed is None:
            parsed = _usage_from_obj(node.get("info") if isinstance(node.get("info"), dict) else None)
        if parsed is None and node.get("type") in {"token_count", "usage"}:
            parsed = _usage_from_obj(node)
        if parsed:
            found.append(parsed)
            return
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk_usage(value, found)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                _walk_usage(item, found)


def _parse_claude_file(path: Path) -> tuple[dict[str, dict], list[dict]]:
    """Un enregistrement par appel. Le journal répète souvent le même usage."""
    best: dict[str, dict] = {}
    loose: list[dict] = []
    project = None
    for event in _iter_jsonl(path):
        if not isinstance(event, dict):
            continue
        if project is None and isinstance(event.get("cwd"), str):
            project = event.get("cwd")
        if event.get("type") != "assistant":
            continue
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
        if not usage:
            continue
        fresh = _int(usage.get("input_tokens"))
        cache_read = _int(usage.get("cache_read_input_tokens"))
        cache_create = _int(usage.get("cache_creation_input_tokens"))
        creation = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        write_1h = min(_int(creation.get("ephemeral_1h_input_tokens")), cache_create)
        output = _int(usage.get("output_tokens"))
        details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
        thinking = min(_int(details.get("thinking_tokens")), output)
        if fresh + cache_read + cache_create + output == 0:
            continue
        model = message.get("model") or event.get("requestedModel") or "claude"
        if model == "<synthetic>":
            model = "claude (synthétique)"
        call = _call(
            event.get("timestamp"),
            "claude",
            model,
            "claude-code",
            fresh + cache_read + cache_create,
            cache_read,
            output - thinking,
            thinking,
            cache_create,
        )
        call["cache_write_1h"] = write_1h
        request_id = event.get("requestId")
        if not request_id:
            loose.append(call)
            continue
        previous = best.get(request_id)
        score = (call["output"] + call["reasoning"], call["t"] or "")
        if previous is None or score >= (previous["output"] + previous["reasoning"], previous["t"] or ""):
            best[request_id] = call
    calls = list(best.values()) + loose
    _stamp_project(calls, _short_project(project))
    _annotate_growth(calls)
    return best, loose


def collect_claude(seen_files: set[str]) -> list[dict]:
    root = HOME / ".claude" / "projects"
    if not root.is_dir():
        return []
    best: dict[str, dict] = {}
    loose: list[dict] = []
    for path in root.glob("**/*.jsonl"):
        seen_files.add(str(path))
        file_best, file_loose = _cached(path, _parse_claude_file)
        loose.extend(file_loose)
        for request_id, call in file_best.items():
            previous = best.get(request_id)
            score = (call["output"] + call["reasoning"], call["t"] or "")
            if previous is None or score >= (previous["output"] + previous["reasoning"], previous["t"] or ""):
                best[request_id] = call
    return list(best.values()) + loose


def _parse_claude_quota(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    samples = data.get("samples") if isinstance(data, dict) else None
    if not isinstance(samples, list):
        return None
    history = []
    latest = None
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        usage = sample.get("u") if isinstance(sample.get("u"), dict) else {}
        five_hour = usage.get("fh")
        week = usage.get("sd")
        if five_hour is None and week is None:
            continue
        stamp = sample.get("t")
        when = None
        if isinstance(stamp, (int, float)) and stamp > 0:
            when = datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()
        point = {"t": when, "five_hour": five_hour, "week": week}
        history.append(point)
        latest = point
    if latest is None:
        return None
    latest = dict(latest)
    latest["history"] = history[-120:]
    return latest


def _parse_claude_resets(path: Path) -> dict | None:
    """Dates de renouvellement des fenêtres, telles que les a relevées l'application."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cached = data.get("cachedUsageUtilization") if isinstance(data, dict) else None
    if not isinstance(cached, dict):
        return None
    utilization = cached.get("utilization") if isinstance(cached.get("utilization"), dict) else {}

    def resets(key):
        block = utilization.get(key)
        return block.get("resets_at") if isinstance(block, dict) else None

    five_hour = resets("five_hour")
    week = resets("seven_day")
    if five_hour is None and week is None:
        return None
    fetched = cached.get("fetchedAtMs")
    fetched_at = None
    if isinstance(fetched, (int, float)) and fetched > 0:
        fetched_at = datetime.fromtimestamp(fetched / 1000, timezone.utc).isoformat()
    return {
        "fetched_at": fetched_at,
        "five_hour_resets_at": five_hour,
        "week_resets_at": week,
    }


def collect_claude_quota(seen_files: set[str]) -> dict | None:
    quota = None
    path = HOME / ".config" / "Claude" / "plan-usage-history.json"
    if path.is_file():
        seen_files.add(str(path))
        quota = _cached(path, _parse_claude_quota)
    state = HOME / ".claude.json"
    if state.is_file():
        seen_files.add(str(state))
        resets = _cached(state, _parse_claude_resets)
        if resets and quota is not None:
            quota = dict(quota)
            quota.update(resets)
    return quota


def _rate_window(block, when: str | None) -> dict | None:
    """Une fenêtre de limite Codex : pourcentage utilisé, durée, date de renouvellement."""
    if not isinstance(block, dict) or block.get("used_percent") is None:
        return None
    resets = block.get("resets_at")
    resets_at = None
    if isinstance(resets, (int, float)) and resets > 0:
        resets_at = datetime.fromtimestamp(resets, timezone.utc).isoformat()
    elif isinstance(resets, str):
        resets_at = resets
    else:
        seconds = block.get("resets_in_seconds")
        if isinstance(seconds, (int, float)) and when:
            moment = _parse_moment(when)
            if moment:
                resets_at = datetime.fromtimestamp(moment.timestamp() + seconds, timezone.utc).isoformat()
    return {
        "percent": block.get("used_percent"),
        "window_minutes": _int(block.get("window_minutes")) or None,
        "resets_at": resets_at,
    }


def _parse_moment(text) -> datetime | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _parse_chatgpt_session(path: Path) -> dict:
    """Journal d'une session Codex : un enregistrement par appel (événement token_count),
    le modèle courant (turn_context) et les relevés de limites (rate_limits)."""
    calls: list[dict] = []
    generic: list[dict] = []
    limits: list[dict] = []
    project = None
    model = "chatgpt"
    last_total = None
    for event in _iter_jsonl(path):
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        when = event.get("timestamp") or event.get("ts")
        if project is None:
            cwd = payload.get("cwd") or event.get("cwd")
            if isinstance(cwd, str) and cwd:
                project = cwd
        name = payload.get("model") or event.get("model")
        if isinstance(name, str) and name:
            model = name
        if payload.get("type") == "token_count" or event.get("type") == "token_count":
            source = payload or event
            rates = source.get("rate_limits")
            if isinstance(rates, dict):
                primary = _rate_window(rates.get("primary"), when)
                secondary = _rate_window(rates.get("secondary"), when)
                if primary or secondary:
                    limits.append({"t": when, "primary": primary, "secondary": secondary,
                                   "plan": rates.get("plan_type")})
            info = source.get("info") if isinstance(source.get("info"), dict) else None
            if not info:
                continue
            total = info.get("total_token_usage")
            if total is not None and total == last_total:
                continue  # même relevé répété : pas un nouvel appel
            last_total = total
            usage = info.get("last_token_usage")
            if not isinstance(usage, dict):
                continue
            output = _int(usage.get("output_tokens"))
            reasoning = min(_int(usage.get("reasoning_output_tokens")), output)
            calls.append(_call(
                when, "chatgpt", model, "codex-session",
                usage.get("input_tokens"),
                usage.get("cached_input_tokens"),
                output - reasoning,  # la sortie OpenAI inclut le raisonnement
                reasoning,
            ))
            continue
        found: list[dict] = []
        _walk_usage(event, found)
        for usage in found:
            generic.append(_call(when, "chatgpt", model, "codex-session",
                                 usage["input"], usage["cached"], usage["output"], usage["reasoning"]))
    # Ancien format sans token_count : on garde la lecture générique.
    if not calls:
        calls = generic
    _stamp_project(calls, _short_project(project) if project else "Codex")
    _annotate_growth(calls)
    return {"calls": calls, "limits": limits}


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _parse_chatgpt_state(path: Path) -> list[tuple[str, dict]]:
    """Fils Codex résumés dans la base. Chaque ligne garde l'identifiant du fil."""
    calls: list[tuple[str, dict]] = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return calls
    query = """
        select {id}, coalesce(model, 'chatgpt'), tokens_used, updated_at, coalesce(cwd, '')
        from threads
        where tokens_used > 0
    """
    try:
        try:
            rows = con.execute(query.format(id="id")).fetchall()
        except sqlite3.Error:
            rows = con.execute(query.format(id="''")).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    for thread_id, model, tokens, updated, cwd in rows:
        when = None
        if isinstance(updated, (int, float)) and updated > 0:
            when = datetime.fromtimestamp(updated, timezone.utc).isoformat()
        call = _call(when, "chatgpt", model, "codex-state", 0, 0, tokens, 0)
        call["unsplit"] = True  # total seul : impossible de séparer entrée et sortie, donc pas de coût
        call["project"] = _short_project(cwd) if cwd else "Codex"
        calls.append((str(thread_id or "").lower(), call))
    return calls


def collect_chatgpt(seen_files: set[str]) -> tuple[list[dict], dict | None]:
    calls: list[dict] = []
    limits: list[dict] = []
    session_ids: set[str] = set()
    sessions = HOME / ".codex" / "sessions"
    if sessions.is_dir():
        for path in sessions.glob("**/*.jsonl"):
            seen_files.add(str(path))
            parsed = _cached(path, _parse_chatgpt_session)
            calls.extend(parsed["calls"])
            limits.extend(parsed["limits"])
            match = _UUID.search(path.name)
            if match and parsed["calls"]:
                session_ids.add(match.group(0).lower())
    state_dir = HOME / ".codex"
    if state_dir.is_dir():
        for db_path in sorted(state_dir.glob("state_*.sqlite")):
            seen_files.add(str(db_path))
            signature = (
                _signature(db_path),
                _signature(Path(str(db_path) + "-wal")),
                _signature(Path(str(db_path) + "-shm")),
            )
            rows = _cached_sig(str(db_path), signature, lambda path=db_path: _parse_chatgpt_state(path))
            for thread_id, call in rows:
                # Le fil a déjà son détail appel par appel : le total de la base ferait doublon.
                if thread_id and thread_id in session_ids:
                    continue
                # Sans identifiant, on ne peut pas savoir : la base ne sert qu'à défaut de sessions.
                if not thread_id and session_ids:
                    continue
                calls.append(call)
    return calls, _chatgpt_quota(limits)


def _chatgpt_quota(limits: list[dict]) -> dict | None:
    """Dernier relevé des limites Codex, avec l'historique des pourcentages."""
    limits = sorted((item for item in limits if item.get("t")), key=lambda item: _parse_moment(item["t"]) or datetime.min.replace(tzinfo=timezone.utc))
    if not limits:
        return None
    history = []
    for item in limits:
        history.append({
            "t": item["t"],
            "primary": (item["primary"] or {}).get("percent"),
            "secondary": (item["secondary"] or {}).get("percent"),
        })
    latest = dict(limits[-1])
    latest["plan"] = next((item["plan"] for item in reversed(limits) if item.get("plan")), None)
    latest["history"] = history[-120:]
    return latest


def _load_prices(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        sys.stderr.write(f"prices.json illisible : {error}\n")
        return {}
    return data if isinstance(data, dict) else {}


def _price_lookup(models: dict):
    keys = sorted((k for k, v in models.items() if isinstance(v, dict)), key=len, reverse=True)
    found: dict[str, dict | None] = {}

    def lookup(model: str) -> dict | None:
        if model not in found:
            found[model] = models.get(model) if isinstance(models.get(model), dict) else next(
                (models[k] for k in keys if model.startswith(k)), None)
        return found[model]
    return lookup


def _cost(call: dict, price: dict) -> float | None:
    """Coût d'un appel au tarif public, palier long contexte compris."""
    try:
        tier = price
        long = price.get("long")
        # Un enregistrement peut regrouper plusieurs appels (tour Grok) : le seuil porte sur un appel.
        prompt = call["input"] / max(1, call.get("n", 1))
        if isinstance(long, dict) and prompt >= float(long.get("threshold", float("inf"))):
            tier = {**price, **long}
        base = float(tier["input"])
        write = call.get("cache_write", 0)
        write_1h = min(call.get("cache_write_1h", 0), write)
        fresh = max(0, call["input"] - call["cached"] - write)
        write_5m_price = float(tier.get("write_5m", base))
        total = (
            fresh * base
            + call["cached"] * float(tier.get("cache_read", base))
            + (write - write_1h) * write_5m_price
            + write_1h * float(tier.get("write_1h", write_5m_price))
            + (call["output"] + call["reasoning"]) * float(tier["output"])
        )
    except (KeyError, TypeError, ValueError):
        return None
    return total / 1e6


def _price_calls(calls: list[dict], seen: set[str]) -> dict:
    prices = {}
    if PRICES.is_file():
        seen.add(str(PRICES))
        prices = _cached(PRICES, _load_prices)
    models = prices.get("models") if isinstance(prices.get("models"), dict) else {}
    lookup = _price_lookup(models)
    # Rapport entre le coût facturé par le fournisseur et le tarif actuel, par modèle et par mois :
    # sert à estimer les rares appels sans coût officiel au prix qui s'appliquait alors.
    ratio: dict[tuple, list[float]] = {}
    for call in calls:
        price = lookup(call["model"])
        computed = _cost(call, price) if price and not call.get("unsplit") else None
        call["cost"] = computed
        if "cost_official" in call and computed:
            key = (call["provider"], call["model"], (call.get("t") or "")[:7])
            pair = ratio.setdefault(key, [0.0, 0.0])
            pair[0] += call["cost_official"]
            pair[1] += computed
    for call in calls:
        if "cost_official" in call:
            call["cost"] = call["cost_official"]
            continue
        pair = ratio.get((call["provider"], call["model"], (call.get("t") or "")[:7]))
        if call["cost"] is not None and pair and pair[1] > 0:
            call["cost"] *= pair[0] / pair[1]
    sources = prices.get("sources") if isinstance(prices.get("sources"), dict) else {}
    return {"sources": sources, "models": sorted(models)}


def _collect_unlocked() -> dict:
    seen: set[str] = set()
    calls: list[dict] = []
    grok_calls, grok_quota = collect_grok(seen)
    calls.extend(grok_calls)
    calls.extend(collect_gemini_cli(seen))
    calls.extend(collect_antigravity(seen))
    chatgpt_calls, chatgpt_quota = collect_chatgpt(seen)
    calls.extend(chatgpt_calls)
    calls.extend(collect_claude(seen))
    claude_quota = collect_claude_quota(seen)
    pricing = _price_calls(calls, seen)
    _forget_unseen(seen)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "buckets": _aggregate(calls),
        "projects": _aggregate_projects(calls),
        "hours": _aggregate_hours(calls),
        "timezone": datetime.now().astimezone().tzname(),
        "pricing": pricing,
        "quota": {
            "grok": grok_quota,
            "claude": claude_quota,
            "chatgpt": chatgpt_quota,
        },
        "sources": {
            "grok": str(HOME / ".grok" / "logs" / "unified.jsonl"),
            "gemini_cli": str(HOME / ".gemini" / "tmp"),
            "antigravity": str(HOME / ".gemini" / "antigravity-cli" / "brain"),
            "chatgpt": str(HOME / ".codex"),
            "claude": str(HOME / ".claude" / "projects"),
            "claude_quota": str(HOME / ".config" / "Claude" / "plan-usage-history.json"),
            "claude_state": str(HOME / ".claude.json"),
        },
    }


def collect() -> dict:
    with _lock:
        return _collect_unlocked()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not INDEX.is_file():
                self._send(500, b"index manquant", "text/plain; charset=utf-8")
                return
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/usage":
            payload = json.dumps(collect(), ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
            return
        self._send(404, b"introuvable", "text/plain; charset=utf-8")


def main() -> None:
    if "--check" in sys.argv:
        import time
        started = time.perf_counter()
        data = collect()
        cold = time.perf_counter() - started
        started = time.perf_counter()
        data = collect()
        warm = time.perf_counter() - started
        by = {}
        for bucket in data["buckets"]:
            row = by.setdefault(bucket["provider"], {"calls": 0, "input": 0, "output": 0, "reasoning": 0})
            row["calls"] += bucket["calls"]
            row["input"] += bucket["input"]
            row["output"] += bucket["output"]
            row["reasoning"] += bucket["reasoning"]
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        quota = (data.get("quota") or {}).get("grok") or {}
        print(json.dumps({
            "buckets": len(data["buckets"]),
            "payload_bytes": len(payload),
            "cold_s": round(cold, 3),
            "warm_s": round(warm, 3),
            "by_provider": by,
            "grok_quota_percent": quota.get("percent"),
            "grok_tier": quota.get("tier"),
        }, ensure_ascii=False, indent=2))
        return

    port = PORT
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    try:
        server = ThreadingHTTPServer((HOST, port), Handler)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            sys.stderr.write(
                f"Le port {port} est déjà utilisé : le tableau de bord tourne sans doute déjà sur http://{HOST}:{port}\n"
                f"Arrête l'autre processus, ou choisis un autre port : python3 server.py {port + 1}\n"
            )
            sys.exit(1)
        raise
    print(f"Dashboard sur http://{HOST}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\narrêt", flush=True)


if __name__ == "__main__":
    main()
