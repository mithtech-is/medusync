# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
# For license information, please see license.txt

"""Canonical-mapping upsert for `medusync.api.receive_mapped`.

Ordinary commerce semantics: a Medusa product becomes an Item, an order
becomes a Sales Order with its line items, an invoice becomes a Sales
Invoice, a customer becomes a Customer.

Writes go through the doctype layer rather than `db_set`, so ERPNext's own
validation runs and the connector never has to reimplement it. Everything
here runs under `frappe.flags.medusync_inbound`, so the save this makes is
recognised as an inbound write and is not echoed back to the store.
"""

import frappe
from medusync import config, invoicing, links
from medusync.handlers.commerce.sales_financials import apply_financials
from medusync.handlers.commerce.address_sync import sync_customer_addresses
from medusync.handlers.commerce.contact_sync import sync_customer_contact

_INSERT_DEFAULTS = {
	"Item": {"item_group": "Products", "stock_uom": "Nos", "is_stock_item": 1},
	"Customer": {
		"customer_type": "Individual",
		"customer_group": "Individual",
		"territory": "India",
	},
}
_SALES_DOCS = ("Sales Order", "Sales Invoice")
_CONTACT_SAVEPOINT = "medusync_order_contact"


def _cust_result(doctype, doc, addresses, phone, status):
	# Customer branch also syncs Address docs + a Contact for the phone (a flat
	# mapping cannot create the linked Address/Contact doctypes, and mobile_no is
	# read-only); non-Customer doctypes get the plain result.
	r = {"doctype": doctype, "name": doc.name, "status": status}
	if doctype == "Customer":
		if addresses is not None:
			r["addresses"] = sync_customer_addresses(doc.name, addresses)
		if phone:
			r["contact"] = sync_customer_contact(doc.name, phone, first_name=doc.get("customer_name"))
	return r


def _apply_defaults(doc, doctype):
	for field, value in _INSERT_DEFAULTS.get(doctype, {}).items():
		if doc.get(field):
			continue
		meta_field = doc.meta.get_field(field)
		if meta_field and meta_field.fieldtype == "Link":
			if frappe.db.exists(meta_field.options, value):
				doc.set(field, value)
		elif meta_field:
			doc.set(field, value)
	apply_store_account_manager(doc)


def apply_store_account_manager(doc, site_id=None):
	"""Give an arriving record its store's default account manager.

	An account manager is a user of THIS site, so it is chosen here, per
	store (Medusync Site -> Default Account Manager), rather than written
	into a mapping the store evaluates. It only fills an empty field, only
	on a doctype that has one, and only with a user that exists here -- so
	it can never turn a signup into a refusal.
	"""
	if doc.get("account_manager"):
		return
	field = doc.meta.get_field("account_manager")
	if not field or field.fieldtype != "Link" or field.options != "User":
		return
	site_id = site_id or frappe.flags.get("medusync_site_id")
	if not site_id:
		return
	user = frappe.db.get_value("Medusync Site", site_id, "default_account_manager")
	if user and frappe.db.exists("User", user):
		doc.set("account_manager", user)


def _ensure_item_group(name):
	# Item Group is a Link AND a tree doctype: a payload item_group that
	# does not exist fails the Item save. Auto-create as a leaf under the
	# root so category values from Medusa metadata just work.
	if not name:
		return
	if frappe.db.exists("Item Group", name):
		return
	frappe.get_doc({
		"doctype": "Item Group",
		"item_group_name": name,
		"parent_item_group": "All Item Groups",
		"is_group": 0,
	}).insert(ignore_permissions=True)


def _ensure_item(item_code, item_name=None):
	"""Guarantee an Item exists so a Sales Order/Invoice line can link."""
	if not item_code:
		return None
	if not frappe.db.exists("Item", {"item_code": item_code}):
		it = frappe.new_doc("Item")
		it.item_code = item_code
		it.item_name = item_name or item_code
		it.item_group = "Products"
		it.stock_uom = "Nos"
		it.is_stock_item = 1
		it.insert(ignore_permissions=True)
	return item_code


def _set_fields(doc, payload):
	"""Write scalar fields, skipping None (a null Data field crashes
	Frappe's .strip()) and anything that isn't a real field."""
	for field, value in payload.items():
		if value is None:
			continue
		if doc.meta.get_field(field):
			doc.set(field, value)


def rehearsal_doc(doctype, payload, existing=None):
	"""The document a real arrival would build, for a rehearsal to validate.

	The flat payload is only part of a sales document: `_upsert_sales_doc`
	finds the Customer and appends the lines the store sent. Rehearsing the
	payload alone would report an order with no customer and no lines — a
	complaint about nothing that ever arrives, which buries the real ones
	(a mandatory field this site added, say).

	Nothing is written: the caller validates the document and rolls back.
	None means 'nothing realistic to build', and the rehearsal falls back
	to the plain payload.
	"""
	if existing:
		doc = frappe.get_doc(doctype, existing)
		_set_fields(doc, payload)
		return doc
	customer = _rehearsal_customer(payload)
	line = _rehearsal_line()
	if not customer or not line:
		return None
	doc = frappe.new_doc(doctype)
	doc.customer = customer
	if not doc.get("company"):
		doc.company = frappe.defaults.get_user_default("Company") or frappe.db.get_value(
			"Company", {}, "name"
		)
	if doctype == "Sales Order":
		doc.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 7)
	_set_fields(doc, payload)
	doc.append("items", line)
	_apply_defaults(doc, doctype)
	# What insert() does before validation: fetch the line details (tax
	# codes, UOM, warehouse) and the party defaults. Without it a rehearsal
	# reports empty tax codes that a real save would have filled.
	try:
		doc.set_missing_values()
	except Exception:
		pass
	return doc


def _rehearsal_customer(payload):
	"""The Customer a real order would name, or the closest stand-in."""
	medusa_customer_id = payload.get("medusa_customer_id")
	if medusa_customer_id:
		found = links.name_for("Customer", medusa_customer_id, entity="customer")
		if found:
			return found
	email = payload.get("contact_email")
	if email:
		found = frappe.db.get_value("Customer", {"email_id": email}, "name")
		if found:
			return found
	# An order carries the address the shopper checked out with, and plenty
	# of validation (tax, state) reads it, so rehearse with a customer that
	# has one. One the store has already sent comes first, then any.
	sent = frappe.get_all(
		links.LINK_DOCTYPE,
		filters={"document_type": "Customer"},
		pluck="document_name",
		limit=20,
	)
	for name in sent:
		if _has_address(name):
			return name
	with_address = frappe.db.sql(
		"""select dl.link_name from `tabDynamic Link` dl join tabCustomer c on c.name = dl.link_name
			where dl.link_doctype = 'Customer' and dl.parenttype = 'Address' and c.disabled = 0 limit 1"""
	)
	if with_address:
		return with_address[0][0]
	if sent and frappe.db.exists("Customer", sent[0]):
		return sent[0]
	return frappe.db.get_value("Customer", {"disabled": 0}, "name")


def _has_address(customer) -> bool:
	return bool(
		frappe.db.exists(
			"Dynamic Link",
			{"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"},
		)
	)


def _rehearsal_line():
	"""One line, from an item this site would really sell."""
	from medusync import price_lists, selection

	code = frappe.db.get_value(
		selection.INCLUSION_DOCTYPE, {"document_type": "Item"}, "document_name"
	)
	if not code or not frappe.db.exists("Item", code):
		code = frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1}, "name")
	if not code:
		return None
	rate = 0
	try:
		row = pricing_current_price(code, price_lists.legacy_selling_price_list())
		rate = float(row.price_list_rate or 0) if row else 0
	except Exception:
		rate = 0
	return {"item_code": code, "qty": 1, "rate": rate}


def pricing_current_price(item_code, price_list):
	"""Indirection so the import stays inside the call (pricing imports
	outbound, which imports this module on some paths)."""
	from medusync.handlers.commerce import pricing

	return pricing.current_price(item_code, price_list)


def _mapping_constants(doctype: str) -> dict:
	"""The fixed values the mapping for this doctype would have written.

	A site makes its own fields mandatory -- a source, a market segment, an
	account manager -- and answers them once, as fixed values on the mapping.
	A record this pack creates on its own should carry the same answers, or
	it is a second, thinner kind of customer that the site never agreed to.
	"""
	out: dict = {}
	try:
		for row in config.mappings_for(doctype):
			mapping = frappe.get_cached_doc(config.MAPPING_DOCTYPE, row["name"])
			for pair in mapping.field_map or []:
				constant = (pair.get("constant_value") or "").strip()
				if constant and pair.frappe_field and pair.direction != "To Medusa":
					out.setdefault(pair.frappe_field, constant)
	except Exception:
		frappe.log_error(title="medusync could not read the mapping's fixed values", message=frappe.get_traceback())
	return out


def _customer_from_order(medusa_customer_id, email, payload):
	"""The shopper this order names, when the site has never seen them.

	A store creates its customer at checkout and does not always announce
	it, so the first order from somebody new arrives naming a Customer that
	is not here yet, and the order is refused. The order is the
	introduction: take it. Built the way the customer mapping would have
	built it -- its fixed values, this pack's defaults, the store's account
	manager -- so what lands is the record that mapping would have made.

	None when the order names nobody at all; the caller then refuses it as
	before.
	"""
	if not (email or medusa_customer_id):
		return None
	billing = payload.get("medusa_billing_address") or {}
	shipping = payload.get("medusa_shipping_address") or {}
	name = str(billing.get("name") or shipping.get("name") or "").strip()
	if not name and email:
		name = str(email).split("@")[0]
	if not name:
		return None
	try:
		doc = frappe.new_doc("Customer")
		# The site's own answers first, over any doctype default: a new
		# Customer starts as a Company here, and the mapping saying
		# "Individual" is the answer this site actually gave.
		for field, value in _mapping_constants("Customer").items():
			if doc.meta.get_field(field):
				doc.set(field, value)
		_apply_defaults(doc, "Customer")
		doc.customer_name = name
		if email and doc.meta.get_field("email_id"):
			doc.email_id = email
		doc.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="medusync could not create the customer an order named", message=frappe.get_traceback())
		return None
	if medusa_customer_id:
		links.remember_all("Customer", doc.name, {"medusa_customer_id": medusa_customer_id})
	phone = billing.get("phone") or shipping.get("phone")
	if phone:
		# A contact is a nicety, the order is not. ERPNext refuses a phone
		# that already belongs to another contact, and two shoppers sharing
		# a number is no reason to lose the sale -- so the contact gets its
		# own savepoint and its failure stays inside it.
		frappe.db.savepoint(_CONTACT_SAVEPOINT)
		try:
			sync_customer_contact(doc.name, phone, first_name=name)
		except Exception:
			frappe.db.rollback(save_point=_CONTACT_SAVEPOINT)
	return doc.name


def _upsert_sales_doc(doctype, key_field, key_value, payload, event, event_id):
	payload = dict(payload)
	payload.pop("grand_total", None)  # read-only / computed
	opts = invoicing.options()
	doctype = invoicing.target_doctype(doctype, opts)
	taken = links.take_link_keys(payload, doctype)
	if links.is_link_key(key_field) and key_value not in (None, ""):
		taken.setdefault(key_field, key_value)
	order_id = taken.get("medusa_order_id")
	# The customer id names the Customer, not this document.
	medusa_customer_id = taken.pop("medusa_customer_id", None) or payload.pop("medusa_customer_id", None)
	items = payload.pop("medusa_items", None) or []
	contact_email = payload.get("contact_email")

	customer = None
	if medusa_customer_id:
		customer = links.name_for("Customer", medusa_customer_id, entity="customer")
	if not customer and contact_email:
		customer = frappe.db.get_value("Customer", {"email_id": contact_email}, "name")

	existing = links.find_by_key(doctype, key_field, key_value)

	if existing:
		doc = frappe.get_doc(doctype, existing)
		if doc.docstatus == 0:
			_set_fields(doc, payload)
			doc.save(ignore_permissions=True)
		links.remember_all(doctype, doc.name, taken)
		return {"doctype": doctype, "name": doc.name, "status": "updated"}

	if not customer:
		customer = _customer_from_order(medusa_customer_id, contact_email, payload)
	if not customer:
		raise Exception(
			"no matching Customer (medusa_customer_id=%s / email=%s)"
			% (medusa_customer_id, contact_email)
		)

	doc = frappe.new_doc(doctype)
	doc.customer = customer
	if key_field and key_field != "name" and key_value not in (None, "") and doc.meta.get_field(key_field):
		doc.set(key_field, key_value)
	if not doc.get("company"):
		doc.company = frappe.defaults.get_user_default("Company") or frappe.db.get_value(
			"Company", {}, "name"
		)
	if doctype == "Sales Order":
		doc.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 7)
	_set_fields(doc, payload)
	for row in items:
		code = _ensure_item(row.get("item_code"), row.get("item_name"))
		if not code:
			continue
		doc.append(
			"items",
			{"item_code": code, "qty": row.get("qty") or 1, "rate": row.get("rate") or 0},
		)
	if not doc.get("items"):
		raise Exception("no valid line items for %s" % doctype)
	apply_financials(doc, customer, payload)
	if doctype == "Sales Invoice":
		invoicing.insert_invoice(doc, payload, opts)
	else:
		doc.insert(ignore_permissions=True)
	links.remember_all(doctype, doc.name, taken)
	made = invoicing.after_create(doc, payload, opts, order_id)
	return {"doctype": doctype, "name": doc.name, "status": "created", "made": made}


def upsert_via_mapping(
	doctype,
	key_field,
	key_value,
	payload,
	event,
	event_id,
	allow_create=True,
	allow_update=True,
):
	# Stop both apps' outbound hooks from echoing this inbound write.
	frappe.flags.medusync_inbound = True
	frappe.flags.in_medusa_sync = True

	is_delete = bool(event) and event.endswith(".deleted")
	addresses = payload.pop("medusa_addresses", None) if doctype == "Customer" else None
	phone = payload.pop("mobile_no", None) if doctype == "Customer" else None
	if doctype == "Item" and payload.get("item_group"):
		_ensure_item_group(str(payload.get("item_group")))

	if doctype in _SALES_DOCS and not is_delete:
		return _upsert_sales_doc(doctype, key_field, key_value, payload, event, event_id)

	payload = dict(payload)
	taken = links.take_link_keys(payload, doctype)
	if links.is_link_key(key_field) and key_value not in (None, ""):
		taken.setdefault(key_field, key_value)

	existing = links.find_by_key(doctype, key_field, key_value)

	if is_delete:
		if not existing:
			return {"doctype": doctype, "name": None, "status": "skipped", "reason": "already absent"}
		doc = frappe.get_doc(doctype, existing)
		# Safe delete semantics: never destroy a submitted (accounting)
		# document — cancel it. Masters that carry a `disabled` flag are
		# disabled. Only trivial draft docs with no disable flag are
		# actually hard-deleted.
		if getattr(doc, "docstatus", 0) == 1:
			doc.cancel()
			return {"doctype": doctype, "name": existing, "status": "updated", "action": "cancelled"}
		if doc.meta.get_field("disabled"):
			doc.db_set("disabled", 1)
			return {"doctype": doctype, "name": existing, "status": "updated", "action": "disabled"}
		status_field = doc.meta.get_field("status")
		if status_field and "Cancelled" in (status_field.options or ""):
			doc.db_set("status", "Cancelled")
			return {"doctype": doctype, "name": existing, "status": "updated", "action": "cancelled"}
		frappe.delete_doc(doctype, existing, ignore_permissions=True)
		links.forget(doctype, existing)
		return {"doctype": doctype, "name": existing, "status": "updated", "action": "deleted"}

	if existing:
		if not allow_update:
			return {"doctype": doctype, "name": existing, "status": "skipped", "reason": "update not permitted"}
		doc = frappe.get_doc(doctype, existing)
		_set_fields(doc, payload)
		doc.save(ignore_permissions=True)
		links.remember_all(doctype, doc.name, taken)
		return _cust_result(doctype, doc, addresses, phone, "updated")

	# Item dedupe: a stub may already exist under this item_code (created
	# as a Sales Order line) with no product link — update it rather than
	# colliding on the primary key.
	if doctype == "Item" and payload.get("item_code") and frappe.db.exists(
		"Item", payload.get("item_code")
	):
		doc = frappe.get_doc("Item", payload.get("item_code"))
		_set_fields(doc, payload)
		doc.save(ignore_permissions=True)
		links.remember_all(doctype, doc.name, taken)
		return _cust_result(doctype, doc, addresses, phone, "updated")

	if not allow_create:
		return {"doctype": doctype, "name": None, "status": "skipped", "reason": "create not permitted"}

	doc = frappe.new_doc(doctype)
	if key_field != "name" and key_value not in (None, ""):
		doc.set(key_field, key_value)
	_set_fields(doc, payload)
	_apply_defaults(doc, doctype)
	if doctype == "Customer" and not doc.get("customer_name"):
		doc.customer_name = (
			doc.get("email_id") or (str(key_value) if key_value else None) or "Medusa Customer"
		)
	doc.insert(ignore_permissions=True)
	links.remember_all(doctype, doc.name, taken)
	return _cust_result(doctype, doc, addresses, phone, "created")
