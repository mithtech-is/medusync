# ERPNext -> Medusa stock levels. One-way: ERPNext owns stock and is the
# SINGLE reservation authority. We push SELLABLE = actual - reserved - safety
# (not raw actual_qty), so Medusa never oversells.
#
# Many warehouses, many stores. Which warehouse feeds which store, and under
# which stock-location id, is the map on Medusync Site (see medusync.warehouses).
# One warehouse can feed several stores under different ids, and each store is
# told its own; a warehouse nobody mapped is skipped before any work is done.
#
# Three triggers:
#   - Stock Ledger Entry.after_insert  -> actual_qty changed (receipts/issues)
#   - Sales Order submit/cancel/update -> reserved_qty changed (no SLE fires)
#   - a store confirming it holds the product -> the level as it stands
#     (push_current, from followup.py)
# The first two defer to an after-commit job `push_level` that reads the
# settled Bin (at after_insert neither Bin nor qty_after_transaction is final);
# the third runs inside a delivery job, after that commit has long happened.
import frappe

from medusync import config, warehouses
from medusync.outbound import emit


def _guard() -> bool:
    return not frappe.flags.get("medusync_inbound") and config.is_enabled()


def on_sle(doc, method=None):
    """Stock movement -> re-push sellable. Cheap, never raises."""
    try:
        if not _guard():
            return
        if not warehouses.is_watched(doc.warehouse):
            return
        frappe.enqueue(
            "medusync.handlers.commerce.inventory.push_level",
            queue="short",
            item_code=doc.item_code,
            warehouse=doc.warehouse,
            ref="sle-%s" % doc.name,
            enqueue_after_commit=True,
        )
    except Exception:
        frappe.log_error(title="medusync inventory SLE hook failed", message=frappe.get_traceback())


def on_sales_order(doc, method=None):
    """SO submit/cancel/amend changes reserved_qty with NO SLE -> re-push each
    line's sellable at the warehouse that line reserves from.

    A single order can reserve one item from two warehouses, so the pair is
    what must be unique here, not the item.
    """
    try:
        if not _guard():
            return
        watched = warehouses.watched()
        if not watched:
            return
        seen = set()
        for line in doc.items or []:
            warehouse = line.get("warehouse")
            if not line.item_code or warehouse not in watched:
                continue
            key = (line.item_code, warehouse)
            if key in seen:
                continue
            seen.add(key)
            frappe.enqueue(
                "medusync.handlers.commerce.inventory.push_level",
                queue="short",
                item_code=line.item_code,
                warehouse=warehouse,
                ref="so-%s-%s" % (doc.name, method or "update"),
                enqueue_after_commit=True,
            )
    except Exception:
        frappe.log_error(title="medusync inventory SO hook failed", message=frappe.get_traceback())


def sellable(item_code, warehouse) -> float:
    """What a store may sell: on hand, less what is already promised, less
    the buffer the business keeps back. Never negative — a store cannot
    sell minus four of something."""
    bin_row = frappe.db.get_value(
        "Bin",
        {"item_code": item_code, "warehouse": warehouse},
        ["actual_qty", "reserved_qty"],
        as_dict=True,
    ) or {}
    actual = float(bin_row.get("actual_qty") or 0)
    reserved = float(bin_row.get("reserved_qty") or 0)
    safety = float(frappe.db.get_value("Item", item_code, "safety_stock") or 0)
    return max(0.0, actual - reserved - safety)


def push_level(item_code, warehouse, ref, only_site=None):
    """After-commit: read the settled Bin, then tell each store that draws
    on this warehouse — each under the stock-location id it knows.
    `only_site` narrows that to one store."""
    targets = warehouses.targets_for(warehouse)
    if not targets:
        return
    locations = dict(targets)
    quantity = sellable(item_code, warehouse)
    payload = {"sku": item_code, "warehouse": warehouse, "quantity": quantity}

    def for_store(site_id, body):
        if site_id not in locations:
            return None
        if only_site and site_id != only_site:
            return None
        # A store on the legacy single-warehouse setting never named a
        # location; the plugin then picks its own, as it always did.
        return {**body, "location_id": locations[site_id]}

    emit(
        "inventory.level.set",
        payload,
        # The warehouse belongs in the key. Two warehouses moving for one
        # item in one save would otherwise produce the same event id and
        # the second would be dropped as a duplicate.
        ref="%s:%s:%s" % (item_code, warehouse, ref),
        doctype="Item",
        docname=item_code,
        per_site=for_store,
    )


def push_current(item_code, site_id, ref):
    """Tell one store this item's sellable stock at every warehouse it draws on.

    Stock is sent when it moves. An item untouched since before it was
    chosen would otherwise reach the store showing none.
    """
    try:
        if not _guard():
            return
        if not frappe.db.get_value("Item", item_code, "is_stock_item"):
            # Nothing is counted for a service; telling the store "0" would
            # only make it unsellable.
            return
        for warehouse in sorted(warehouses.watched()):
            if any(store == site_id for store, _ in warehouses.targets_for(warehouse)):
                push_level(item_code, warehouse, ref, only_site=site_id)
    except Exception:
        frappe.log_error(title="medusync inventory push_current failed", message=frappe.get_traceback())
