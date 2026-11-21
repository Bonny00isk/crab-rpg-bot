# -*- coding: utf-8 -*-
"""Additional authenticated Mini App features: inventory, dig, lab, evolution and social."""
import json, time
from aiohttp import web
from aiogram.types import LabeledPrice
import database
from config import ADMIN_IDS
from data import (STONE_COLORS, RESOURCES, PASSIVE_MUTATIONS, ACTIVE_MUTATIONS,
                  NECTARS_DATA, EVOLUTION_NODES, DIG_DURATION_OPTIONS_HOURS,
                  EVENT_CHESTS, MYTHIC_EVENTS, PROMO_CODES, SHOP_ITEMS, MYTHIC_EVENT_UNLOCK_MOLTS,
                  MYTHIC_EVENT_UNLOCK_MAX_METERS, MYTHIC_EVENT_UNLOCK_KILLS)
from game_logic import (get_mutation_cost, cost_upgrade_mutation, can_molt,
                        calculate_dna_reward, apply_permanent_boost,
                        roll_dig_loot, roll_dig_resources, roll_chest_loot, mythic_events_unlocked,
                        get_depth_zone_name, get_molt_rank, format_number)

def register_extra(app, auth, user_json, bot=None):
    async def data(request):
        uid,_=auth(request)
        u=await database.run_async(database.get_user,uid)
        if not u: raise web.HTTPNotFound(text="Profile not found")
        stones=await database.run_async(database.get_stones,uid)
        resources=await database.run_async(database.get_resources,uid)
        muts=await database.run_async(database.get_user_mutations_v3,uid)
        chests=await database.run_async(database.get_chests,uid)
        nectar=await database.get_nectars_data(uid)
        return web.json_response({
            "stones":[{"color":s["color"],"color_name":STONE_COLORS.get(s["color"],{}).get("name",s["color"]),"level":s["level"],"count":s["count"]} for s in stones],
            "resources":[{"key":k,"name":v["name"],"count":resources.get(k,0)} for k,v in RESOURCES.items()],
            "mutations":[{"key":m["variant_key"],"name":(ACTIVE_MUTATIONS if m["is_active"] else PASSIVE_MUTATIONS).get(m["variant_key"],{}).get("name",m["variant_key"]),"active":bool(m["is_active"]),"level":m["level"],"equipped":bool(m["equipped"])} for m in muts],
            "chests":[{"key":k,"name":EVENT_CHESTS.get(int(k) if str(k).isdigit() else k,{}).get("name",str(k)),"count":v} for k,v in chests.items() if v>0],
            "nectar":nectar,
            "nectars":[{"key":k,**v,"count":nectar["nectars_inv"].get(k,0)} for k,v in NECTARS_DATA.items()],
            "passiveShop":[{"key":k,"name":v["name"],"cost":get_mutation_cost(sum(1 for m in muts if not m["is_active"]))} for k,v in PASSIVE_MUTATIONS.items() if not any(m["variant_key"]==k for m in muts)],
            "crystal":(__import__("data").CRABS[int(u["crab_type"])]["crystal"] if u.get("crab_type") else ""),
            "dig":{"start":u.get("dig_start_ts"),"duration":u.get("dig_duration_seconds"),"hours":DIG_DURATION_OPTIONS_HOURS},
            "evolution":{"stage":u.get("evolution_stage",0),"tree":json.loads(u.get("evolution_tree") or "[]"),"nodes":EVOLUTION_NODES},
            "stats":{"total_gold":u.get("total_earned_gold",0),"total_dna":u.get("total_dna_earned",0),"kills":u.get("kills",0),"boss_kills":u.get("boss_kills",0)}
        })

    async def dig_start(request):
        uid,_=auth(request); body=await request.json()
        hours=int(body.get("hours",0))
        if hours not in DIG_DURATION_OPTIONS_HOURS: raise web.HTTPBadRequest(text="Invalid digging duration")
        ok=await database.run_async(database.try_start_dig,uid,hours*3600)
        if not ok: raise web.HTTPConflict(text="Digging is already running")
        return web.json_response({"ok":True,"state":await database.run_async(database.get_user,uid)})

    async def dig_collect(request):
        uid,_=auth(request); u=await database.run_async(database.get_user,uid)
        start=u.get("dig_start_ts"); duration=u.get("dig_duration_seconds") or 3600
        now=int(time.time())
        if not start or now-start<duration: raise web.HTTPConflict(text="Digging is not finished")
        if not await database.run_async(database.try_collect_dig,uid,start): raise web.HTTPConflict(text="Already collected")
        hours=duration/3600; stones=roll_dig_loot(hours); resources=roll_dig_resources(hours)
        for color,level in stones: await database.run_async(database.add_stone,uid,color,level,1)
        for key in resources: await database.run_async(database.add_resource,uid,key,1)
        return web.json_response({"ok":True,"stones":[{"color":STONE_COLORS[c]["name"],"level":l} for c,l in stones],"resources":[RESOURCES[k]["name"] for k in resources]})

    async def mutations_action(request):
        uid,_=auth(request); body=await request.json(); action=body.get("action"); key=str(body.get("key",""))
        passive=PASSIVE_MUTATIONS.get(key); active=ACTIVE_MUTATIONS.get(key)
        owned=await database.run_async(database.get_user_mutations_v3,uid)
        item=next((m for m in owned if m["variant_key"]==key),None)
        user=await database.run_async(database.get_user,uid)
        if action=="buy":
            if not passive: raise web.HTTPBadRequest(text="Invalid passive mutation")
            if item: raise web.HTTPConflict(text="Mutation already owned")
            bought=sum(1 for m in owned if not m["is_active"])
            cost=get_mutation_cost(bought)
            if not await database.run_async(database.try_spend,uid,"dna_points",cost): raise web.HTTPConflict(text="Not enough DNA")
            await database.run_async(database.add_new_mutation_v3,uid,key,0)
            return web.json_response({"ok":True,"cost":cost})
        if action=="equip":
            if not item or not active: raise web.HTTPBadRequest(text="Active mutation not owned")
            slot=active.get("slot")
            for m in owned:
                if m.get("is_active") and m.get("equipped") and ACTIVE_MUTATIONS.get(m["variant_key"],{}).get("slot")==slot:
                    await database.run_async(database.unequip_active_mutation,uid,m["variant_key"])
            await database.run_async(database.equip_active_mutation,uid,key)
            return web.json_response({"ok":True})
        if action=="unequip":
            if not item or not active: raise web.HTTPBadRequest(text="Mutation not owned")
            await database.run_async(database.unequip_active_mutation,uid,key)
            return web.json_response({"ok":True})
        if action=="upgrade":
            if not item: raise web.HTTPBadRequest(text="Mutation not owned")
            cost=cost_upgrade_mutation(int(item["level"]))
            if not await database.run_async(database.try_spend,uid,"dna_points",cost): raise web.HTTPConflict(text="Not enough DNA")
            await database.run_async(database.upgrade_mutation_v3,uid,key)
            return web.json_response({"ok":True,"cost":cost})
        raise web.HTTPBadRequest(text="Unknown mutation action")

    async def evolution_pick(request):
        uid,_=auth(request); body=await request.json(); key=str(body.get("key",""))
        user=await database.run_async(database.get_user,uid)
        tree=json.loads(user.get("evolution_tree") or "[]")
        stage=int(user.get("evolution_stage") or 0)
        if min(10,int(user["crab_level"])//50)-stage<=0: raise web.HTTPConflict(text="No evolution points")
        node=EVOLUTION_NODES.get(key)
        if not node or key in tree: raise web.HTTPBadRequest(text="Invalid evolution node")
        from data import CRABS
        crystal=CRABS[int(user["crab_type"])]["crystal"]
        if not key.startswith(crystal): raise web.HTTPBadRequest(text="Evolution does not match your crab")
        await database.run_async(database.update_evolution_tree,uid,stage+1,key)
        return web.json_response({"ok":True})

    async def nectar_action(request):
        uid,_=auth(request); body=await request.json(); action=body.get("action"); key=str(body.get("key",""))
        if action=="equip":
            nectar=await database.get_nectars_data(uid)
            if key and nectar["nectars_inv"].get(key,0)<=0: raise web.HTTPConflict(text="Nectar not owned")
            await database.update_nectars_data(uid,active_nectar=key or None)
            return web.json_response({"ok":True})
        if action=="craft":
            recipe=NECTARS_DATA.get(key)
            if not recipe: raise web.HTTPBadRequest(text="Unknown nectar")
            def craft_atomic():
                with database.get_conn() as conn, conn.transaction():
                    for rk,need in recipe["res_cost"].items():
                        row=conn.execute("UPDATE resources SET count=count-%s WHERE user_id=%s AND key=%s AND count >= %s RETURNING count",(need,uid,rk,need)).fetchone()
                        if not row: raise RuntimeError("Not enough resources: "+RESOURCES[rk]["name"])
                    st=recipe["stone_cost"]
                    row=conn.execute("UPDATE stones SET count=count-%s WHERE user_id=%s AND color=%s AND level=%s AND count >= %s RETURNING count",(st["count"],uid,st["color"],st["level"],st["count"])).fetchone()
                    if not row: raise RuntimeError("Not enough required stones")
                    row=conn.execute("SELECT nectars_inv FROM users WHERE user_id=%s FOR UPDATE",(uid,)).fetchone()
                    inv=json.loads(row["nectars_inv"] or "{}")
                    inv[key]=int(inv.get(key,0))+1
                    conn.execute("UPDATE users SET nectars_inv=%s WHERE user_id=%s",(json.dumps(inv),uid))
            try: await database.run_async(craft_atomic)
            except RuntimeError as e: raise web.HTTPConflict(text=str(e))
            return web.json_response({"ok":True})
        raise web.HTTPBadRequest(text="Unknown nectar action")



    async def shell_shop(request):
        uid,_=auth(request); body=await request.json(); key=body.get("key")
        u=await database.run_async(database.get_user,uid)
        if key=="speed_dig":
            cost=3
            if not u.get("dig_start_ts"): raise web.HTTPConflict(text="No active digging")
            if not await database.run_async(database.try_spend,uid,"nautilus_shells",cost): raise web.HTTPConflict(text="Need 3 shells")
            await database.run_async(database.update_user,uid,dig_start_ts=int(time.time())-(u.get("dig_duration_seconds") or 3600))
            return web.json_response({"ok":True,"message":"Digging completed"})
        if key=="resource_pack":
            cost=5
            if not await database.run_async(database.try_spend,uid,"nautilus_shells",cost): raise web.HTTPConflict(text="Need 5 shells")
            for rk in RESOURCES: await database.run_async(database.add_resource,uid,rk,2)
            return web.json_response({"ok":True,"message":"Resources +2 each"})
        raise web.HTTPBadRequest(text="Unknown shell item")

    async def promo(request):
        uid,_=auth(request); body=await request.json(); code=str(body.get("code","")).strip().lower()
        # Admin-created promos live in PostgreSQL; legacy static codes remain supported.
        exists=await database.run_async(database.admin_list_promos)
        if any(p["code"]==code for p in exists):
            ok,reward=await database.run_async(database.redeem_admin_promo,uid,code)
            if not ok: raise web.HTTPConflict(text=reward)
            return web.json_response({"ok":True,"reward":reward})
        item=PROMO_CODES.get(code)
        if not item: raise web.HTTPBadRequest(text="Invalid promo code")
        ok=await database.run_async(database.try_redeem_promo,uid,code,gold=item.get("gold",0),dna=item.get("dna",0),shells=item.get("shells",0),boost=item.get("boost",False))
        if not ok: raise web.HTTPConflict(text="Promo already redeemed")
        return web.json_response({"ok":True,"reward":item})

    async def open_chest(request):
        uid,_=auth(request); body=await request.json(); key=str(body.get("key",""))
        chests=await database.run_async(database.get_chests,uid)
        if int(chests.get(key,0))<=0: raise web.HTTPConflict(text="No chest of this type")
        chest_key=int(key) if key.isdigit() else key
        chest=EVENT_CHESTS.get(chest_key)
        if not chest: raise web.HTTPBadRequest(text="Unknown chest")
        owned=await database.run_async(database.get_user_mutations_v3,uid)
        owned_keys=[m["variant_key"] for m in owned]
        loot=roll_chest_loot(chest_key,owned_keys)
        if not await database.run_async(database.try_spend_chest,uid,key,1): raise web.HTTPConflict(text="Chest already opened")
        if loot.get("shells",0):
            await database.run_async(database.update_user,uid,nautilus_shells=int((await database.run_async(database.get_user,uid)).get("nautilus_shells",0))+int(loot["shells"]))
        for color,level in loot.get("stones",[]):
            await database.run_async(database.add_stone,uid,color,level,1)
        mutation=loot.get("mutation")
        if mutation and mutation.get("key") and mutation["key"] not in owned_keys:
            await database.run_async(database.add_new_mutation_v3,uid,mutation["key"],1)
        return web.json_response({"ok":True,"loot":{"shells":loot.get("shells",0),"stones":[{"color":color,"level":level} for color,level in loot.get("stones",[])],"mutation":mutation}})


    async def stars_invoice(request):
        uid,_=auth(request)
        if bot is None: raise web.HTTPServiceUnavailable(text="Telegram payments unavailable")
        body=await request.json(); key=str(body.get("key",""))
        item=SHOP_ITEMS.get(key)
        if not item: raise web.HTTPBadRequest(text="Unknown shop item")
        u=await database.run_async(database.get_user,uid)
        if key=="eternal_tide" and u.get("permanent_boost"): raise web.HTTPConflict(text="Permanent boost already owned")
        url=await bot.create_invoice_link(title=item["title"],description=item["description"],payload="shop:"+key,provider_token="",currency="XTR",prices=[LabeledPrice(label=item["title"],amount=int(item["stars"]))])
        return web.json_response({"url":url,"stars":item["stars"]})

    async def store_info(request):
        uid,_=auth(request); u=await database.run_async(database.get_user,uid)
        return web.json_response({"shells":u.get("nautilus_shells",0),"stars_items":[{"key":k,**v} for k,v in SHOP_ITEMS.items()],"shell_items":[{"key":"speed_dig","title":"⏩ Ускорить копание","shells":3},{"key":"resource_pack","title":"🎁 Набор ресурсов","shells":5}]})

    async def molt(request):
        uid,_=auth(request); u=await database.run_async(database.get_user,uid)
        if not can_molt(int(u["crab_level"])): raise web.HTTPConflict(text="Molt requires level 100")
        gain=apply_permanent_boost(calculate_dna_reward(int(u["crab_level"])),u)
        await database.run_async(database.update_user,uid,crab_level=1,gold=0,dna_points=int(u["dna_points"])+gain,molts=int(u["molts"])+1,total_dna_earned=int(u["total_dna_earned"])+gain,cur_meters=1,evolution_stage=0,evolution_tree="[]",crab_type=None,cur_hp=0,in_hunt=0,monster_json=None)
        return web.json_response({"ok":True,"dna":gain,"state":user_json(await database.run_async(database.get_user,uid))})

    async def events(request):
        uid,_=auth(request); u=await database.run_async(database.get_user,uid)
        active=await database.run_async(database.get_active_event)
        unlocked=mythic_events_unlocked(u)
        if not active:
            return web.json_response({"unlocked":unlocked,"event":None,"requirements":{"molts":MYTHIC_EVENT_UNLOCK_MOLTS,"meters":MYTHIC_EVENT_UNLOCK_MAX_METERS,"kills":MYTHIC_EVENT_UNLOCK_KILLS}})
        board=await database.run_async(database.get_event_leaderboard,active["id"],20)
        names=[]
        for row in board:
            p=await database.run_async(database.get_user,row["user_id"])
            names.append({"nickname":p["nickname"] if p else "Игрок","damage":row["damage"]})
        return web.json_response({"unlocked":unlocked,"event":{"id":active["id"],"name":active["name"],"description":active["description"],"ends_at":active["ends_at"],"leaderboard":names}})

    async def leaderboard(request):
        uid,_=auth(request)
        rows=await database.run_async(database.get_top_players,20)
        return web.json_response({"players":[{"rank":i+1,"nickname":r["nickname"],"level":r["crab_level"],"molts":r["molts"],"meters":r["max_meters"],"zone":get_depth_zone_name(r["max_meters"])} for i,r in enumerate(rows)]})



    async def random_players(request):
        uid,_=auth(request)
        rows=await database.run_async(database.get_random_players,uid,5)
        return web.json_response({"players":[{"nickname":p.get("nickname"),"level":p.get("crab_level",1),"molts":p.get("molts",0),"meters":p.get("max_meters",0),"zone":get_depth_zone_name(p.get("max_meters",0))} for p in rows]})

    async def player_search(request):
        uid,_=auth(request); body=await request.json(); name=str(body.get("nickname","")).strip()
        if not name or len(name)>32: raise web.HTTPBadRequest(text="Enter a nickname")
        target=await database.run_async(database.find_user_by_nickname,name)
        if not target: raise web.HTTPNotFound(text="Player not found")
        stones=await database.run_async(database.get_stones,target["user_id"])
        muts=await database.run_async(database.get_user_mutations_v3,target["user_id"])
        from game_logic import get_effective_stats
        stats=get_effective_stats(target,stones,muts) if target.get("crab_type") else {}
        from data import CRABS
        return web.json_response({"nickname":target.get("nickname"),"crab":CRABS.get(target.get("crab_type"),{}).get("name","No crab"),"level":target.get("crab_level",1),"molts":target.get("molts",0),"rank":get_molt_rank(target.get("molts",0)),"zone":get_depth_zone_name(target.get("max_meters",0)),"meters":target.get("max_meters",0),"kills":target.get("kills",0),"boss_kills":target.get("boss_kills",0),"damage":round(stats.get("damage",0),1),"hp":round(stats.get("max_hp",0)),"equipped":[ACTIVE_MUTATIONS.get(m["variant_key"],{}).get("name",m["variant_key"]) for m in muts if m.get("is_active") and m.get("equipped")]})

    async def nickname(request):
        uid,_=auth(request); body=await request.json(); name=str(body.get("nickname","")).strip()
        import re
        if not re.fullmatch(r"[a-zA-Zа-яА-ЯёЁ0-9 _\\-]{3,16}",name): raise web.HTTPBadRequest(text="Nickname must be 3–16 characters")
        user=await database.run_async(database.get_user,uid); now=int(time.time())
        last=int(user.get("last_nick_change_ts") or 0)
        if now-last<7*86400: raise web.HTTPConflict(text="Nickname change is available once per 7 days")
        exists=await database.run_async(database.find_user_by_nickname,name)
        if exists and int(exists["user_id"])!=uid: raise web.HTTPConflict(text="Nickname is already taken")
        await database.run_async(database.update_user,uid,nickname=name,last_nick_change_ts=now)
        return web.json_response({"ok":True,"nickname":name})


    def require_admin(uid):
        if int(uid) not in ADMIN_IDS:
            raise web.HTTPForbidden(text="Admin access required")

    async def admin_grant(request):
        uid,_=auth(request); require_admin(uid)
        body=await request.json()
        target=int(body.get("user_id",0)); amount=int(body.get("amount",0))
        if target<=0: raise web.HTTPBadRequest(text="Invalid player Telegram ID")
        player=await database.run_async(database.get_user,target)
        if not player: raise web.HTTPNotFound(text="Player is not registered")
        kind=str(body.get("kind",""))
        if kind=="currency":
            field=str(body.get("currency",""))
            if field not in ("gold","dna_points","nautilus_shells"): raise web.HTTPBadRequest(text="Invalid currency")
            if amount==0 or abs(amount)>1000000000: raise web.HTTPBadRequest(text="Amount out of range")
            await database.run_async(database.admin_grant_currency,target,field,amount)
            return web.json_response({"ok":True,"player":player.get("nickname"),"amount":amount,"kind":field})
        if kind=="item":
            item_type=str(body.get("item_type","")); key=str(body.get("item_key",""))
            valid=False
            if item_type=="chest": valid=(str(key) in {str(k) for k in EVENT_CHESTS})
            elif item_type=="resource": valid=key in RESOURCES
            elif item_type=="mutation": valid=key in PASSIVE_MUTATIONS or key in ACTIVE_MUTATIONS
            elif item_type=="nectar": valid=key in NECTARS_DATA
            elif item_type=="stone":
                parts=key.split(":")
                valid=len(parts)==2 and parts[0] in STONE_COLORS and parts[1].isdigit() and 1<=int(parts[1])<=10
            if not valid: raise web.HTTPBadRequest(text="Unknown item")
            if amount<1 or amount>1000: raise web.HTTPBadRequest(text="Item amount must be 1-1000")
            await database.run_async(database.admin_grant_item,target,item_type,key,amount)
            return web.json_response({"ok":True,"player":player.get("nickname"),"amount":amount,"item_type":item_type,"item_key":key})
        raise web.HTTPBadRequest(text="Unknown grant kind")

    async def admin_promo_create(request):
        uid,_=auth(request); require_admin(uid)
        body=await request.json()
        code=str(body.get("code","")).strip().lower()
        rewards={}
        for field in ("gold","dna","shells"):
            value=int(body.get(field,0) or 0)
            if value<0 or value>1000000000: raise web.HTTPBadRequest(text="Reward out of range")
            if value: rewards[field]=value
        item_type=str(body.get("item_type","")); item_key=str(body.get("item_key","")); item_count=int(body.get("item_count",0) or 0)
        if item_type:
            valid=(item_type=="chest" and item_key in {str(k) for k in EVENT_CHESTS}) or (item_type=="resource" and item_key in RESOURCES) or (item_type=="mutation" and (item_key in PASSIVE_MUTATIONS or item_key in ACTIVE_MUTATIONS)) or (item_type=="nectar" and item_key in NECTARS_DATA)
            if item_type=="stone":
                parts=item_key.split(":"); valid=len(parts)==2 and parts[0] in STONE_COLORS and parts[1].isdigit() and 1<=int(parts[1])<=10
            if not valid or item_count<1 or item_count>1000: raise web.HTTPBadRequest(text="Invalid promo item")
            rewards.update({"item_type":item_type,"item_key":item_key,"item_count":item_count})
        if not rewards: raise web.HTTPBadRequest(text="Specify at least one reward")
        max_uses=int(body.get("max_uses",0) or 0)
        if max_uses<0 or max_uses>10000000: raise web.HTTPBadRequest(text="Invalid activation limit")
        expires_hours=int(body.get("expires_hours",0) or 0)
        if expires_hours<0 or expires_hours>87600: raise web.HTTPBadRequest(text="Invalid expiry")
        expires_at=int(time.time())+expires_hours*3600 if expires_hours else 0
        try: code=await database.run_async(database.admin_create_promo,code,rewards,max_uses,expires_at)
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower(): raise web.HTTPConflict(text="Promo code already exists")
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({"ok":True,"code":code})

    async def admin_promo_list(request):
        uid,_=auth(request); require_admin(uid)
        return web.json_response({"promos":await database.run_async(database.admin_list_promos)})

    async def admin_promo_disable(request):
        uid,_=auth(request); require_admin(uid)
        body=await request.json(); code=str(body.get("code","")).strip().lower()
        ok=await database.run_async(database.admin_disable_promo,code)
        if not ok: raise web.HTTPNotFound(text="Active promo not found")
        return web.json_response({"ok":True})

    DAILY_REWARDS = [
        {"gold":100,"label":"100 золота","icon":"coin"},{"dna":3,"label":"3 ДНК","icon":"dna"},{"shells":2,"label":"2 раковины","icon":"shell"},{"gold":150,"label":"150 золота","icon":"coin"},{"chest":"1","label":"Жемчужный сундук","icon":"chest"},{"gold":200,"label":"200 золота","icon":"coin"},{"chest":"1","label":"Жемчужный сундук","icon":"chest"},{"dna":5,"label":"5 ДНК","icon":"dna"},{"shells":3,"label":"3 раковины","icon":"shell"},{"gold":300,"label":"300 золота","icon":"coin"},{"chest":"2","label":"Золотой сундук","icon":"chest"},{"dna":8,"label":"8 ДНК","icon":"dna"},{"gold":350,"label":"350 золота","icon":"coin"},{"chest":"2","label":"Золотой сундук","icon":"chest"},{"shells":5,"label":"5 раковин","icon":"shell"},{"gold":450,"label":"450 золота","icon":"coin"},{"dna":10,"label":"10 ДНК","icon":"dna"},{"chest":"2","label":"Золотой сундук","icon":"chest"},{"gold":500,"label":"500 золота","icon":"coin"},{"shells":6,"label":"6 раковин","icon":"shell"},{"chest":"3","label":"Роскошный сундук","icon":"chest"},{"dna":15,"label":"15 ДНК","icon":"dna"},{"gold":600,"label":"600 золота","icon":"coin"},{"shells":8,"label":"8 раковин","icon":"shell"},{"chest":"3","label":"Роскошный сундук","icon":"chest"},{"gold":750,"label":"750 золота","icon":"coin"},{"dna":20,"label":"20 ДНК","icon":"dna"},{"chest":"3","label":"Роскошный сундук","icon":"chest"},{"shells":10,"label":"10 раковин","icon":"shell"},{"gold":1000,"dna":15,"label":"1000 золота + 15 ДНК","icon":"treasure"},{"chest":"3","shells":20,"gold":1500,"label":"Роскошный сундук + 20 раковин + 1500 золота","icon":"crown"}
    ]
    def _daily_today():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).date().isoformat()
    def _daily_state(uid):
        from contextlib import closing
        today=_daily_today()
        with closing(database.get_conn()) as conn, conn:
            row=conn.execute("SELECT cycle,day_index,last_claim_date FROM daily_reward_progress WHERE user_id=%s",(uid,)).fetchone()
            if not row: row={"cycle":1,"day_index":0,"last_claim_date":None}
            claims=conn.execute("SELECT cycle,day_index,claimed_date FROM daily_reward_claims WHERE user_id=%s ORDER BY cycle,day_index",(uid,)).fetchall()
        claimed_today=row.get("last_claim_date")==today
        cycle=int(row["cycle"]); progress=int(row["day_index"])
        if progress>=31 and not claimed_today: cycle+=1; progress=0
        next_day=0 if claimed_today else progress+1
        return {"cycle":cycle,"day_index":progress,"claimable_day":next_day,"claimed_today":claimed_today,"today":today,"claims":[{"cycle":int(x["cycle"]),"day":int(x["day_index"]),"date":x["claimed_date"]} for x in claims],"rewards":DAILY_REWARDS}
    async def daily_rewards_state(request):
        uid,_=auth(request)
        if not await database.run_async(database.get_user,uid): raise web.HTTPNotFound(text="Profile not found")
        return web.json_response(await database.run_async(_daily_state,uid))
    async def daily_rewards_claim(request):
        uid,_=auth(request)
        def claim_atomic():
            from contextlib import closing
            today=_daily_today()
            with closing(database.get_conn()) as conn, conn.transaction():
                if not conn.execute("SELECT user_id FROM users WHERE user_id=%s FOR UPDATE",(uid,)).fetchone(): raise ValueError("Profile not found")
                row=conn.execute("SELECT cycle,day_index,last_claim_date FROM daily_reward_progress WHERE user_id=%s FOR UPDATE",(uid,)).fetchone()
                if not row:
                    conn.execute("INSERT INTO daily_reward_progress(user_id,cycle,day_index,last_claim_date) VALUES(%s,1,0,NULL) ON CONFLICT(user_id) DO NOTHING",(uid,))
                    row=conn.execute("SELECT cycle,day_index,last_claim_date FROM daily_reward_progress WHERE user_id=%s FOR UPDATE",(uid,)).fetchone()
                if row["last_claim_date"]==today: raise RuntimeError("Ежедневная награда уже получена. Возвращайся завтра.")
                cycle=int(row["cycle"]); day=int(row["day_index"])+1
                if day>31: cycle+=1; day=1
                reward=dict(DAILY_REWARDS[day-1])
                conn.execute("UPDATE users SET gold=gold+%s,dna_points=dna_points+%s,nautilus_shells=nautilus_shells+%s WHERE user_id=%s",(int(reward.get("gold",0)),int(reward.get("dna",0)),int(reward.get("shells",0)),uid))
                if reward.get("chest"):
                    conn.execute("INSERT INTO chests(user_id,chest_type,count) VALUES(%s,%s,1) ON CONFLICT(user_id,chest_type) DO UPDATE SET count=chests.count+1",(uid,str(reward["chest"])))
                conn.execute("INSERT INTO daily_reward_claims(user_id,cycle,day_index,claimed_date,reward_json) VALUES(%s,%s,%s,%s,%s)",(uid,cycle,day,today,json.dumps(reward,ensure_ascii=False)))
                conn.execute("INSERT INTO daily_reward_progress(user_id,cycle,day_index,last_claim_date) VALUES(%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET cycle=EXCLUDED.cycle,day_index=EXCLUDED.day_index,last_claim_date=EXCLUDED.last_claim_date",(uid,cycle,day,today))
                return {"day":day,"cycle":cycle,"reward":reward}
        try: result=await database.run_async(claim_atomic)
        except RuntimeError as exc: raise web.HTTPConflict(text=str(exc))
        except ValueError as exc: raise web.HTTPNotFound(text=str(exc))
        return web.json_response({"ok":True,**result,"state":await database.run_async(_daily_state,uid)})

    app.router.add_get("/api/daily-rewards",daily_rewards_state)
    app.router.add_post("/api/daily-rewards/claim",daily_rewards_claim)
    app.router.add_get("/api/game-data",data)
    app.router.add_post("/api/molt",molt)
    app.router.add_get("/api/shop",store_info)
    app.router.add_post("/api/shop/invoice",stars_invoice)
    app.router.add_post("/api/shop/shell",shell_shop)
    app.router.add_post("/api/promo",promo)
    app.router.add_post("/api/chest/open",open_chest)
    app.router.add_get("/api/events",events)
    app.router.add_post("/api/dig/start",dig_start)
    app.router.add_post("/api/dig/collect",dig_collect)
    app.router.add_post("/api/mutations/action",mutations_action)
    app.router.add_post("/api/evolution/pick",evolution_pick)
    app.router.add_post("/api/nectar/action",nectar_action)
    app.router.add_get("/api/leaderboard",leaderboard)
    app.router.add_post("/api/nickname",nickname)
    app.router.add_post("/api/player/search",player_search)
    app.router.add_get("/api/players/random",random_players)
    app.router.add_post("/api/admin/grant",admin_grant)
    app.router.add_post("/api/admin/promos",admin_promo_create)
    app.router.add_get("/api/admin/promos",admin_promo_list)
    app.router.add_post("/api/admin/promos/disable",admin_promo_disable)
