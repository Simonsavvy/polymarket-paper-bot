"""
Polymarket PAPER market-maker. No wallet, no keys, no real orders. Uses only public data.

Each cycle it:
  1. picks markets that pay liquidity rewards and look safe to quote,
  2. places VIRTUAL two-sided quotes around the midpoint,
  3. checks the public trade feed to see which virtual quotes would have been filled,
  4. estimates the liquidity rewards it would have earned,
  5. saves everything to paper.db so report.py can show profit/loss.

Two virtual accounts run side by side (default $10 and $1000) so we can see what
account size is needed for this to make sense.
"""
import json, sqlite3, time, sys, os, urllib.request, urllib.parse, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("POLY_DB", os.path.join(HERE, "paper.db"))

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

ACCOUNTS = {"tiny_$10": 10.0, "small_$1000": 1000.0}
CYCLE_SECONDS = 60
MAX_MARKETS = 8            # markets quoted per account
QUOTE_SPREAD_FRAC = 0.5    # quote at this fraction of the rewards max spread from mid
MIN_MID, MAX_MID = 0.15, 0.85
MIN_DAYS_TO_END = 3.0      # avoid markets about to resolve (gap risk)
MIN_RATE = 1.0             # min reward $/day for a market to be considered
MAX_INV_FRAC = 0.5         # max fraction of account cost basis tied up in one market


def get(url, params=None, retries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            time.sleep(1 + i)
    raise last


def db():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS pos(acct TEXT, cond TEXT, question TEXT, yes_tok TEXT, no_tok TEXT,
        yes_sh REAL, yes_cost REAL, no_sh REAL, no_cost REAL, realized REAL, rewards REAL, last_mid REAL,
        PRIMARY KEY(acct, cond));
    CREATE TABLE IF NOT EXISTS fills(ts INTEGER, acct TEXT, cond TEXT, question TEXT, side TEXT, price REAL, size REAL);
    CREATE TABLE IF NOT EXISTS equity(ts INTEGER, acct TEXT, cash REAL, inv_value REAL, rewards REAL, realized REAL, equity REAL);
    CREATE TABLE IF NOT EXISTS log(ts INTEGER, msg TEXT);
    """)
    return c


def log(c, msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)
    c.execute("INSERT INTO log VALUES(?,?)", (int(time.time()), msg))
    c.commit()


def kv_get(c, k, default=None):
    r = c.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_set(c, k, v):
    c.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (k, json.dumps(v)))
    c.commit()


def parse_end(s):
    try:
        return time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except Exception:
        return None


def candidates():
    """Markets with active liquidity rewards that look safe to quote."""
    rewards = {}
    cur = ""
    for _ in range(40):
        d = get(CLOB + "/rewards/markets/current", {"next_cursor": cur} if cur else None)
        for m in d.get("data", []):
            rate = m.get("total_daily_rate") or 0
            if rate >= MIN_RATE:
                rewards[m["condition_id"]] = m
        cur = d.get("next_cursor")
        if not cur or cur == "LTE=":
            break
    out = []
    # Gamma: pull the most liquid active markets and join to reward info
    for off in range(0, 1000, 100):
        try:
            ms = get(GAMMA + "/markets", {"active": "true", "closed": "false", "limit": 100, "offset": off,
                                          "order": "liquidity", "ascending": "false"})
        except Exception:
            break
        if not ms:
            break
        for m in ms:
            cid = m.get("conditionId")
            if cid not in rewards or not m.get("enableOrderBook"):
                continue
            end = parse_end(m.get("endDate") or "")
            if end is None or (end - time.time()) / 86400 < MIN_DAYS_TO_END:
                continue
            if m.get("gameStartTime"):  # sports games: fast moving, skip
                continue
            try:
                toks = json.loads(m["clobTokenIds"])
                prices = json.loads(m["outcomePrices"])
            except Exception:
                continue
            mid = float(prices[0])
            if not (MIN_MID <= mid <= MAX_MID):
                continue
            r = rewards[cid]
            out.append(dict(cond=cid, q=m["question"], yes=toks[0], no=toks[1], rate=r["total_daily_rate"],
                            max_spread=r["rewards_max_spread"] / 100.0, min_size=r["rewards_min_size"],
                            tick=float(m.get("orderPriceMinTickSize") or 0.01), liq=float(m.get("liquidityNum") or 0)))
    return out


def book(tok):
    b = get(CLOB + "/book", {"token_id": tok})
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b["bids"]), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in b["asks"])
    return bids, asks


def qscore(v, s, size):
    return ((v - s) / v) ** 2 * size if s < v else 0.0


def others_q(bids, asks, mid, v, min_size):
    qb = sum(qscore(v, mid - p, z) for p, z in bids if z >= min_size and mid - p < v)
    qa = sum(qscore(v, p - mid, z) for p, z in asks if z >= min_size and p - mid < v)
    return qb, qa


def combine(qb, qa, mid):
    """Polymarket scoring: two-sided counts fully; one-sided only allowed (at 1/3) if mid in 0.10-0.90."""
    two = min(qb, qa)
    if 0.10 <= mid <= 0.90:
        return max(two, max(qb, qa) / 3.0)
    return two


def to_yes_trade(t, yes_tok):
    """Convert a trade on either token into (yes_price, taker_side)."""
    price, side = float(t["price"]), t["side"]
    if t["asset"] == yes_tok:
        return price, side
    return 1.0 - price, ("SELL" if side == "BUY" else "BUY")


def pick(cands, cash_total):
    """Choose markets we can afford to quote: both sides need min_size shares."""
    scored = []
    for m in cands:
        cost = m["min_size"] * 0.5 * 2  # rough, two sides at ~mid
        if cost > cash_total * MAX_INV_FRAC * 1.0:
            continue
        # crude attractiveness: reward per $ of competing liquidity
        scored.append((m["rate"] / (m["liq"] + 1000.0), m))
    scored.sort(key=lambda x: -x[0])
    return [m for _, m in scored[:MAX_MARKETS]]


def run_cycle(c, cache):
    now = int(time.time())
    if not cache.get("cands"):
        saved = kv_get(c, "cands")
        if saved:
            cache["cands"], cache["cand_ts"] = saved["cands"], saved["ts"]
    if now - cache.get("cand_ts", 0) > 900 or not cache.get("cands"):
        cache["cands"] = candidates()
        cache["cand_ts"] = now
        kv_set(c, "cands", {"cands": cache["cands"], "ts": now})
        log(c, f"refreshed candidate list: {len(cache['cands'])} reward markets pass filters")
    cands = cache["cands"]
    last_ts = kv_get(c, "last_trade_ts", now - 1)
    dt = min(900, max(1, now - kv_get(c, "last_cycle_ts", now - CYCLE_SECONDS)))

    for acct, capital in ACCOUNTS.items():
        rows = {r[0]: r for r in c.execute(
            "SELECT cond,question,yes_tok,no_tok,yes_sh,yes_cost,no_sh,no_cost,realized,rewards,last_mid FROM pos WHERE acct=?",
            (acct,))}
        held = set(cond for cond, r in rows.items() if r[4] > 0 or r[6] > 0)
        chosen = pick(cands, capital)
        conds = {m["cond"]: m for m in chosen}
        # keep managing markets where we hold inventory
        for cond in held:
            if cond not in conds:
                for m in cands:
                    if m["cond"] == cond:
                        conds[cond] = m
        cost_basis = sum(r[5] + r[7] for r in rows.values())
        realized_total = sum(r[8] for r in rows.values())
        rewards_total = sum(r[9] for r in rows.values())
        cash = capital + realized_total + rewards_total - cost_basis

        for cond, m in conds.items():
            r = rows.get(cond)
            if r is None:
                c.execute("INSERT INTO pos VALUES(?,?,?,?,?,0,0,0,0,0,0,NULL)", (acct, cond, m["q"], m["yes"], m["no"]))
                r = (cond, m["q"], m["yes"], m["no"], 0, 0, 0, 0, 0, 0, None)
            (_, q, ytok, ntok, ysh, ycost, nsh, ncost, real, rew, _) = r
            try:
                bids, asks = book(ytok)
            except Exception:
                continue
            if not bids or not asks:
                continue
            mid = (bids[0][0] + asks[0][0]) / 2.0
            if not (0.05 < mid < 0.95):
                continue
            v = m["max_spread"]
            tick = m["tick"]
            d = max(tick, round(v * QUOTE_SPREAD_FRAC / tick) * tick)
            size = float(m["min_size"])
            bid_p = round((mid - d) / tick) * tick
            ask_p = round((mid + d) / tick) * tick
            bid_p, ask_p = max(bid_p, tick), min(ask_p, 1 - tick)
            no_bid_p = 1 - ask_p  # our "ask" on YES == buying NO at this price

            inv_cap = capital * MAX_INV_FRAC
            can_bid = (ycost + ncost) < inv_cap and cash >= size * bid_p
            can_ask = (ycost + ncost) < inv_cap and cash >= size * no_bid_p
            # skew: if heavily long one side, stop adding to it
            if ysh - nsh >= size * 2: can_bid = False
            if nsh - ysh >= size * 2: can_ask = False

            # --- simulate fills from public trades since last cycle
            try:
                tr = get(DATA + "/trades", {"market": cond, "limit": 100})
            except Exception:
                tr = []
            new_tr = [t for t in tr if int(t["timestamp"]) > last_ts]
            new_tr.sort(key=lambda t: t["timestamp"])
            bid_open, ask_open = can_bid, can_ask
            for t in new_tr:
                yp, side = to_yes_trade(t, ytok)
                # conservative: trade must go THROUGH our price (strictly), i.e. we'd be at the front
                if side == "SELL" and bid_open and yp < bid_p:
                    ysh += size; ycost += size * bid_p; cash -= size * bid_p; bid_open = False
                    c.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?)", (now, acct, cond, q, "BUY YES", bid_p, size))
                if side == "BUY" and ask_open and yp > ask_p:
                    nsh += size; ncost += size * no_bid_p; cash -= size * no_bid_p; ask_open = False
                    c.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?)", (now, acct, cond, q, "BUY NO", no_bid_p, size))

            # --- merge pairs (1 YES + 1 NO redeems for $1): realized profit
            pairs = min(ysh, nsh)
            if pairs > 0:
                avg_y = ycost / ysh; avg_n = ncost / nsh
                real += pairs * (1.0 - avg_y - avg_n)
                ycost -= pairs * avg_y; ncost -= pairs * avg_n
                ysh -= pairs; nsh -= pairs
                cash += pairs * (avg_y + avg_n) + pairs * (1.0 - avg_y - avg_n)

            # --- estimate rewards for this cycle (time-fraction of the daily pool)
            ourb = qscore(v, mid - bid_p, size) if can_bid else 0.0
            oura = qscore(v, ask_p - mid, size) if can_ask else 0.0
            ob, oa = others_q(bids, asks, mid, v, m["min_size"])
            our_q = combine(ourb, oura, mid)
            tot_q = combine(ob + ourb, oa + oura, mid)
            share = our_q / tot_q if tot_q > 0 else 0.0
            earned = m["rate"] * share * (dt / 86400.0)
            rew += earned

            c.execute("""UPDATE pos SET yes_sh=?,yes_cost=?,no_sh=?,no_cost=?,realized=?,rewards=?,last_mid=?
                         WHERE acct=? AND cond=?""", (ysh, ycost, nsh, ncost, real, rew, mid, acct, cond))

        # --- mark to market and record equity
        tot = c.execute("SELECT yes_sh,no_sh,yes_cost,no_cost,realized,rewards,last_mid FROM pos WHERE acct=?", (acct,)).fetchall()
        inv_val = sum((r[0] * (r[6] or 0) + r[1] * (1 - (r[6] or 0))) for r in tot)
        cost = sum(r[2] + r[3] for r in tot)
        realized = sum(r[4] for r in tot)
        rewards = sum(r[5] for r in tot)
        cash = capital + realized + rewards - cost
        equity = cash + inv_val
        c.execute("INSERT INTO equity VALUES(?,?,?,?,?,?,?)", (now, acct, cash, inv_val, rewards, realized, equity))
        c.commit()

    kv_set(c, "last_trade_ts", now)
    kv_set(c, "last_cycle_ts", now)


def main():
    c = db()
    log(c, f"paper bot started. accounts={ACCOUNTS}. NO REAL MONEY, NO WALLET.")
    cache = {}
    once = "--once" in sys.argv
    while True:
        t0 = time.time()
        try:
            run_cycle(c, cache)
        except Exception:
            log(c, "cycle error: " + traceback.format_exc().splitlines()[-1])
        if once:
            if os.environ.get("POLY_EXPORT"):
                import dashboard
                with open(os.environ["POLY_EXPORT"], "w") as f:
                    json.dump(dashboard.data(), f)
            break
        time.sleep(max(1, CYCLE_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    main()
