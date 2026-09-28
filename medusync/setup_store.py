# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
# For license information, please see license.txt

"""Everything a store needs that is NOT code, in one command.

Code travels in git. Settings and data do not: which items sell, the
store's URL and secrets, the warehouse a stock level comes from, the
fixed values an arriving order carries. On a fresh site all of that is
typed in by hand, from memory, and a missed line is found later by a
document that would not save.

        bench --site <site> execute medusync.setup_store.run \\
                --kwargs "{'config': '/path/to/store.json'}"

        # look first, write nothing:
        bench --site <site> execute medusync.setup_store.run \\
                --kwargs "{'config': '/path/to/store.json', 'dry_run': True}"

Idempotent: it writes only what differs, so it can be re-run after
editing the file, and a second run reports nothing to do. A sample
config is in ``docs/store.example.json``.

Secrets are never read from the config file. Export them, or leave them
out and type them in Desk:

        MEDUSYNC_INBOUND_SECRET    Medusa -> here
        MEDUSYNC_OUTBOUND_SECRET   here -> Medusa

What it deliberately does NOT do:

  * switch a mapping on. Neither side enables a mapping it has not
    rehearsed in its current shape, so that stays a person's decision
    (Test -> Rehearse here, Test push in Medusa, then enable).
  * choose documents. Which items sell is a commercial decision; use
    the Item list's "Medusa sync..." action, or
    ``medusync.selection.choose_all`` with filters.
  * touch anything in Medusa. The store's own half is listed in the
    report, to be done there.
"""

import json
import os

import frappe

from medusync import config, defaults, selection

SITE_DOCTYPE = "Medusync Site"

#: Read from the environment, never from the config file.
SECRET_ENV = {
	"inbound_secret": "MEDUSYNC_INBOUND_SECRET",
	"outbound_secret": "MEDUSYNC_OUTBOUND_SECRET",
}

#: Settings fields this script is allowed to write. Anything else in the
#: config's "settings" block is reported, not guessed at.
SETTINGS_FIELDS = (
	"enabled",
	"medusa_url",
	"inbound_path",
	"request_timeout",
	"verify_ssl",
	"use_background_jobs",
	"max_attempts",
	"log_retention_days",
	"log_payloads",
	"inventory_source_warehouse",
	"pricing_selling_price_list",
	"products_doctype",
	"allow_medusa_catalogue_updates",
)

#: Site fields this script is allowed to write.
SITE_FIELDS = (
	"title",
	"enabled",
	"medusa_url",
	"inbound_path",
	"request_timeout",
	"verify_ssl",
	"handler_pack",
	"default_account_manager",
	"order_document",
	"submit_documents",
	"record_payments",
	"mode_of_payment",
	"invoice_numbering",
	"erpnext_invoice_series",
	"store_invoice_prefix",
	"send_invoice_to_store",
	"invoice_print_format",
	"trip_after",
)


def run(config_file: str | None = None, config: str | None = None, dry_run: bool = False) -> dict:
	"""Apply a store config. Returns what changed and what is left."""
	path = config_file or config
	if not path:
		frappe.throw("setup_store.run needs config='/path/to/store.json'")

	cfg = _load(path)
	report = {
		"config": path,
		"dry_run": bool(dry_run),
		"changed": [],
		"unchanged": [],
		"todo": [],
		"warnings": [],
	}

	_mappings_exist(report, dry_run)
	_settings(cfg, report, dry_run)
	_selection(cfg, report, dry_run)
	_store(cfg, report, dry_run)
	_constants(cfg, report, dry_run)
	_left_to_do(cfg, report)

	if dry_run:
		frappe.db.rollback()
	else:
		frappe.db.commit()
	_print(report)
	return report


def _load(path: str) -> dict:
	with open(os.path.expanduser(path)) as fh:
		cfg = json.load(fh)
	if not isinstance(cfg, dict):
		frappe.throw("the config must be a JSON object")
	for key in cfg:
		if key not in ("settings", "selection", "store", "mapping_constants", "notes"):
			frappe.throw(f"unknown config section: {key}")
	return cfg


def _mappings_exist(report: dict, dry_run: bool) -> None:
	"""`install-app` does not create the default mappings; this does."""
	defaults.ensure_fixed_value_options()
	if frappe.db.count(config.MAPPING_DOCTYPE):
		return
	if dry_run:
		report["changed"].append("would create the default mappings (none on this site)")
		return
	made = defaults.apply_defaults(reason="setup_store")
	report["changed"].append(f"created the default mappings ({len(made.get('mappings', []))})")


def _write(doc, values: dict, allowed: tuple, where: str, report: dict, dry_run: bool) -> bool:
	"""Set the fields that differ. Returns True when something differed."""
	touched = False
	for field, wanted in (values or {}).items():
		if field in SECRET_ENV:
			continue
		if field not in allowed:
			report["warnings"].append(f"{where}: ignoring '{field}' (not a field this script writes)")
			continue
		if not doc.meta.has_field(field):
			report["warnings"].append(f"{where}: this site's {doc.doctype} has no field '{field}'")
			continue
		current = doc.get(field)
		if _same(current, wanted):
			report["unchanged"].append(f"{where}.{field} = {_show(current)}")
			continue
		report["changed"].append(f"{where}.{field}: {_show(current)} -> {_show(wanted)}")
		if not dry_run:
			doc.set(field, wanted)
		touched = True
	return touched


def _secrets(doc, where: str, report: dict, dry_run: bool) -> bool:
	touched = False
	for field, env in SECRET_ENV.items():
		wanted = os.environ.get(env)
		if not wanted:
			if not doc.get_password(field, raise_exception=False):
				report["todo"].append(f"{where}: {field} is empty — export {env} and re-run, or type it in Desk")
			continue
		if doc.get_password(field, raise_exception=False) == wanted:
			report["unchanged"].append(f"{where}.{field} (from {env})")
			continue
		report["changed"].append(f"{where}.{field} set from {env}")
		if not dry_run:
			doc.set(field, wanted)
		touched = True
	return touched


def _same(current, wanted) -> bool:
	if isinstance(wanted, bool):
		return bool(current) == wanted
	if isinstance(wanted, int) and not isinstance(wanted, bool):
		return int(current or 0) == wanted
	return (current or "") == (wanted or "")


def _show(value) -> str:
	return "(blank)" if value in (None, "") else str(value)


def _settings(cfg: dict, report: dict, dry_run: bool) -> None:
	doc = frappe.get_doc(config.SETTINGS_DOCTYPE)
	touched = _write(doc, cfg.get("settings"), SETTINGS_FIELDS, "Settings", report, dry_run)
	touched = _secrets(doc, "Settings", report, dry_run) or touched
	if touched and not dry_run:
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		frappe.clear_cache(doctype=config.SETTINGS_DOCTYPE)


def _selection(cfg: dict, report: dict, dry_run: bool) -> None:
	"""Which doctypes are under selection, and in which mode.

	"Only chosen documents" is the safe one on a live site: a catalogue
	of 60,000 items under "Every document unless excluded" goes out in
	full the moment the mapping is switched on.
	"""
	wanted = cfg.get("selection") or []
	if not wanted:
		return
	doc = frappe.get_doc(config.SETTINGS_DOCTYPE)
	touched = False
	for entry in wanted:
		doctype = entry.get("document_type")
		mode = entry.get("mode") or selection.MODE_ONLY_CHOSEN
		if mode not in (selection.MODE_ONLY_CHOSEN, selection.MODE_UNLESS_EXCLUDED):
			frappe.throw(f"selection mode must be one of {selection.MODE_ONLY_CHOSEN!r} / {selection.MODE_UNLESS_EXCLUDED!r}")
		if not frappe.db.exists("DocType", doctype):
			report["warnings"].append(f"Selection: no DocType {doctype} on this site")
			continue
		row = next((r for r in doc.selection_doctypes if r.document_type == doctype), None)
		if row is None:
			report["changed"].append(f"Selection: {doctype} -> {mode}")
			if not dry_run:
				doc.append("selection_doctypes", {"document_type": doctype, "mode": mode, "enabled": 1})
			touched = True
			continue
		if row.mode != mode or not row.enabled:
			report["changed"].append(f"Selection: {doctype}: {row.mode} -> {mode}")
			if not dry_run:
				row.mode = mode
				row.enabled = 1
			touched = True
		else:
			report["unchanged"].append(f"Selection: {doctype} = {mode}")
		if mode == selection.MODE_ONLY_CHOSEN:
			chosen = selection.chosen_count(doctype) or {}
			report["todo"].append(
				f"Selection: {doctype} is on 'Only chosen documents' — {chosen.get('chosen', 0)} chosen "
				f"of {chosen.get('total', 0)}. Choose the rest from the {doctype} list -> Medusa sync..."
			)
	if touched and not dry_run:
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		frappe.clear_cache(doctype=config.SETTINGS_DOCTYPE)


def _store(cfg: dict, report: dict, dry_run: bool) -> None:
	spec = cfg.get("store") or {}
	if not spec:
		return
	site_id = spec.get("site_id")
	if not site_id:
		frappe.throw("store.site_id is required (it is the id Medusa signs its messages with)")

	name = frappe.db.get_value(SITE_DOCTYPE, {"site_id": site_id}, "name")
	if name:
		doc = frappe.get_doc(SITE_DOCTYPE, name)
		where = f"Store {site_id}"
	else:
		doc = frappe.new_doc(SITE_DOCTYPE)
		doc.site_id = site_id
		where = f"Store {site_id} (new)"
		report["changed"].append(f"{where}: creating")

	values = {k: v for k, v in spec.items() if k not in ("site_id", "warehouses", "price_lists")}
	touched = _write(doc, values, SITE_FIELDS, where, report, dry_run) or not name
	touched = _secrets(doc, where, report, dry_run) or touched
	touched = _rows(doc, "warehouses", "warehouse", spec.get("warehouses"), ("location_id",), where, report, dry_run) or touched
	touched = _rows(doc, "price_lists", "price_list", spec.get("price_lists"), ("direction",), where, report, dry_run) or touched

	if touched and not dry_run:
		doc.flags.ignore_permissions = True
		if name:
			doc.save(ignore_permissions=True)
		else:
			doc.insert(ignore_permissions=True)


def _rows(doc, table: str, key: str, wanted, fields: tuple, where: str, report: dict, dry_run: bool) -> bool:
	"""Child rows keyed on one link field. Rows the config does not name
	are left alone — this fills a store in, it does not prune it."""
	if not wanted:
		return False
	touched = False
	for entry in wanted:
		value = entry.get(key)
		if not value:
			frappe.throw(f"{where}.{table}: every row needs '{key}'")
		row = next((r for r in doc.get(table) or [] if r.get(key) == value), None)
		if row is None:
			report["changed"].append(f"{where}.{table}: + {value}")
			if not dry_run:
				doc.append(table, {key: value, "enabled": 1, **{f: entry.get(f) for f in fields if entry.get(f) is not None}})
			touched = True
			continue
		for field in fields:
			if entry.get(field) is None or _same(row.get(field), entry[field]):
				continue
			report["changed"].append(f"{where}.{table}.{value}.{field}: {_show(row.get(field))} -> {_show(entry[field])}")
			if not dry_run:
				row.set(field, entry[field])
			touched = True
		if not row.enabled:
			report["changed"].append(f"{where}.{table}.{value}: enabled")
			if not dry_run:
				row.enabled = 1
			touched = True
	return touched


def _constants(cfg: dict, report: dict, dry_run: bool) -> None:
	"""Fixed values an arriving document carries.

	A store order does not know a Sales Type or which of the company's
	addresses it was sold from; ERPNext insists on them anyway. A fixed
	value in the field map answers that once, for every order, instead
	of a person editing each draft.

	Editing a field map invalidates the rehearsal on BOTH sides, so a
	mapping this touches has to be rehearsed and switched on again.
	"""
	wanted = cfg.get("mapping_constants") or {}
	for doctype, constants in wanted.items():
		names = frappe.get_all(config.MAPPING_DOCTYPE, filters={"document_type": doctype}, pluck="name")
		if not names:
			report["warnings"].append(f"Constants: no mapping for {doctype}")
			continue
		for name in names:
			doc = frappe.get_doc(config.MAPPING_DOCTYPE, name)
			touched = False
			for field, value in constants.items():
				row = next((r for r in doc.field_map if r.frappe_field == field), None)
				if row is None:
					report["changed"].append(f"Mapping {doc.name}: + {field} = {value} (fixed)")
					if not dry_run:
						doc.append("field_map", {"frappe_field": field, "constant_value": value, "direction": "From Medusa"})
					touched = True
					continue
				if _same(row.constant_value, value):
					report["unchanged"].append(f"Mapping {doc.name}: {field} = {value}")
					continue
				report["changed"].append(f"Mapping {doc.name}: {field}: {_show(row.constant_value)} -> {value} (fixed)")
				if not dry_run:
					row.constant_value = value
				touched = True
			if not touched:
				continue
			if not dry_run:
				doc.flags.ignore_permissions = True
				doc.save(ignore_permissions=True)
			report["todo"].append(
				f"Mapping {doc.name}: field map changed — rehearse it again here (Test -> Rehearse) "
				"and in Medusa (Test push) before switching it on"
			)


def _left_to_do(cfg: dict, report: dict) -> None:
	"""The half of setup no script can do."""
	for row in frappe.get_all(
		config.MAPPING_DOCTYPE,
		fields=["name", "document_type", "enabled", "last_test_status"],
		order_by="document_type",
	):
		if row.enabled:
			continue
		report["todo"].append(
			f"Mapping {row.name} ({row.document_type}) is off, rehearsal {row.last_test_status or 'Untested'} — "
			"rehearse here and in Medusa, then switch it on in Medusa"
		)
	if not frappe.db.get_single_value(config.SETTINGS_DOCTYPE, "enabled"):
		report["todo"].append("Settings: Enable Sync is off — nothing is sent or accepted until it is on")
	report["todo"].append(
		"In Medusa: region + tax region with a tax provider, a sales channel and a shipping option, "
		"the publishable key on the storefront, and this site's ERPNext URL/key/secret in its env"
	)
	report["todo"].append(
		"Both clocks must agree — signed messages carry a timestamp. On the ERPNext host: sudo chronyc makestep"
	)


def _print(report: dict) -> None:
	head = "would change" if report["dry_run"] else "changed"
	print(f"\nmedusync setup — {report['config']}")
	for key, title in (("changed", head), ("warnings", "warnings"), ("todo", "left to do")):
		rows = report.get(key) or []
		print(f"\n{title} ({len(rows)}):")
		for line in rows:
			print(f"  - {line}")
	print(f"\nalready as asked: {len(report.get('unchanged') or [])} settings\n")
