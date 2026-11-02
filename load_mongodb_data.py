#!/usr/bin/env python3
"""Validate and batch-import the generated JSON array into test.orders.

Single-writer contract: manually control this one-time import. Do not run concurrent
loader instances or write to test.orders while an import is in progress.
"""

import argparse
import io
import json
import math
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path


DEFAULT_DATASET = Path("dataset/orders.json")
DATABASE_NAME = "test"
COLLECTION_NAME = "orders"
BATCH_SIZE = 500
MAX_RECORD_CHARS = 8 * 1024 * 1024
TOP_LEVEL_FIELDS = {"order_id", "timestamp", "customer", "items", "payment", "metadata"}
CUSTOMER_FIELDS = {"id", "tier", "segment", "geo"}
GEO_FIELDS = {"country", "state", "city", "zip", "coordinates"}
ITEM_FIELDS = {"sku", "category", "brand", "qty", "price", "tax_pct", "discounts"}
PAYMENT_FIELDS = {"method", "status", "transaction_id", "gateway_latency_ms", "totals"}
TOTAL_FIELDS = {"subtotal", "discount", "tax", "total"}
METADATA_FIELDS = {"channel", "user_agent", "session_id", "flags"}


class DatasetError(ValueError):
    """Raised when the input is not a valid generated-order JSON array."""


class JsonArrayStream:
    """Incrementally decode one top-level JSON array from a text stream."""

    def __init__(self, stream, chunk_size=64 * 1024):
        self.stream = stream
        self.chunk_size = chunk_size
        self.decoder = json.JSONDecoder(parse_constant=self._reject_non_json_constant)
        self.buffer = ""
        self.position = 0
        self.eof = False

    def _fill(self):
        chunk = self.stream.read(self.chunk_size)
        if chunk == "":
            self.eof = True
            return
        self.buffer += chunk

    @staticmethod
    def _reject_non_json_constant(value):
        raise DatasetError(f"non-standard JSON constant is not allowed: {value}")

    def _skip_whitespace(self):
        while True:
            while self.position < len(self.buffer) and self.buffer[self.position].isspace():
                self.position += 1
            if self.position < len(self.buffer) or self.eof:
                return
            self.buffer = ""
            self.position = 0
            self._fill()

    def _next_character(self):
        self._skip_whitespace()
        if self.position >= len(self.buffer):
            return None
        return self.buffer[self.position]

    def _decode_value(self):
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
                self.position = end
                return value
            except json.JSONDecodeError as exc:
                if self.eof:
                    raise DatasetError(f"invalid JSON value near character {self.position}") from exc
                if len(self.buffer) - self.position > MAX_RECORD_CHARS:
                    raise DatasetError("a JSON record exceeds the bounded parser size limit") from exc
                if self.position:
                    self.buffer = self.buffer[self.position :]
                    self.position = 0
                self._fill()

    def __iter__(self):
        if self._next_character() != "[":
            raise DatasetError("dataset root must be a JSON array")
        self.position += 1
        first = True
        while True:
            character = self._next_character()
            if character is None:
                raise DatasetError("dataset array is not closed")
            if character == "]":
                if not first:
                    self.position += 1
                    if self._next_character() == ",":
                        raise DatasetError("dataset array contains a trailing comma")
                else:
                    self.position += 1
                if self._next_character() is not None:
                    raise DatasetError("unexpected content after dataset array")
                return
            if not first:
                if character != ",":
                    raise DatasetError("expected comma between array elements")
                self.position += 1
                character = self._next_character()
                if character in (None, "]", ","):
                    raise DatasetError("dataset array contains a missing element")
            yield self._decode_value()
            first = False
            if self.position > self.chunk_size:
                self.buffer = self.buffer[self.position :]
                self.position = 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, default=DEFAULT_DATASET,
        help="existing generator JSON array (never generated or modified)",
    )
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error(f"dataset file does not exist: {args.input}")
    return args


def require_keys(value, expected, path):
    if not isinstance(value, dict) or set(value) != expected:
        raise DatasetError(f"{path} must contain exactly the specified fields")


def require_number(value, path):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DatasetError(f"{path} must be numeric")


def validate_document(document, index):
    prefix = f"record {index}"
    require_keys(document, TOP_LEVEL_FIELDS, prefix)
    try:
        uuid.UUID(document["order_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise DatasetError(f"{prefix}.order_id must be a UUID") from exc
    if not isinstance(document["timestamp"], str):
        raise DatasetError(f"{prefix}.timestamp must be an ISO-8601 string")
    try:
        datetime.fromisoformat(document["timestamp"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatasetError(f"{prefix}.timestamp must be an ISO-8601 string") from exc

    customer = document["customer"]
    require_keys(customer, CUSTOMER_FIELDS, f"{prefix}.customer")
    if not all(isinstance(customer[field], str) for field in ("id", "tier", "segment")):
        raise DatasetError(f"{prefix}.customer identity and labels must be strings")
    try:
        uuid.UUID(customer["id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise DatasetError(f"{prefix}.customer.id must be a UUID") from exc
    geo = customer["geo"]
    require_keys(geo, GEO_FIELDS, f"{prefix}.customer.geo")
    if not all(isinstance(geo[field], str) for field in ("country", "state", "city", "zip")):
        raise DatasetError(f"{prefix}.customer.geo labels must be strings")
    coordinates = geo["coordinates"]
    if not isinstance(coordinates, list) or len(coordinates) != 2:
        raise DatasetError(f"{prefix}.customer.geo.coordinates must be a two-number array")
    for coordinate in coordinates:
        require_number(coordinate, f"{prefix}.customer.geo.coordinates")

    items = document["items"]
    if not isinstance(items, list) or len(items) != 3:
        raise DatasetError(f"{prefix}.items must contain exactly three entries")
    for item_index, item in enumerate(items):
        item_path = f"{prefix}.items[{item_index}]"
        require_keys(item, ITEM_FIELDS, item_path)
        if not all(isinstance(item[field], str) for field in ("sku", "category", "brand")):
            raise DatasetError(f"{item_path} identifiers and labels must be strings")
        if isinstance(item["qty"], bool) or not isinstance(item["qty"], int) or item["qty"] <= 0:
            raise DatasetError(f"{item_path}.qty must be a positive integer")
        require_number(item["price"], f"{item_path}.price")
        require_number(item["tax_pct"], f"{item_path}.tax_pct")
        if not isinstance(item["discounts"], list):
            raise DatasetError(f"{item_path}.discounts must be an array")

    payment = document["payment"]
    require_keys(payment, PAYMENT_FIELDS, f"{prefix}.payment")
    if not all(isinstance(payment[field], str) for field in ("method", "status", "transaction_id")):
        raise DatasetError(f"{prefix}.payment labels and transaction_id must be strings")
    try:
        uuid.UUID(payment["transaction_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise DatasetError(f"{prefix}.payment.transaction_id must be a UUID") from exc
    latency = payment["gateway_latency_ms"]
    if isinstance(latency, bool) or not isinstance(latency, int) or latency < 0:
        raise DatasetError(f"{prefix}.payment.gateway_latency_ms must be a nonnegative integer")
    totals = payment["totals"]
    require_keys(totals, TOTAL_FIELDS, f"{prefix}.payment.totals")
    for field in TOTAL_FIELDS:
        require_number(totals[field], f"{prefix}.payment.totals.{field}")
    expected_total = round(totals["subtotal"] - totals["discount"] + totals["tax"], 2)
    if round(totals["total"], 2) != expected_total:
        raise DatasetError(f"{prefix}.payment.totals.total does not match subtotal - discount + tax")

    metadata = document["metadata"]
    require_keys(metadata, METADATA_FIELDS, f"{prefix}.metadata")
    if not all(isinstance(metadata[field], str) for field in ("channel", "user_agent", "session_id")):
        raise DatasetError(f"{prefix}.metadata labels and session_id must be strings")
    try:
        uuid.UUID(metadata["session_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise DatasetError(f"{prefix}.metadata.session_id must be a UUID") from exc
    if not isinstance(metadata["flags"], list):
        raise DatasetError(f"{prefix}.metadata.flags must be an array")


def validate_dataset(stream):
    count = 0
    stream.seek(0)
    for count, document in enumerate(JsonArrayStream(stream), start=1):
        validate_document(document, count)
    if count == 0:
        raise DatasetError("dataset array is empty")
    return count


def import_batches(stream, collection):
    batch = []
    imported = 0
    stream.seek(0)
    for index, document in enumerate(JsonArrayStream(stream), start=1):
        batch.append(document)
        if len(batch) >= BATCH_SIZE:
            collection.insert_many(batch, ordered=True)
            imported += len(batch)
            batch.clear()
            print(f"Imported {imported} documents", flush=True)
    if batch:
        collection.insert_many(batch, ordered=True)
        imported += len(batch)
    return imported


def main():
    args = parse_args()
    try:
        with tempfile.TemporaryFile(mode="w+b") as snapshot:
            with args.input.open("rb") as source:
                shutil.copyfileobj(source, snapshot, length=64 * 1024)
            snapshot.seek(0)
            with io.TextIOWrapper(snapshot, encoding="utf-8", newline="") as stream:
                document_count = validate_dataset(stream)
                stream.seek(0)

                try:
                    from pymongo import MongoClient
                except ImportError:
                    print("Missing approved dependency: pymongo", file=sys.stderr)
                    return 1

                uri = os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017")
                try:
                    with MongoClient(uri, appname="database-import", connectTimeoutMS=10_000) as client:
                        collection = client[DATABASE_NAME][COLLECTION_NAME]
                        if collection.count_documents({}, limit=1) != 0:
                            print("Refusing import: test.orders is not empty.", file=sys.stderr)
                            return 1
                        try:
                            imported = import_batches(stream, collection)
                        except Exception as exc:
                            print("Import failed; partial records may remain and were not removed.", file=sys.stderr)
                            print(f"Failure type: {type(exc).__name__}", file=sys.stderr)
                            return 1
                except Exception as exc:
                    print(f"MongoDB import failed: {type(exc).__name__}", file=sys.stderr)
                    return 1
    except (DatasetError, OSError, UnicodeError) as exc:
        print(f"Dataset validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"Imported {imported} documents into {DATABASE_NAME}.{COLLECTION_NAME}.")
    if imported != document_count:
        print(f"Warning: validated {document_count} input records but imported {imported}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
