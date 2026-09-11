import io
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from products.models import Product, ProductImage, ProductUploadSession, ProductUploadItem
from products.serializers import ProductSerializer

User = get_user_model()


class ProductSearchAndRankingTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.iphone15 = Product.objects.create(
            name="iPhone 15",
            brand="Apple",
            category="Smartphones",
            description="Latest Apple flagship smartphone.",
            original_price=79900.0,
            offer_price=69999.0,
            delivery_charge=Decimal("49.00"),
            discount=12,
            stock=10,
            rating=4.8,
            num_reviews=150,
        )

        self.iphone14 = Product.objects.create(
            name="iPhone 14",
            brand="Apple",
            category="Smartphones",
            description="Apple previous generation smartphone.",
            original_price=69900.0,
            offer_price=59999.0,
            delivery_charge=Decimal("49.00"),
            discount=14,
            stock=8,
            rating=4.6,
            num_reviews=120,
        )

        self.spigen_case = Product.objects.create(
            name="Spigen Tough Armor Case for iPhone 15 Pro",
            brand="Spigen",
            category="Accessories",
            description="Dual layer protective cover for iPhone.",
            original_price=2999.0,
            offer_price=1499.0,
            delivery_charge=Decimal("49.00"),
            discount=50,
            stock=25,
            rating=4.5,
            num_reviews=40,
        )

        self.samsung_s24 = Product.objects.create(
            name="Samsung Galaxy S24 Ultra",
            brand="Samsung",
            category="Smartphones",
            description="Flagship Android phone with S-Pen.",
            original_price=129999.0,
            offer_price=119999.0,
            delivery_charge=Decimal("49.00"),
            discount=8,
            stock=5,
            rating=4.9,
            num_reviews=95,
        )

        self.boat_earbuds = Product.objects.create(
            name="boAt Airdopes 141",
            brand="boAt",
            category="Earbuds",
            description="Wireless Bluetooth earbuds with long battery.",
            original_price=4490.0,
            offer_price=1299.0,
            delivery_charge=Decimal("0.00"),
            discount=71,
            stock=30,
            rating=4.3,
            num_reviews=210,
        )

    def test_exact_product_name_ranking(self):
        res = self.client.get("/api/products/?search=iPhone 15")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("success"))
        products = data.get("products", [])
        self.assertTrue(len(products) >= 2)
        self.assertEqual(products[0]["_id"], self.iphone15._id)
        self.assertEqual(products[0]["name"], "iPhone 15")

    def test_prefix_ranking(self):
        res = self.client.get("/api/products/?search=iPhone")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        names = [p["name"] for p in data.get("products", [])]
        self.assertIn("iPhone 15", names[:2])
        self.assertIn("iPhone 14", names[:2])
        spigen_index = names.index("Spigen Tough Armor Case for iPhone 15 Pro")
        self.assertGreater(spigen_index, names.index("iPhone 15"))

    def test_partial_name_matching(self):
        res = self.client.get("/api/products/?search=S24 Ultra")
        self.assertEqual(res.status_code, 200)
        products = res.json().get("products", [])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["name"], "Samsung Galaxy S24 Ultra")

    def test_brand_and_category_matching(self):
        res = self.client.get("/api/products/?search=Earbuds")
        self.assertEqual(res.status_code, 200)
        products = res.json().get("products", [])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["name"], "boAt Airdopes 141")

        res_brand = self.client.get("/api/products/?search=boAt")
        self.assertEqual(res_brand.status_code, 200)
        brand_products = res_brand.json().get("products", [])
        self.assertEqual(len(brand_products), 1)
        self.assertEqual(brand_products[0]["brand"], "boAt")

    def test_empty_search_behaviour(self):
        res = self.client.get("/api/products/?search=")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("total"), 5)
        self.assertEqual(len(data.get("products", [])), 5)


class ProductImageModelAndAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            email="admin@akmobiles.com",
            password="adminpassword123",
            name="Admin User",
            phone="9000000001",
            role="admin",
        )
        self.user = User.objects.create_user(
            email="customer@akmobiles.com",
            password="customerpassword123",
            name="Customer User",
            phone="9000000002",
        )
        self.product = Product.objects.create(
            name="OnePlus 12",
            brand="OnePlus",
            category="Smartphones",
            description="Flagship smartphone",
            original_price=69999.0,
            offer_price=64999.0,
            delivery_charge=Decimal("49.00"),
            stock=15,
        )

    def test_first_image_becomes_primary_automatically(self):
        img1 = ProductImage.objects.create(
            product=self.product,
            url="https://blob.vercel-storage.com/products/img1.jpg",
            sort_order=0,
            is_primary=True,
        )
        img2 = ProductImage.objects.create(
            product=self.product,
            url="https://blob.vercel-storage.com/products/img2.jpg",
            sort_order=1,
            is_primary=False,
        )

        self.assertEqual(self.product.primary_image._id, img1._id)
        self.assertTrue(img1.is_primary)
        self.assertFalse(img2.is_primary)

    def test_unique_primary_image_constraint(self):
        ProductImage.objects.create(
            product=self.product,
            url="https://blob.vercel-storage.com/products/img1.jpg",
            sort_order=0,
            is_primary=True,
        )
        # Attempting to create a second is_primary=True image directly should violate constraint
        with self.assertRaises(IntegrityError):
            ProductImage.objects.create(
                product=self.product,
                url="https://blob.vercel-storage.com/products/img2.jpg",
                sort_order=1,
                is_primary=True,
            )

    def test_serializer_primary_and_images_ordering(self):
        img1 = ProductImage.objects.create(
            product=self.product,
            url="https://blob.vercel-storage.com/products/front.jpg",
            alt_text="Front view",
            sort_order=0,
            is_primary=True,
        )
        img2 = ProductImage.objects.create(
            product=self.product,
            url="https://blob.vercel-storage.com/products/back.jpg",
            alt_text="Back view",
            sort_order=1,
            is_primary=False,
        )

        serializer = ProductSerializer(self.product)
        data = serializer.data

        self.assertIn("primaryImage", data)
        self.assertEqual(data["primaryImage"]["url"], img1.url)
        self.assertEqual(data["primaryImage"]["isPrimary"], True)

        self.assertEqual(len(data["images"]), 2)
        self.assertEqual(data["images"][0]["url"], img1.url)
        self.assertEqual(data["images"][1]["url"], img2.url)
        self.assertEqual(data["deliveryCharge"], "49.00")

    def test_set_primary_image_endpoint(self):
        self.client.force_authenticate(user=self.admin)
        img1 = ProductImage.objects.create(
            product=self.product,
            url="https://blob.vercel-storage.com/products/img1.jpg",
            sort_order=0,
            is_primary=True,
        )
        img2 = ProductImage.objects.create(
            product=self.product,
            url="https://blob.vercel-storage.com/products/img2.jpg",
            sort_order=1,
            is_primary=False,
        )

        res = self.client.put(f"/api/products/{self.product._id}/images/{img2._id}/primary")
        self.assertEqual(res.status_code, 200)

        img1.refresh_from_db()
        img2.refresh_from_db()

        self.assertTrue(img2.is_primary)
        self.assertEqual(img2.sort_order, 0)
        self.assertFalse(img1.is_primary)
        self.assertEqual(img1.sort_order, 1)

    def test_delete_primary_image_promotes_next_image(self):
        self.client.force_authenticate(user=self.admin)
        img1 = ProductImage.objects.create(
            product=self.product,
            url="https://blob.vercel-storage.com/products/img1.jpg",
            sort_order=0,
            is_primary=True,
        )
        img2 = ProductImage.objects.create(
            product=self.product,
            url="https://blob.vercel-storage.com/products/img2.jpg",
            sort_order=1,
            is_primary=False,
        )

        res = self.client.delete(f"/api/products/{self.product._id}/images/{img1._id}")
        self.assertEqual(res.status_code, 200)

        img2.refresh_from_db()
        self.assertTrue(img2.is_primary)
        self.assertEqual(img2.sort_order, 0)
        self.assertEqual(self.product.product_images.count(), 1)

    def test_reorder_images_endpoint(self):
        self.client.force_authenticate(user=self.admin)
        img1 = ProductImage.objects.create(
            product=self.product,
            url="https://blob.vercel-storage.com/products/img1.jpg",
            sort_order=0,
            is_primary=True,
        )
        img2 = ProductImage.objects.create(
            product=self.product,
            url="https://blob.vercel-storage.com/products/img2.jpg",
            sort_order=1,
            is_primary=False,
        )

        res = self.client.put(
            f"/api/products/{self.product._id}/images/reorder",
            {"imageIds": [img2._id, img1._id]},
            format="json",
        )
        self.assertEqual(res.status_code, 200)

        img1.refresh_from_db()
        img2.refresh_from_db()

        self.assertEqual(img2.sort_order, 0)
        self.assertTrue(img2.is_primary)
        self.assertEqual(img1.sort_order, 1)
        self.assertFalse(img1.is_primary)


class ProductUploadAndSecurityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            email="admin_upload@akmobiles.com",
            password="adminpassword123",
            name="Admin User",
            phone="9000000010",
            role="admin",
        )
        self.customer = User.objects.create_user(
            email="customer_upload@akmobiles.com",
            password="customerpassword123",
            name="Customer User",
            phone="9000000011",
        )

    def _create_valid_image_bytes(self, fmt="JPEG", size=(100, 100)):
        import io
        from PIL import Image
        buf = io.BytesIO()
        img = Image.new("RGB", size, color="blue")
        img.save(buf, format=fmt)
        return buf.getvalue()

    def test_unauthenticated_upload_rejected(self):
        jpeg_data = self._create_valid_image_bytes("JPEG")
        file = SimpleUploadedFile("test.jpg", jpeg_data, content_type="image/jpeg")
        res = self.client.post("/api/upload", {"image": file}, format="multipart")
        self.assertEqual(res.status_code, 401)

    def test_non_staff_upload_rejected(self):
        self.client.force_authenticate(user=self.customer)
        jpeg_data = self._create_valid_image_bytes("JPEG")
        file = SimpleUploadedFile("test.jpg", jpeg_data, content_type="image/jpeg")
        res = self.client.post("/api/upload", {"image": file}, format="multipart")
        self.assertEqual(res.status_code, 403)

    def test_invalid_mime_and_extension_rejected(self):
        self.client.force_authenticate(user=self.admin)
        fake_file = SimpleUploadedFile("virus.jpg", b"MZ\x90\x00\x03\x00\x00\x00", content_type="application/x-msdownload")
        res = self.client.post("/api/upload", {"image": fake_file}, format="multipart")
        self.assertEqual(res.status_code, 400)

    def test_truncated_and_corrupt_image_rejected(self):
        self.client.force_authenticate(user=self.admin)
        # Header is JPEG magic bytes, but remainder is truncated corrupt data
        truncated_data = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01corrupt_truncated_bytes"
        truncated_file = SimpleUploadedFile("corrupt.jpg", truncated_data, content_type="image/jpeg")
        res = self.client.post("/api/upload", {"image": truncated_file}, format="multipart")
        self.assertEqual(res.status_code, 400)

    def test_svg_and_html_rejected(self):
        self.client.force_authenticate(user=self.admin)
        svg_file = SimpleUploadedFile("test.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", content_type="image/svg+xml")
        res = self.client.post("/api/upload", {"image": svg_file}, format="multipart")
        self.assertEqual(res.status_code, 400)

    def test_oversized_file_rejected(self):
        self.client.force_authenticate(user=self.admin)
        oversized = SimpleUploadedFile("large.jpg", b"\xff\xd8\xff" + b"0" * (6 * 1024 * 1024), content_type="image/jpeg")
        res = self.client.post("/api/upload", {"image": oversized}, format="multipart")
        self.assertEqual(res.status_code, 400)

    def test_upload_session_creation_and_staging(self):
        self.client.force_authenticate(user=self.admin)
        res_session = self.client.post("/api/products/upload-session", {}, format="json")
        self.assertEqual(res_session.status_code, 201)
        token = res_session.json()["token"]

        jpeg_data = self._create_valid_image_bytes("JPEG", size=(250, 250))
        file = SimpleUploadedFile("valid.jpg", jpeg_data, content_type="image/jpeg")

        with patch("products.views.put_to_vercel_blob") as mock_put:
            mock_put.return_value = {
                "url": f"https://blob.vercel-storage.com/products/{token}/valid.jpg",
                "storage_key": f"products/{token}/valid.jpg",
            }
            res_stage = self.client.post(
                f"/api/products/upload-session/{token}/stage",
                {"image": file},
                format="multipart",
            )
            self.assertEqual(res_stage.status_code, 201)
            self.assertTrue(res_stage.json()["success"])
            item_id = res_stage.json()["item"]["id"]

            # Check staged item exists
            self.assertTrue(ProductUploadItem.objects.filter(_id=item_id, session__token=token).exists())

    def test_authorize_upload_creates_staging_key(self):
        self.client.force_authenticate(user=self.admin)
        res_session = self.client.post("/api/products/upload-session", {}, format="json")
        self.assertEqual(res_session.status_code, 201)
        token = res_session.json()["token"]

        res_auth = self.client.post(
            f"/api/products/upload-session/{token}/authorize-upload",
            {"fileName": "iphone.jpg", "contentType": "image/jpeg", "fileSize": 102400},
            format="json",
        )
        self.assertEqual(res_auth.status_code, 201)
        data = res_auth.json()
        self.assertTrue(data.get("success"))
        item = data.get("item")
        self.assertTrue(item["storageKey"].startswith(f"products/staging/{token}/"))
        self.assertTrue(item["storageKey"].endswith(".jpg"))
        self.assertEqual(item["status"], "uploading")
        self.assertIn("uploadUrl", item)

    def test_finalize_upload_promotes_staged_blob_to_permanent(self):
        self.client.force_authenticate(user=self.admin)
        res_session = self.client.post("/api/products/upload-session", {}, format="json")
        token = res_session.json()["token"]

        # 1. Authorize
        res_auth = self.client.post(
            f"/api/products/upload-session/{token}/authorize-upload",
            {"fileName": "sample.png", "contentType": "image/png", "fileSize": 20480},
            format="json",
        )
        self.assertEqual(res_auth.status_code, 201)
        item_id = res_auth.json()["item"]["id"]

        # 2. Mock Blob storage fetch and put to simulate Vercel Blob workflow
        png_data = self._create_valid_image_bytes("PNG", size=(300, 300))
        with patch("products.views.finalize_staged_upload_to_permanent") as mock_finalize:
            mock_finalize.return_value = {
                "url": f"https://mock.blob.vercel-storage.com/products/{token}/{item_id}.png",
                "storage_key": f"products/{token}/{item_id}.png",
                "format": "PNG",
                "content_type": "image/png",
                "file_size": len(png_data),
                "width": 300,
                "height": 300,
            }

            res_final = self.client.post(
                f"/api/products/upload-session/{token}/finalize-upload",
                {"itemId": item_id, "fileName": "sample.png"},
                format="json",
            )
            self.assertEqual(res_final.status_code, 200)
            data = res_final.json()
            self.assertTrue(data.get("success"))
            self.assertEqual(data["item"]["url"], f"https://mock.blob.vercel-storage.com/products/{token}/{item_id}.png")

            # Check DB updated
            db_item = ProductUploadItem.objects.get(_id=item_id)
            self.assertEqual(db_item.url, f"https://mock.blob.vercel-storage.com/products/{token}/{item_id}.png")
            self.assertEqual(db_item.storage_key, f"products/{token}/{item_id}.png")

    def test_finalize_upload_normalizes_renamed_file_extension(self):
        """A JPEG file incorrectly named 'ajay-photo.png' must be finalized as canonical '.jpg'."""
        self.client.force_authenticate(user=self.admin)
        res_session = self.client.post("/api/products/upload-session", {}, format="json")
        token = res_session.json()["token"]

        res_auth = self.client.post(
            f"/api/products/upload-session/{token}/authorize-upload",
            {"fileName": "ajay-photo.png", "contentType": "image/png", "fileSize": 50000},
            format="json",
        )
        self.assertEqual(res_auth.status_code, 201)
        item_id = res_auth.json()["item"]["id"]

        with patch("products.views.finalize_staged_upload_to_permanent") as mock_finalize:
            mock_finalize.return_value = {
                "url": f"https://mock.blob.vercel-storage.com/products/{token}/{item_id}.jpg",
                "storage_key": f"products/{token}/{item_id}.jpg",
                "format": "JPEG",
                "content_type": "image/jpeg",
                "file_size": 50000,
                "width": 800,
                "height": 600,
            }

            res_final = self.client.post(
                f"/api/products/upload-session/{token}/finalize-upload",
                {"itemId": item_id, "fileName": "ajay-photo.png"},
                format="json",
            )
            self.assertEqual(res_final.status_code, 200)
            data = res_final.json()
            self.assertTrue(data["item"]["url"].endswith(".jpg"))

    def test_finalize_upload_idempotent_replay(self):
        """Calling finalize on an already-ready item returns 200 with existing metadata without duplicating."""
        self.client.force_authenticate(user=self.admin)
        session = ProductUploadSession.objects.create(
            user=self.admin,
            expires_at=timezone.now() + timezone.timedelta(hours=1),
            status="active",
        )
        ready_url = f"https://blob.vercel-storage.com/products/{session.token}/ready-item.jpg"
        item = ProductUploadItem.objects.create(
            session=session,
            url=ready_url,
            storage_key=f"products/{session.token}/ready-item.jpg",
            is_committed=False,
        )

        res = self.client.post(
            f"/api/products/upload-session/{session.token}/finalize-upload",
            {"itemId": str(item._id), "fileName": "ready-item.jpg"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["item"]["url"], ready_url)

    def test_finalize_upload_expired_session_returns_409(self):
        self.client.force_authenticate(user=self.admin)
        session = ProductUploadSession.objects.create(
            user=self.admin,
            expires_at=timezone.now() - timezone.timedelta(minutes=5),
            status="active",
        )
        item = ProductUploadItem.objects.create(
            session=session,
            url=f"https://blob.vercel-storage.com/products/staging/{session.token}/item.upload",
            storage_key=f"products/staging/{session.token}/item.upload",
            is_committed=False,
        )

        res = self.client.post(
            f"/api/products/upload-session/{session.token}/finalize-upload",
            {"itemId": str(item._id), "fileName": "test.jpg"},
            format="json",
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json().get("code"), "SESSION_EXPIRED")

    def test_finalize_upload_corrupt_image_returns_400_and_deletes_item(self):
        from common.storage import StorageValidationError
        self.client.force_authenticate(user=self.admin)
        session = ProductUploadSession.objects.create(
            user=self.admin,
            expires_at=timezone.now() + timezone.timedelta(hours=1),
            status="active",
        )
        item = ProductUploadItem.objects.create(
            session=session,
            url=f"https://blob.vercel-storage.com/products/staging/{session.token}/corrupt.upload",
            storage_key=f"products/staging/{session.token}/corrupt.upload",
            is_committed=False,
        )

        with patch("products.views.finalize_staged_upload_to_permanent", side_effect=StorageValidationError("File content does not match allowed image formats.")):
            with patch("products.views.delete_from_storage_safe") as mock_del:
                res = self.client.post(
                    f"/api/products/upload-session/{session.token}/finalize-upload",
                    {"itemId": str(item._id), "fileName": "corrupt.jpg"},
                    format="json",
                )
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.json().get("code"), "INVALID_IMAGE")
                mock_del.assert_called_once()
                # Verify item removed from DB
                self.assertFalse(ProductUploadItem.objects.filter(_id=item._id).exists())

    def test_authorize_upload_rejects_sixth_image(self):
        """Authorizing a 6th image when 5 staged items exist returns 400 MAX_IMAGES_EXCEEDED."""
        self.client.force_authenticate(user=self.admin)
        res_session = self.client.post("/api/products/upload-session", {}, format="json")
        token = res_session.json()["token"]
        session = ProductUploadSession.objects.get(token=token)

        # Create 5 uncommitted staged items
        for i in range(5):
            ProductUploadItem.objects.create(
                session=session,
                url=f"https://blob.vercel-storage.com/products/staging/{token}/item_{i}.upload",
                storage_key=f"products/staging/{token}/item_{i}.upload",
                is_committed=False,
            )

        res_sixth = self.client.post(
            f"/api/products/upload-session/{token}/authorize-upload",
            {"fileName": "sixth.jpg", "contentType": "image/jpeg", "fileSize": 102400},
            format="json",
        )
        self.assertEqual(res_sixth.status_code, 400)
        self.assertEqual(res_sixth.json().get("code"), "MAX_IMAGES_EXCEEDED")

    def test_create_product_rejects_more_than_five_images(self):
        self.client.force_authenticate(user=self.admin)
        images = [{"url": f"https://blob.vercel-storage.com/p/img_{i}.jpg", "sortOrder": i} for i in range(6)]
        payload = {
            "name": "Excess Image Phone",
            "brand": "Apple",
            "category": "Smartphones",
            "description": "Phone description",
            "originalPrice": 80000,
            "offerPrice": 75000,
            "deliveryCharge": "49.00",
            "stock": 10,
            "images": images,
        }
        res = self.client.post("/api/products", payload, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json().get("code"), "MAX_IMAGES_EXCEEDED")

    def test_update_product_rejects_more_than_five_images(self):
        self.client.force_authenticate(user=self.admin)
        product = Product.objects.create(
            name="Existing Phone",
            brand="Samsung",
            category="Smartphones",
            description="Test phone",
            original_price=50000,
            offer_price=45000,
            stock=5,
        )
        images = [{"url": f"https://blob.vercel-storage.com/p/img_{i}.jpg", "sortOrder": i} for i in range(6)]
        res = self.client.put(f"/api/products/{product._id}", {"images": images}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json().get("code"), "MAX_IMAGES_EXCEEDED")

    def test_add_product_image_rejects_sixth_image(self):
        self.client.force_authenticate(user=self.admin)
        product = Product.objects.create(
            name="Max Image Phone",
            brand="Apple",
            category="Smartphones",
            description="Test phone",
            original_price=90000,
            offer_price=85000,
            stock=5,
        )
        for i in range(5):
            ProductImage.objects.create(
                product=product,
                url=f"https://blob.vercel-storage.com/p/img_{i}.jpg",
                sort_order=i,
                is_primary=(i == 0),
            )

        res = self.client.post(
            f"/api/products/{product._id}/images",
            {"url": "https://blob.vercel-storage.com/p/img_sixth.jpg"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json().get("code"), "MAX_IMAGES_EXCEEDED")

    def test_reorder_product_images_rejects_duplicates_and_excess(self):
        self.client.force_authenticate(user=self.admin)
        product = Product.objects.create(
            name="Reorder Phone",
            brand="Apple",
            category="Smartphones",
            description="Test phone",
            original_price=90000,
            offer_price=85000,
            stock=5,
        )
        img1 = ProductImage.objects.create(product=product, url="https://blob.vercel-storage.com/p/1.jpg", sort_order=0, is_primary=True)
        img2 = ProductImage.objects.create(product=product, url="https://blob.vercel-storage.com/p/2.jpg", sort_order=1, is_primary=False)

        # Duplicate ID check
        res_dup = self.client.put(f"/api/products/{product._id}/images/reorder", {"imageIds": [img1._id, img1._id]}, format="json")
        self.assertEqual(res_dup.status_code, 400)

        # Excess IDs check (>5)
        res_excess = self.client.put(f"/api/products/{product._id}/images/reorder", {"imageIds": ["1", "2", "3", "4", "5", "6"]}, format="json")
        self.assertEqual(res_excess.status_code, 400)
        self.assertEqual(res_excess.json().get("code"), "MAX_IMAGES_EXCEEDED")

    def test_cleanup_orphaned_blobs_dry_run(self):
        session = ProductUploadSession.objects.create(
            user=self.admin,
            expires_at=timezone.now() - timezone.timedelta(hours=2),
            status="active",
        )
        item = ProductUploadItem.objects.create(
            session=session,
            url="https://blob.vercel-storage.com/products/test_orphan.jpg",
            storage_key="products/test_orphan.jpg",
            is_committed=False,
        )

        call_command("cleanup_orphaned_blobs", dry_run=True)
        self.assertTrue(ProductUploadItem.objects.filter(_id=item._id).exists())
