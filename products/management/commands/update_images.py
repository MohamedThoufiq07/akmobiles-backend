"""
Replace product images with the curated, correct image URLs.

Usage:
    python manage.py update_images

Each listed product's `images` field is overwritten with a single
{ url, alt } entry. Products not in the map keep their existing images.
"""

from django.core.management.base import BaseCommand

from products.models import Product

# Exact DB product name -> correct image URL
IMAGE_MAP = {
    # ---- Apple iPhones ----
    "iPhone 15": "https://image.cdn.shpy.in/301826/1-1708520956671.jpeg?width=600&format=webp",
    "iPhone 15 Pro Max": "https://image.cdn.shpy.in/301826/1-1708521332360.jpeg?format=webp",
    "iPhone 14": "https://grest.in/cdn/shop/files/Frame_3_5.png?v=1775222416&width=3840",

    # ---- Samsung ----
    "Samsung Galaxy S24 Ultra": "https://pngdownload.io/wp-content/uploads/2024/02/Samsung-Galaxy-S24-Ultra-Titanium-Violet-Smartphone-transparent-PNG-image-jpg.webp",
    "Samsung Galaxy S24": "https://static.vecteezy.com/system/resources/previews/041/329/788/non_2x/samsung-galaxy-s24-ultra-titanium-blue-back-view-free-png.png",

    # ---- Vivo ----
    "Vivo T3 5G": "https://img-prd-pim.poorvika.com/cdn-cgi/image/width=500,height=500,quality=75/product/vivo-t3-5g-cosmic-blue-128gb-8gb-ram-front-back-view.png",
    "Vivo X100 Pro": "https://in-exstatic-vivofs.vivo.com/gdHFRinHEMrj3yPG/1702983248432/61ad5ee6e72682f52d8aa495e314c56c.png",
    "Vivo V30 Pro": "https://in-exstatic-vivofs.vivo.com/gdHFRinHEMrj3yPG/1709633883246/7e1e7e35082e2abf290ec7c423d4361f.png",

    # ---- Oppo ----
    "Oppo Find X7 Ultra": "https://cdn.beebom.com/mobile/oppo-find-x7-ultra/oppo-find-x7-ultra-back-and-front.png",
    "Oppo Reno 11 Pro": "https://www.giztop.com/media/catalog/product/cache/97cc1143d2e20f2b0c8ea91aaa12053c/o/p/oppo_reno_11-1_1_.png",

    # ---- Xiaomi / Redmi ----
    "Xiaomi 14": "https://i05.appmifile.com/886_item_uk/07/06/2024/f6882a3273c493e81ddafd8366010e8c.png",
    "Redmi 13C": "https://img-prd-pim.poorvika.com/prodvarval/Redmi-13-5g-orchid-pink-128gb-6gb-ram-Front-Back-View-Thumbnail.png",
    "Redmi Note 13 Pro+ 5G": "https://i03.appmifile.com/789_item_in/04/07/2024/291d6375bb3ce600675227b27a29ac3c.png",

    # ---- Realme ----
    "Realme GT 5 Pro": "https://fdn2.gsmarena.com/vv/bigpic/realme-gt5-pro.jpg",
    "Realme 12 Pro+ 5G": "https://img-prd-pim.poorvika.com/cdn-cgi/image/width=500,height=500,quality=75/product/realme-12-pro-5g-Navigator-beige-256gb-8gb-ram-front-back-view.png",

    # ---- Nothing ----
    "Nothing Phone (2)": "https://www.pngall.com/wp-content/uploads/13/Nothing-Phone-1-PNG-Photos.png",
    "Nothing Phone (2a)": "https://cdn.shopify.com/s/files/1/0585/2479/5086/products/black-1.png?v=1709369706",

    # ---- Nokia ----
    "Nokia G42 5G": "https://cdn.beebom.com/mobile/nokia-g42-5g3.png",

    # ---- Motorola ----
    "Motorola Edge 50 Pro": "https://motorolain.vtexassets.com/arquivos/ids/159178/motorola-edge-50-pro-PDP-ecomm-render-color5-5-.png?v=638614765175970000",
    "Moto G84 5G": "https://p3-ofp.static.pub//fes/cms/2025/07/04/2qxujwoenuvy55t3ornuxcs6sdgtqk899280.png",

    # ---- OnePlus ----
    "OnePlus 12": "https://image01-in.oneplus.net/media/202407/04/9052428d8c69bd8bb884c7913af5fa73.png",
    "OnePlus 12R": "https://oasis.opstatics.com/content/dam/oasis/page/2024/global/product/aston/aston_blue.png",

    # ---- Earbuds ----
    "Apple AirPods Pro (2nd Gen) USB-C": "https://www.sathya.store/img/product/xnmxLT0B28I9PDZW.png",
    "Sony WF-1000XM5 Noise Cancelling Earbuds": "https://media.tatacroma.com/Croma%20Assets/Entertainment/Wireless%20Earbuds/Images/301580_0_eu3o0d.png",
    "Samsung Galaxy Buds2 Pro": "https://png.pngtree.com/png-vector/20250220/ourmid/pngtree-pink-samsung-galaxy-buds-2-pro-wireless-bluetooth-earphones-clipart-illustration-png-image_15539537.png",
    "OnePlus Buds 3": "https://oasis.opstatics.com/content/dam/oasis/page/2024/global/product/euler/spec_blue.png",
    "boAt Airdopes 141": "https://www.boat-lifestyle.com/cdn/shop/files/AD141-FI_Grey01.png?v=1698391770",

    # ---- Chargers ----
    "Apple 20W USB-C Power Adapter": "https://inventstore.in/wp-content/uploads/2023/06/adapter-20w.png",
    "Samsung 25W USB-C Fast Charger": "https://media-ik.croma.com/prod/https://media.tatacroma.com/Croma%20Assets/Communication/Chargers%20and%20Batteries/Images/222115_0_u6y7vy.png",
    "Anker 511 Charger (Nano 3) 30W": "https://store.ooredoo.com.kw/media/catalog/product/cache/fe0a302af94e109db698e86ead4007fa/a/r/artboard_1_7.png",
    "OnePlus SUPERVOOC 80W Type-A Power Adapter": "https://image01-eu.oneplus.net/media/202503/03/4feb5923d1dfed07c14dfe30329681d8.png",
    "Spigen ArcStation 40W Dual USB C Charger": "https://incredideals.co/cdn/shop/files/619o71RvKDL__AC_SL1500_1cc8878b-bd65-41a7-9743-b861efce8b4c_jpg.webp?v=1737264381",

    # ---- Smart Watches ----
    "Apple Watch Series 9 GPS": "https://pngimg.com/uploads/apple_watch/apple_watch_PNG13.png",
    "Samsung Galaxy Watch 6 LTE": "https://www.myg.in/images/thumbnails/300/300/detailed/87/61fDRIfPQEL._SX679_-removebg-preview.png.png",
    "OnePlus Watch 2": "https://image01-in.oneplus.net/india-oneplus-statics-file/epb/202402/26/2JL5MkAYL9E27en1.png",
    "Noise ColorFit Pro 5 Max": "https://instamart-media-assets.swiggy.com/swiggy/image/upload/fl_lossy,f_auto,q_auto,h_600/NI_CATALOG/IMAGES/ciw/2026/2/20/ed44186a-11c6-4fbf-8fef-2b317f7f596f_K1WNTQ5QLR_MN_19022026.png",
    "boAt Wave Sigma Smartwatch": "https://www.boat-lifestyle.com/cdn/shop/files/WaveSigma-FI_Black01_600x.png?v=1692856673",

    # ---- Power Banks ----
    "Mi Power Bank 3i 20000mAh": "https://gadgetshieldz.com/cdn/shop/files/mi-3i-20000mah-power-bank-cosmic-orange-full.webp?v=1778740283&width=1080&width=1080",
    "Anker PowerCore 24K 140W Power Bank": "https://cdn.shopify.com/s/files/1/0917/4807/3750/files/A1289011-Anker_737_Power_Bank_PowerCore_24K_5_cbe40b31-37b4-4a09-8b6c-b5f7fce2f651.png?v=1739687842",
    "Ambrane Stylo 20K 20000mAh Power Bank": "https://img-prd-pim.poorvika.com/cdn-cgi/image/width=500,height=500,quality=75/product/ambrane-stylo-n20-22-5w-20000-mah-power-bank-Purple-Front-view.png",
    "URBN 10000mAh Ultra Compact Power Bank": "https://instamart-media-assets.swiggy.com/swiggy/image/upload/fl_lossy,f_auto,q_auto,h_600/NI_CATALOG/IMAGES/ciw/2026/2/20/108c1933-df4a-407e-b672-c30ddcd05880_MUHEXI81IT_MN_20022026.png",
    "Realme 10000mAh Power Bank 3": "https://m.media-amazon.com/images/I/71Hg2IZMj9L._AC_UF894,1000_QL80_.jpg",
}


class Command(BaseCommand):
    help = "Overwrite product images with curated correct URLs."

    def handle(self, *args, **options):
        updated = []
        missing = []

        for name, url in IMAGE_MAP.items():
            product = Product.objects.filter(name=name).first()
            if not product:
                missing.append(name)
                continue
            product.images = [{"url": url, "alt": name}]
            product.save(update_fields=["images"])
            updated.append(name)

        self.stdout.write(self.style.SUCCESS(f"Updated images for {len(updated)} products."))
        if missing:
            self.stdout.write(
                self.style.WARNING(
                    "No matching product found for: " + ", ".join(missing)
                )
            )
