app_name = "medusync"

# The field mapper is shared by the Medusync Mapping form and the Mappings
# page, so it is bundled app-wide rather than living in one doctype's JS.
app_include_js = ["medusync.bundle.js"]
app_title = "Medusync"
app_publisher = "Mithtech Innovative Solutions PVT LTD"
app_description = "Two-way sync between a Frappe/ERPNext site and a Medusa v2 backend."
app_email = "mithtech.is@gmail.com"
app_license = "mit"

# The handler packs (inventory, pricing, fulfilment, returns) reach into
# ERPNext's stock and selling modules; a Frappe-only site cannot host them.
required_apps = ["erpnext"]

after_install = "medusync.install.after_install"

# Two things every upgrade should do: bring the shipped mapping set up to
# date without overwriting anybody's edits, and notice a mapping that has
# gone stale. Both report rather than raise -- an operator whose site is
# down after an upgrade does not upgrade again.
after_migrate = "medusync.install.after_migrate"

# ── Document events ──────────────────────────────────────────────────
# One wildcard binding, and no business doctype named anywhere in this
# file. Two runtime questions decide what actually happens on a save:
#
#   which doctypes sync   -> Medusync Mapping rows, editable in the Desk
#   which domain code runs -> the handler packs this site opted into
#                             (site_config.json → medusync_handler_packs)
#
# `on_doc_event` returns immediately when neither has anything to say, and
# both lookups are served from a request-local cache, so the cost on an
# unrelated save is a dict miss.
doc_events = {
	"*": {
		"after_insert": "medusync.outbound.on_doc_event",
		"on_update": "medusync.outbound.on_doc_event",
		"on_submit": "medusync.outbound.on_doc_event",
		"on_cancel": "medusync.outbound.on_doc_event",
		"on_trash": "medusync.outbound.on_doc_event",
		"on_update_after_submit": "medusync.outbound.on_doc_event",
	},
	# A mapping is one configuration living in two systems: saving it here
	# refreshes the hot-path cache and tells the connected sites.
	"Medusync Mapping": {
		"on_update": "medusync.mapping_sync.on_mapping_update",
		"on_trash": "medusync.mapping_sync.on_mapping_trash",
	},
	# A site carries the warehouse and price-list maps, so saving one
	# invalidates three hot-path caches, not one.
	"Medusync Site": {
		"on_update": [
			"medusync.sites.clear_cache",
			"medusync.warehouses.clear_cache",
			"medusync.price_lists.clear_cache",
			"medusync.mapping_sync.on_site_update",
		],
		"on_trash": [
			"medusync.sites.clear_cache",
			"medusync.warehouses.clear_cache",
			"medusync.price_lists.clear_cache",
		],
	},
}

# Both of these are bookkeeping ABOUT a document, not a live reference to
# it, and Frappe blocks a delete on any dynamic link it does not know to
# ignore. Without this, the log row recording that a document synced is
# the very thing that stops it ever being deleted: from the desk with
# "Cannot delete because ... is linked with Medusync Log ...", and from
# the store, where an inbound `*.deleted` event raises instead of
# applying and the two systems drift apart without saying so.
ignore_links_on_delete = ["Medusync Log", "Medusync Exclusion"]

scheduler_events = {
	"cron": {
		# Failed deliveries wait for their backoff (Medusync Log.next_attempt_at);
		# this sweep re-enqueues the ones that are due.
		"* * * * *": [
			"medusync.tasks.retry_due",
		],
	},
	"daily": [
		"medusync.tasks.prune_logs",
		# A mapping that names a field the DocType no longer has fails
		# silently: the payload simply stops carrying it. Somebody should
		# be told before they notice from the wrong end.
		"medusync.drift.run",
	],
}

# The receive endpoint authenticates by HMAC over the raw body, not by
# session, so it must be reachable without a logged-in user AND without
# a CSRF token.
ignore_csrf = 1
