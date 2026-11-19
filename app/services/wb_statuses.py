"""
WB assembly task statuses (wbStatus from POST /api/v3/orders/status) grouped by what they mean for an order.
"""

# Handed over and travelling to the buyer: the order is in delivery.
# Cross-border supplies: delivering_by_carrier (from 12.11.2026) replaces accepted_by_carrier and sent_to_carrier,
# which WB keeps returning for assembly tasks created before that date.
WB_IN_TRANSIT_STATUSES = (
    "waiting",
    "sorted",
    "ready_for_pickup",
    "delivering_by_carrier",
    "accepted_by_carrier",
    "sent_to_carrier",
)
