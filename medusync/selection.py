# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
# For license information, please see license.txt

"""Which documents are allowed to sync, decided in ERPNext.

ERPNext owns this decision. A mapping says a doctype *can* sync; this says
whether a particular document *may*, and to which stores.

Nothing is added to the doctype. Each doctype listed under Sync Selection
runs in one of two modes, and the decision for each document lives in the
connector's own lists:

    Every document unless excluded   Medusync Exclusion names what stays out
    Only chosen documents            Medusync Inclusion names what goes

A row with no site covers every store; a row naming a site covers that
store alone. The form's "Medusa sync" button and the list view's bulk
action write those rows, so the person looking at an Item can still see
and change the decision without leaving it.

Defaults matter. A doctype nobody listed syncs exactly as its mappings
say, and listing one in "unless excluded" mode stops nothing until
somebody excludes a record.
"""

import frappe

EXCLUSION_DOCTYPE = "Medusync Exclusion"
INCLUSION_DOCTYPE = "Medusync Inclusion"

MODE_UNLESS_EXCLUDED = "Every document unless excluded"
MODE_ONLY_CHOSEN = "Only chosen documents"


# ── What is under selection ──────────────────────────────────────────


def selection_doctypes() -> list[str]:
	"""DocTypes that carry the per-document selector.

	Exactly the ones the operator listed under Sync Selection in
	Settings. The catalogue doctype used to be included on its own, which
	is how an Item form grew a "Sync with Medusa" row nobody asked for. A
	doctype that is not listed is not under selection, and every document
	of it syncs — the same as before the selector existed.
	"""
	from medusync import config

	try:
		settings = config.settings()
	except Exception:
		return []
	out = []
	for row in settings.get("selection_doctypes") or []:
		name = (row.document_type or "").strip()
		if row.enabled and name and name not in out:
			out.append(name)
	return [dt for dt in out if frappe.db.exists("DocType", dt)]


def is_under_selection(doctype: str) -> bool:
	return bool(doctype) and doctype in selection_doctypes()


def modes() -> dict:
	"""doctype -> mode, for every doctype under selection."""
	from medusync import config

	try:
		settings = config.settings()
	except Exception:
		return {}
	out = {}
	for row in settings.get("selection_doctypes") or []:
		name = (row.document_type or "").strip()
		if row.enabled and name and name not in out and frappe.db.exists("DocType", name):
			out[name] = row.get("mode") or MODE_UNLESS_EXCLUDED
	return out


def mode_of(doctype: str) -> str | None:
	return modes().get(doctype)


# ── The rule ─────────────────────────────────────────────────────────


def is_allowed(doctype: str, name: str, site_id: str, doc=None) -> bool:
	"""May this document reach this store?

	`doc` is accepted for the callers that already hold one; the decision
	no longer reads it.
	"""
	mode = mode_of(doctype)
	if not mode:
		# The operator never put this doctype under selection, so the
		# mappings alone govern it.
		return True
	if mode == MODE_ONLY_CHOSEN:
		return _listed(INCLUSION_DOCTYPE, doctype, name, site_id)
	return not _listed(EXCLUSION_DOCTYPE, doctype, name, site_id)


def sites_allowed(doctype: str, name: str, candidates, doc=None) -> list:
	"""Filter a list of site rows down to the ones this document may reach."""
	if not mode_of(doctype):
		return list(candidates)
	return [s for s in candidates if is_allowed(doctype, name, s["site_id"])]


def site_filter(site: str | None):
	"""Filter that matches one store's rows, or the whole-document rows.

	A whole-document row stores NULL, and `=` never matches NULL, so it
	has to be asked for by name.
	"""
	return ["is", "not set"] if not site else ["=", site]


def _listed(list_doctype: str, doctype: str, name: str, site_id: str | None) -> bool:
	base = {"document_type": doctype, "document_name": name}
	try:
		# Two indexed lookups rather than one clever filter: a row with no
		# site covers every store, and one naming this store covers only it.
		if frappe.db.exists(list_doctype, {**base, "site": site_filter(None)}):
			return True
		return bool(site_id and frappe.db.exists(list_doctype, {**base, "site": site_id}))
	except Exception:
		return False


# ── Changing the decision ────────────────────────────────────────────


def exclude(doctype: str, name: str, site: str | None = None, reason: str | None = None, source: str = "Manual") -> str:
	"""Add (or keep) a Don't Sync entry."""
	existing = frappe.db.exists(
		EXCLUSION_DOCTYPE,
		{"document_type": doctype, "document_name": name, "site": site_filter(site)},
	)
	if existing:
		return existing
	doc = frappe.new_doc(EXCLUSION_DOCTYPE)
	doc.update(
		{
			"document_type": doctype,
			"document_name": name,
			"site": site or None,
			"reason": reason,
			"source": source,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _set_rows(list_doctype: str, doctype: str, name: str, wanted: set, extra: dict | None = None) -> None:
	"""Make the rows for one document name exactly the `wanted` sites.

	"" in `wanted` is the every-store row. It replaces per-store rows
	rather than sitting beside them, so the list reads as one decision.
	"""
	if "" in wanted:
		wanted = {""}
	existing = {
		(row.site or ""): row.name
		for row in frappe.get_all(
			list_doctype,
			filters={"document_type": doctype, "document_name": name},
			fields=["name", "site"],
		)
	}
	for site_id in set(existing) - wanted:
		frappe.delete_doc(list_doctype, existing[site_id], force=1, ignore_permissions=True)
	for site_id in wanted - set(existing):
		row = frappe.new_doc(list_doctype)
		row.update({"document_type": doctype, "document_name": name, "site": site_id or None, **(extra or {})})
		row.insert(ignore_permissions=True)


def apply_choice(doctype: str, name: str, chosen_sites) -> dict:
	"""Record which stores one document should reach.

	`chosen_sites` is the full answer — every store not in it is off for
	this document — so the dialog and a bulk action can share it.
	"""
	from medusync import sites

	mode = mode_of(doctype)
	if not mode:
		frappe.throw(
			frappe._("{0} is not under Sync Selection. List it in Medusync Settings first.").format(doctype)
		)
	every = {s["site_id"] for s in sites.all_sites(enabled_only=False)}
	chosen = {s for s in (chosen_sites or []) if s in every}
	if mode == MODE_ONLY_CHOSEN:
		wanted = {""} if every and chosen == every else chosen
		_set_rows(INCLUSION_DOCTYPE, doctype, name, wanted)
	else:
		off = every - chosen
		wanted = {""} if every and off == every else off
		# Only the rows this dialog owns: a Manual exclusion somebody
		# added with a reason is theirs to remove.
		manual = set(
			frappe.get_all(
				EXCLUSION_DOCTYPE,
				filters={"document_type": doctype, "document_name": name, "source": "Manual"},
				pluck="site",
			)
		)
		if manual and ({(s or "") for s in manual} - wanted):
			frappe.throw(
				frappe._(
					"{0} has a manual exclusion. Remove it from Medusync Exclusion before switching the store back on."
				).format(name)
			)
		_set_rows_unchecked(doctype, name, wanted)
	return {"doctype": doctype, "name": name, "sites": sorted(chosen)}


def _set_rows_unchecked(doctype: str, name: str, wanted: set) -> None:
	if "" in wanted:
		wanted = {""}
	existing = {
		(row.site or ""): (row.name, row.source)
		for row in frappe.get_all(
			EXCLUSION_DOCTYPE,
			filters={"document_type": doctype, "document_name": name},
			fields=["name", "site", "source"],
		)
	}
	for site_id, (row_name, source) in existing.items():
		if site_id not in wanted and source != "Manual":
			frappe.delete_doc(EXCLUSION_DOCTYPE, row_name, force=1, ignore_permissions=True)
	for site_id in wanted - set(existing):
		exclude(doctype, name, site=site_id or None, reason="Switched off from the document.", source="Unchecked")


# ── What the Desk calls ──────────────────────────────────────────────


@frappe.whitelist()
def document_selection(doctype: str, name: str) -> dict:
	"""The stores one document reaches, for the form's dialog."""
	frappe.has_permission(doctype, "read", doc=name, throw=True)
	from medusync import sites

	mode = mode_of(doctype)
	rows = [
		{
			"site_id": s["site_id"],
			"title": s.get("title") or s["site_id"],
			"allowed": is_allowed(doctype, name, s["site_id"]) if mode else True,
		}
		for s in sites.all_sites(enabled_only=False)
	]
	return {"mode": mode, "sites": rows}


@frappe.whitelist(methods=["POST"])
def set_document_selection(doctype: str, names, sites) -> dict:
	"""Set the stores for one document or many."""
	names = frappe.parse_json(names) if isinstance(names, str) else names
	chosen = frappe.parse_json(sites) if isinstance(sites, str) else sites
	if isinstance(names, str):
		names = [names]
	done = []
	for name in names or []:
		frappe.has_permission(doctype, "write", doc=name, throw=True)
		done.append(apply_choice(doctype, name, chosen)["name"])
	return {"doctype": doctype, "updated": done}


def boot_session(bootinfo) -> None:
	"""Tell the Desk which doctypes carry the Medusa sync button."""
	try:
		if frappe.session.user != "Guest":
			bootinfo.medusync_selection = modes()
	except Exception:
		bootinfo.medusync_selection = {}


@frappe.whitelist(methods=["POST"])
def choose_all(doctype: str, sites=None, filters=None, limit: int = 0) -> dict:
	"""Choose every document of a type, so somebody can un-choose a few.

	The opposite starting point to picking them one at a time, and the one
	people actually want on a catalogue of sixty thousand: take the lot,
	then drop the handful that should not go. `filters` narrows it to a
	group or a brand first.

	Only for a doctype in chosen mode — under "every document unless
	excluded" everything is already chosen and this would write sixty
	thousand rows saying so.
	"""
	frappe.only_for("System Manager")
	if mode_of(doctype) != MODE_ONLY_CHOSEN:
		return {
			"ok": False,
			"message": frappe._(
				"{0} is not in 'Only chosen documents' mode, so everything already syncs."
			).format(doctype),
		}
	from medusync import sites as site_registry

	chosen = frappe.parse_json(sites) if isinstance(sites, str) else sites
	if not chosen:
		chosen = [s["site_id"] for s in site_registry.all_sites(enabled_only=False)]
	filters = frappe.parse_json(filters) if isinstance(filters, str) else filters

	names = frappe.get_all(
		doctype, filters=filters or {}, pluck="name", limit=int(limit) or None
	)
	for name in names:
		apply_choice(doctype, name, chosen)
	return {"ok": True, "doctype": doctype, "chosen": len(names), "sites": chosen}


@frappe.whitelist(methods=["POST"])
def choose_none(doctype: str, filters=None) -> dict:
	"""Drop every choice for a doctype. The undo for `choose_all`."""
	frappe.only_for("System Manager")
	names = None
	if filters:
		filters = frappe.parse_json(filters) if isinstance(filters, str) else filters
		names = set(frappe.get_all(doctype, filters=filters, pluck="name"))
	removed = 0
	for row in frappe.get_all(
		INCLUSION_DOCTYPE, filters={"document_type": doctype}, fields=["name", "document_name"]
	):
		if names is not None and row["document_name"] not in names:
			continue
		frappe.delete_doc(INCLUSION_DOCTYPE, row["name"], ignore_permissions=True, force=True)
		removed += 1
	return {"ok": True, "doctype": doctype, "removed": removed}


@frappe.whitelist()
def chosen_for_pull(doctype: str, site_id: str | None = None):
	"""What a store's own pull may bring in.

	The store also fetches on its own every few minutes, straight from the
	document list, and that road never passed the checks the pushes make:
	under "Only chosen documents" it would bring the whole table in. It
	asks this first. None means the doctype is not restricted to chosen
	documents, so anything may come, as before; a list means only those.
	A row with no site covers every store, and one naming this store
	covers it alone -- the same rule the pushes apply.
	"""
	frappe.only_for("System Manager")
	if mode_of(doctype) != MODE_ONLY_CHOSEN:
		return None
	rows = frappe.get_all(
		INCLUSION_DOCTYPE,
		filters={"document_type": doctype},
		fields=["document_name", "site"],
	)
	return sorted({r["document_name"] for r in rows if not r["site"] or r["site"] == site_id})


@frappe.whitelist()
def chosen_count(doctype: str) -> dict:
	"""How many are chosen, against how many there are."""
	frappe.only_for("System Manager")
	return {
		"doctype": doctype,
		"mode": mode_of(doctype),
		"chosen": frappe.db.count(INCLUSION_DOCTYPE, {"document_type": doctype}),
		"total": frappe.db.count(doctype),
	}
