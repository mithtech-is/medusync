# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
# For license information, please see license.txt

"""Try a mapping before you trust it.

A mapping is a small program somebody wrote in a form. Until now the only
way to learn what it did was to save a real document and read the log
afterwards — a poor place to discover a wrong field name, and a worse one
to discover a condition that excludes everything.

This module answers three questions without touching anything:

    what does a record on this side look like?   sample_for / fields_of
    what would we send?                          dry_run_outbound
    what would arrive do?                        dry_run_inbound

The dry runs share their decisions with the real paths rather than
re-deriving them: outbound calls the same `build_payload` and the same
condition and selection checks, inbound calls the same `plan_inbound`
that `apply_inbound` executes. A studio that reasons independently is
worse than no studio, because it is believed.

"Touches nothing" is the load-bearing claim. Validation is the one place
that has to run real document code, so it runs inside a savepoint that is
always rolled back, with the inbound flag set so no hook of ours reacts
to it.
"""

import json

import frappe
from frappe.utils import now_datetime

from medusync import api, catalogue, config, links, outbound, selection, sites

MAPPING_DOCTYPE = config.MAPPING_DOCTYPE

#: Structure, not data. Nothing here can be mapped to anything.
LAYOUT_FIELDTYPES = ("Section Break", "Column Break", "Tab Break", "HTML", "Button", "Fold")

#: Never invented and never shown: a sample is for reading, and these
#: either mean nothing out of context or should not be read at all.
SKIP_FIELDTYPES = ("Password", "Signature", "Barcode", "Geolocation")

_SAVEPOINT = "medusync_studio"


# ── What a record looks like ─────────────────────────────────────────


def _latest(doctype: str) -> str | None:
	"""The most recently touched record, or None if there are none."""
	try:
		rows = frappe.get_all(doctype, fields=["name"], order_by="modified desc", limit=1)
	except Exception:
		return None
	return rows[0].name if rows else None


def _readable(data: dict) -> dict:
	return {k: v for k, v in (data or {}).items() if not str(k).startswith("_")}


def _synthesised_value(df, depth: int):
	"""A plausible value for one field.

	Plausible matters more than pretty: an operator picking fields needs
	to see that a Select is one of a fixed set and that a Link points at
	something real, because those are what break in production.
	"""
	fieldtype = df.fieldtype
	if fieldtype in SKIP_FIELDTYPES:
		return None
	if fieldtype == "Check":
		return int(df.default or 0)
	if fieldtype in ("Int", "Long Int"):
		return int(df.default or 1)
	if fieldtype in ("Float", "Currency", "Percent"):
		return float(df.default or 1)
	if fieldtype == "Date":
		return frappe.utils.today()
	if fieldtype == "Datetime":
		return str(now_datetime())
	if fieldtype == "Time":
		return "09:00:00"
	if fieldtype == "Select":
		options = [o for o in (df.options or "").splitlines() if o.strip()]
		return df.default or (options[0] if options else None)
	if fieldtype in ("Link", "Dynamic Link"):
		# A real one where possible: a made-up link is exactly the value
		# that would pass a dry run and fail for real.
		if fieldtype == "Link" and df.options:
			return df.default or _latest(df.options)
		return df.default
	if fieldtype in ("Table", "Table MultiSelect"):
		if depth <= 0 or not df.options:
			return []
		return [_synthesise(df.options, depth - 1)]
	if fieldtype in ("Attach", "Attach Image", "Image"):
		return None
	return df.default or "Sample %s" % (df.label or df.fieldname)


def _synthesise(doctype: str, depth: int = 1) -> dict:
	meta = frappe.get_meta(doctype)
	out = {"doctype": doctype, "name": "SAMPLE-%s" % frappe.scrub(doctype).upper()}
	for df in meta.fields:
		if df.fieldtype in LAYOUT_FIELDTYPES:
			continue
		out[df.fieldname] = _synthesised_value(df, depth)
	return out


def sample_for(doctype: str, name: str | None = None) -> dict:
	"""A record of this doctype to reason about.

	A real one when the site has any: only a real record shows the shapes
	an operator will actually meet, empty fields included. A synthesised
	one otherwise, because a brand-new mapping is exactly when a sample is
	most useful and exactly when there may be nothing to sample.
	"""
	if not doctype or not frappe.db.exists("DocType", doctype):
		frappe.throw(
			frappe._("There is no DocType called {0}.").format(doctype or "?"),
			frappe.DoesNotExistError,
		)
	record = name or _latest(doctype)
	if record and frappe.db.exists(doctype, record):
		doc = frappe.get_doc(doctype, record)
		return {
			"doctype": doctype,
			"name": doc.name,
			"from_record": True,
			"data": _readable(doc.as_dict(convert_dates_to_str=True)),
		}
	return {
		"doctype": doctype,
		"name": None,
		"from_record": False,
		"data": _synthesise(doctype),
	}


def fields_of(doctype: str) -> list[dict]:
	"""The field picker: every mappable field, with the value the sample
	actually had in it."""
	sample = sample_for(doctype)["data"]
	meta = frappe.get_meta(doctype)
	out = [
		{
			"fieldname": "name",
			"label": "Name (ID)",
			"fieldtype": "Data",
			"options": None,
			"reqd": 1,
			"sample": sample.get("name"),
		}
	]
	for df in meta.fields:
		if df.fieldtype in LAYOUT_FIELDTYPES:
			continue
		out.append(
			{
				"fieldname": df.fieldname,
				"label": df.label or df.fieldname,
				"fieldtype": df.fieldtype,
				"options": df.options,
				"reqd": int(df.reqd or 0),
				"fetch_from": df.fetch_from or None,
				"read_only": int(df.read_only or 0),
				"default": df.default or None,
				"sample": sample.get(df.fieldname),
			}
		)
	return out


def sample_inbound(mapping) -> dict:
	"""What a payload from the other side would plausibly look like.

	Built by running this site's own sample back through the field map,
	so the names are the ones the far side uses. Not a substitute for a
	real captured payload, but enough to exercise the translation.
	"""
	data = sample_for(mapping.document_type)["data"]
	if mapping.include_all_fields or not mapping.field_map:
		return dict(data)
	out = {}
	for row in mapping.field_map:
		if row.direction in api._NOT_INBOUND:
			continue
		out[row.medusa_path or row.frappe_field] = data.get(row.frappe_field)
	key = mapping.key_field or "name"
	out.setdefault(key, data.get(key))
	return out


# ── What would we send ───────────────────────────────────────────────


def dry_run_outbound(mapping_name: str, docname: str | None = None) -> dict:
	"""What this mapping would put on the wire, and who would get it."""
	mapping = frappe.get_doc(MAPPING_DOCTYPE, mapping_name)
	if mapping.direction == "From Medusa":
		return {
			"ok": False,
			"message": "This mapping is From Medusa. Nothing leaves this site through it.",
		}

	sample = sample_for(mapping.document_type, name=docname)
	doc = (
		frappe.get_doc(mapping.document_type, sample["name"])
		if sample["from_record"]
		else _in_memory(mapping.document_type, sample["data"])
	)

	payload = outbound.build_payload(mapping, doc)
	condition_passes = outbound._condition_passes(mapping, doc)
	events = [mapping.resolved_event_name(e) for e in mapping.docevent_list()]

	site_rows = []
	for site in sites.sites_for_mapping(mapping):
		allowed = True
		reason = None
		if sample["from_record"]:
			allowed = selection.is_allowed(doc.doctype, doc.name, site["site_id"], doc=doc)
			if not allowed:
				reason = "excluded by the sync selection on this document"
		site_rows.append({"site_id": site["site_id"], "allowed": allowed, "reason": reason})
	if not site_rows:
		site_rows = []

	missing_there, store_note = _store_requirements(mapping)
	warnings = _outbound_warnings(mapping, events, condition_passes, site_rows)
	if store_note:
		warnings.append(store_note)
	errors = []
	if missing_there:
		errors.append(
			frappe._("The store will not create this record without {0}. Map each one from a field here.").format(
				", ".join(missing_there)
			)
		)
	return {
		"ok": True,
		"direction": "outbound",
		"mapping": mapping.name,
		"doctype": mapping.document_type,
		"sample": sample,
		"events": events,
		"payload": payload,
		"condition": mapping.condition or None,
		"condition_passes": condition_passes,
		"sites": site_rows,
		"warnings": warnings,
		"errors": errors,
		"missing_in_store": missing_there,
	}


def _store_requirements(mapping) -> tuple[list[str], str | None]:
	"""What the store will not create this record without, and nothing
	here fills. Asked of the store itself, so the answer is its own; a
	store that cannot be reached is reported, not treated as satisfied.

	Covered means a pair that flows to the store from a field here — a
	fixed value only ever flows in, so it covers nothing over there.
	"""
	from medusync import medusa_fields

	if not mapping.get("medusa_entity"):
		return [], None
	try:
		body = medusa_fields.fields(mapping.medusa_entity, site_id=mapping.get("site") or None)
	except Exception as exc:
		return [], frappe._("Could not read what the store requires: {0}").format(str(exc)[:200])
	required = [f for f in (body.get("fields") or []) if f.get("required")]
	covered = {
		row.medusa_path
		for row in (mapping.field_map or [])
		if row.medusa_path
		and row.frappe_field
		and not row.get("constant_value")
		and row.direction in ("Two-way", "To Medusa")
	}
	return [f.get("label") or f.get("path") for f in required if f.get("path") not in covered], None


def _outbound_warnings(mapping, events, condition_passes, site_rows) -> list[str]:
	"""Things that are not errors but mean nothing will ever happen.

	A mapping that is enabled, valid and silent is the hardest fault to
	diagnose from the outside, so it is named here rather than left for
	somebody to notice a week later.
	"""
	warnings = []
	if not events:
		warnings.append("No document events are selected, so nothing will ever trigger this.")
	if not condition_passes:
		warnings.append("The condition does not pass for this sample, so nothing would be sent.")
	if not site_rows:
		warnings.append("No enabled store matches this mapping, so there is nowhere to send it.")
	elif not any(row["allowed"] for row in site_rows):
		warnings.append("Every matching store is excluded for this document.")
	return warnings


def _in_memory(doctype: str, data: dict):
	"""A document object that is never saved. Used only when the site has
	no real record of this doctype to reason about."""
	doc = frappe.new_doc(doctype)
	for key, value in (data or {}).items():
		if key in ("doctype", "name"):
			continue
		try:
			doc.set(key, value)
		except Exception:
			# A synthesised child row the doctype will not take is not a
			# reason to refuse the whole rehearsal.
			continue
	return doc


# ── What would an inbound payload do ─────────────────────────────────


def _rehearsal_site(mapping) -> str | None:
	"""The store a rehearsed arrival pretends to come from: the mapping's
	own store, else the only enabled one. None when that is ambiguous."""
	if mapping.get("site"):
		return mapping.site
	enabled = frappe.get_all(sites.SITE_DOCTYPE, filters={"enabled": 1}, pluck="name")
	return enabled[0] if len(enabled) == 1 else None


def _with_insert_defaults(doctype: str, payload: dict, site_id: str | None) -> dict:
	"""The payload a real insert would save: what arrived, plus what the
	commerce pack fills on a new record (the store's default account
	manager among it). The rehearsal must see those, or it reports a field
	as missing that the real sync would have filled."""
	try:
		from medusync.handlers.commerce import mapped as commerce
	except Exception:
		return dict(payload)
	doc = frappe.new_doc(doctype)
	doc.update(payload)
	before = {f.fieldname: doc.get(f.fieldname) for f in doc.meta.fields}
	previous = frappe.flags.get("medusync_site_id")
	frappe.flags.medusync_site_id = site_id
	try:
		commerce._apply_defaults(doc, doctype)
	finally:
		frappe.flags.medusync_site_id = previous
	out = dict(payload)
	for f in doc.meta.fields:
		value = doc.get(f.fieldname)
		if value != before.get(f.fieldname):
			out[f.fieldname] = value
	return out


def dry_run_inbound(mapping_name: str, sample: dict | None = None) -> dict:
	"""What would happen if this arrived. Nothing is written."""
	mapping = frappe.get_doc(MAPPING_DOCTYPE, mapping_name)
	if mapping.direction == "To Medusa":
		return {
			"ok": False,
			"message": "This mapping is To Medusa. Nothing arrives through it.",
		}

	data = sample if sample is not None else sample_inbound(mapping)
	event = mapping.medusa_event or "%s.updated" % frappe.scrub(mapping.document_type)
	plan = api.plan_inbound(mapping, {"data": data, "event": event})

	changes = {}
	errors = []
	if plan.action == "updated" and plan.existing:
		# `medusa_customer_id` and the other link keys are not columns: they
		# live in Medusync Link on purpose. Asking the database for one is an
		# "Unknown column" error, so query only the real fields and read the
		# link keys the way a mapping names them -- the same split the real
		# apply makes with links.take_link_keys before it writes.
		columns = [field for field in plan.payload if not links.is_link_key(field)]
		before = (
			frappe.db.get_value(mapping.document_type, plan.existing, columns or ["name"], as_dict=True)
			or {}
		)
		linked = [field for field in plan.payload if links.is_link_key(field)]
		if linked:
			existing_doc = frappe.get_doc(mapping.document_type, plan.existing)
			for field in linked:
				before[field] = links.value_for(existing_doc, field)
		for field, after in plan.payload.items():
			if before.get(field) != after:
				changes[field] = {"before": before.get(field), "after": after}
		errors = _validate_only(mapping.document_type, plan.payload, existing=plan.existing)
	elif plan.action == "created":
		payload = _with_insert_defaults(mapping.document_type, plan.payload, _rehearsal_site(mapping))
		changes = {field: {"before": None, "after": after} for field, after in payload.items()}
		errors = _validate_only(mapping.document_type, payload, key=(plan.key_field, plan.key_value))

	return {
		"ok": True,
		"direction": "inbound",
		"mapping": mapping.name,
		"doctype": mapping.document_type,
		"event": event,
		"sample": data,
		"payload": plan.payload,
		"action": plan.action,
		"existing": plan.existing,
		"reason": plan.reason,
		"changes": changes,
		"errors": errors,
		"warnings": _inbound_warnings(plan, errors),
	}


def _inbound_warnings(plan, errors) -> list[str]:
	warnings = []
	if plan.action == "skipped":
		warnings.append("Nothing would happen: %s." % (plan.reason or "the mapping refused it"))
	if not plan.payload:
		warnings.append("The field map carries nothing inbound, so there is nothing to write.")
	if errors:
		warnings.append("This document would be refused by ERPNext.")
	return warnings


def _validate_only(doctype: str, payload: dict, existing: str | None = None, key=None) -> list[str]:
	"""Run the doctype's own validation and throw the result away.

	The only part of a rehearsal that has to execute real document code: a
	payload can translate perfectly and still be refused by the doctype,
	and that is worth knowing before a queue of 5xxs says so.

	Two safeguards. The savepoint is always rolled back, so anything the
	validation wrote (a naming series, a child row) never lands. The
	inbound flag is set, so if some validation does save a related
	document our own hooks treat it as an inbound write and stay quiet.
	"""
	errors: list[str] = []
	previous_flag = frappe.flags.get("medusync_inbound")
	frappe.flags.medusync_inbound = True
	frappe.db.savepoint(_SAVEPOINT)
	try:
		# A pack that builds this doctype from more than the field map (a
		# sales document's customer and lines) stands the document up the
		# way a real arrival would, so what ERPNext refuses here is what it
		# would refuse then.
		from medusync import handlers

		builder = handlers.rehearsal_builder(doctype)
		doc = builder(doctype, payload, existing) if builder else None
		if doc is None:
			doc = (
				frappe.get_doc(doctype, existing)
				if existing
				else frappe.new_doc(doctype)
			)
			doc.update(payload)
		if key and key[0] and key[0] != "name" and key[1]:
			doc.set(key[0], key[1])
		doc.flags.ignore_permissions = True
		# Frappe sets `_action` on the way into a real save, and plenty of
		# doctype validation reads it. Without it the rehearsal fails with
		# an AttributeError that says nothing about the payload.
		doc._action = "save"
		doc.run_method("validate")
		# Child rows are given their parent during insert, which a rehearsal
		# never reaches: without one the mandatory check reports "parent"
		# missing on rows the doctype's own validation has just added (a
		# payment schedule, say), which is true of no real save.
		doc.name = doc.name or "rehearsal"
		doc.set_parent_in_children()
		doc._validate_mandatory()
		doc._validate_selects()
		doc._validate_links()
	except Exception as exc:
		errors.append(_readable_error(exc))
	finally:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.flags.medusync_inbound = previous_flag
		frappe.clear_last_message()
	return errors


def _readable_error(exc) -> str:
	"""What ERPNext actually complained about.

	`frappe.throw` puts the sentence a human should read into the message
	log and raises something far less useful, so prefer the log.
	"""
	try:
		messages = frappe.get_message_log() or []
		if messages:
			last = messages[-1]
			text = last.get("message") if isinstance(last, dict) else str(last)
			if text:
				return frappe.utils.strip_html(str(text)).strip()
	except Exception:
		pass
	return str(exc) or exc.__class__.__name__


# ── Running both, and the gate ───────────────────────────────────────


def run(mapping_name: str, docname: str | None = None, sample: dict | None = None) -> dict:
	"""Rehearse every direction this mapping actually uses.

	Pass or fail is deliberately narrow. A structural problem fails: the
	mapping cannot translate, or the document ERPNext would build is one
	it would refuse. A condition that does not match the sample does not
	fail — excluding this particular record may be exactly the intent —
	but it is reported as a warning, because a mapping that can never fire
	is the hardest fault to see from the outside.
	"""
	mapping = frappe.get_doc(MAPPING_DOCTYPE, mapping_name)
	result = {
		"mapping": mapping.name,
		"title": mapping.title,
		"direction": mapping.direction,
		"outbound": None,
		"inbound": None,
		"warnings": [],
		"errors": [],
	}

	if mapping.direction in ("Two-way", "To Medusa"):
		try:
			result["outbound"] = dry_run_outbound(mapping.name, docname=docname)
		except Exception as exc:
			result["errors"].append("Outbound rehearsal failed: %s" % _readable_error(exc))

	if mapping.direction in ("Two-way", "From Medusa"):
		try:
			result["inbound"] = dry_run_inbound(mapping.name, sample)
		except Exception as exc:
			result["errors"].append("Inbound rehearsal failed: %s" % _readable_error(exc))

	for half in ("outbound", "inbound"):
		report = result[half]
		if not report:
			continue
		if not report.get("ok"):
			result["errors"].append(report.get("message") or "%s rehearsal refused" % half)
			continue
		result["warnings"].extend(report.get("warnings") or [])
		result["errors"].extend(report.get("errors") or [])

	result["passed"] = not result["errors"]
	return result


def record_result(mapping_name: str, *, passed: bool, report: str = "") -> None:
	"""Remember that this exact mapping was rehearsed.

	Written with `db.set_value` and no modification stamp on purpose: a
	rehearsal is not an edit, and a save here would bump the version and
	change the very signature it just approved.
	"""
	mapping = frappe.get_cached_doc(MAPPING_DOCTYPE, mapping_name)
	frappe.db.set_value(
		MAPPING_DOCTYPE,
		mapping_name,
		{
			"tested_signature": mapping.test_signature() if passed else None,
			"last_test_at": now_datetime(),
			"last_test_status": "Passed" if passed else "Failed",
			"last_test_report": (report or "")[:100000],
		},
		update_modified=False,
	)
	frappe.clear_cache(doctype=MAPPING_DOCTYPE)


@frappe.whitelist()
def test_and_enable(mapping_name: str, docname: str | None = None, sample=None) -> dict:
	"""Rehearse, record, and switch it on if it held up."""
	frappe.only_for("System Manager")
	if isinstance(sample, str):
		sample = json.loads(sample or "null")
	result = run(mapping_name, docname=docname, sample=sample)
	record_result(mapping_name, passed=result["passed"], report=json.dumps(result, indent=2, default=str))
	if result["passed"]:
		doc = frappe.get_doc(MAPPING_DOCTYPE, mapping_name)
		if not doc.enabled:
			doc.enabled = 1
			doc.save(ignore_permissions=True)
		result["enabled"] = True
	else:
		result["enabled"] = bool(frappe.db.get_value(MAPPING_DOCTYPE, mapping_name, "enabled"))
	return result


# ── Rehearsing against the real store ────────────────────────────────


@frappe.whitelist()
def send_test(mapping_name: str, docname: str | None = None) -> dict:
	"""Send a real, signed, marked-as-a-rehearsal event to every store.

	The local dry run proves the translation. This proves the rest: the
	shared secret, the network between the two machines, the replay
	window, and the far side's own verdict on the payload. It is the only
	check that can fail for a reason the local one cannot see, which is
	most of the reasons a sync actually fails.

	The envelope carries `dry_run`, so the far side decides and reports
	but writes nothing. The row it leaves here is marked `is_test`, so the
	retry sweep ignores it, it cannot suppress a genuine event as a
	duplicate, and it is pruned within a day.
	"""
	frappe.only_for("System Manager")
	report = dry_run_outbound(mapping_name, docname=docname)
	if not report.get("ok"):
		return report

	mapping = frappe.get_cached_doc(MAPPING_DOCTYPE, mapping_name)
	event = (report["events"] or [mapping.resolved_event_name("on_update")])[0]
	outbound.emit(
		event,
		report["payload"],
		ref="studio-%s-%s" % (frappe.scrub(mapping.name), frappe.generate_hash(length=6)),
		is_test=True,
	)
	report["sent"] = True
	report["event_sent"] = event
	return report


# ── Desk entry points ────────────────────────────────────────────────


@frappe.whitelist()
def get_sample(doctype: str, name: str | None = None) -> dict:
	frappe.only_for("System Manager")
	if name and not frappe.has_permission(doctype, "read", doc=name):
		frappe.throw(frappe._("You are not allowed to read {0} {1}.").format(doctype, name))
	return sample_for(doctype, name=name)


@frappe.whitelist()
def get_fields(doctype: str) -> list[dict]:
	frappe.only_for("System Manager")
	return fields_of(doctype)


@frappe.whitelist()
def rehearse(mapping_name: str, docname: str | None = None, sample=None) -> dict:
	"""Run both directions and report, without recording or enabling."""
	frappe.only_for("System Manager")
	if isinstance(sample, str):
		sample = json.loads(sample or "null")
	return run(mapping_name, docname=docname, sample=sample)
