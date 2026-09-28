# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
# For license information, please see license.txt

"""Commerce handler pack for the Medusa wire.

Ordinary commerce, and the pack a site gets by default:

  Product  -> Item             (with the mandatory item_group / stock_uom)
  Order    -> Sales Order      (customer link + child line items)
  Invoice  -> Sales Invoice
  Customer -> Customer

Everything here is opt-in per site through `site_config.json`:

    "medusync_handler_packs": ["commerce"]

`hooks.py` names none of these doctypes; it binds one wildcard handler
which asks the registry for the hooks the configured packs declare below.
"""

from medusync.handlers import register_handler

# Doctype-aware upsert used by the mapped push path
# (see medusync.handlers.get_mapped_upsert).
MAPPED_UPSERT = "medusync.handlers.commerce.mapped.upsert_via_mapping"

# Document events this pack acts on. The core binds nothing for these
# doctypes; a site without this pack runs none of it.
#
#   Stock Ledger Entry / Sales Order  -> stock level, per mapped warehouse
#   Sales Order / Payment Entry       -> order source + what has been paid
#   Delivery Note / Shipment / Sales Invoice -> the post-order reverse path
#   Item Price / Item / Customer      -> price, MOQ and customer group
OUTBOUND_HOOKS = {
	"Stock Ledger Entry": {
		"after_insert": "medusync.handlers.commerce.inventory.on_sle",
	},
	# Two callables, both wanted: one re-pushes the stock the order
	# reserves, the other reports where the order came from and what has
	# been paid. A single dotted path here would silently drop one.
	"Sales Order": {
		"on_submit": [
			"medusync.handlers.commerce.inventory.on_sales_order",
			"medusync.order_meta.on_sales_order",
		],
		"on_cancel": [
			"medusync.handlers.commerce.inventory.on_sales_order",
			"medusync.order_meta.on_sales_order",
		],
		"on_update_after_submit": [
			"medusync.handlers.commerce.inventory.on_sales_order",
			"medusync.order_meta.on_sales_order",
		],
	},
	# Money received by bank transfer, cheque or UPI never touches Medusa,
	# so the storefront only learns of it from here.
	"Payment Entry": {
		"on_submit": "medusync.order_meta.on_payment_entry",
		"on_cancel": "medusync.order_meta.on_payment_entry",
	},
	"Delivery Note": {
		"on_submit": "medusync.handlers.commerce.reverse.on_delivery_note",
		"on_cancel": "medusync.handlers.commerce.reverse.on_delivery_note",
	},
	"Shipment": {
		"on_submit": "medusync.handlers.commerce.reverse.on_shipment",
		"on_update_after_submit": "medusync.handlers.commerce.reverse.on_shipment",
		"on_cancel": "medusync.handlers.commerce.reverse.on_shipment",
	},
	"Sales Invoice": {
		"on_submit": "medusync.handlers.commerce.reverse.on_sales_invoice",
		"on_cancel": "medusync.handlers.commerce.reverse.on_sales_invoice",
	},
	"Item Price": {
		"after_insert": "medusync.handlers.commerce.pricing.on_item_price",
		"on_update": "medusync.handlers.commerce.pricing.on_item_price",
		"on_trash": "medusync.handlers.commerce.pricing.on_item_price",
	},
	"Item": {
		"on_update": "medusync.handlers.commerce.pricing.on_item",
	},
	"Customer": {
		"after_insert": "medusync.handlers.commerce.pricing.on_customer_group_link",
		"on_update": "medusync.handlers.commerce.pricing.on_customer_group_link",
	},
}

# Once a store confirms it holds an item as a product, it is sent that
# item's current price and stock. Both travel as their own messages and
# otherwise only when they change, so a product reaching a store for the
# first time would sit there unpriced and out of stock until somebody
# edited its price or stock moved.
DELIVERED_HOOKS = {
	"Item": "medusync.handlers.commerce.followup.after_product_delivered",
}

# A sales document is more than its field map: the customer and the lines
# are built from what the store sends. A rehearsal validating the mapped
# fields alone would report an order with no customer and no lines.
REHEARSAL_BUILDERS = {
	"Sales Order": "medusync.handlers.commerce.mapped.rehearsal_doc",
	"Sales Invoice": "medusync.handlers.commerce.mapped.rehearsal_doc",
}


def register() -> None:
	"""Register this pack's inbound handlers. Idempotent — safe to re-run.

	One entry: the Medusa-initiated return request. Inbound order, product,
	customer and invoice upserts flow through the mapped push path
	(MAPPED_UPSERT above), not this registry; only the return-request
	method call needs a handler here, because there is no doctype to
	upsert — it calls create_pending_return instead.
	"""
	from medusync.handlers.commerce.reverse import handle_return_requested

	register_handler("order.return_requested", handle_return_requested)
