# AK Mobiles — Django Backend (DRF + PostgreSQL)

This is a **1:1 port** of the original MERN backend (`ak-mobiles-backend`) to
**Django + Django REST Framework + PostgreSQL**. The API paths, the JSON response
shapes (`_id`, `success` envelope, camelCase fields, nested objects), the auth
flow (JWT `Bearer`), and the error format (`{ success: false, message }`) are all
preserved — so your existing **React frontend works with little to no change**.

---

## Quick start (Tanglish)

```bash
# 1. Virtual env
python -m venv venv
venv\Scripts\activate          # Windows PowerShell:  venv\Scripts\Activate.ps1

# 2. Install
pip install -r requirements.txt

# 3. Env file
copy .env.example .env         # then edit DB + JWT + Razorpay values

# 4. Postgres-la db create pannu (psql / pgAdmin):  CREATE DATABASE akmobiles;

# 5. Migrate
python manage.py makemigrations
python manage.py migrate

# 6. Admin user (email login)
python manage.py createsuperuser    # role auto = admin

# 7. Run
python manage.py runserver          # http://localhost:8000
```

> **Postgres illama test panna?** `.env`-la `USE_SQLITE=True` set pannu — udanே SQLite-la run aagum. Production-la `False`.

---

## Frontend connect panna (almost zero touch)

Un React `.env` / axios baseURL maathu:

```
# old (Express)
VITE_API_URL=http://localhost:5000/api
# new (Django)
VITE_API_URL=http://localhost:8000/api
```

CORS already `FRONTEND_URL` (default `http://localhost:5173`) ku allow pannirukken.
Vera frontend port-na `.env`-la maathu.

### Frontend-la paaka vேndியvை (rare edge cases)

99% endpoints exact same shape. Idhu mattum verify pannu:

1. **Reset password** — Express dev-mode-la `resetToken`-a response-la anuppichadhu;
   adhe behaviour vechirukken (`NODE_ENV=development`-la mattum). Production-la
   real email sending nee add pannanum (`forgot_password` view-la TODO).
2. **bcrypt passwords** — DB maaridhu, so pazhaya Mongo password hashes carry
   aagaadhu. Users fresh-ah register pannanum, illa migrate panna bcrypt hasher
   add pannanum (kelu, naan kaatturen).
3. Vera ellame (login, products, orders, wishlist, reviews, payments, admin,
   settings, contact, newsletter, upload) **byte-for-byte same shape**.

---

## Endpoint map (Express → Django) — paths identical

| Method | Path | Old controller | Django view |
|---|---|---|---|
| POST | /api/auth/register | authController.register | accounts.views.register |
| POST | /api/auth/login | login | accounts.views.login |
| GET/PUT | /api/auth/profile | getProfile/updateProfile | accounts.views.profile |
| POST | /api/auth/forgot-password | forgotPassword | accounts.views.forgot_password |
| POST | /api/auth/reset-password | resetPassword | accounts.views.reset_password |
| PUT | /api/auth/wishlist/:id | toggleWishlist | accounts.views.toggle_wishlist |
| GET | /api/products | getProducts | products.views.products_root |
| POST | /api/products | createProduct | products.views.products_root |
| GET | /api/products/featured | getFeaturedProducts | products.views.get_featured |
| GET | /api/products/top | getTopProducts | products.views.get_top |
| GET/PUT/DELETE | /api/products/:id | get/update/deleteProduct | products.views.product_detail |
| GET | /api/products/:id/related | getRelatedProducts | products.views.get_related |
| POST | /api/products/:id/reviews | createReview | products.views.create_review |
| POST | /api/orders | createOrder | orders.views.orders_root |
| GET | /api/orders | getAllOrders (admin) | orders.views.orders_root |
| GET | /api/orders/myorders | getMyOrders | orders.views.my_orders |
| GET | /api/orders/stats | getOrderStats (admin) | orders.views.order_stats |
| GET | /api/orders/:id | getOrderById | orders.views.order_detail |
| PUT | /api/orders/:id/status | updateOrderStatus (admin) | orders.views.update_status |
| GET | /api/payment/key | getRazorpayKey | payments.views.razorpay_key |
| POST | /api/payment/create-order | createRazorpayOrder | payments.views.create_order |
| POST | /api/payment/verify | verifyPayment | payments.views.verify_payment |
| POST | /api/contact | submitContact | core.views.contact |
| GET | /api/contact | getContactMessages (admin) | core.views.contact |
| POST | /api/newsletter/subscribe | subscribe | core.views.newsletter_subscribe |
| GET/PUT | /api/settings | get/updateSettings | core.views.store_settings |
| GET | /api/users | getAllUsers (admin) | core.views.all_users |
| GET | /api/users/:id | getUserById (admin) | core.views.user_by_id |
| GET | /api/admin/dashboard | getDashboardStats | core.views.dashboard_stats |
| GET | /api/admin/reports/sales | getSalesReport | core.views.sales_report |
| GET | /api/admin/reports/top-products | getTopSellingProducts | core.views.top_products |
| POST | /api/upload | upload route | core.views.upload_image |
| GET | /api/health | health check | akmobiles.urls.health |

---

## How the Mongo → Postgres mapping works

| Mongo concept | Django |
|---|---|
| `_id` (ObjectId) | `CharField` PK, 24-hex generated id — frontend `_id` unchanged |
| `timestamps` | `created_at`/`updated_at`, serialized as `createdAt`/`updatedAt` |
| embedded sub-docs (shippingAddress, specifications, images, statusHistory, addresses) | `JSONField` — same nested shape |
| refs (`user`, `orderItems.product`) | `ForeignKey` / id strings; `.populate()` reproduced in serializers |
| `wishlist` (array of refs) | `ManyToMany`; serialized as id list, populated on `/profile` |
| reviews (embedded) | real `Review` table, serialized embedded inside product |
| pre-save hooks (discount %, estimatedDelivery, statusHistory) | `Model.save()` overrides |

## Tech stack

Django 5 · DRF · `djangorestframework-simplejwt` (single 7-day Bearer token) ·
`django-cors-headers` · PostgreSQL (`psycopg2-binary`) · `razorpay` (demo fallback
when keys absent).

## Smoke test

`USE_SQLITE=True python smoke_test.py` — exercises every endpoint and asserts the
response shapes. All pass.
