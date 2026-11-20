# -*- coding: utf-8 -*-
"""PostgreSQL persistence layer for Neon."""
import asyncio
import json
import os
import time
from contextlib import closing

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required. Configure the Neon connection string in the host environment.")

def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15)

async def run_async(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

def init_db():
    """Create the complete PostgreSQL schema. Safe to run on every boot."""
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT, nickname TEXT, crab_type INTEGER,
            shore INTEGER, crab_level INTEGER DEFAULT 1, gold BIGINT DEFAULT 0,
            dna_points BIGINT DEFAULT 0, molts INTEGER DEFAULT 0, max_meters INTEGER DEFAULT 0,
            cur_meters INTEGER DEFAULT 0, cur_hp DOUBLE PRECISION DEFAULT 0, in_hunt INTEGER DEFAULT 0,
            monster_json TEXT, kills BIGINT DEFAULT 0, boss_kills BIGINT DEFAULT 0,
            dig_start_ts BIGINT, dig_duration_seconds INTEGER, last_hp_regen_ts BIGINT,
            last_nick_change_ts BIGINT, registered_at BIGINT, total_earned_gold BIGINT DEFAULT 0,
            total_dna_earned BIGINT DEFAULT 0, nautilus_shells BIGINT DEFAULT 0,
            buff_damage_mult DOUBLE PRECISION, buff_expires_ts BIGINT, permanent_boost INTEGER DEFAULT 0,
            battle_message_id BIGINT, boss_cooldown_ts BIGINT DEFAULT 0,
            evolution_stage INTEGER DEFAULT 0, evolution_tree TEXT DEFAULT '[]',
            active_nectar TEXT, nectars_inv TEXT DEFAULT '{}', strength_charges INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS stones (
            user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            color TEXT NOT NULL, level INTEGER NOT NULL, count BIGINT DEFAULT 0,
            PRIMARY KEY(user_id,color,level))""",
        """CREATE TABLE IF NOT EXISTS mutations_v3 (
            user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            variant_key TEXT NOT NULL, is_active INTEGER, level INTEGER DEFAULT 1,
            equipped INTEGER DEFAULT 0, PRIMARY KEY(user_id,variant_key))""",
        """CREATE TABLE IF NOT EXISTS resources (
            user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            key TEXT NOT NULL, count BIGINT DEFAULT 0, PRIMARY KEY(user_id,key))""",
        """CREATE TABLE IF NOT EXISTS chests (
            user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            chest_type TEXT NOT NULL, count BIGINT DEFAULT 0, PRIMARY KEY(user_id,chest_type))""",
        """CREATE TABLE IF NOT EXISTS events (
            id BIGSERIAL PRIMARY KEY, name TEXT, description TEXT,
            started_at BIGINT, ends_at BIGINT, active INTEGER DEFAULT 1)""",
        """CREATE TABLE IF NOT EXISTS event_damage (
            event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            damage BIGINT DEFAULT 0, PRIMARY KEY(event_id,user_id))""",
        """CREATE TABLE IF NOT EXISTS promo_redemptions (
            user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            code TEXT NOT NULL, redeemed_at BIGINT, PRIMARY KEY(user_id,code))""",
        """CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY, rewards TEXT NOT NULL DEFAULT '{}',
            max_uses INTEGER DEFAULT 0, used_count INTEGER DEFAULT 0,
            expires_at BIGINT DEFAULT 0, active INTEGER DEFAULT 1,
            created_at BIGINT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_users_molts_level ON users(molts DESC, crab_level DESC)",
        "CREATE INDEX IF NOT EXISTS idx_events_active_id ON events(active, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_event_damage_event_damage ON event_damage(event_id, damage DESC)",
    ]
    with closing(get_conn()) as conn, conn:
        for statement in statements:
            conn.execute(statement)
        # Additive upgrades for databases created by earlier bot versions.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS evolution_stage INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS evolution_tree TEXT DEFAULT '[]'")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS active_nectar TEXT DEFAULT NULL")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS nectars_inv TEXT DEFAULT '{}'")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS strength_charges INTEGER DEFAULT 0")

# ---------------- USERS ----------------
def get_user(user_id):
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=%s", (user_id,)).fetchone()
        return dict(row) if row else None

def user_exists(user_id):
    return get_user(user_id) is not None

def create_user(user_id, username):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO users (user_id, username, nickname, registered_at, last_hp_regen_ts) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
            (user_id, username, username, int(time.time()), int(time.time())),
        )

def update_user(user_id, **fields):
    if not fields:
        return
    keys = ", ".join(f"{k}=%s" for k in fields)
    values = list(fields.values()) + [user_id]
    with closing(get_conn()) as conn, conn:
        conn.execute(f"UPDATE users SET {keys} WHERE user_id=%s", values)

def try_spend(user_id, field, amount):
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            f"UPDATE users SET {field} = {field} - %s WHERE user_id=%s AND {field} >= %s",
            (amount, user_id, amount),
        )
        return cur.rowcount > 0

def try_start_dig(user_id, duration_seconds):
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE users SET dig_start_ts=%s, dig_duration_seconds=%s WHERE user_id=%s AND dig_start_ts IS NULL",
            (int(time.time()), duration_seconds, user_id),
        )
        return cur.rowcount > 0

def try_collect_dig(user_id, expected_start_ts):
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE users SET dig_start_ts=NULL, dig_duration_seconds=NULL WHERE user_id=%s AND dig_start_ts=%s",
            (user_id, expected_start_ts),
        )
        return cur.rowcount > 0

def try_start_new_hunt(user_id, monster_json, cur_hp, now):
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE users SET in_hunt=1, monster_json=%s, cur_hp=%s, last_hp_regen_ts=%s "
            "WHERE user_id=%s AND in_hunt=0",
            (monster_json, cur_hp, now, user_id),
        )
        return cur.rowcount > 0

def try_apply_attack_result(user_id, expected_monster_json, **updates):
    set_clause = ", ".join(f"{k}=%s" for k in updates)
    values = list(updates.values()) + [user_id, expected_monster_json]
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            f"UPDATE users SET {set_clause} WHERE user_id=%s AND monster_json=%s AND in_hunt=1",
            values,
        )
        return cur.rowcount > 0

def get_top_players(limit=10):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT nickname, crab_level, molts, max_meters FROM users ORDER BY molts DESC, crab_level DESC LIMIT %s",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

def find_user_by_nickname(nickname):
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(nickname) = LOWER(%s) LIMIT 1", (nickname,)
        ).fetchone()
        return dict(row) if row else None

def get_random_players(exclude_user_id, limit=3):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE user_id != %s ORDER BY RANDOM() LIMIT %s",
            (exclude_user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

# ---------------- STONES И РЕСУРСЫ ----------------
def add_stone(user_id, color, level, amount=1):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO stones (user_id, color, level, count) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT(user_id, color, level) DO UPDATE SET count = count + %s",
            (user_id, color, level, amount, amount),
        )

def get_stones(user_id):
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT color, level, count FROM stones WHERE user_id=%s AND count > 0", (user_id,)).fetchall()
        return [dict(r) for r in rows]

def add_resource(user_id, key, amount=1):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO resources (user_id, key, count) VALUES (%s, %s, %s) "
            "ON CONFLICT(user_id, key) DO UPDATE SET count = count + %s",
            (user_id, key, amount, amount),
        )

def get_resources(user_id):
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT key, count FROM resources WHERE user_id=%s", (user_id,)).fetchall()
        return {r["key"]: r["count"] for r in rows}

def try_craft(user_id, recipe):
    with closing(get_conn()) as conn, conn:
        current = {r["key"]: r["count"] for r in conn.execute("SELECT key, count FROM resources WHERE user_id=%s", (user_id,)).fetchall()}
        for key, need in recipe.items():
            if current.get(key, 0) < need: return False
        for key, need in recipe.items():
            conn.execute("UPDATE resources SET count = count - %s WHERE user_id=%s AND key=%s", (need, user_id, key))
        return True

# ---------------- НОВЫЕ МУТАЦИИ V3 И ЭВОЛЮЦИЯ ----------------
def get_user_mutations_v3(user_id):
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT * FROM mutations_v3 WHERE user_id=%s", (user_id,)).fetchall()
        return [dict(r) for r in rows]

def get_mutations_v2(user_id):
    # Compatibility alias: the current game uses the v3 mutation inventory.
    return get_user_mutations_v3(user_id)


def add_new_mutation_v3(user_id, variant_key, is_active):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO mutations_v3 (user_id, variant_key, is_active, level, equipped) VALUES (%s, %s, %s, 1, 0) ON CONFLICT DO NOTHING",
            (user_id, variant_key, is_active)
        )

def equip_active_mutation(user_id, variant_key):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE mutations_v3 SET equipped=1 WHERE user_id=%s AND variant_key=%s", (user_id, variant_key))

def unequip_active_mutation(user_id, variant_key):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE mutations_v3 SET equipped=0 WHERE user_id=%s AND variant_key=%s", (user_id, variant_key))

def upgrade_mutation_v3(user_id, variant_key):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE mutations_v3 SET level = level + 1 WHERE user_id=%s AND variant_key=%s", (user_id, variant_key))

def update_evolution_tree(user_id, new_stage, node_key):
    """Обновляет этап эволюции и добавляет новый узел в древо"""
    with closing(get_conn()) as conn, conn:
        row = conn.execute("SELECT evolution_tree FROM users WHERE user_id=%s", (user_id,)).fetchone()
        tree = json.loads(row["evolution_tree"]) if row and row["evolution_tree"] else []
        tree.append(node_key)
        conn.execute(
            "UPDATE users SET evolution_stage=%s, evolution_tree=%s WHERE user_id=%s",
            (new_stage, json.dumps(tree), user_id)
        )

# ---------------- СУНДУКИ И ИВЕНТЫ ----------------
def add_chest(user_id, chest_type, amount=1):
    with closing(get_conn()) as conn, conn:
        conn.execute("INSERT INTO chests (user_id, chest_type, count) VALUES (%s, %s, %s) ON CONFLICT(user_id, chest_type) DO UPDATE SET count = count + %s", (user_id, chest_type, amount, amount))

def get_chests(user_id):
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT chest_type, count FROM chests WHERE user_id=%s AND count > 0", (user_id,)).fetchall()
        return {r["chest_type"]: r["count"] for r in rows}

def try_spend_chest(user_id, chest_type, amount=1):
    with closing(get_conn()) as conn, conn:
        cur = conn.execute("UPDATE chests SET count = count - %s WHERE user_id=%s AND chest_type=%s AND count >= %s", (amount, user_id, chest_type, amount))
        return cur.rowcount > 0

def create_event(name, description, duration_seconds):
    now = int(time.time())
    with closing(get_conn()) as conn, conn:
        cur = conn.execute("INSERT INTO events (name, description, started_at, ends_at, active) VALUES (%s, %s, %s, %s, 1) RETURNING id", (name, description, now, now + duration_seconds))
        return cur.fetchone()["id"]

def get_active_event():
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM events WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

def add_event_damage(event_id, user_id, damage):
    with closing(get_conn()) as conn, conn:
        conn.execute("INSERT INTO event_damage (event_id, user_id, damage) VALUES (%s, %s, %s) ON CONFLICT(event_id, user_id) DO UPDATE SET damage = damage + %s", (event_id, user_id, damage, damage))

def get_event_leaderboard(event_id, limit=50):
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT user_id, damage FROM event_damage WHERE event_id=%s ORDER BY damage DESC LIMIT %s", (event_id, limit)).fetchall()
        return [dict(r) for r in rows]

def get_all_event_participants(event_id):
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT user_id, damage FROM event_damage WHERE event_id=%s ORDER BY damage DESC", (event_id,)).fetchall()
        return [dict(r) for r in rows]

def close_event(event_id):
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE events SET active=0 WHERE id=%s", (event_id,))

# ---------------- БУФЕРИЗАЦИЯ ----------------
async def add_to_buffer(redis, user_id: int, gold: int = 0, kills: int = 0, cur_meters: int = 0, max_meters: int = 0, cur_hp: float = None, last_hp_regen_ts: int = None):
    # Redis is optional. Without it, persist immediately so a free host restart
    # cannot discard rewards or player progress.
    if redis is None:
        def _save():
            with closing(get_conn()) as conn, conn:
                query = """UPDATE users SET gold=gold+%s, total_earned_gold=total_earned_gold+%s,
                    kills=kills+%s,
                    cur_meters=CASE WHEN %s>0 THEN %s ELSE cur_meters END,
                    max_meters=GREATEST(max_meters,%s)"""
                params = [gold, gold, kills, cur_meters, cur_meters, max_meters]
                if cur_hp is not None:
                    query += ", cur_hp=%s"
                    params.append(float(cur_hp))
                if last_hp_regen_ts is not None:
                    query += ", last_hp_regen_ts=%s"
                    params.append(int(last_hp_regen_ts))
                query += " WHERE user_id=%s"
                params.append(user_id)
                conn.execute(query, params)
        await run_async(_save)
        return
    key = f"user_buffer:{user_id}"
    await redis.hincrby(key, "gold", gold)
    await redis.hincrby(key, "kills", kills)
    if cur_meters > 0: await redis.hset(key, "cur_meters", cur_meters)
    current_max = await redis.hget(key, "max_meters")
    current_max = int(current_max) if current_max else 0
    if max_meters > current_max: await redis.hset(key, "max_meters", max_meters)
    if cur_hp is not None: await redis.hset(key, "cur_hp", str(cur_hp))
    if last_hp_regen_ts is not None: await redis.hset(key, "last_hp_regen_ts", last_hp_regen_ts)
    await redis.hset(key, "last_action_ts", int(time.time()))

async def flush_user_buffer(redis, user_id: int):
    if redis is None:
        return False
    key = f"user_buffer:{user_id}"
    buffer_data = await redis.hgetall(key)
    if not buffer_data: return False
    gold = int(buffer_data.get(b"gold", 0))
    kills = int(buffer_data.get(b"kills", 0))
    cur_meters = int(buffer_data.get(b"cur_meters", 0))
    max_meters = int(buffer_data.get(b"max_meters", 0))
    raw_hp = buffer_data.get(b"cur_hp")
    raw_regen_ts = buffer_data.get(b"last_hp_regen_ts")

    def _atomic_db_update():
        with closing(get_conn()) as conn, conn:
            query = """
                UPDATE users SET gold = gold + %s, total_earned_gold = total_earned_gold + %s, kills = kills + %s,
                cur_meters = CASE WHEN %s > 0 THEN %s ELSE cur_meters END, max_meters = GREATEST(max_meters, %s)
            """
            params = [gold, gold, kills, cur_meters, cur_meters, max_meters]
            if raw_hp is not None:
                query += ", cur_hp = %s"
                params.append(float(raw_hp))
            if raw_regen_ts is not None:
                query += ", last_hp_regen_ts = %s"
                params.append(int(raw_regen_ts))
            query += " WHERE user_id = %s"
            params.append(user_id)
            conn.execute(query, params)

    await run_async(_atomic_db_update)
    await redis.delete(key)
    return True

# ---------------- NECTARS ----------------
async def auto_update_db_nectars():
    # Columns are created by init_db() so schema setup remains synchronous/atomic.
    return None

async def get_nectars_data(user_id):
    def _read():
        with closing(get_conn()) as conn:
            row = conn.execute(
                "SELECT active_nectar, nectars_inv, strength_charges FROM users WHERE user_id=%s",
                (user_id,),
            ).fetchone()
            if row:
                return {
                    "active_nectar": row["active_nectar"],
                    "nectars_inv": json.loads(row["nectars_inv"] or "{}"),
                    "strength_charges": row["strength_charges"],
                }
            return {"active_nectar": None, "nectars_inv": {}, "strength_charges": 0}
    return await run_async(_read)

async def update_nectars_data(user_id, active_nectar=False, nectars_inv=None, strength_charges=None):
    updates, params = [], []
    if active_nectar is not False:
        updates.append("active_nectar = %s")
        params.append(active_nectar)
    if nectars_inv is not None:
        updates.append("nectars_inv = %s")
        params.append(json.dumps(nectars_inv))
    if strength_charges is not None:
        updates.append("strength_charges = %s")
        params.append(strength_charges)
    if updates:
        params.append(user_id)
        def _write():
            with closing(get_conn()) as conn, conn:
                conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = %s", params)
        await run_async(_write)

def try_redeem_promo(user_id, code, gold=0, dna=0, shells=0, boost=False):
    """Atomically redeem a promo once and grant its rewards."""
    with closing(get_conn()) as conn, conn:
        inserted = conn.execute(
            "INSERT INTO promo_redemptions(user_id,code,redeemed_at) VALUES(%s,%s,%s) "
            "ON CONFLICT(user_id,code) DO NOTHING RETURNING user_id",
            (user_id, code, int(time.time())),
        ).fetchone()
        if not inserted:
            return False
        cur = conn.execute(
            "UPDATE users SET gold=gold+%s, dna_points=dna_points+%s, "
            "nautilus_shells=nautilus_shells+%s, "
            "permanent_boost=CASE WHEN %s THEN 1 ELSE permanent_boost END WHERE user_id=%s",
            (gold, dna, shells, bool(boost), user_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"Cannot redeem promo for missing user {user_id}")
        return True

def try_spend_stone(user_id, color, level, count):
    with closing(get_conn()) as conn, conn:
        cursor = conn.execute(
            "UPDATE stones SET count = count - %s WHERE user_id = %s AND color = %s AND level = %s AND count >= %s",
            (count, user_id, color, level, count),
        )
        return cursor.rowcount > 0

def try_spend_resource(user_id, resource_key, count):
    with closing(get_conn()) as conn, conn:
        cursor = conn.execute(
            "UPDATE resources SET count = count - %s WHERE user_id = %s AND key = %s AND count >= %s",
            (count, user_id, resource_key, count),
        )
        return cursor.rowcount > 0

# ---------------- ADMIN PANEL: GRANTS AND MANAGED PROMOS ----------------
def admin_grant_currency(user_id, field, amount):
    allowed = {"gold", "dna_points", "nautilus_shells"}
    if field not in allowed or int(amount) == 0:
        raise ValueError("Invalid currency or amount")
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(f"UPDATE users SET {field} = GREATEST(0, {field} + %s) WHERE user_id=%s", (int(amount), user_id))
        return cur.rowcount > 0

def admin_grant_item(user_id, item_type, item_key, amount=1):
    amount = int(amount)
    if amount < 1 or amount > 100000:
        raise ValueError("Invalid item amount")
    if item_type == "chest":
        add_chest(user_id, str(item_key), amount)
    elif item_type == "resource":
        add_resource(user_id, str(item_key), amount)
    elif item_type == "stone":
        color, level = str(item_key).split(":", 1)
        add_stone(user_id, color, int(level), amount)
    elif item_type == "mutation":
        for _ in range(amount):
            add_new_mutation_v3(user_id, str(item_key), 0)
    elif item_type == "nectar":
        def _grant():
            with closing(get_conn()) as conn, conn:
                row = conn.execute("SELECT nectars_inv FROM users WHERE user_id=%s FOR UPDATE", (user_id,)).fetchone()
                if not row: return False
                inv = json.loads(row["nectars_inv"] or "{}")
                inv[str(item_key)] = int(inv.get(str(item_key), 0)) + amount
                conn.execute("UPDATE users SET nectars_inv=%s WHERE user_id=%s", (json.dumps(inv), user_id))
                return True
        return _grant()
    else:
        raise ValueError("Unsupported item type")
    return True

def admin_create_promo(code, rewards, max_uses=0, expires_at=0):
    code = str(code).strip().lower()
    if not code or len(code) > 32 or not code.replace("_","").replace("-","").isalnum():
        raise ValueError("Promo code must be 1-32 letters, digits, _ or -")
    with closing(get_conn()) as conn, conn:
        conn.execute("INSERT INTO promo_codes(code,rewards,max_uses,expires_at,active,created_at) VALUES(%s,%s,%s,%s,1,%s)",
                     (code, json.dumps(rewards, ensure_ascii=False), int(max_uses), int(expires_at), int(time.time())))
    return code

def admin_list_promos():
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT code,rewards,max_uses,used_count,expires_at,active,created_at FROM promo_codes ORDER BY created_at DESC LIMIT 100").fetchall()
        result=[]
        for row in rows:
            item=dict(row); item["rewards"]=json.loads(item["rewards"] or "{}"); result.append(item)
        return result

def admin_disable_promo(code):
    with closing(get_conn()) as conn, conn:
        cur=conn.execute("UPDATE promo_codes SET active=0 WHERE code=%s AND active=1",(str(code).lower(),))
        return cur.rowcount>0

def redeem_admin_promo(user_id, code):
    code=str(code).strip().lower()
    with closing(get_conn()) as conn, conn.transaction():
        promo=conn.execute("SELECT * FROM promo_codes WHERE code=%s FOR UPDATE",(code,)).fetchone()
        if not promo or not promo["active"]: return (False,"Promo code is inactive or unknown")
        now=int(time.time())
        if promo["expires_at"] and now>int(promo["expires_at"]): return (False,"Promo code has expired")
        if promo["max_uses"] and int(promo["used_count"])>=int(promo["max_uses"]): return (False,"Promo code use limit reached")
        inserted=conn.execute("INSERT INTO promo_redemptions(user_id,code,redeemed_at) VALUES(%s,%s,%s) ON CONFLICT(user_id,code) DO NOTHING RETURNING user_id",(user_id,code,now)).fetchone()
        if not inserted: return (False,"Promo already redeemed")
        rewards=json.loads(promo["rewards"] or "{}")
        gold=int(rewards.get("gold",0)); dna=int(rewards.get("dna",0)); shells=int(rewards.get("shells",0))
        conn.execute("UPDATE users SET gold=gold+%s,dna_points=dna_points+%s,nautilus_shells=nautilus_shells+%s WHERE user_id=%s",(gold,dna,shells,user_id))
        kind=rewards.get("item_type"); key=str(rewards.get("item_key","")); count=int(rewards.get("item_count",0))
        if kind and count>0:
            if kind=="chest":
                conn.execute("INSERT INTO chests(user_id,chest_type,count) VALUES(%s,%s,%s) ON CONFLICT(user_id,chest_type) DO UPDATE SET count=chests.count+EXCLUDED.count",(user_id,key,count))
            elif kind=="resource":
                conn.execute("INSERT INTO resources(user_id,key,count) VALUES(%s,%s,%s) ON CONFLICT(user_id,key) DO UPDATE SET count=resources.count+EXCLUDED.count",(user_id,key,count))
            elif kind=="stone":
                color,level=key.split(":",1)
                conn.execute("INSERT INTO stones(user_id,color,level,count) VALUES(%s,%s,%s,%s) ON CONFLICT(user_id,color,level) DO UPDATE SET count=stones.count+EXCLUDED.count",(user_id,color,int(level),count))
            elif kind=="mutation":
                conn.execute("INSERT INTO mutations_v3(user_id,variant_key,is_active,level,equipped) VALUES(%s,%s,0,1,0) ON CONFLICT(user_id,variant_key) DO NOTHING",(user_id,key))
            elif kind=="nectar":
                row=conn.execute("SELECT nectars_inv FROM users WHERE user_id=%s FOR UPDATE",(user_id,)).fetchone()
                inv=json.loads(row["nectars_inv"] or "{}"); inv[key]=int(inv.get(key,0))+count
                conn.execute("UPDATE users SET nectars_inv=%s WHERE user_id=%s",(json.dumps(inv),user_id))
        conn.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE code=%s",(code,))
        return (True,rewards)
