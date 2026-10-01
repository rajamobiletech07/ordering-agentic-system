"""Repeatable, idempotent dummy-data seeding.

    python -m scripts.seed            # seed / refresh
    python -m scripts.seed --verify   # print row counts

Guarantees
----------
* Deterministic: ``random.Random(42)`` and uuid5-derived IDs - same input, same rows.
* Safe to re-run: every insert is ``ON CONFLICT (<pk>) DO UPDATE``, so reruns refresh rows in
  place and never create duplicates. Dates are anchored to "today" so data stays current.
* Valid relationships: composite tenant FKs, one open recommendation per product/location,
  at most one successful payment per order, line totals = qty * cost.

Named fixtures (stable IDs, used by the demo, example conversations and E2E tests) are
defined in :data:`FIXTURES`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text

from ordering.common.db import admin_session, dispose_engines
from ordering.common.ids import stable_uuid

RNG_SEED = 42
TODAY = date.today()
NOW = datetime.now(UTC)

TENANTS = [
    {"tenant_id": "acme-retail", "name": "Acme Retail Group", "currency": "USD",
     "approval_threshold": Decimal("10000.00"), "payment_limit": Decimal("50000.00")},
    {"tenant_id": "globex-grocers", "name": "Globex Grocers", "currency": "USD",
     "approval_threshold": Decimal("5000.00"), "payment_limit": Decimal("25000.00")},
]

USERS = [
    ("acme-retail", "u-acme-buyer", "priya.buyer@acme.example", "Priya Raman", ["buyer"]),
    ("acme-retail", "u-acme-approver", "marco.approver@acme.example", "Marco Silva", ["buyer", "approver"]),
    ("acme-retail", "u-acme-viewer", "lee.viewer@acme.example", "Lee Chen", ["viewer"]),
    ("acme-retail", "u-acme-admin", "ops.admin@acme.example", "Acme Ops Admin", ["admin"]),
    ("globex-grocers", "u-globex-buyer", "sam.buyer@globex.example", "Sam Okafor", ["buyer"]),
    ("globex-grocers", "u-globex-admin", "ops.admin@globex.example", "Globex Ops Admin", ["admin"]),
]

# vendor → category it supplies (products are only sourced from matching vendors)
VENDOR_CATEGORY = {
    "Northwind Dairy Co": "Dairy", "Blue Ridge Produce": "Produce", "Hill Country Dairy": "Dairy",
    "Summit Beverages": "Beverages", "Golden Grain Bakery": "Bakery", "Coastal Seafood Supply": "Meat",
    "Evergreen Household": "Household", "Prairie Meats": "Meat", "Sunrise Snacks": "Snacks",
    "Riverbend Pantry": "Pantry", "Alpine Frozen Foods": "Frozen", "Crescent Personal Care": "Personal Care",
    "Harbor Coffee Roasters": "Beverages", "Mesa Organics": "Produce",
    "Lakeside Dairy": "Dairy", "Orchard Valley Farms": "Produce", "Metro Beverage Dist": "Beverages",
    "Stonemill Bakery": "Bakery", "Pacific Catch": "Meat", "CleanHome Supply": "Household", "Heartland Meats": "Meat",
    "Crunchtime Snacks": "Snacks", "Staples & Co Pantry": "Pantry", "Polar Frozen": "Frozen",
    "Bloom Personal Care": "Personal Care",
}
# fixture vendors get fixed terms so recommendation reasons and vendor rules agree
FIXTURE_VENDOR_TERMS = {"VND-003": (3, 12), "VND-013": (7, 16), "VND-004": (5, 12), "GVN-001": (3, 12)}
PERISHABLE = {"Dairy", "Produce", "Bakery", "Meat"}

VENDOR_NAMES = {
    "acme-retail": [
        "Northwind Dairy Co", "Blue Ridge Produce", "Hill Country Dairy", "Summit Beverages",
        "Golden Grain Bakery", "Coastal Seafood Supply", "Evergreen Household", "Prairie Meats",
        "Sunrise Snacks", "Riverbend Pantry", "Alpine Frozen Foods", "Crescent Personal Care",
        "Harbor Coffee Roasters", "Mesa Organics",
    ],
    "globex-grocers": [
        "Lakeside Dairy", "Orchard Valley Farms", "Metro Beverage Dist", "Stonemill Bakery",
        "Pacific Catch", "CleanHome Supply", "Heartland Meats", "Crunchtime Snacks",
        "Staples & Co Pantry", "Polar Frozen", "Bloom Personal Care",
    ],
}

CATEGORIES = {
    "Dairy": ["Whole Milk 1 gal", "2% Milk 1 gal", "Greek Yogurt 32oz", "Cheddar Block 16oz", "Butter 1lb",
              "Eggs Large Dozen", "Cottage Cheese 24oz", "Cream Cheese 8oz", "Half & Half 1qt", "Mozzarella 16oz"],
    "Produce": ["Bananas 3lb", "Gala Apples 5lb", "Baby Spinach 10oz", "Avocados 4ct", "Strawberries 1lb",
                "Russet Potatoes 5lb", "Yellow Onions 3lb", "Roma Tomatoes 2lb", "Carrots 2lb", "Blueberries 1pt"],
    "Bakery": ["Sourdough Loaf", "Whole Wheat Bread", "Bagels 6ct", "Croissants 4ct", "Tortillas 10ct",
               "Hamburger Buns 8ct", "Muffins 4ct", "Dinner Rolls 12ct"],
    "Beverages": ["Sparkling Water 12pk", "Orange Juice 52oz", "Cold Brew 32oz", "Cola 12pk", "Green Tea 6pk",
                  "Sports Drink 8pk", "Premium Coffee Beans 5lb", "Almond Milk 64oz", "Kombucha 16oz"],
    "Frozen": ["Frozen Pizza", "Mixed Vegetables 2lb", "Ice Cream 1.5qt", "Chicken Nuggets 2lb",
               "Frozen Berries 3lb", "Waffles 10ct", "Veggie Burgers 4ct"],
    "Household": ["Paper Towels 6pk", "Laundry Detergent 100oz", "Dish Soap 24oz", "Trash Bags 45ct",
                  "Toilet Paper 12pk", "All-Purpose Cleaner", "Sponges 6pk"],
    "Personal Care": ["Shampoo 12oz", "Toothpaste 2pk", "Body Wash 18oz", "Deodorant", "Hand Soap 3pk",
                      "Sunscreen SPF50"],
    "Snacks": ["Tortilla Chips 13oz", "Trail Mix 26oz", "Pretzels 16oz", "Granola Bars 12ct",
               "Popcorn 6pk", "Dark Chocolate Bar", "Rice Cakes"],
    "Pantry": ["Spaghetti 1lb", "Marinara Sauce 24oz", "Jasmine Rice 5lb", "Black Beans 15oz", "Olive Oil 1L",
               "Peanut Butter 16oz", "Oat Cereal 18oz", "Chicken Broth 32oz", "Canned Tuna 4pk"],
    "Meat": ["Chicken Breast 2lb", "Ground Beef 1lb", "Pork Chops 1.5lb", "Salmon Fillet 1lb", "Bacon 12oz",
             "Turkey Slices 9oz"],
}
BASE_COST = {"Dairy": 3.2, "Produce": 2.6, "Bakery": 3.0, "Beverages": 5.5, "Frozen": 5.0, "Household": 8.0,
             "Personal Care": 6.0, "Snacks": 3.5, "Pantry": 3.0, "Meat": 7.5}
CITIES = {
    "acme-retail": [("Austin", "South"), ("Dallas", "South"), ("Houston", "South"), ("Denver", "West"),
                    ("Phoenix", "West"), ("Chicago", "Midwest"), ("Columbus", "Midwest"), ("Atlanta", "Southeast")],
    "globex-grocers": [("Seattle", "Northwest"), ("Portland", "Northwest"), ("Boise", "Mountain"),
                       ("Sacramento", "West"), ("Reno", "West")],
}
LAYOUT = {"acme-retail": {"products": 110, "stores": 30, "dcs": 6, "recs": 560, "orders": 560},
          "globex-grocers": {"products": 90, "stores": 20, "dcs": 4, "recs": 440, "orders": 440}}

ORDER_STATUS_MIX = [("fulfilled", 30), ("submitted", 25), ("approved", 10), ("pending_approval", 10),
                    ("draft", 5), ("cancelled", 12), ("payment_failed", 8)]
REC_STATUS_MIX = [("open", 34), ("accepted", 26), ("dismissed", 14), ("expired", 26)]  # 'ordered' set by orders


@dataclass(frozen=True)
class Fixture:
    tenant: str
    sku: str
    product_name: str
    category: str
    location_code: str
    vendor_code: str
    unit_cost: str
    on_hand: int
    safety_stock: int
    daily_demand: str
    recommended_qty: int
    risk: str
    reason: str


# Demo / test fixtures - referenced by examples/conversations.md and tests/e2e.
FIXTURES = [
    Fixture("acme-retail", "SKU-10042", "Organic Whole Milk 1 gal", "Dairy", "STORE-014", "VND-003", "3.85",
            on_hand=18, safety_stock=60, daily_demand="22.00", recommended_qty=360, risk="critical",
            reason="On-hand 18 units covers <1 day of demand (22/day) against a 3-day vendor lead time; "
                   "14-day forecast of 308 units plus safety stock 60 requires 360 units (30 cases of 12)."),
    Fixture("acme-retail", "SKU-10077", "Premium Coffee Beans 5lb", "Beverages", "DC-02", "VND-013", "24.50",
            on_hand=140, safety_stock=400, daily_demand="95.00", recommended_qty=1600, risk="high",
            reason="Distribution center stock of 140 covers 1.5 days; holiday promo lifts forecast to 1,330 "
                   "units over 14 days. Order 1,600 (100 cases of 16) to restore safety stock of 400."),
    Fixture("acme-retail", "SKU-10108", "Sparkling Water 12pk", "Beverages", "STORE-003", "VND-004", "4.10",
            on_hand=35, safety_stock=48, daily_demand="12.00", recommended_qty=144, risk="high",
            reason="Heat-wave demand uplift (+40%) pushes 14-day forecast to 168; on-hand 35 is below "
                   "safety stock 48. Recommend 144 units (12 cases)."),
    Fixture("globex-grocers", "GLX-20015", "Greek Yogurt 32oz", "Dairy", "STORE-G07", "GVN-001", "4.60",
            on_hand=10, safety_stock=36, daily_demand="9.00", recommended_qty=144, risk="critical",
            reason="On-hand 10 covers ~1 day; 14-day forecast 126 plus safety stock 36 requires 144 units."),
]


def money(x: float | Decimal | str) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def weighted(rng: random.Random, mix: list[tuple[str, int]]) -> str:
    return rng.choices([m[0] for m in mix], weights=[m[1] for m in mix], k=1)[0]


class Builder:
    def __init__(self) -> None:
        self.rng = random.Random(RNG_SEED)
        self.rows: dict[str, list[dict]] = {k: [] for k in [
            "tenants", "users", "vendors", "products", "locations", "inventory_positions", "recommendations",
            "orders", "order_lines", "payments", "notifications"]}
        self.order_seq = 0

    # ------------------------------------------------------------------ dimensions
    def build(self) -> dict[str, list[dict]]:
        self.rows["tenants"] = TENANTS
        for t, uid, email, name, roles in USERS:
            self.rows["users"].append({"tenant_id": t, "user_id": uid, "email": email, "display_name": name,
                                       "roles": roles})
        for tenant in TENANTS:
            self._tenant(tenant["tenant_id"])
        return self.rows

    def _tenant(self, t: str) -> None:
        rng, layout = self.rng, LAYOUT[t]
        prefix_v = "VND" if t == "acme-retail" else "GVN"
        vendors = []
        for i, name in enumerate(VENDOR_NAMES[t], start=1):
            code = f"{prefix_v}-{i:03d}"
            category = VENDOR_CATEGORY[name]
            lead = rng.choice([2, 3, 3]) if category in PERISHABLE else rng.choice([4, 5, 7, 7, 10])
            moq = rng.choice([6, 12, 12, 24])
            lead, moq = FIXTURE_VENDOR_TERMS.get(code, (lead, moq))
            v = {"vendor_id": stable_uuid(t, "vendor", code), "tenant_id": t, "vendor_code": code, "name": name,
                 "lead_time_days": lead, "min_order_qty": moq,
                 "payment_terms": rng.choice(["NET30", "NET30", "NET45", "NET15", "PREPAID"]),
                 "rating": money(rng.uniform(3.2, 4.9)),
                 "contact_email": f"orders@{name.lower().replace(' ', '').replace('&', '')[:18]}.example"}
            vendors.append(v)
        self.rows["vendors"].extend(vendors)
        vendor_by_code = {v["vendor_code"]: v for v in vendors}

        # locations
        locs = []
        cities = CITIES[t]
        store_prefix = "STORE-" if t == "acme-retail" else "STORE-G"
        for i in range(1, layout["stores"] + 1):
            city, region = cities[(i - 1) % len(cities)]
            code = f"{store_prefix}{i:03d}" if t == "acme-retail" else f"{store_prefix}{i:02d}"
            locs.append({"location_id": stable_uuid(t, "loc", code), "tenant_id": t, "location_code": code,
                         "name": f"{city} {['Downtown', 'North', 'Midtown', 'Eastside', 'Lakeview'][i % 5]}",
                         "location_type": "store", "region": region, "city": city})
        for i in range(1, layout["dcs"] + 1):
            city, region = cities[(i - 1) % len(cities)]
            code = f"DC-{i:02d}" if t == "acme-retail" else f"DC-G{i:02d}"
            locs.append({"location_id": stable_uuid(t, "loc", code), "tenant_id": t, "location_code": code,
                         "name": f"{city} Distribution Center", "location_type": "dc", "region": region, "city": city})
        self.rows["locations"].extend(locs)
        loc_by_code = {loc["location_code"]: loc for loc in locs}

        # products
        prods = []
        pool = [(c, n) for c, names in CATEGORIES.items() for n in names]
        sku_base = 10001 if t == "acme-retail" else 20001
        sku_prefix = "SKU" if t == "acme-retail" else "GLX"
        fixture_skus = {f.sku: f for f in FIXTURES if f.tenant == t}
        for i in range(layout["products"]):
            sku = f"{sku_prefix}-{sku_base + i}"
            if sku in fixture_skus:
                fx = fixture_skus[sku]
                cat, name, cost, vendor = fx.category, fx.product_name, money(fx.unit_cost), vendor_by_code[fx.vendor_code]
            else:
                cat, base_name = pool[(i * 7) % len(pool)]
                brand = rng.choice(["Organic", "Select", "Family", "Premium", "Value", "Fresh"])
                name = f"{brand} {base_name}"
                cost = money(BASE_COST[cat] * rng.uniform(0.6, 2.2))
                vendor = rng.choice([v for v in vendors if VENDOR_CATEGORY[v["name"]] == cat] or vendors)
            prods.append({"product_id": stable_uuid(t, "product", sku), "tenant_id": t, "sku": sku, "name": name,
                          "category": cat, "uom": "EA", "unit_cost": cost, "default_vendor_id": vendor["vendor_id"]})
        self.rows["products"].extend(prods)
        prod_by_sku = {p["sku"]: p for p in prods}

        # inventory positions: each product stocked at ~12 locations (+ fixture locations)
        positions: dict[tuple, dict] = {}
        for p in prods:
            for loc in rng.sample(locs, k=min(12, len(locs))):
                demand = money(rng.uniform(1.5, 40.0) * (4 if loc["location_type"] == "dc" else 1))
                safety = max(6, int(float(demand) * rng.uniform(2, 4)))
                scenario = rng.random()
                if scenario < 0.18:      # low stock
                    on_hand = rng.randint(0, max(1, safety // 2))
                elif scenario < 0.30:    # high demand spike
                    on_hand = rng.randint(safety // 2, safety)
                    demand = money(float(demand) * rng.uniform(1.5, 2.5))
                else:
                    on_hand = rng.randint(safety, safety * 4)
                positions[(p["product_id"], loc["location_id"])] = {
                    "tenant_id": t, "product_id": p["product_id"], "location_id": loc["location_id"],
                    "on_hand": on_hand, "on_order": rng.choice([0, 0, 0, 12, 24, 48]), "allocated": rng.randint(0, 5),
                    "safety_stock": safety, "avg_daily_demand": demand}
        for fx in fixture_skus.values():
            p, loc = prod_by_sku[fx.sku], loc_by_code[fx.location_code]
            positions[(p["product_id"], loc["location_id"])] = {
                "tenant_id": t, "product_id": p["product_id"], "location_id": loc["location_id"],
                "on_hand": fx.on_hand, "on_order": 0, "allocated": 0, "safety_stock": fx.safety_stock,
                "avg_daily_demand": money(fx.daily_demand)}
        self.rows["inventory_positions"].extend(positions.values())

        recs = self._recommendations(t, layout["recs"], positions, prods, vendors, fixture_skus, prod_by_sku, loc_by_code)
        self._orders(t, layout["orders"], recs, prods, locs, vendors)

    # ------------------------------------------------------------------ recommendations
    def _recommendations(self, t, count, positions, prods, vendors, fixture_skus, prod_by_sku, loc_by_code):
        rng = self.rng
        prod_by_id = {p["product_id"]: p for p in prods}
        vendor_by_id = {v["vendor_id"]: v for v in vendors}
        vendor_by_code = {v["vendor_code"]: v for v in vendors}
        recs: list[dict] = []
        open_keys: set[tuple] = set()

        for fx in fixture_skus.values():
            p, loc, v = prod_by_sku[fx.sku], loc_by_code[fx.location_code], vendor_by_code[fx.vendor_code]
            demand = money(fx.daily_demand)
            recs.append({
                "recommendation_id": stable_uuid(t, "rec-fixture", fx.sku, fx.location_code), "tenant_id": t,
                "product_id": p["product_id"], "sku": p["sku"], "location_id": loc["location_id"],
                "vendor_id": v["vendor_id"], "current_stock": fx.on_hand, "forecast_demand": money(demand * 14),
                "forecast_horizon_days": 14, "safety_stock": fx.safety_stock, "recommended_qty": fx.recommended_qty,
                "unit_cost": money(fx.unit_cost), "reason": fx.reason, "risk_level": fx.risk,
                "expected_delivery_date": TODAY + timedelta(days=v["lead_time_days"]), "status": "open",
                "created_at": NOW - timedelta(hours=6)})
            open_keys.add((p["product_id"], loc["location_id"]))

        keys = sorted(positions.keys(), key=lambda k: (str(k[0]), str(k[1])))
        rng.shuffle(keys)
        i = 0
        while len(recs) < count:
            key = keys[i % len(keys)]
            i += 1
            pos = positions[key]
            status = weighted(rng, REC_STATUS_MIX)
            if status == "open" and key in open_keys:
                status = "expired"
            p = prod_by_id[key[0]]
            v = vendor_by_id[p["default_vendor_id"]]
            demand = float(pos["avg_daily_demand"]) * rng.uniform(0.85, 1.35)
            horizon = rng.choice([7, 14, 14, 21, 28])
            forecast = money(demand * horizon)
            stock = pos["on_hand"] if status == "open" else rng.randint(0, pos["safety_stock"] * 3)
            need = float(forecast) + pos["safety_stock"] - stock - pos["on_order"]
            case = v["min_order_qty"]
            qty = max(case, int(math.ceil(max(need, case) / case) * case))
            cover = stock / max(demand, 0.1)
            if stock < pos["safety_stock"] * 0.5:
                risk = "critical"
            elif stock < pos["safety_stock"]:
                risk = "high"
            elif stock < pos["safety_stock"] * 1.5:
                risk = "medium"
            else:
                risk = "low"
            driver = rng.choice(["baseline trend", "weekend traffic", "promotion uplift", "seasonal peak",
                                 "competitor stock-out", "weather-driven demand"])
            reason = (f"On-hand {stock} covers {cover:.1f} days vs {v['lead_time_days']}-day lead time; "
                      f"{horizon}-day forecast {float(forecast):.0f} units ({driver}) plus safety stock "
                      f"{pos['safety_stock']} requires {qty} units in cases of {case}.")
            if status == "open":
                created = NOW - timedelta(days=rng.randint(0, 2), hours=rng.randint(0, 23))
                eta = TODAY + timedelta(days=v["lead_time_days"])
            else:
                created = NOW - timedelta(days=rng.randint(3, 45), hours=rng.randint(0, 23))
                eta = created.date() + timedelta(days=v["lead_time_days"] + 1)
            rec_id = stable_uuid(t, "rec", len(recs))
            recs.append({
                "recommendation_id": rec_id, "tenant_id": t, "product_id": p["product_id"], "sku": p["sku"],
                "location_id": key[1], "vendor_id": v["vendor_id"], "current_stock": stock, "forecast_demand": forecast,
                "forecast_horizon_days": horizon, "safety_stock": pos["safety_stock"], "recommended_qty": qty,
                "unit_cost": p["unit_cost"], "reason": reason, "risk_level": risk,
                "expected_delivery_date": eta, "status": status,
                "created_at": created})
            if status == "open":
                open_keys.add(key)
        self.rows["recommendations"].extend(recs)
        return recs

    # ------------------------------------------------------------------ orders / payments / notifications
    def _orders(self, t, count, recs, prods, locs, vendors):
        rng = self.rng
        prod_by_id = {p["product_id"]: p for p in prods}
        fixture_rec_ids = {r["recommendation_id"] for r in recs if r["reason"] and r["created_at"] == NOW - timedelta(hours=6)}
        linkable = [r for r in recs if r["status"] in ("accepted", "expired", "dismissed")
                    and r["recommendation_id"] not in fixture_rec_ids]
        rng.shuffle(linkable)
        users = [u for u in USERS if u[0] == t and ("buyer" in u[4] or "approver" in u[4])]
        tenant = next(x for x in TENANTS if x["tenant_id"] == t)

        for n in range(count):
            status = weighted(rng, ORDER_STATUS_MIX)
            order_id = stable_uuid(t, "order", n)
            created = NOW - timedelta(days=rng.randint(0, 60), hours=rng.randint(0, 23))
            linked = linkable.pop() if (linkable and rng.random() < 0.72) else None
            if linked:
                if status != "cancelled":
                    linked["status"] = "ordered"
                vendor_id, location_id = linked["vendor_id"], linked["location_id"]
                lines = [(prod_by_id[linked["product_id"]], linked["recommended_qty"], linked["unit_cost"])]
            else:
                vendor = rng.choice(vendors)
                location_id = rng.choice(locs)["location_id"]
                vendor_id = vendor["vendor_id"]
                candidates = [p for p in prods if p["default_vendor_id"] == vendor_id] or prods
                lines = [(p, rng.choice([12, 24, 36, 48, 96, 144]), p["unit_cost"])
                         for p in rng.sample(candidates, k=min(len(candidates), rng.randint(1, 3)))]
            total = sum((money(q * float(c)) for _, q, c in lines), Decimal("0.00"))
            payment_status = {
                "fulfilled": "captured", "submitted": "captured",
                "approved": rng.choice(["authorized", "pending"]),
                "pending_approval": "not_started", "draft": "not_started",
                "cancelled": rng.choice(["not_started", "not_started", "refunded"]),
                "payment_failed": "failed",
            }[status]
            if payment_status in ("authorized", "captured", "refunded", "pending") and total > tenant["payment_limit"]:
                status, payment_status = "payment_failed", "failed"
            self.order_seq += 1
            order_number = f"{'AC' if t == 'acme-retail' else 'GX'}-{2026}{self.order_seq:06d}"
            user = rng.choice(users)
            self.rows["orders"].append({
                "order_id": order_id, "tenant_id": t, "order_number": order_number,
                "recommendation_id": linked["recommendation_id"] if linked else None,
                "location_id": location_id, "vendor_id": vendor_id, "status": status, "payment_status": payment_status,
                "currency": "USD", "total_amount": total,
                "expected_delivery_date": created.date() + timedelta(days=rng.randint(2, 14)),
                "idempotency_key": f"seed-{order_id}", "approval_id": None, "created_by": user[1],
                "notes": None, "created_at": created})
            for ln, (p, q, c) in enumerate(lines, start=1):
                self.rows["order_lines"].append({
                    "order_line_id": stable_uuid(t, "line", order_id, ln), "tenant_id": t, "order_id": order_id,
                    "line_no": ln, "product_id": p["product_id"], "sku": p["sku"], "ordered_qty": q,
                    "unit_cost": money(c), "line_total": money(q * float(c)), "created_at": created})
            self._payments(t, order_id, status, payment_status, total, created)
            self._notifications(t, order_id, order_number, status, user, created)

    def _payments(self, t, order_id, status, payment_status, total, created):
        rng = self.rng
        if payment_status == "not_started":
            return
        attempts: list[tuple[str, str | None]] = []
        if payment_status == "failed":
            for _ in range(rng.randint(1, 2)):
                attempts.append(("failed", rng.choice(["card_declined", "insufficient_funds", "limit_exceeded",
                                                       "processor_error"])))
        else:
            if rng.random() < 0.15:
                attempts.append(("failed", "processor_error"))
            attempts.append((payment_status, None))
        for i, (st, code) in enumerate(attempts, start=1):
            pid = stable_uuid(t, "payment", order_id, i)
            self.rows["payments"].append({
                "payment_id": pid, "tenant_id": t, "order_id": order_id, "amount": total, "currency": "USD",
                "method": rng.choice(["card", "card", "ach", "invoice"]), "method_last4": f"{rng.randint(0, 9999):04d}",
                "status": st, "provider_reference": f"sim_{pid.hex[:16]}" if st != "failed" else None,
                "failure_code": code, "failure_reason": code.replace("_", " ") if code else None, "attempt": i,
                "idempotency_key": f"seed-pay-{order_id}-{i}", "approval_id": None,
                "created_at": created + timedelta(minutes=5 * i)})

    def _notifications(self, t, order_id, order_number, status, user, created):
        rng = self.rng
        events = []
        if status in ("submitted", "fulfilled", "approved"):
            events.append(("order_confirmation", "email"))
        if status == "fulfilled":
            events.append(("order_delivered", rng.choice(["email", "sms"])))
        if status == "payment_failed":
            events.append(("payment_failed", "email"))
        if status == "cancelled":
            events.append(("order_cancelled", "in_app"))
        if status == "pending_approval":
            events.append(("approval_requested", "in_app"))
        for i, (template, channel) in enumerate(events, start=1):
            nid = stable_uuid(t, "notification", order_id, i)
            st = rng.choices(["delivered", "failed", "dead_letter"], weights=[90, 6, 4])[0]
            recipient = user[2] if channel == "email" else ("+15125550100" if channel == "sms" else user[1])
            self.rows["notifications"].append({
                "notification_id": nid, "tenant_id": t, "order_id": order_id, "channel": channel,
                "recipient": recipient, "template": template,
                "payload": json.dumps({"specversion": "1.0", "type": f"com.ordering.{template}",
                                       "source": "/seed", "id": str(nid), "subject": order_number,
                                       "data": {"order_number": order_number}}),
                "status": st, "attempts": 1 if st == "delivered" else (5 if st == "dead_letter" else 2),
                "max_attempts": 5, "next_attempt_at": created + timedelta(days=365) if st == "failed" else created,
                "last_error": None if st == "delivered" else "simulated provider bounce",
                "idempotency_key": f"seed-ntf-{nid}", "delivered_at": created + timedelta(minutes=1) if st == "delivered" else None,
                "created_at": created})


UPSERTS = {
    "tenants": ("tenant_id", ["name", "currency", "approval_threshold", "payment_limit"]),
    "users": ("tenant_id, user_id", ["email", "display_name", "roles"]),
    "vendors": ("vendor_id", ["vendor_code", "name", "lead_time_days", "min_order_qty", "payment_terms", "rating",
                              "contact_email"]),
    "products": ("product_id", ["sku", "name", "category", "uom", "unit_cost", "default_vendor_id"]),
    "locations": ("location_id", ["location_code", "name", "location_type", "region", "city"]),
    "inventory_positions": ("tenant_id, product_id, location_id",
                            ["on_hand", "on_order", "allocated", "safety_stock", "avg_daily_demand"]),
    "recommendations": ("recommendation_id", ["vendor_id", "current_stock", "forecast_demand", "forecast_horizon_days", "safety_stock",
                                              "recommended_qty", "unit_cost", "reason", "risk_level",
                                              "expected_delivery_date", "status"]),
    "orders": ("order_id", ["status", "payment_status", "total_amount", "expected_delivery_date"]),
    "order_lines": ("order_line_id", ["ordered_qty", "unit_cost", "line_total"]),
    "payments": ("payment_id", ["amount", "status", "failure_code", "failure_reason"]),
    "notifications": ("notification_id", ["status", "attempts", "last_error", "delivered_at"]),
}


async def seed() -> dict[str, int]:
    rows = Builder().build()
    async with admin_session() as session:
        # release open-recommendation slots first so re-runs cannot trip ux_rec_open_per_item
        seeded_open = [r["recommendation_id"] for r in rows["recommendations"] if r["status"] == "open"]
        await session.execute(text(
            "UPDATE recommendations SET status = 'expired' WHERE status = 'open' "
            "AND NOT (recommendation_id = ANY(:ids))"), {"ids": seeded_open})
        # Seed-owned child rows are replaced wholesale (same transaction) so a changed generator
        # can never collide with rows from a previous definition. Rows created by the running
        # application (demo orders, payments, notifications) are never touched.
        await session.execute(text("DELETE FROM payments WHERE idempotency_key LIKE 'seed-pay-%'"))
        await session.execute(text("DELETE FROM notifications WHERE idempotency_key LIKE 'seed-ntf-%'"))
        await session.execute(text(
            "DELETE FROM order_lines ol USING orders o WHERE o.tenant_id = ol.tenant_id "
            "AND o.order_id = ol.order_id AND o.idempotency_key LIKE 'seed-%'"))
        for table, (conflict, update_cols) in UPSERTS.items():
            data = rows[table]
            if not data:
                continue
            cols = list(data[0].keys())
            col_list = ", ".join(cols)
            values = ", ".join(f"CAST(:{c} AS JSONB)" if c == "payload" else f":{c}" for c in cols)
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({values}) ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
            for start in range(0, len(data), 500):
                await session.execute(text(sql), data[start:start + 500])
        await session.execute(text("SELECT setval('order_number_seq', GREATEST(nextval('order_number_seq'), 200000))"))
    return {k: len(v) for k, v in rows.items()}


async def counts() -> dict[str, int]:
    out = {}
    async with admin_session() as session:
        for table in UPSERTS:
            out[table] = (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verify", action="store_true", help="only print table row counts")
    args = parser.parse_args()
    try:
        if not args.verify:
            generated = await seed()
            print(json.dumps({"generated": generated}, indent=2))
        print(json.dumps({"table_counts": await counts()}, indent=2))
    finally:
        await dispose_engines()


if __name__ == "__main__":
    asyncio.run(main())
