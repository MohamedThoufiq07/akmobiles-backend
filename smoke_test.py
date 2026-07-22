"""
End-to-end smoke test using Django's test client. Verifies the response shapes
match the MERN backend (_id, success envelope, camelCase fields, nested objects).

Self-contained: runs against a fresh in-memory SQLite database that is created
and migrated on startup, so it needs no Postgres and is fully repeatable
(just run `python smoke_test.py` — no prior `migrate` required).
"""
import os, django, json
os.environ["USE_SQLITE"] = "True"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "akmobiles.settings")
django.setup()

# Use an isolated in-memory DB and build its schema up front. This keeps the
# test reproducible (no leftover rows between runs) and avoids the "no such
# table" error you'd get from an un-migrated sqlite file.
from django.conf import settings as _settings
from django.core.management import call_command
_settings.DATABASES["default"] = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": ":memory:",
    "ATOMIC_REQUESTS": False,
    "AUTOCOMMIT": True,
    "CONN_MAX_AGE": 0,
    "CONN_HEALTH_CHECKS": False,
    "OPTIONS": {},
    "TIME_ZONE": None,
    "USER": "",
    "PASSWORD": "",
    "HOST": "",
    "PORT": "",
    "TEST": {"CHARSET": None, "COLLATION": None, "MIGRATE": True, "MIRROR": None, "NAME": None},
}
call_command("migrate", run_syncdb=True, verbosity=0)

from django.test import Client
from accounts.models import User
from products.models import Product

c = Client()
ok = lambda label, cond: print(("  PASS" if cond else "  FAIL"), label)

print("\n== AUTH ==")
r = c.post("/api/auth/register", data=json.dumps({"name":"Thoufiq","email":"t@x.com","password":"secret1"}), content_type="application/json")
body = r.json()
ok("register 201", r.status_code == 201)
ok("has token", "token" in body)
ok("user has _id", "_id" in body["user"])
ok("user has wishlist[]", body["user"]["wishlist"] == [])
token = body["token"]

r = c.post("/api/auth/login", data=json.dumps({"email":"t@x.com","password":"secret1"}), content_type="application/json")
ok("login 200", r.status_code == 200 and r.json()["success"])
ok("login wrong pwd 401", c.post("/api/auth/login", data=json.dumps({"email":"t@x.com","password":"nope"}), content_type="application/json").status_code == 401)

# make an admin
admin = User.objects.create_user(email="a@x.com", password="secret1", name="Admin", role="admin")
ra = c.post("/api/auth/login", data=json.dumps({"email":"a@x.com","password":"secret1"}), content_type="application/json")
admin_token = ra.json()["token"]
AUTH = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
ADMIN = {"HTTP_AUTHORIZATION": f"Bearer {admin_token}"}

print("\n== PRODUCTS ==")
prod_payload = {
    "name":"AK Phone X","brand":"AK","category":"Smartphones","description":"nice",
    "highlights":["5G","AMOLED"],
    "specifications":{"ram":"8GB","storage":"128GB","processor":"SD8"},
    "images":[{"url":"http://img/1.jpg","alt":"front"}],
    "originalPrice":20000,"offerPrice":15000,"stock":10,"isFeatured":True,
}
r = c.post("/api/products", data=json.dumps(prod_payload), content_type="application/json", **ADMIN)
pb = r.json()
ok("create product 201 (admin)", r.status_code == 201)
ok("discount auto-calc 25", pb["product"]["discount"] == 25)
ok("camelCase offerPrice", pb["product"]["offerPrice"] == 15000)
ok("specs preserved", pb["product"]["specifications"]["ram"] == "8GB")
ok("images shape", pb["product"]["images"][0]["alt"] == "front")
pid = pb["product"]["_id"]

ok("create product blocked for normal user 403",
   c.post("/api/products", data=json.dumps(prod_payload), content_type="application/json", **AUTH).status_code == 403)

r = c.get("/api/products?category=Smartphones&minPrice=1000&sort=price_low")
lb = r.json()
ok("list envelope page/pages/total", all(k in lb for k in ("products","page","pages","total")))
ok("filter+sort returns product", len(lb["products"]) == 1)

r = c.get("/api/products?ram=8GB")
ok("JSON spec filter ram=8GB", len(r.json()["products"]) == 1)
r = c.get("/api/products?search=AK")
ok("search works", len(r.json()["products"]) == 1)
r = c.get("/api/products/featured")
ok("featured", len(r.json()["products"]) == 1)
r = c.get(f"/api/products/{pid}")
ok("detail by _id", r.json()["product"]["_id"] == pid)

print("\n== WISHLIST ==")
r = c.put(f"/api/auth/wishlist/{pid}", **AUTH)
ok("toggle add", r.json()["wishlist"] == [pid])
r = c.get("/api/auth/profile", **AUTH)
ok("profile populated wishlist", r.json()["user"]["wishlist"][0]["_id"] == pid)

print("\n== REVIEWS ==")
r = c.post(f"/api/products/{pid}/reviews", data=json.dumps({"rating":4,"comment":"good"}), content_type="application/json", **AUTH)
ok("create review 201", r.status_code == 201)
r = c.get(f"/api/products/{pid}")
ok("rating recalculated to 4", r.json()["product"]["rating"] == 4)
ok("numReviews 1", r.json()["product"]["numReviews"] == 1)
ok("review embedded", r.json()["product"]["reviews"][0]["comment"] == "good")

print("\n== ORDERS ==")
order_payload = {
    "orderItems":[{"product":pid,"name":"AK Phone X","image":"http://img/1.jpg","price":15000,"quantity":2}],
    "shippingAddress":{"name":"T","email":"t@x.com","phone":"9","addressLine1":"L1","city":"Cud","state":"TN","postalCode":"6"},
    "paymentInfo":{"status":"Pending"},
    "itemsPrice":30000,"taxPrice":0,"shippingPrice":0,"totalPrice":30000,
}
r = c.post("/api/orders", data=json.dumps(order_payload), content_type="application/json", **AUTH)
obody = r.json()
ok("create order 201", r.status_code == 201)
ok("order has _id", "_id" in obody["order"])
ok("statusHistory seeded Placed", obody["order"]["statusHistory"][0]["status"] == "Placed")
ok("estimatedDelivery set", obody["order"]["estimatedDelivery"] is not None)
oid = obody["order"]["_id"]
ok("stock decremented 10->8", Product.objects.get(_id=pid).stock == 8)
ok("numSold 0->2", Product.objects.get(_id=pid).numSold if False else Product.objects.get(_id=pid).num_sold == 2)

r = c.get("/api/orders/myorders", **AUTH)
mo = r.json()["orders"]
ok("myorders populated product.name", mo[0]["orderItems"][0]["product"]["name"] == "AK Phone X")

r = c.get(f"/api/orders/{oid}", **AUTH)
ok("order detail populate user", r.json()["order"]["user"]["email"] == "t@x.com")

r = c.put(f"/api/orders/{oid}/status", data=json.dumps({"status":"Shipped"}), content_type="application/json", **ADMIN)
ok("admin update status", r.json()["order"]["orderStatus"] == "Shipped")
ok("status history appended", len(r.json()["order"]["statusHistory"]) == 2)

r = c.get("/api/orders", **ADMIN)
ok("admin all orders envelope", "orders" in r.json() and "total" in r.json())
r = c.get("/api/orders/stats", **ADMIN)
ok("order stats", r.json()["stats"]["totalOrders"] == 1)

print("\n== CORE ==")
r = c.post("/api/contact", data=json.dumps({"name":"a","email":"a@a.com","subject":"hi","message":"m"}), content_type="application/json")
ok("contact submit 201", r.status_code == 201)
r = c.post("/api/newsletter/subscribe", data=json.dumps({"email":"n@n.com"}), content_type="application/json")
ok("newsletter subscribe 201", r.status_code == 201)
ok("newsletter dup 400", c.post("/api/newsletter/subscribe", data=json.dumps({"email":"n@n.com"}), content_type="application/json").status_code == 400)
r = c.get("/api/settings")
ok("settings public flashSale fields", "flashSaleActive" in r.json()["settings"])
r = c.put("/api/settings", data=json.dumps({"flashSaleActive":True,"flashSaleTitle":"Big"}), content_type="application/json", **ADMIN)
ok("settings update admin", r.json()["settings"]["flashSaleActive"] is True)
r = c.get("/api/admin/dashboard", **ADMIN)
ok("dashboard stats", r.json()["stats"]["totalOrders"] == 1)
r = c.get("/api/users", **ADMIN)
ok("admin users list (excludes admins)", r.json()["total"] == 1)

print("\n== PAYMENTS ==")
r = c.get("/api/payment/key")
ok("razorpay key public", "key" in r.json())
r = c.post("/api/payment/create-order", data=json.dumps({"amount":300}), content_type="application/json", **AUTH)
ok("create-order demo fallback", r.json().get("demo") is True and r.json()["order"]["amount"] == 30000)

print("\n== AUTH GUARD ==")
ok("protected route no token 401", c.get("/api/auth/profile").status_code == 401)
ok("health ok", c.get("/api/health").json()["status"] == "OK")

print("\nDONE")
