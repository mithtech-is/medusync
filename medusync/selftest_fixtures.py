# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
"""Fixtures the two in-site smoke tests share.

Both scripts need the same three things and both got the same thing
wrong when they were written: `Medusync Mapping.enabled` defaults to 1,
so a mapping created without saying otherwise is born enabled, and the
first save is refused by `gate_enable` - "Rehearse this mapping before
switching it on." Neither script could get past its own setup.

The fix belongs here rather than in each script. A mapping is created
switched OFF and switched on through `enable()`, which takes the same
studio route the desk button takes; the settings are snapshotted and put
back; and the store both scripts deliver to is built the same way. The
next script that needs a mapping gets it right for free, and a change to
the gate is answered in one place.
"""

import frappe

from medusync import config, sites

SETTINGS_DOCTYPE = config.SETTINGS_DOCTYPE
MAPPING_DOCTYPE = config.MAPPING_DOCTYPE
SITE_DOCTYPE = sites.SITE_DOCTYPE
LOG_DOCTYPE = "Medusync Log"

#: Plain settings fields the smoke tests overwrite. The passwords are
#: handled apart from these because they only come back through
#: get_password() - reading the column would snapshot ciphertext and
#: "restore" it as a plaintext secret nobody can sign with.
_SETTINGS_FIELDS = (
	"enabled",
	"medusa_url",
	"inbound_path",
	"request_timeout",
	"verify_ssl",
	"use_background_jobs",
	"max_attempts",
	"log_payloads",
	"allow_legacy_secret",
)
_SETTINGS_SECRETS = ("inbound_secret", "outbound_secret")


# -- Settings ---------------------------------------------------------


def snapshot_settings() -> dict:
	"""Everything the smoke tests are about to overwrite."""
	doc = frappe.get_single(SETTINGS_DOCTYPE)
	snap = {field: doc.get(field) for field in _SETTINGS_FIELDS}
	for field in _SETTINGS_SECRETS:
		snap[field] = doc.get_password(field, raise_exception=False)
	return snap


def configure_settings(**values):
	"""Point the Single at the test rig."""
	doc = frappe.get_single(SETTINGS_DOCTYPE)
	for key, value in values.items():
		doc.set(key, value)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache()
	return frappe.get_single(SETTINGS_DOCTYPE)


def restore_settings(snap: dict) -> None:
	"""Put the Single back exactly as it was.

	A smoke test that leaves the site switched on and pointed at a stub
	is worse than one that never ran: every save afterwards would try to
	deliver there. Both scripts call this from a `finally`.
	"""
	doc = frappe.get_single(SETTINGS_DOCTYPE)
	for key, value in snap.items():
		doc.set(key, value)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache()


# -- The store --------------------------------------------------------


def ensure_site(site_id: str, *, medusa_url=None, outbound_secret=None, inbound_secret=None,
                inbound_path="/webhooks/erpnext-inbound", enabled=1):
	"""The store the smoke test delivers to.

	`trip_after` is set absurdly high on purpose. The breaker exists so a
	store that is down stops holding up the queue, but these scripts
	deliberately provoke failures - with the stock threshold the breaker
	would open part way through and turn a real assertion into "Skipped".
	"""
	if frappe.db.exists(SITE_DOCTYPE, site_id):
		doc = frappe.get_doc(SITE_DOCTYPE, site_id)
	else:
		doc = frappe.new_doc(SITE_DOCTYPE)
		doc.site_id = site_id
	doc.title = "Selftest store"
	doc.enabled = enabled
	doc.medusa_url = medusa_url
	doc.inbound_path = inbound_path
	doc.outbound_secret = outbound_secret
	doc.inbound_secret = inbound_secret
	doc.verify_ssl = 0
	doc.request_timeout = 5
	doc.trip_after = 10000
	doc.consecutive_failures = 0
	doc.tripped_at = None
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	refresh_caches()
	return doc


def point_site_at(site_id: str, medusa_url: str) -> None:
	"""Re-point a store mid-test.

	Delivery reads the URL from the Medusync Site, never from the Single
	- `outbound.deliver` resolves it through `sites.endpoint(site)`. A
	test that changes `Medusync Settings.medusa_url` and expects the next
	delivery to go somewhere else is testing nothing.
	"""
	doc = frappe.get_doc(SITE_DOCTYPE, site_id)
	doc.medusa_url = medusa_url
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	refresh_caches()


# -- The mapping ------------------------------------------------------


def todo_mapping(title: str, **over) -> dict:
	"""The ToDo mapping both smoke tests reason about.

	`enabled` is 0 and must stay 0. The field defaults to 1, and a
	mapping that arrives enabled without a recorded rehearsal is refused
	by `MedusyncMapping.gate_enable`. Switch it on with `enable()`.
	"""
	spec = {
		"doctype": MAPPING_DOCTYPE,
		"title": title,
		"enabled": 0,
		"document_type": "ToDo",
		"direction": "Two-way",
		"docevents": "after_insert\non_update\non_trash",
		"key_field": "name",
		"field_map": [
			{"frappe_field": "description", "medusa_path": "title", "direction": "Two-way"},
			{"frappe_field": "status", "medusa_path": "state", "direction": "To Medusa"},
			{"frappe_field": "priority", "medusa_path": "priority", "direction": "From Medusa"},
		],
	}
	spec.update(over)
	return spec


def make_mapping(title: str, **over):
	"""A freshly created, still-disabled mapping under `title`."""
	drop(MAPPING_DOCTYPE, title)
	doc = frappe.get_doc(todo_mapping(title, **over))
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


def enable(mapping_name: str) -> dict:
	"""Switch a mapping on the way the desk does.

	Setting `enabled = 1` and saving is refused for a mapping that has
	never been rehearsed - that is the gate doing its job, not a bug to
	work around, so nothing here writes `tested_signature` by hand. The
	supported route is `studio.test_and_enable`: it rehearses, records
	the signature of exactly what it approved, and only then switches on.
	"""
	from medusync import studio

	result = studio.test_and_enable(mapping_name)
	frappe.db.commit()
	refresh_caches()
	return result


# -- Inbound, the way the receiver does it ----------------------------


def receive_inbound(mapping, envelope: dict) -> dict:
	"""Apply an inbound message inside the loop-guard context.

	The HTTP receiver never calls `api.apply_inbound` bare: it wraps every
	inbound write in `echo.inbound_context`, which raises
	`frappe.flags.medusync_inbound` so the write the message makes is not
	itself pushed straight back out. A smoke test that called
	`apply_inbound` directly would be exercising a path production never
	takes - and would "prove" the loop guard while never engaging it.
	"""
	from medusync import echo
	from medusync.api import apply_inbound

	with echo.inbound_context(correlation_id="selftest", origin="medusa:selftest"):
		return apply_inbound(mapping, envelope)


# -- Housekeeping -----------------------------------------------------


def refresh_caches() -> None:
	"""The hot paths answer from caches; a fixture change has to reach them."""
	sites.clear_cache()
	config.clear_mapping_cache()
	frappe.clear_cache()


def drop(doctype: str, name) -> None:
	if name and frappe.db.exists(doctype, name):
		frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)


def purge_logs(site_id: str) -> int:
	"""Remove the log rows this run created, so a second run counts from
	the same place a first one did."""
	names = frappe.get_all(LOG_DOCTYPE, filters={"site": site_id}, pluck="name")
	for name in names:
		frappe.delete_doc(LOG_DOCTYPE, name, ignore_permissions=True, force=True)
	frappe.db.commit()
	return len(names)


def outbound_count(docname: str | None = None) -> int:
	filters = {"direction": "Outbound"}
	if docname:
		filters["document_name"] = docname
	return frappe.db.count(LOG_DOCTYPE, filters)


# -- Reporting --------------------------------------------------------


def report(results) -> None:
	"""Print the tally and fail the process if anything failed."""
	passed = sum(1 for _, cond, _ in results if cond)
	failed = len(results) - passed
	for label, cond, detail in results:
		suffix = f"   <- {detail}" if detail is not None and not cond else ""
		print(("  PASS  " if cond else "  FAIL  ") + label + suffix)
	print(f"\n{passed} passed, {failed} failed")
	if failed:
		raise SystemExit(1)
