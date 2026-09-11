from django.core.management.base import BaseCommand
from django.utils import timezone
from common.storage import delete_from_storage_safe
from orders.models import Order
from products.models import ProductImage, ProductUploadItem, ProductUploadSession


class Command(BaseCommand):
    help = "Safely cleans up abandoned or uncommitted upload session files and orphaned blobs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simulate cleanup without deleting any files or records.",
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        now = timezone.now()

        expired_items = ProductUploadItem.objects.filter(
            is_committed=False,
            session__expires_at__lt=now,
        ).select_related("session")

        total_found = expired_items.count()
        self.stdout.write(f"Found {total_found} expired/uncommitted staged items.")

        deleted_count = 0
        retained_count = 0

        for item in expired_items:
            # Verify if referenced by any active product image or historical order
            is_in_product_images = ProductImage.objects.filter(url=item.url).exists()
            is_in_orders = Order.objects.filter(order_items__icontains=item.url).exists()

            if is_in_product_images or is_in_orders:
                self.stdout.write(f"[RETAIN] URL {item.url} is referenced in products/orders.")
                retained_count += 1
                continue

            if dry_run:
                self.stdout.write(f"[DRY RUN] Would delete storage key: {item.storage_key} ({item.url})")
                deleted_count += 1
            else:
                success = delete_from_storage_safe(item.storage_key, item.url)
                item.delete()
                self.stdout.write(f"[DELETED] Key: {item.storage_key} (Storage removed: {success})")
                deleted_count += 1

        # Also expire stale active sessions
        if not dry_run:
            ProductUploadSession.objects.filter(status="active", expires_at__lt=now).update(status="expired")

        mode_str = "DRY RUN COMPLETE" if dry_run else "CLEANUP COMPLETE"
        self.stdout.write(self.style.SUCCESS(f"{mode_str}: {deleted_count} deleted/simulated, {retained_count} retained."))
