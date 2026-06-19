"""
Product views — port of controllers/productController.js.

Endpoints (under /api/products/):
  GET    /              getProducts (filters, sort, search, pagination)
  GET    /featured      getFeaturedProducts
  GET    /top           getTopProducts
  GET    /<id>          getProductById
  GET    /<id>/related  getRelatedProducts
  POST   /<id>/reviews  createReview            (auth)
  POST   /              createProduct           (admin)
  PUT    /<id>          updateProduct           (admin)
  DELETE /<id>          deleteProduct           (admin)

Note: the Node version used a small in-memory cache for featured/top. We query
directly here — same output, just without the cache layer.
"""

from functools import reduce
from operator import or_

from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from common.permissions import IsAdmin, paginate_queryset
from .models import Product, Review
from .serializers import ProductSerializer

SORT_MAP = {
    "price_low": "offer_price",
    "price_high": "-offer_price",
    "popular": "-num_sold",
    "rating": "-rating",
    "newest": "-created_at",
}


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def products_root(request):
    """GET -> list (public). POST -> create product (admin only)."""
    if request.method == "GET":
        return _list_products(request)
    if not (request.user and request.user.is_authenticated and request.user.role == "admin"):
        return Response(
            {"success": False, "message": "Access denied. Admin privileges required."}, status=403
        )
    serializer = ProductSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"success": True, "product": serializer.data}, status=201)


def _list_products(request):
    q = request.query_params
    qs = Product.objects.all()

    brand = q.get("brand")
    if brand:
        qs = qs.filter(brand__in=brand.split(","))

    category = q.get("category")
    if category:
        qs = qs.filter(category=category)

    min_price, max_price = q.get("minPrice"), q.get("maxPrice")
    if min_price:
        qs = qs.filter(offer_price__gte=float(min_price))
    if max_price:
        qs = qs.filter(offer_price__lte=float(max_price))

    ram = q.get("ram")
    if ram:
        qs = qs.filter(reduce(or_, (Q(specifications__ram=v) for v in ram.split(","))))

    storage = q.get("storage")
    if storage:
        qs = qs.filter(reduce(or_, (Q(specifications__storage=v) for v in storage.split(","))))

    rating = q.get("rating")
    if rating:
        qs = qs.filter(rating__gte=float(rating))

    discount = q.get("discount")
    if discount:
        qs = qs.filter(discount__gte=float(discount))

    if q.get("flashSale") == "true":
        qs = qs.filter(flash_sale=True)

    search = q.get("search") or q.get("keyword")
    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(brand__icontains=search)
            | Q(description__icontains=search)
        )

    qs = qs.order_by(SORT_MAP.get(q.get("sort"), "-created_at"))

    items, page, pages, total = paginate_queryset(qs, q.get("page", 1), q.get("limit", 12))
    return Response({
        "success": True,
        "products": ProductSerializer(items, many=True).data,
        "page": page,
        "pages": pages,
        "total": total,
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def get_featured(request):
    products = Product.objects.filter(is_featured=True)[:8]
    return Response({"success": True, "products": ProductSerializer(products, many=True).data})


@api_view(["GET"])
@permission_classes([AllowAny])
def get_top(request):
    products = Product.objects.order_by("-rating")[:8]
    return Response({"success": True, "products": ProductSerializer(products, many=True).data})


@api_view(["GET"])
@permission_classes([AllowAny])
def get_related(request, product_id):
    product = Product.objects.filter(_id=product_id).first()
    if not product:
        return Response({"success": False, "message": "Product not found"}, status=404)
    related = (
        Product.objects.filter(Q(brand=product.brand) | Q(category=product.category))
        .exclude(_id=product._id)[:5]
    )
    return Response({"success": True, "products": ProductSerializer(related, many=True).data})


@api_view(["GET", "PUT", "DELETE"])
def product_detail(request, product_id):
    product = Product.objects.filter(_id=product_id).first()

    if request.method == "GET":
        if not product:
            return Response({"success": False, "message": "Product not found"}, status=404)
        return Response({"success": True, "product": ProductSerializer(product).data})

    # PUT / DELETE require admin
    if not (request.user and request.user.is_authenticated and request.user.role == "admin"):
        return Response(
            {"success": False, "message": "Access denied. Admin privileges required."}, status=403
        )
    if not product:
        return Response({"success": False, "message": "Product not found"}, status=404)

    if request.method == "DELETE":
        product.delete()
        return Response({"success": True, "message": "Product deleted"})

    serializer = ProductSerializer(product, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"success": True, "product": serializer.data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_review(request, product_id):
    product = Product.objects.filter(_id=product_id).first()
    if not product:
        return Response({"success": False, "message": "Product not found"}, status=404)

    if Review.objects.filter(product=product, user=request.user).exists():
        return Response(
            {"success": False, "message": "You have already reviewed this product."}, status=400
        )

    Review.objects.create(
        product=product,
        user=request.user,
        name=request.user.name,
        rating=int(request.data.get("rating")),
        comment=request.data.get("comment", ""),
    )
    product.recalculate_rating()
    return Response({"success": True, "message": "Review added"}, status=201)
