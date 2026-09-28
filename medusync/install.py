# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
# For license information, please see license.txt

import frappe

def after_install():
	"""Create the Single with safe defaults, switched OFF.

	Installing the app must never start moving data on its own — the
	operator sets the URL and secrets, then flips Enable Sync.

	Handler packs are not registered here: which packs a site loads is a
	per-site choice (`site_config.json` → `medusync_handler_packs`) and the
	registry builds itself lazily. See medusync/handlers/__init__.py.
	"""
	settings = frappe.get_single("Medusync Settings")
	settings.enabled = 0
	if not settings.inbound_path:
		settings.inbound_path = "/webhooks/erpnext-inbound"
	if not settings.request_timeout:
		settings.request_timeout = 15
	if not settings.max_attempts:
		settings.max_attempts = 3
	if not settings.log_retention_days:
		settings.log_retention_days = 180
	settings.verify_ssl = 1
	settings.use_background_jobs = 1
	settings.log_payloads = 1
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()

	# Deliberately no fields on any doctype: Medusa ids live in Medusync
	# Link and the sync decision in Medusync Exclusion / Inclusion.


def after_migrate():
	"""Runs on every `bench migrate`. Must never raise.

	Applying the shipped defaults and looking for drift are both things an
	operator wants done on upgrade and neither is worth failing a migrate
	over. A site that will not migrate because one mapping went stale is a
	site nobody upgrades.
	"""
	from medusync import defaults, drift, sync_field

	try:
		sync_field.sync_fields()
	except Exception:
		frappe.log_error(title="medusync could not place the sync tick", message=frappe.get_traceback())

	# Before the defaults land: a shipped fixed value names an option in
	# another list, and the mapping is only usable once that option exists.
	try:
		added = defaults.ensure_fixed_value_options()
		if added:
			print(
				"medusync: added %s option(s) the shipped mappings point at: %s"
				% (len(added), ", ".join("%s %r" % (a["doctype"], a["name"]) for a in added))
			)
	except Exception:
		frappe.log_error(title="medusync could not add fixed-value options", message=frappe.get_traceback())

	try:
		if defaults.installed_version() < defaults.DEFAULTS_VERSION:
			result = defaults.apply_defaults(reason="migrate")
			touched = len(result["created"]) + len(result["applied"])
			if touched or result["flagged"]:
				print(
					"medusync: default mappings — %s new, %s updated, %s need a decision"
					% (len(result["created"]), len(result["applied"]), len(result["flagged"]))
				)
	except Exception:
		frappe.log_error(title="medusync could not apply default mappings", message=frappe.get_traceback())

	try:
		report = drift.run()
		if report["flagged"]:
			print(
				"medusync: %s mapping(s) name a field that no longer exists and have been "
				"switched off — see the Needs Attention column" % len(report["flagged"])
			)
	except Exception:
		frappe.log_error(title="medusync drift check failed on migrate", message=frappe.get_traceback())
