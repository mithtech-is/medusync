# Price and stock follow a product to the store.
#
# A product message carries the item, not what it costs or how many are on
# hand: in ERPNext those live in Item Price and Bin and travel as messages of
# their own, sent when they change. So once a store confirms it holds an item
# as a product, whether it was just created or pushed again, it is told the
# price and stock as they stand now.
#
# Only a confirmed product counts. Stock and MOQ messages are about Items too;
# answering their confirmations with more stock messages would never stop.

import frappe

from medusync.handlers.commerce import inventory, pricing


def after_product_delivered(doctype, docname, *, site_id, event_name, response):
	if not confirmed_product(response):
		return
	ref = "follow-%s" % frappe.generate_hash(length=10)
	pricing.push_current(docname, site_id, ref)
	inventory.push_current(docname, site_id, ref)


def confirmed_product(response) -> bool:
	"""Did the store answer that it now holds this record as a product?"""
	result = response.get("result") if isinstance(response, dict) else None
	if not isinstance(result, dict):
		return False
	for row in result.get("results") or []:
		if (
			isinstance(row, dict)
			and row.get("entity") == "product"
			and row.get("ok")
			and row.get("id")
			and not row.get("deleted")
		):
			return True
	return False
