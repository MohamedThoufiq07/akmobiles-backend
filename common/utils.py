"""
Shared helpers.

generate_object_id() produces a 24-character hex string that looks exactly like a
MongoDB ObjectId. We use it as the primary key for every model so that the JSON
the React frontend receives has the same `_id` shape it had on the MERN stack
(e.g. "65f1c2a4e8b3a91d4c7f0021"). That means existing frontend URLs like
/products/:id and any cached data keep working with zero changes.
"""

import os
import time
import binascii


_counter = int.from_bytes(os.urandom(3), "big")


def generate_object_id() -> str:
    """Return a 24-char hex string mimicking a MongoDB ObjectId."""
    global _counter
    timestamp = int(time.time()).to_bytes(4, "big")
    random_part = os.urandom(5)
    _counter = (_counter + 1) & 0xFFFFFF
    counter = _counter.to_bytes(3, "big")
    return binascii.hexlify(timestamp + random_part + counter).decode()
