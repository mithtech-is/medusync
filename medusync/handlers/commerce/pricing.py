# Pricing + B2B: ERPNext -> Medusa. ERPNext price always wins.
#   Item Price -> variant.price.set        (lists a store maps as its price)
#                 and, once a store confirms it holds the product, the
#                 price in force today (push_current, from followup.py)
#   Item       -> variant.meta.set         (MOQ = min_order_qty)
#   Customer   -> customer.group.set       (B2B customer group)
#
# Which price list reaches which store is per store, not per site, and a cost
# list can be marked Don't Sync so it never leaves. See medusync.price_lists.

import frappe
from frappe.utils import getdate, nowdate

from medusync import config, links, price_lists, selection
from medusync.outbound import emit


def _guard():
    return not frappe.flags.get("medusync_inbound") and config.is_enabled()


def _deliver(event, payload, ref, doctype, docname, per_site=None):
    """Log + hand off through the shared channel (queued by default, with
    the same retry/backoff as mapped events). Runs inside a doc event, so
    the outbound HTTP call must never happen inline here."""
    emit(event, payload, ref=ref, doctype=doctype, docname=docname, per_site=per_site)


def on_item_price(doc, method=None):
    """One Item Price can mean different things to different stores, so the
    rules are resolved first and the stores are grouped by what they asked
    for. A store that mapped nothing hears nothing."""
    try:
        if not _guard():
            return
        # Frappe runs on_update inside insert(), right after after_insert, so a
        # new price would go out twice at once. Two messages arriving together
        # each found no price on the variant and each created one. after_insert
        # has already sent it; the same rule the mappings follow in outbound.
        if method == "on_update" and doc.flags.get("in_insert"):
            return
        rules = price_lists.rules_for(doc.price_list)
        if not rules:
            return
        deleted = method == "on_trash"
        ref = "%s-%s" % (doc.name, method)

        base_stores = {r["site_id"] for r in rules if r["role"] == price_lists.ROLE_BASE}
        # A price belongs to an item, and an item under "Only chosen
        # documents" reaches only the stores it was chosen for. Filed under
        # Item Price, this message would otherwise miss that check and go
        # out for every item on the list.
        base_stores = {s for s in base_stores if selection.is_allowed("Item", doc.item_code, s)}
        if base_stores:
            payload = {
                "sku": doc.item_code,
                "price_list": doc.price_list,
                "amount": float(doc.price_list_rate or 0),
                "currency": doc.currency,
                "valid_from": str(doc.get("valid_from") or ""),
                "valid_upto": str(doc.get("valid_upto") or ""),
                "deleted": bool(deleted),
            }
            _deliver(
                "variant.price.set",
                payload,
                ref,
                "Item Price",
                doc.name,
                per_site=lambda site_id, body: body if site_id in base_stores else None,
            )

    except Exception:
        frappe.log_error(title="medusync pricing on_item_price failed", message=frappe.get_traceback())


def current_price(item_code, price_list):
    """The Item Price a sale made today would use, picked the way ERPNext
    picks it: no customer, supplier or batch; the item's stock UOM or none;
    in force today; the latest `valid_from` first. None when there is none.
    """
    meta = frappe.get_meta("Item Price")
    filters = {"item_code": item_code, "price_list": price_list}
    for field in ("customer", "supplier", "batch_no"):
        if meta.has_field(field):
            filters[field] = ["is", "not set"]
    stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
    today = getdate(nowdate())
    rows = frappe.get_all(
        "Item Price",
        filters=filters,
        fields=["name", "price_list", "price_list_rate", "currency", "uom", "valid_from", "valid_upto"],
        order_by="valid_from desc, uom desc",
    )
    for row in rows:
        if row.uom and row.uom != stock_uom:
            continue
        if row.valid_from and getdate(row.valid_from) > today:
            continue
        if row.valid_upto and getdate(row.valid_upto) < today:
            continue
        return row
    return None


def push_current(item_code, site_id, ref):
    """Tell one store this item's price in force today, from each list it
    takes as its base price.

    Prices are sent when an Item Price changes. One untouched since before
    the item was chosen would never reach the store, and a product without
    a price cannot be sold.
    """
    try:
        if not _guard():
            return
        if not selection.is_allowed("Item", item_code, site_id):
            return
        for price_list in sorted(price_lists.watched()):
            wanted = any(
                r["site_id"] == site_id and r["role"] == price_lists.ROLE_BASE
                for r in price_lists.rules_for(price_list)
            )
            if not wanted:
                continue
            row = current_price(item_code, price_list)
            if not row:
                continue
            payload = {
                "sku": item_code,
                "price_list": row.price_list,
                "amount": float(row.price_list_rate or 0),
                "currency": row.currency,
                "valid_from": str(row.get("valid_from") or ""),
                "valid_upto": str(row.get("valid_upto") or ""),
                "deleted": False,
            }
            _deliver(
                "variant.price.set",
                payload,
                "%s-%s" % (row.name, ref),
                "Item Price",
                row.name,
                per_site=lambda s, body: body if s == site_id else None,
            )
    except Exception:
        frappe.log_error(title="medusync pricing push_current failed", message=frappe.get_traceback())


def on_item(doc, method=None):
    try:
        if not _guard():
            return
        payload = {"sku": doc.item_code, "moq": float(doc.get("min_order_qty") or 0)}
        _deliver("variant.meta.set", payload, "%s-%s" % (doc.name, method or "u"), "Item", doc.name)
    except Exception:
        frappe.log_error(title="medusync pricing on_item failed", message=frappe.get_traceback())


def on_customer_group_link(doc, method=None):
    try:
        if not _guard():
            return
        grp = doc.get("customer_group")
        if not grp:
            return
        payload = {
            "medusa_customer_id": links.medusa_id_for("Customer", doc.name, entity="customer"),
            "email": doc.get("email_id"),
            "group": grp,
        }
        _deliver("customer.group.set", payload, "%s-%s" % (doc.name, method or "u"), "Customer", doc.name)
    except Exception:
        frappe.log_error(title="medusync pricing on_customer_group failed", message=frappe.get_traceback())
