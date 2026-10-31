db.orders.aggregate([
    { "$match": {
        "payment.status": "completed",
        "timestamp": { "$gte": "2026-01-01T00:00:00Z" }
    } },
    { "$sort": { "payment.totals.total": -1 } },
    { "$limit": 1000 },
    { "$project": {
        "order_id": 1, "customer.id": 1, "payment.totals.total": 1
    } }
], { allowDiskUse: false });
