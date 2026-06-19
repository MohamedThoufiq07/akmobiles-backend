"""
Seed the products table from the MERN seeder export.

Usage:
    python manage.py seed_products          # add products (skips ones already present by name)
    python manage.py seed_products --flush   # delete all products first, then seed

The JSON next to this file (products_seed.json) is the exact `products` array
exported from the old Node backend's seeder.js, so the new Postgres catalog
matches what was in MongoDB. camelCase keys are mapped to the model's snake_case.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from products.models import Product

DATA_FILE = Path(__file__).resolve().parent / "products_seed.json"

# JSON (Mongo) key -> model field
FIELD_MAP = {
    "name": "name",
    "brand": "brand",
    "category": "category",
    "description": "description",
    "highlights": "highlights",
    "specifications": "specifications",
    "images": "images",
    "originalPrice": "original_price",
    "offerPrice": "offer_price",
    "stock": "stock",
    "rating": "rating",
    "numReviews": "num_reviews",
    "isFeatured": "is_featured",
    "flashSale": "flash_sale",
    "numSold": "num_sold",
}


class Command(BaseCommand):
    help = "Seed products from products_seed.json (exported from the old MERN seeder)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete all existing products before seeding.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))

        if options["flush"]:
            deleted, _ = Product.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Flushed existing products ({deleted} rows)."))

        existing = set(Product.objects.values_list("name", flat=True))
        created = skipped = 0

        for item in raw:
            if item["name"] in existing:
                skipped += 1
                continue
            fields = {model_field: item[key] for key, model_field in FIELD_MAP.items() if key in item}
            # save() recomputes `discount` from the prices.
            Product(**fields).save()
            created += 1

        self.stdout.write(
            self.style.SUCCESS(f"Seeded {created} products ({skipped} already existed).")
        )
        self.stdout.write(f"Total products now: {Product.objects.count()}")
