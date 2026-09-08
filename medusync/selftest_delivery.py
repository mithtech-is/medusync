# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
"""Live outbound delivery test against a stub receiver.

    bench --site <site> execute medusync.selftest_delivery.run

Proves the half `selftest.run` deliberately skips: that the wildcard hook
fires on a real save, that the envelope reaching the store is signed with
that store's outbound secret, that the field map was applied on the way
out, and that a store which cannot be reached is recorded rather than
swallowed.

It needs nothing set up first. The receiver is `medusync.selftest_receiver`,
started here on a port the OS picks and stopped again on the way out, and
the store and mapping are created and removed by this script. Everything
it changes is put back, including Medusync Settings.
"""

import frappe

from medusync import selftest_fixtures as fx
from medusync.selftest_receiver import Receiver

SITE_ID = "selftest-delivery"
MAPPING = "Selftest Delivery ToDo"
SECRET = "outbound-secret-xyz"
INBOUND_PATH = "/webhooks/erpnext-inbound"

results = []


def ok(label, cond, detail=None):
	results.append((label, bool(cond), detail))


def run():
	frappe.set_user("Administrator")
	snapshot = fx.snapshot_settings()
	receiver = Receiver(SECRET)
	try:
		with receiver:
			_setup(receiver)
			todo = _create(receiver)
			_update(todo)
			_unreachable(todo)
			frappe.delete_doc("ToDo", todo.name, ignore_permissions=True, force=True)
		_enqueue_signature()
	finally:
		_teardown(snapshot)

	fx.report(results)


def _setup(receiver):
	"""A store pointed at the stub, and a mapping switched on properly.

	The mapping is created disabled and enabled through the studio.
	`Medusync Mapping.enabled` defaults to 1, so building it any other
	way is refused by the rehearsal gate - which is what used to stop
	this script on its third statement.
	"""
	fx.configure_settings(
		medusa_url="https://medusa.example.com",
		inbound_path=INBOUND_PATH,
		use_background_jobs=0,  # deliver inline so the test can assert
		max_attempts=1,
		log_payloads=1,
		enabled=1,
	)
	# Delivery reads the URL and the secret from the Medusync Site, never
	# from the Single, so the stub has to be configured here.
	fx.ensure_site(
		SITE_ID,
		medusa_url=receiver.url,
		outbound_secret=SECRET,
		inbound_path=INBOUND_PATH,
	)
	fx.make_mapping(MAPPING, site=SITE_ID)
	result = fx.enable(MAPPING)
	ok("the mapping rehearsed and switched on", result.get("passed") and result.get("enabled"), result.get("errors"))


def _teardown(snapshot):
	fx.drop(fx.MAPPING_DOCTYPE, MAPPING)
	fx.drop(fx.SITE_DOCTYPE, SITE_ID)
	fx.purge_logs(SITE_ID)
	fx.restore_settings(snapshot)


# -- a create ---------------------------------------------------------


def _create(receiver):
	before = fx.outbound_count()

	todo = frappe.get_doc({
		"doctype": "ToDo", "description": "delivery test", "status": "Open", "priority": "Medium",
	})
	todo.insert(ignore_permissions=True)
	frappe.db.commit()

	after = fx.outbound_count()
	# Exactly ONE - Frappe runs on_update inside insert(), and a mapping
	# listening to both triggers must not emit the same state twice.
	ok("a create queues exactly one outbound event", after == before + 1, f"{before} -> {after}")

	rows = frappe.get_all(
		"Medusync Log",
		filters={"direction": "Outbound", "document_name": todo.name},
		fields=["name", "status", "status_code", "event", "event_id", "error", "attempt", "action"],
		order_by="creation desc",
		limit=1,
	)
	ok("a log row exists for the saved document", bool(rows), rows)
	if rows:
		row = rows[0]
		ok("delivery succeeded", row.status == "Success", dict(row))
		ok("the receiver returned 200 (so the signature verified)", row.status_code == 200, row.status_code)
		ok("event name is the derived one", row.event == "todo.created", row.event)
		ok("event_id identifies the document", todo.name in (row.event_id or ""), row.event_id)
		ok("no error recorded", not row.error, row.error)
		ok("what the store said it did is stored on the row", row.action == "created", row.action)

	# What the stub actually saw.
	mine = receiver.for_document(todo.name)
	ok("the stub received the event", bool(mine), len(receiver.records))
	if mine:
		r = mine[-1]
		ok("HMAC verified on the receiving side", r["ok"] is True, r["signature"])
		ok("posted to the configured inbound path", r["path"] == INBOUND_PATH, r["path"])
		ok("sent as JSON", r["content_type"] == "application/json", r["content_type"])
		# The payload is keyed by OUR fieldnames; Medusa applies the field
		# map on receipt. An event that renamed `description` to `title`
		# on the wire would be describing a system that does not exist.
		ok("the field map keeps Frappe fieldnames on the wire",
		   r["data"].get("description") == "delivery test", r["data"])
		ok("the Medusa path is not what was sent", "title" not in r["data"], r["data"])
		ok("To Medusa field is included", r["data"].get("status") == "Open", r["data"])
		ok("From-Medusa-only field withheld", "priority" not in r["data"], r["data"])
		ok("event id also sent as a header", r["event_id_header"] == r["event_id"], r)
		ok("the envelope names this store", (r["origin"] or {}).get("site_id") == SITE_ID, r["origin"])
		ok("a real event is not marked as a dry run", r["dry_run"] is False, r["dry_run"])

	return todo


# -- an update --------------------------------------------------------


def _update(todo):
	todo.reload()
	todo.description = "delivery test edited"
	todo.save(ignore_permissions=True)
	frappe.db.commit()
	rows = frappe.get_all(
		"Medusync Log",
		filters={"direction": "Outbound", "document_name": todo.name},
		fields=["event", "event_id", "status"], order_by="creation desc", limit=2,
	)
	ok("an update fires its own event", len(rows) == 2 and rows[0].event == "todo.updated",
	   [dict(r) for r in rows])
	ok("the two events have different ids", len({r.event_id for r in rows}) == 2,
	   [r.event_id for r in rows])
	ok("the update also delivered", rows[0].status == "Success", dict(rows[0]))


# -- a store that is not answering ------------------------------------


def _unreachable(todo):
	"""A failing endpoint must be recorded, not swallowed.

	The URL is changed on the Medusync Site, not on the Single: delivery
	resolves the endpoint through `sites.endpoint(site)`, so re-pointing
	the Single here would have left the next delivery going to the stub
	and the assertion proving nothing.
	"""
	fx.point_site_at(SITE_ID, "http://127.0.0.1:9")  # closed port
	todo.reload()
	todo.description = "delivery test unreachable"
	todo.save(ignore_permissions=True)
	frappe.db.commit()
	row = frappe.get_all(
		"Medusync Log", filters={"direction": "Outbound", "document_name": todo.name},
		fields=["status", "error", "attempt"], order_by="creation desc", limit=1,
	)[0]
	# `Poison`, not `Failed`: max_attempts is 1, so the first refusal is
	# already terminal, and _retry_or_fail marks a row that has given up
	# as Poison so the retry sweep leaves it alone. A test asserting
	# `Failed` here is asserting the wrong end of that branch.
	ok("an unreachable Medusa is recorded as terminal", row.status == "Poison", dict(row))
	ok("...with the reason attached", bool(row.error), row.error)
	ok("...on the attempt that gave up", row.attempt == 1, row.attempt)


# -- the queued path --------------------------------------------------


def _enqueue_signature():
	"""The background path must actually be callable.

	`frappe.enqueue` reserves several kwarg names for itself - `event`,
	`queue`, `timeout`, `job_name`, `now`, `at_front`. A job argument
	sharing one of those names is swallowed by enqueue and never reaches
	the function, which then dies in the worker with "missing 1 required
	positional argument". Inline delivery calls the function directly and
	sees none of this, so every assertion above can pass while the queued
	path is broken - that is exactly what happened on the first live run.

	Rather than require a running worker, assert the contract: no
	parameter of `deliver` may collide with an enqueue-reserved name.
	"""
	import inspect

	from medusync import outbound

	RESERVED = {
		"queue", "timeout", "event", "is_async", "job_name", "now",
		"enqueue_after_commit", "at_front", "job_id", "deduplicate",
		"on_success", "on_failure", "retry",
	}
	params = set(inspect.signature(outbound.deliver).parameters) - {"self"}
	clashes = params & RESERVED
	ok("no deliver() parameter collides with a frappe.enqueue kwarg", not clashes, sorted(clashes))

	# And every enqueue call site must pass every non-defaulted parameter.
	# There are exactly two: `outbound.send`, which enqueues (or, inline,
	# calls) `deliver` for a fresh event, and `tasks.retry_due`, the sweep
	# that re-enqueues a parked row. `dispatch` is NOT one - it hands off
	# to `send` - and `_retry_or_fail` parks the row rather than
	# re-enqueuing, so a check that reads those two sees the arguments
	# nowhere and fails on a delivery path that is in fact correct.
	from medusync import tasks

	src = inspect.getsource(outbound.send) + inspect.getsource(tasks.retry_due)
	required = {
		n for n, p in inspect.signature(outbound.deliver).parameters.items()
		if p.default is inspect.Parameter.empty
	}
	missing = {n for n in required if f"{n}=" not in src}
	ok("every required deliver() argument is supplied at the enqueue call sites",
	   not missing, sorted(missing))
