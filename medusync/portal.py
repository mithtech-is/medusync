# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
# For license information, please see license.txt

"""What the Mappings page shows.

The two systems hold one configuration between them, so the two lists of it
should read the same. The Medusa admin has always shown a table — name,
which entity, which doctype, which way, how many pairs, when it last ran,
and a switch. This side showed a stock Frappe list view: titles, a Status
column and nothing about what any row actually does.

These are the same columns as that table, computed here rather than in the
page, so the two lists agree on what a mapping *is* without the page
knowing anything about the doctype's shape.
"""

import frappe

from medusync import sites

MAPPING_DOCTYPE = "Medusync Mapping"
_FILLED_SAVEPOINT = "medusync_filled_probe"


@frappe.whitelist()
def list_mappings() -> dict:
	"""Every mapping, in the shape the Medusa admin lists them."""
	frappe.only_for("System Manager")

	rows = frappe.get_all(
		MAPPING_DOCTYPE,
		fields=[
			"name",
			"title",
			"enabled",
			"medusa_entity",
			"document_type",
			"direction",
			"site",
			"last_synced_at",
			"last_test_status",
			"attention",
		],
		order_by="modified desc",
	)

	# One query for every mapping's pair count beats one per row. Counted in
	# Python rather than with a SQL function, which frappe.get_all refuses
	# as a string in `fields`.
	counts: dict[str, int] = {}
	if rows:
		for r in frappe.get_all(
			"Medusync Field Map",
			filters={"parenttype": MAPPING_DOCTYPE},
			fields=["parent"],
			limit_page_length=0,
		):
			counts[r["parent"]] = counts.get(r["parent"], 0) + 1

	out = []
	for r in rows:
		out.append(
			{
				"name": r["name"],
				"title": r["title"] or r["name"],
				"enabled": bool(r["enabled"]),
				"medusa_entity": r["medusa_entity"] or "",
				"doctype": r["document_type"] or "",
				"direction": r["direction"] or "",
				"site": r["site"] or "",
				"pairs": counts.get(r["name"], 0),
				"last_run": r["last_synced_at"],
				"test_status": r["last_test_status"] or "Untested",
				"attention": r["attention"] or "",
			}
		)

	return {
		"items": out,
		"active": sum(1 for r in out if r["enabled"]),
		"sites": [s["site_id"] for s in sites.all_sites(enabled_only=False)],
	}


@frappe.whitelist()
def set_enabled(name: str, enabled: int | str) -> dict:
	"""Flip a mapping from the list.

	Switching one ON still goes through the same gate as anywhere else: a
	mapping that has not been rehearsed is refused, and the page reports
	why rather than silently leaving the switch where it was.
	"""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(MAPPING_DOCTYPE, name)
	doc.enabled = 1 if frappe.utils.cint(enabled) else 0
	doc.save(ignore_permissions=True)
	return {"name": doc.name, "enabled": bool(doc.enabled)}


@frappe.whitelist()
def delete_mapping(name: str) -> dict:
	"""Remove a mapping here, and on the store it is paired with.

	The deletion travels through the ordinary `mapping.deleted` push, so
	the far copy goes too — one configuration should not survive in one
	system after being removed from the other.
	"""
	frappe.only_for("System Manager")
	# `force` because Medusync Log rows are dynamically linked to the
	# mapping they were written under, and Frappe refuses a delete while any
	# exist. Keeping the log and dropping the rule is the right way round: a
	# log says what happened, and what happened does not stop being true
	# when the rule is retired. The rows keep the mapping's name as text, so
	# the history stays readable — the link just no longer resolves.
	frappe.delete_doc(MAPPING_DOCTYPE, name, force=1, ignore_permissions=True)
	return {"deleted": name}


@frappe.whitelist()
def field_options(doctype: str, fieldname: str) -> dict:
	"""The values one ERPNext field will actually accept.

	A fixed value often targets a Link — `company` is the standard case: a
	sync has to belong to one, nothing in a store corresponds to it, and
	typing the name is how you find out later that it was wrong. A Select
	answers with its own options; a Link answers with the records that
	exist on this site; anything else answers with nothing and the caller
	asks for free text.

	The mirror of `medusa_fields.fields` for this side, so both halves of
	the mapper offer real values rather than a blank box.
	"""
	frappe.only_for("System Manager")
	if not doctype or not fieldname:
		frappe.throw(frappe._("Need a doctype and a fieldname."))

	df = frappe.get_meta(doctype).get_field(fieldname)
	if not df:
		frappe.throw(frappe._("{0} has no field {1}.").format(doctype, fieldname))

	if df.fieldtype == "Select":
		options = [o.strip() for o in (df.options or "").split("\n") if o.strip()]
		return {"fieldtype": df.fieldtype, "options": options}

	if df.fieldtype != "Link" or not df.options:
		return {"fieldtype": df.fieldtype, "options": []}

	# A cap, not a page: a fixed value belongs to a short controlled list.
	# Something with thousands of rows is not a sensible constant, and the
	# caller falls back to free text rather than pretending this is all.
	LIMIT = 200
	names = frappe.get_all(
		df.options,
		pluck="name",
		limit_page_length=LIMIT + 1,
		order_by="name asc",
		ignore_permissions=False,
	)
	return {
		"fieldtype": df.fieldtype,
		"link_doctype": df.options,
		"options": names[:LIMIT],
		"truncated": len(names) > LIMIT,
	}


@frappe.whitelist()
def fields_filled_on_arrival(doctype: str) -> list:
	"""Required fields this site fills itself when a record arrives.

	A store cannot know that a Sales Order's customer and lines are built
	from the order it sends, nor that ERPNext fills the currency and the
	price list from the customer. Asking an operator to map those is asking
	for pairs that mean nothing, and refusing to enable the mapping until
	somebody does is worse: the gate stops being a check and becomes a
	riddle. So the side that owns the document answers for it.

	Answered by building the document the way a real arrival would -- the
	handler pack's own builder, the one a rehearsal uses -- and reporting
	which required fields came out filled. Nothing is written. An empty list
	means "ask about all of them", which is what a store did before this.
	"""
	frappe.only_for("System Manager")
	from medusync import handlers

	builder = handlers.rehearsal_builder(doctype)
	if not builder:
		return []
	meta = frappe.get_meta(doctype)
	required = [field.fieldname for field in meta.fields if field.reqd]
	filled: list = []
	frappe.db.savepoint(_FILLED_SAVEPOINT)
	try:
		doc = builder(doctype, {})
		if doc:
			filled = [name for name in required if doc.get(name) not in (None, "", [])]
	except Exception:
		frappe.log_error(
			title="medusync could not answer what it fills", message=frappe.get_traceback()
		)
		filled = []
	finally:
		frappe.db.rollback(save_point=_FILLED_SAVEPOINT)
	return filled
