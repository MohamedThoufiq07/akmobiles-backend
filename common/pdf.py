"""
Pure-Python PDF generator for server-side invoice / receipt delivery.
Compatible with Vercel serverless functions without external binary dependencies.
"""
import io


def generate_invoice_pdf(order) -> bytes:
    """
    Generates a clean, valid PDF receipt for a confirmed order.
    """
    buffer = io.BytesIO()
    
    order_id = str(order._id)
    created_at = order.created_at.strftime("%d %B %Y") if order.created_at else "N/A"
    ship = order.shipping_address or {}
    customer_name = ship.get("name") or (order.user.name if hasattr(order, "user") and order.user else "Customer")
    phone = ship.get("phone", "")
    address_line = f"{ship.get('addressLine1', '')} {ship.get('addressLine2', '')}".strip()
    city_state = f"{ship.get('city', '')}, {ship.get('state', '')} {ship.get('postalCode', '')}".strip()
    
    items = order.order_items or []
    subtotal = f"Rs. {order.items_price:.2f}"
    tax = f"Rs. {order.tax_price:.2f}"
    shipping = "Free" if order.shipping_price == 0 else f"Rs. {order.shipping_price:.2f}"
    total = f"Rs. {order.total_price:.2f}"
    
    # Build text stream
    lines = [
        "AK MOBILES - PAYMENT RECEIPT",
        "========================================",
        f"Order ID: {order_id}",
        f"Date: {created_at}",
        f"Customer: {customer_name}",
        f"Phone: {phone}",
        f"Delivery: {address_line}",
        f"          {city_state}",
        "----------------------------------------",
        "ITEMS ORDERED:",
    ]
    for it in items:
        name = it.get("name", "Product")[:30]
        qty = it.get("quantity", 1)
        price = it.get("price", 0.0)
        lines.append(f" - {name} x {qty} @ Rs. {price:.2f}")
        
    lines.extend([
        "----------------------------------------",
        f"Subtotal:       {subtotal}",
        f"GST (Included): {tax}",
        f"Delivery Fee:   {shipping}",
        f"Total Paid:     {total}",
        "========================================",
        "Thank you for shopping with AK Mobiles!",
    ])
    
    text_content = "\n".join(lines)
    
    # Build valid standard PDF 1.4 document
    stream_content = "BT\n/F1 10 Tf\n14 TL\n40 760 Td\n"
    for line in lines:
        safe_line = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream_content += f"({safe_line}) '\n"
    stream_content += "ET\n"
    
    stream_bytes = stream_content.encode("latin1", "replace")
    stream_len = len(stream_bytes)
    
    pdf = bytearray()
    pdf.extend(b"%PDF-1.4\n")
    
    # 1 0 obj: Catalog
    o1 = len(pdf)
    pdf.extend(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    
    # 2 0 obj: Pages
    o2 = len(pdf)
    pdf.extend(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    
    # 3 0 obj: Page
    o3 = len(pdf)
    pdf.extend(b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n")
    
    # 4 0 obj: Font
    o4 = len(pdf)
    pdf.extend(b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>\nendobj\n")
    
    # 5 0 obj: Contents stream
    o5 = len(pdf)
    pdf.extend(f"5 0 obj\n<< /Length {stream_len} >>\nstream\n".encode("ascii"))
    pdf.extend(stream_bytes)
    pdf.extend(b"\nendstream\nendobj\n")
    
    # Cross-reference table
    xref_offset = len(pdf)
    pdf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    pdf.extend(f"{o1:010d} 00000 n \n".encode("ascii"))
    pdf.extend(f"{o2:010d} 00000 n \n".encode("ascii"))
    pdf.extend(f"{o3:010d} 00000 n \n".encode("ascii"))
    pdf.extend(f"{o4:010d} 00000 n \n".encode("ascii"))
    pdf.extend(f"{o5:010d} 00000 n \n".encode("ascii"))
    
    # Trailer
    pdf.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
    
    return bytes(pdf)
