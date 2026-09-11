# Generated manually for safe data migration of legacy Product.images JSON to ProductImage models.

import logging
from django.db import migrations

logger = logging.getLogger(__name__)


def migrate_legacy_images_to_product_images(apps, schema_editor):
    Product = apps.get_model("products", "Product")
    ProductImage = apps.get_model("products", "ProductImage")

    valid_count = 0
    skipped_count = 0
    duplicate_count = 0

    for product in Product.objects.all():
        raw_images = product.images or []
        if not isinstance(raw_images, list):
            skipped_count += 1
            continue

        sort_index = 0
        has_primary = ProductImage.objects.filter(product=product, is_primary=True).exists()

        for item in raw_images:
            raw_url = ""
            alt = product.name or ""

            if isinstance(item, dict):
                raw_url = (item.get("url") or "").strip()
                alt = (item.get("alt") or product.name or "").strip()
            elif isinstance(item, str):
                raw_url = item.strip()

            if not raw_url:
                skipped_count += 1
                continue

            # Idempotency check: if this product already has a ProductImage with this URL, skip
            if ProductImage.objects.filter(product=product, url=raw_url).exists():
                duplicate_count += 1
                continue

            is_primary = not has_primary
            ProductImage.objects.create(
                product=product,
                url=raw_url,
                storage_key="",
                alt_text=alt[:255],
                sort_order=sort_index,
                is_primary=is_primary,
                content_type="image/jpeg",
            )
            if is_primary:
                has_primary = True
            sort_index += 1
            valid_count += 1

    logger.info(
        "Legacy image migration summary: %d migrated, %d skipped, %d duplicates",
        valid_count,
        skipped_count,
        duplicate_count,
    )


def reverse_migration(apps, schema_editor):
    # Backward compatibility: legacy images JSON field was never modified/dropped
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("products", "0002_product_delivery_charge_productuploadsession_and_more"),
    ]

    operations = [
        migrations.RunPython(
            migrate_legacy_images_to_product_images,
            reverse_code=reverse_migration,
        ),
    ]
