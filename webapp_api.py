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
from config import BOT_TOKEN, ADMIN_IDS
from data import CRABS, UNIQUE_ABILITIES, SHIELD_ABILITY, ELITE_ENCOUNTER_MULT
from game_logic import (
    get_effective_stats, level_up_cost, get_depth_zone_name, get_molt_rank,
    roll_monster, player_attack, monster_attack, gold_reward, apply_idle_regen,
    next_monster_meters, is_guard_camp_meter, roll_guard_camp, is_elite_encounter_meter,
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
        data_check_string = "
".join(f"{k}={v}" for k, v in sorted(fields.items()))
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
        "unique": UNIQUE_ABILITIES.get(int(crab_type), {}),
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
                "is_elite": bool(monster.get("is_elite")),
                "camp_remaining": len(monster.get("camp_remaining", [])),
                "unique_used": bool(monster.get("unique_used")),
                "shield_cd": int(monster.get("shield_cd", 0)),
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
    payload = _user_json(user)
    payload["is_admin"] = uid in ADMIN_IDS
    return web.json_response(payload)


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
    payload = _user_json(await database.run_async(database.get_user, uid))\n    payload["is_admin"] = uid in ADMIN_IDS\n    return web.json_response(payload)


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
    body = await request.json()
    action = body.get("action", "attack")
    user = await database.run_async(database.get_user, uid)
    if not user or not user.get("crab_type"):
        raise web.HTTPConflict(text="Choose your crab first")
    stones = await database.run_async(database.get_stones, uid)
    mutations = await database.run_async(database.get_user_mutations_v3, uid)
    stats = get_effective_stats(user, stones, mutations)
    now = int(time.time())

    if not user.get("in_hunt"):
        current_meters = int(user.get("cur_meters") or 0)
        meters = next_monster_meters(current_meters, mutations)
        if meters <= current_meters:
            raise web.HTTPConflict(text="Depth barrier: upgrade mutations to descend")
        if is_guard_camp_meter(current_meters, meters):
            camp = roll_guard_camp(meters)
            monster = camp[0]
            monster["camp_remaining"] = camp[1:]
            monster["is_camp"] = True
        elif is_elite_encounter_meter(current_meters, meters):
            monster = roll_monster(meters, elite_mult=ELITE_ENCOUNTER_MULT)
            monster["is_elite"] = True
        else:
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


    if action == "nectar":
        nectar = await database.get_nectars_data(uid)
        active = nectar.get("active_nectar")
        inv = nectar.get("nectars_inv") or {}
        if not active or int(inv.get(active,0)) <= 0:
            raise web.HTTPConflict(text="No equipped nectar in inventory")
        if active == "strength" and int(nectar.get("strength_charges",0)) > 0:
            raise web.HTTPConflict(text="Strength nectar is already active")
        inv[active] = int(inv.get(active,0))-1
        await database.update_nectars_data(uid,nectars_inv=inv)
        hp = float(user.get("cur_hp") or stats["max_hp"])
        if active == "strength":
            await database.update_nectars_data(uid,strength_charges=5)
            log = "Нектар силы: +20% урона на 5 атак"
        elif active == "refreshing":
            heal = round(stats["max_hp"]*0.35)
            hp = min(stats["max_hp"],hp+heal)
            log = f"Освежающий нектар восстановил {heal} HP"
        elif active == "rage":
            sacrifice = round(stats["max_hp"]*0.15)
            hp = max(0,hp-sacrifice)
            monster["nectar_rage"] = True
            log = f"Нектар ярости: −{sacrifice} HP, критические удары активированы"
        elif active == "sparkling":
            monster["nectar_sparkling"] = True
            log = "Сверкающий нектар: золото с этого врага ×2.5"
        else:
            raise web.HTTPBadRequest(text="Unknown nectar")
        incoming,dodged = monster_attack(monster,stats)
        if monster.get("shield_active"):
            incoming=int(incoming*(1-SHIELD_ABILITY.get("block_percent",80)/100))
            monster["shield_active"]=False
        hp=max(0,hp-incoming)
        log += " · " + ("Уклонение от ответа!" if dodged else f"Враг нанёс −{incoming} HP")
        if hp<=0:
            back=max(0,int(user.get("cur_meters") or 0)-1)
            updates={"in_hunt":0,"monster_json":None,"cur_hp":0,"cur_meters":back}
        else:
            updates={"monster_json":json.dumps(monster,ensure_ascii=False),"cur_hp":hp}
        saved=await database.run_async(database.try_apply_attack_result,uid,user["monster_json"],**updates)
        if not saved: raise web.HTTPConflict(text="Battle state changed. Refresh.")
        return web.json_response({"event":"nectar","log":log,"state":_user_json(await database.run_async(database.get_user,uid))})

    if action == "ability":
        ability = body.get("ability")
        if ability == "shield":
            if int(monster.get("shield_cd",0)) > 0:
                raise web.HTTPConflict(text="Shield is on cooldown")
            monster["shield_active"] = True
            monster["shield_cd"] = 3
        elif ability == "unique":
            if monster.get("unique_used"):
                raise web.HTTPConflict(text="Unique ability already used this battle")
            unique = UNIQUE_ABILITIES.get(int(user["crab_type"]), {})
            key = unique.get("key")
            monster["unique_used"] = True
            if key == "crushing_blow":
                monster["unique_damage_mult"] = float(unique.get("damage_mult",2.5))
                monster["unique_self_cost_once"] = float(unique.get("self_damage_percent",20))/100
            elif key == "sprint":
                monster["sprint_turns"] = int(unique.get("sprint_turns",4))
            elif key == "blood_rage":
                monster["unique_blood_rage"] = True
                monster["unique_self_cost_pct"] = float(unique.get("self_damage_percent",6))/100
            else:
                raise web.HTTPBadRequest(text="Unknown unique ability")
        else:
            raise web.HTTPBadRequest(text="Unknown ability")
        saved = await database.run_async(database.try_apply_attack_result,uid,user["monster_json"],monster_json=json.dumps(monster,ensure_ascii=False))
        if not saved: raise web.HTTPConflict(text="Battle state changed. Refresh.")
        return web.json_response({"event":"ability","log":"Способность активирована","battle":{"name":monster["name"],"hp":monster["hp"],"max_hp":monster["max_hp"],"meters":monster.get("meters",0),"art":monster.get("art","")},"state":_user_json(await database.run_async(database.get_user,uid))})
    if action != "attack":
        raise web.HTTPBadRequest(text="Unknown battle action")

    nectar_state = await database.get_nectars_data(uid)
    strength_charges = int(nectar_state.get("strength_charges",0))
    if strength_charges > 0:
        stats = dict(stats)
        stats["damage"] *= 1.2
        await database.update_nectars_data(uid,strength_charges=strength_charges-1)
    sprint = int(monster.get("sprint_turns",0)) > 0
    fatigue = int(monster.get("fatigue_turns",0))
    battle_stats = dict(stats)
    if fatigue: battle_stats["evasion"] = max(0,float(battle_stats.get("evasion",0))-15)
    enemy_evasion = float(monster.get("evasion",0)) + (15 if fatigue else 0)
    damage, critical, missed = player_attack(stats, -100 if sprint else enemy_evasion, force_crit=bool(monster.get("unique_blood_rage") or monster.get("nectar_rage")))
    damage = int(damage * float(monster.get("unique_damage_mult",1.0)))
    monster["unique_damage_mult"] = 1.0
    monster["hp"] = max(0, int(monster["hp"]) - damage)
    battle_log = "Промах!" if missed else (f"Критический удар: −{damage} HP" if critical else f"Ты наносишь −{damage} HP")
    updates = {}
    if monster["hp"] > 0:
        if sprint:
            incoming, dodged = 0, True
        else:
            incoming, dodged = monster_attack(monster, battle_stats)
        if monster.get("shield_active"):
            incoming = int(incoming * (1 - SHIELD_ABILITY.get("block_percent",80)/100))
            monster["shield_active"] = False
        if int(monster.get("shield_cd",0)) > 0: monster["shield_cd"] = max(0,int(monster["shield_cd"])-1)
        hp = max(0, float(user.get("cur_hp") or stats["max_hp"]) - incoming)
        self_pct = float(monster.get("unique_self_cost_pct",0))
        once_pct = float(monster.pop("unique_self_cost_once",0))
        if self_pct or once_pct:
            hp = max(0,hp - int(stats["max_hp"]*(self_pct+once_pct)))
        if sprint:
            monster["sprint_turns"] = max(0,int(monster.get("sprint_turns",0))-1)
            if monster["sprint_turns"] == 0:
                monster["fatigue_turns"] = 4
        elif fatigue:
            monster["fatigue_turns"] = max(0,fatigue-1)
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
        if monster.pop("nectar_sparkling",False): reward = int(reward*2.5)
        meters = int(monster.get("meters", int(user.get("cur_meters") or 0) + 1))
        new_gold = int(user.get("gold") or 0) + reward
        new_total = int(user.get("total_earned_gold") or 0) + reward
        new_kills = int(user.get("kills") or 0) + 1
        camp_remaining = list(monster.get("camp_remaining") or [])
        if camp_remaining:
            next_monster = camp_remaining.pop(0)
            next_monster["meters"] = meters
            next_monster["is_camp"] = True
            next_monster["camp_remaining"] = camp_remaining
            updates = {"in_hunt":1,"monster_json":json.dumps(next_monster,ensure_ascii=False),"cur_hp":stats["max_hp"],"gold":new_gold,"total_earned_gold":new_total,"kills":new_kills}
            battle_log = f"Лагерь: враг повержен, +{reward} золота. Следующий противник!"
        else:
            updates = {
                "in_hunt": 0, "monster_json": None, "cur_hp": stats["max_hp"],
                "cur_meters": meters, "max_meters": max(int(user.get("max_meters") or 0), meters),
                "kills": new_kills, "gold": new_gold, "total_earned_gold": new_total,
            }
            battle_log = f"Победа! +{reward} золота · глубина {meters} м"
    applied = await database.run_async(database.try_apply_attack_result, uid, user["monster_json"], **updates)
    if not applied:
        raise web.HTTPConflict(text="Battle state changed. Refresh the game.")
    return web.json_response({"event": "attack", "log": battle_log, "battle": None if not updates.get("in_hunt", 1) else {"name": monster["name"], "hp": monster["hp"], "max_hp": monster["max_hp"], "meters": monster.get("meters", 0), "art": monster.get("art", "")}, "state": _user_json(await database.run_async(database.get_user, uid))})



async def api_level_up_max(request):
    uid,_=_auth(request)
    def upgrade():
        with database.get_conn() as conn, conn.transaction():
            user=conn.execute("SELECT crab_level,molts,gold FROM users WHERE user_id=%s FOR UPDATE",(uid,)).fetchone()
            if not user: raise ValueError("Profile not found")
            level=int(user["crab_level"]); gold=int(user["gold"]); total=0; count=0
            while count<5000:
                cost=level_up_cost(level,user["molts"])
                if total+cost>gold: break
                total+=cost; count+=1; level+=1
            if count==0: raise RuntimeError("Not enough gold")
            conn.execute("UPDATE users SET crab_level=%s,gold=gold-%s WHERE user_id=%s",(level,total,uid))
            return count,total
    try: count,total=await database.run_async(upgrade)
    except RuntimeError as exc: raise web.HTTPConflict(text=str(exc))
    except ValueError as exc: raise web.HTTPBadRequest(text=str(exc))
    return web.json_response({"ok":True,"levels":count,"cost":total,"state":_user_json(await database.run_async(database.get_user,uid))})


def setup_webapp(app, bot=None):
    app.router.add_get("/app", lambda request: web.FileResponse("webapp/index.html"))
    app.router.add_get("/app/", lambda request: web.FileResponse("webapp/index.html"))
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/choose-crab", api_choose_crab)
    app.router.add_post("/api/level-up", api_level_up)
    app.router.add_post("/api/level-up/max", api_level_up_max)
    app.router.add_post("/api/hunt", api_hunt)
    from webapp_extra import register_extra
    register_extra(app, _auth, _user_json, bot)
