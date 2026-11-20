# -*- coding: utf-8 -*-
"""Telegram Mini App API. Every mutation is authenticated with Telegram initData."""
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from aiohttp import web

import database
from config import BOT_TOKEN
from data import CRABS
from game_logic import (
    get_effective_stats, level_up_cost, get_depth_zone_name, get_molt_rank,
    roll_monster, player_attack, monster_attack, gold_reward, apply_idle_regen,
    next_monster_meters, get_user_mutations_v3,  # imported defensively below
)

logger = logging.getLogger("crab-rpg-webapp")


def verify_init_data(init_data: str):
    if not init_data or not BOT_TOKEN:
        return None
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = fields.pop("hash", "")
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        auth_date = int(fields.get("auth_date", "0"))
        if auth_date <= 0 or abs(int(time.time()) - auth_date) > 86400:
            return None
        user = json.loads(fields.get("user", "{}"))
        uid = int(user.get("id", 0))
        return (uid, user) if uid > 0 else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _auth(request):
    result = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not result:
        raise web.HTTPUnauthorized(text="Telegram authorization expired. Reopen the Mini App.")
    return result


def _user_json(user):
    if not user:
        return {"registered": False, "needs_crab": True, "crabs": [
            {"id": int(cid), "name": c["name"], "desc": c["desc"]} for cid, c in CRABS.items()
        ]}
    crab_type = user.get("crab_type")
    payload = {
        "registered": True,
        "needs_crab": not bool(crab_type),
        "nickname": user.get("nickname") or "Краб",
        "crab_type": crab_type,
        "crab_name": CRABS.get(crab_type, {}).get("name", "Краб"),
        "crab_level": int(user.get("crab_level") or 1),
        "molts": int(user.get("molts") or 0),
        "gold": int(user.get("gold") or 0),
        "dna_points": int(user.get("dna_points") or 0),
        "shells": int(user.get("nautilus_shells") or 0),
        "cur_meters": int(user.get("cur_meters") or 0),
        "max_meters": int(user.get("max_meters") or 0),
        "kills": int(user.get("kills") or 0),
        "boss_kills": int(user.get("boss_kills") or 0),
        "rank": get_molt_rank(int(user.get("molts") or 0)),
        "zone": get_depth_zone_name(int(user.get("max_meters") or 0)),
        "in_hunt": bool(user.get("in_hunt")),
        "dig_start_ts": user.get("dig_start_ts"),
        "dig_duration_seconds": user.get("dig_duration_seconds"),
        "crabs": [{"id": int(cid), "name": c["name"], "desc": c["desc"]} for cid, c in CRABS.items()],
    }
    if crab_type:
        stones = database.get_stones(user["user_id"])
        mutations = database.get_user_mutations_v3(user["user_id"])
        stats = get_effective_stats(user, stones, mutations)
        payload["stats"] = {
            "damage": round(stats["damage"], 1),
            "evasion": round(stats["evasion"], 1),
            "crit": round(stats["crit_chance"], 1),
            "max_hp": round(stats["max_hp"]),
        }
        payload["level_cost"] = level_up_cost(payload["crab_level"], payload["molts"])
    if user.get("in_hunt") and user.get("monster_json"):
        try:
            monster = json.loads(user["monster_json"])
            payload["battle"] = {
                "name": monster.get("name", "Морское существо"),
                "hp": int(monster.get("hp", 0)),
                "max_hp": int(monster.get("max_hp", 1)),
                "meters": int(monster.get("meters", payload["cur_meters"] + 1)),
                "art": monster.get("art", ""),
            }
        except (ValueError, TypeError):
            payload["battle"] = None
    return payload


async def api_state(request):
    uid, tg_user = _auth(request)
    await database.run_async(database.create_user, uid, tg_user.get("username") or tg_user.get("first_name") or f"Краб{uid % 100000}")
    user = await database.run_async(database.get_user, uid)
    return web.json_response(_user_json(user))


async def api_choose_crab(request):
    uid, _ = _auth(request)
    body = await request.json()
    crab_type = body.get("crab_type")
    if crab_type not in CRABS:
        raise web.HTTPBadRequest(text="Unknown crab type")
    user = await database.run_async(database.get_user, uid)
    if user and user.get("crab_type"):
        raise web.HTTPConflict(text="Crab already selected")
    await database.run_async(database.update_user, uid, crab_type=crab_type, cur_hp=CRABS[crab_type]["max_hp"])
    return web.json_response(_user_json(await database.run_async(database.get_user, uid)))


async def api_level_up(request):
    uid, _ = _auth(request)
    def upgrade():
        with database.get_conn() as conn:
            with conn.transaction():
                user = conn.execute("SELECT crab_level, molts, gold FROM users WHERE user_id=%s FOR UPDATE", (uid,)).fetchone()
                if not user or not user["crab_level"]:
                    raise ValueError("Choose a crab first")
                cost = level_up_cost(user["crab_level"], user["molts"])
                if user["gold"] < cost:
                    raise RuntimeError("Not enough gold")
                conn.execute("UPDATE users SET crab_level=crab_level+1, gold=gold-%s WHERE user_id=%s", (cost, uid))
                return cost
    try:
        cost = await database.run_async(upgrade)
    except RuntimeError as exc:
        raise web.HTTPConflict(text=str(exc))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    return web.json_response({"ok": True, "cost": cost, "state": _user_json(await database.run_async(database.get_user, uid))})


async def api_hunt(request):
    uid, _ = _auth(request)
    user = await database.run_async(database.get_user, uid)
    if not user or not user.get("crab_type"):
        raise web.HTTPConflict(text="Choose your crab first")
    stones = await database.run_async(database.get_stones, uid)
    mutations = await database.run_async(database.get_user_mutations_v3, uid)
    stats = get_effective_stats(user, stones, mutations)
    now = int(time.time())

    if not user.get("in_hunt"):
        meters = int(user.get("cur_meters") or 0) + 1
        monster = roll_monster(meters)
        monster["meters"] = meters
        hp = apply_idle_regen(user, stats, now)
        started = await database.run_async(database.try_start_new_hunt, uid, json.dumps(monster, ensure_ascii=False), hp, now)
        if not started:
            raise web.HTTPConflict(text="Battle already started. Refresh and try again.")
        return web.json_response({"event": "started", "battle": {"name": monster["name"], "hp": monster["hp"], "max_hp": monster["max_hp"], "meters": meters, "art": monster.get("art", "")}, "state": _user_json(await database.run_async(database.get_user, uid))})

    try:
        monster = json.loads(user.get("monster_json") or "{}")
    except ValueError:
        monster = {}
    if not monster or "hp" not in monster:
        await database.run_async(database.update_user, uid, in_hunt=0, monster_json=None)
        raise web.HTTPConflict(text="Battle reset. Press Hunt again.")
    damage, critical, missed = player_attack(stats, monster.get("evasion", 0))
    monster["hp"] = max(0, int(monster["hp"]) - damage)
    battle_log = "Промах!" if missed else (f"Критический удар: −{damage} HP" if critical else f"Ты наносишь −{damage} HP")
    updates = {}
    if monster["hp"] > 0:
        incoming, dodged = monster_attack(monster, stats)
        hp = max(0, float(user.get("cur_hp") or stats["max_hp"]) - incoming)
        battle_log += " · " + ("Ты уклонился от ответа!" if dodged else f"Враг атакует: −{incoming} HP")
        if hp <= 0:
            back = max(0, int(user.get("cur_meters") or 0) - 1)
            updates = {"in_hunt": 0, "monster_json": None, "cur_hp": 0, "cur_meters": back}
            battle_log += " · Краб отступил к берегу."
        else:
            updates = {"monster_json": json.dumps(monster, ensure_ascii=False), "cur_hp": hp}
            battle_log += f" · Твои HP: {round(hp)}/{round(stats['max_hp'])}"
    else:
        reward = gold_reward(monster, stats, user)
        meters = int(monster.get("meters", int(user.get("cur_meters") or 0) + 1))
        updates = {
            "in_hunt": 0, "monster_json": None, "cur_hp": stats["max_hp"],
            "cur_meters": meters, "max_meters": max(int(user.get("max_meters") or 0), meters),
            "kills": int(user.get("kills") or 0) + 1,
            "gold": int(user.get("gold") or 0) + reward,
            "total_earned_gold": int(user.get("total_earned_gold") or 0) + reward,
        }
        battle_log = f"Победа! +{reward} золота · глубина {meters} м"
    applied = await database.run_async(database.try_apply_attack_result, uid, user["monster_json"], **updates)
    if not applied:
        raise web.HTTPConflict(text="Battle state changed. Refresh the game.")
    return web.json_response({"event": "attack", "log": battle_log, "battle": None if not updates.get("in_hunt", 1) else {"name": monster["name"], "hp": monster["hp"], "max_hp": monster["max_hp"], "meters": monster.get("meters", 0), "art": monster.get("art", "")}, "state": _user_json(await database.run_async(database.get_user, uid))})


def setup_webapp(app):
    app.router.add_get("/app", lambda request: web.FileResponse("webapp/index.html"))
    app.router.add_get("/app/", lambda request: web.FileResponse("webapp/index.html"))
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/choose-crab", api_choose_crab)
    app.router.add_post("/api/level-up", api_level_up)
    app.router.add_post("/api/hunt", api_hunt)
