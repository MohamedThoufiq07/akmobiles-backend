import logging
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from orders.models import Order
from orders.services import complete_payment_once, transition_order_status
from payments.models import Payment, PaymentAttempt

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Reconciles internal Orders and Payments against the Razorpay Gateway status."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=True,
            help="Simulate the reconciliation without committing database changes (default: True).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            default=False,
            help="Explicitly commit database changes.",
        )
        parser.add_argument(
            "--order-id",
            type=str,
            help="Filter reconciliation to a single internal Order ID.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        dry_run = not apply_changes
        order_id_filter = options.get("order_id")

        self.stdout.write(self.style.NOTICE(
            f"--- Starting Razorpay Order Reconciliation ({'DRY RUN - No writes' if dry_run else 'APPLY MODE - Writes enabled'}) ---"
        ))

        import razorpay
        if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
            self.stderr.write("Razorpay credentials are not configured.")
            return

        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

        # Find orders: either specifically requested, or placed/uncompleted orders
        qs = Order.objects.all().order_by("-created_at")
        if order_id_filter:
            qs = qs.filter(_id=order_id_filter)
        else:
            # Reconcile orders whose payment is not Completed or order_status might be out of sync
            qs = qs.filter(payment_info__method="Razorpay")

        total_scanned = 0
        total_reconciled = 0
        discrepancies = []

        for order in qs:
            total_scanned += 1
            payment = Payment.objects.filter(order=order).first()
            payment_info = order.payment_info or {}
            rzp_order_id = (payment.razorpay_order_id if payment else None) or payment_info.get("razorpayOrderId")

            old_order_status = order.order_status
            old_payment_status = payment.status if payment else payment_info.get("status", "None")

            # 1. If no Razorpay Order ID exists on record
            if not rzp_order_id:
                if old_order_status == "Placed" and old_payment_status != "Completed":
                    discrepancies.append({
                        "order_id": order._id,
                        "amount": order.total_price,
                        "old_order": old_order_status,
                        "new_order": "AwaitingPayment",
                        "old_payment": old_payment_status,
                        "new_payment": "Cancelled",
                        "reason": "Missing Razorpay order ID for unpaid order",
                    })
                    if not dry_run:
                        with transaction.atomic():
                            order.order_status = "AwaitingPayment"
                            order.save(update_fields=["order_status", "updated_at"])
                            if payment:
                                payment.status = "Cancelled"
                                payment.save(update_fields=["status", "updated_at"])
                continue

            # 2. Fetch ground truth from Razorpay API
            try:
                rzp_order_data = client.order.fetch(rzp_order_id)
                payments_res = client.order.payments(rzp_order_id)
                rzp_payments = payments_res.get("items", []) if isinstance(payments_res, dict) else []
            except Exception as err:
                self.stdout.write(self.style.WARNING(f"Order {order._id} ({rzp_order_id}): API query failed - {err}"))
                continue

            captured_payment = next((p for p in rzp_payments if p.get("status") == "captured"), None)
            auth_payment = next((p for p in rzp_payments if p.get("status") in ["authorized", "processing"]), None)

            # Determine true statuses
            if captured_payment:
                target_order_status = "Placed" if old_order_status in ["AwaitingPayment", "Placed"] else old_order_status
                target_payment_status = "Completed"
                action_desc = "Verified Captured on Gateway"
            elif auth_payment:
                target_order_status = "AwaitingPayment"
                target_payment_status = "Authorized"
                action_desc = "Authorized on Gateway"
            else:
                target_order_status = "AwaitingPayment" if old_order_status not in ["Delivered", "Cancelled"] else old_order_status
                target_payment_status = "Cancelled"
                action_desc = "No capture found on Gateway (Cancelled / Incomplete)"

            # Check if change is needed
            needs_update = (old_order_status != target_order_status) or (old_payment_status != target_payment_status)

            if needs_update or captured_payment:
                total_reconciled += 1
                discrepancies.append({
                    "order_id": order._id,
                    "amount": order.total_price,
                    "rzp_order_id": rzp_order_id,
                    "old_order": old_order_status,
                    "new_order": target_order_status,
                    "old_payment": old_payment_status,
                    "new_payment": target_payment_status,
                    "stock_reduced": payment.stock_reduced if payment else False,
                    "reason": action_desc,
                })

                if not dry_run:
                    with transaction.atomic():
                        locked_order = Order.objects.select_for_update().filter(_id=order._id).first()
                        locked_payment, _ = Payment.objects.select_for_update().get_or_create(
                            order=locked_order,
                            defaults={
                                "user": locked_order.user,
                                "amount": locked_order.total_price,
                                "amount_paise": int(locked_order.total_price * 100),
                                "razorpay_order_id": rzp_order_id,
                                "status": target_payment_status,
                            }
                        )

                        if captured_payment:
                            complete_payment_once(
                                locked_payment,
                                locked_order,
                                rzp_payment_id=captured_payment.get("id", ""),
                            )
                        else:
                            locked_payment.status = target_payment_status
                            locked_payment.save(update_fields=["status", "updated_at"])
                            locked_order.order_status = target_order_status
                            locked_order.save(update_fields=["order_status", "updated_at"])

        # Summary output
        self.stdout.write(f"\nScanned: {total_scanned} orders | Targets found: {len(discrepancies)}")
        self.stdout.write("-" * 90)
        for d in discrepancies:
            self.stdout.write(
                f"Order {d['order_id']} (Rs. {d['amount']}):\n"
                f"  Order:   {d['old_order']} -> {d['new_order']}\n"
                f"  Payment: {d['old_payment']} -> {d['new_payment']}\n"
                f"  Reason:  {d['reason']}\n"
            )
        self.stdout.write("-" * 90)
        if dry_run:
            self.stdout.write(self.style.SUCCESS("Dry run completed. Zero database records were modified."))
            self.stdout.write("To apply these changes, run with --apply after review.")
        else:
            self.stdout.write(self.style.SUCCESS(f"Reconciliation applied. Updated {total_reconciled} records."))
