# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD
"""Customer phone sync (Medusa -> ERPNext Contact).

ERPNext Customer.mobile_no is READ-ONLY (fetch_from customer_primary_contact.
mobile_no) -- writing it is ignored. Phone lives on a linked Contact. So the
Customer branch of mapped.upsert_via_mapping hands the phone here after the
Customer is saved: we create/update one Contact linked to the Customer, set the
phone as its primary mobile, and point Customer.customer_primary_contact at it
so mobile_no fetches correctly. Idempotent (one managed Contact per customer).
Never raises: a bad phone must not fail the customer sync.
"""

import frappe


_SAVEPOINT = "medusync_contact_sync"


def sync_customer_contact(customer_name, phone, first_name=None, last_name=None):
    if not phone:
        return {"skipped": "no phone"}
    # Scoped to a savepoint: this helper is called in the middle of somebody
    # else's work -- a customer sync, or an order being written around it --
    # and a phone ERPNext refuses (it insists they are unique across contacts)
    # must undo this contact and nothing else.
    frappe.db.savepoint(_SAVEPOINT)
    try:
        existing = frappe.db.get_value(
            "Dynamic Link",
            {"parenttype": "Contact", "link_doctype": "Customer", "link_name": customer_name},
            "parent",
        )
        if existing:
            doc = frappe.get_doc("Contact", existing)
        else:
            doc = frappe.new_doc("Contact")
            doc.first_name = first_name or customer_name
            if last_name:
                doc.last_name = last_name
            doc.append("links", {"link_doctype": "Customer", "link_name": customer_name})
        # Replace phone rows with the single synced primary mobile.
        doc.set("phone_nos", [])
        doc.append("phone_nos", {"phone": str(phone), "is_primary_mobile_no": 1, "is_primary_phone": 1})
        doc.flags.ignore_mandatory = True
        doc.save(ignore_permissions=True)
        # Point the customer primary contact at it, and set mobile_no directly
        # (it is fetch_from the contact but db.set_value bypasses the fetch
        # trigger, so set both to keep them consistent immediately).
        frappe.db.set_value("Customer", customer_name, {
            "customer_primary_contact": doc.name,
            "mobile_no": str(phone),
        })
        return {"contact": doc.name, "phone": str(phone)}
    except Exception as exc:
        frappe.db.rollback(save_point=_SAVEPOINT)
        return {"error": str(exc)[:150]}
