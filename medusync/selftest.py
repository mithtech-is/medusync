# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
"""In-site smoke test. `bench --site <site> execute medusync.selftest.run`

Exercises the paths that only exist inside a real Frappe request cycle:
settings encryption, mapping validation, the rehearsal gate, the wildcard
hook's no-op case, payload construction, the inbound apply, and the loop
guard.

Opens no socket. The store it configures has no outbound secret, so
`outbound.deliver` refuses each delivery before it connects to anything
- which is itself asserted below, and which is what lets this script
prove that a save *queues* an event without proving anything about the
wire. Delivery on the wire is `selftest_delivery`.

Everything it changes is put back: the mapping and the store are removed,
the log rows it created are purged, and Medusync Settings is restored
from a snapshot taken before the first write.
"""

import json

import frappe

from medusync import config, outbound, sites
from medusync import selftest_fixtures as fx
from medusync.signing import sign, verify

#: A store id and a mapping title of our own, so a run cannot disturb
#: whatever the site really has configured.
SITE_ID = "selftest"
MAPPING = "Selftest ToDo"

results = []


def ok(label, cond, detail=None):
	results.append((label, bool(cond), detail))


def run():
	frappe.set_user("Administrator")
	snapshot = fx.snapshot_settings()
	try:
		_settings()
		_store()
		_signing()
		_hook_is_inert_when_unconfigured()
		_mapping_validation()
		_rehearsal_gate()
		_payload()
		_inbound()
		_loop_guard()
	finally:
		_teardown(snapshot)

	fx.report(results)


def _teardown(snapshot):
	"""Leave the site as it was found, whether or not the run got through.

	Ordering matters: the mapping and the store go first, so the log rows
	they produced on the way out are purged too, and the Single is
	restored last because saving it while a store is still enabled would
	push mappings at it.
	"""
	fx.drop(fx.MAPPING_DOCTYPE, MAPPING)
	fx.drop(fx.SITE_DOCTYPE, SITE_ID)
	fx.purge_logs(SITE_ID)
	fx.restore_settings(snapshot)


# -- settings ---------------------------------------------------------


def _settings():
	s = fx.configure_settings(
		medusa_url="https://medusa.example.com/",
		inbound_path="/webhooks/erpnext-inbound",
		inbound_secret="inbound-secret-abc",
		outbound_secret="outbound-secret-xyz",
		# Inline, so every assertion below reads a finished log row
		# rather than racing a worker that may not be running.
		use_background_jobs=0,
		# One attempt, so a refused delivery reaches its terminal status
		# immediately instead of parking for the retry sweep.
		max_attempts=1,
		enabled=1,
	)
	ok("trailing slash is stripped from medusa_url", s.medusa_url == "https://medusa.example.com", s.medusa_url)
	ok("endpoint composes correctly",
	   config.medusa_endpoint() == "https://medusa.example.com/webhooks/erpnext-inbound",
	   config.medusa_endpoint())
	ok("password field round-trips through get_password",
	   config.get_secret("inbound_secret") == "inbound-secret-abc",
	   config.get_secret("inbound_secret"))
	ok("the two secrets stay distinct",
	   config.get_secret("outbound_secret") == "outbound-secret-xyz")


def _store():
	"""A store with a URL and no secret.

	Deliberate: it makes every delivery in this script terminate inside
	`deliver` before a socket is opened, and it covers a real failure
	mode - a half-connected store must say so on the log row rather than
	silently sending unsigned events or silently sending nothing.
	"""
	fx.ensure_site(SITE_ID, medusa_url="http://127.0.0.1:9", outbound_secret=None)
	ok("the store is visible to the delivery path",
	   any(s["site_id"] == SITE_ID for s in sites.all_sites()))


# -- signing ----------------------------------------------------------


def _signing():
	body = json.dumps({"event": "ping", "data": {"x": 1}}, separators=(",", ":")).encode()
	hexsig = sign(body, "s3cret")
	ok("hex signature verifies", verify(body, "s3cret", hexsig))
	import base64, hashlib, hmac
	b64 = base64.b64encode(hmac.new(b"s3cret", body, hashlib.sha256).digest()).decode()
	ok("base64 signature also verifies (native Frappe Webhook style)", verify(body, "s3cret", b64))
	ok("wrong secret is rejected", not verify(body, "other", hexsig))
	ok("tampered body is rejected", not verify(body + b" ", "s3cret", hexsig))
	ok("missing signature is rejected", not verify(body, "s3cret", None))


# -- the wildcard hook ------------------------------------------------


def _hook_is_inert_when_unconfigured():
	"""The wildcard hook runs on every save on the site. With no mapping
	for a doctype it must do nothing and, above all, never raise."""
	before = frappe.db.count("Medusync Log")
	tag = frappe.get_doc({"doctype": "Tag", "name": "medusync-selftest-tag"})
	tag.insert(ignore_permissions=True)
	tag.save(ignore_permissions=True)
	after = frappe.db.count("Medusync Log")
	ok("saving an unmapped doctype logs nothing", before == after, f"{before} -> {after}")
	frappe.delete_doc("Tag", tag.name, ignore_permissions=True, force=True)
	ok("...and deleting it is also inert", frappe.db.count("Medusync Log") == before)


# -- mapping validation -----------------------------------------------


def _rejected(spec, label, expect):
	"""Assert a mapping is refused, and refused for the stated reason.

	Checking only for ValidationError is not enough. `validate` runs the
	rehearsal gate last, so every one of these would still raise if its
	own validator were removed - the test would stay green while the
	thing it names stopped being checked.
	"""
	try:
		frappe.get_doc(spec).insert(ignore_permissions=True)
		ok(label, False, "insert succeeded")
	except frappe.ValidationError as exc:
		message = str(exc)
		ok(label, expect.lower() in message.lower(), message)


def _mapping_validation():
	fx.drop(fx.MAPPING_DOCTYPE, MAPPING)

	_rejected(
		fx.todo_mapping("Selftest Bad", site=SITE_ID, docevents="on_updates", field_map=[]),
		"a typo'd docevent is rejected",
		"Unknown document event(s): on_updates",
	)
	_rejected(
		fx.todo_mapping("Selftest Bad2", site=SITE_ID, docevents="on_update",
		                condition="doc.status ==", field_map=[]),
		"a broken condition is rejected at save time",
		"not a valid Python expression",
	)
	_rejected(
		fx.todo_mapping(
			"Selftest Bad3",
			site=SITE_ID,
			docevents="on_update",
			field_map=[{"frappe_field": "not_a_real_field", "medusa_path": "x"}],
		),
		"a field that is not on the doctype is rejected",
		"'not_a_real_field' is not a field on ToDo",
	)

	# Pinned to our own store: a mapping with no site applies to every
	# enabled one, and the site this runs on may have a real store
	# configured that must not receive selftest traffic.
	good = fx.make_mapping(MAPPING, site=SITE_ID)
	ok("a valid mapping saves", frappe.db.exists(fx.MAPPING_DOCTYPE, MAPPING))
	ok("derived event name for after_insert", good.resolved_event_name("after_insert") == "todo.created",
	   good.resolved_event_name("after_insert"))
	ok("derived event name for on_trash", good.resolved_event_name("on_trash") == "todo.deleted",
	   good.resolved_event_name("on_trash"))
	ok("blank medusa_path defaults to the fieldname",
	   all(r.medusa_path for r in good.field_map))


def _rehearsal_gate():
	"""A mapping may not be switched on until it has been rehearsed.

	This is why both smoke tests used to die on their own setup:
	`enabled` defaults to 1, so a mapping created without saying
	otherwise arrives enabled and is refused here. Creating it switched
	off and coming through the studio is the supported route, and it is
	worth asserting rather than working around - the gate is the app's
	main safety property.
	"""
	doc = frappe.get_doc(fx.MAPPING_DOCTYPE, MAPPING)
	ok("a new mapping is not enabled by the fixtures", not doc.enabled, doc.enabled)
	ok("nothing has been rehearsed yet", not doc.get("tested_signature"), doc.get("tested_signature"))

	doc.enabled = 1
	try:
		doc.save(ignore_permissions=True)
		ok("switching on an un-rehearsed mapping is refused", False, "save succeeded")
	except frappe.ValidationError as exc:
		ok("switching on an un-rehearsed mapping is refused", "rehearse" in str(exc).lower(), str(exc))
	frappe.clear_document_cache(fx.MAPPING_DOCTYPE, MAPPING)

	result = fx.enable(MAPPING)
	ok("the rehearsal passed", result.get("passed"), result.get("errors"))
	doc = frappe.get_doc(fx.MAPPING_DOCTYPE, MAPPING)
	ok("the studio switched it on", bool(doc.enabled), doc.enabled)
	ok("...and recorded what it approved", bool(doc.tested_signature), doc.tested_signature)
	ok("the recorded signature matches the mapping as saved",
	   doc.tested_signature == doc.test_signature())
	ok("the mapping is now on the outbound hot path",
	   any(m["name"] == MAPPING for m in config.mappings_for("ToDo")))


# -- payload ----------------------------------------------------------


def _payload():
	mapping = frappe.get_doc(fx.MAPPING_DOCTYPE, MAPPING)
	todo = frappe.get_doc({"doctype": "ToDo", "description": "selftest payload", "status": "Open",
	                       "priority": "High"})
	todo.insert(ignore_permissions=True)

	payload = outbound.build_payload(mapping, todo)
	ok("a mapped field keeps its Frappe fieldname on the wire", payload.get("description") == "selftest payload", payload)
	ok("the Medusa path never appears in an outbound event", "title" not in payload and "state" not in payload, payload)
	ok("To Medusa field is included", payload.get("status") == "Open", payload)
	ok("From Medusa field is excluded from the outbound payload", "priority" not in payload, payload)
	ok("the key field is always present", payload.get("name") == todo.name, payload)

	mapping.include_all_fields = 1
	payload_all = outbound.build_payload(mapping, todo)
	ok("send-all includes unmapped fields", payload_all.get("priority") == "High", list(payload_all)[:8])
	mapping.include_all_fields = 0

	frappe.delete_doc("ToDo", todo.name, ignore_permissions=True, force=True)


# -- inbound ----------------------------------------------------------


def _inbound():
	mapping = frappe.get_doc(fx.MAPPING_DOCTYPE, MAPPING)

	created = fx.receive_inbound(mapping, {
		"event": "todo.created",
		"data": {"title": "created from medusa", "priority": "Low"},
	})
	ok("inbound insert creates a document", created.get("action") == "created", created)
	name = created.get("name")
	doc = frappe.get_doc("ToDo", name)
	ok("medusa path is translated back to the fieldname", doc.description == "created from medusa", doc.description)
	ok("From Medusa field is applied", doc.priority == "Low", doc.priority)

	updated = fx.receive_inbound(mapping, {
		"event": "todo.updated", "key_field": "name", "key_value": name,
		"data": {"title": "updated from medusa"},
	})
	ok("inbound update targets the existing document", updated.get("action") == "updated", updated)
	ok("...and actually changed it",
	   frappe.get_doc("ToDo", name).description == "updated from medusa")

	blocked = fx.receive_inbound(mapping, {
		"event": "todo.deleted", "key_field": "name", "key_value": name, "data": {},
	})
	ok("delete is refused unless the mapping allows it", blocked.get("status") == "Skipped", blocked)

	mapping.allow_delete = 1
	deleted = fx.receive_inbound(mapping, {
		"event": "todo.deleted", "key_field": "name", "key_value": name, "data": {},
	})
	ok("delete works once permitted", deleted.get("action") == "deleted", deleted)
	ok("...and the document is gone", not frappe.db.exists("ToDo", name))
	mapping.allow_delete = 0

	reserved = fx.receive_inbound(mapping, {
		"event": "todo.created",
		"data": {"title": "reserved field test", "owner": "attacker@example.com", "docstatus": 2},
	})
	victim = frappe.get_doc("ToDo", reserved["name"])
	ok("inbound cannot set `owner`", victim.owner != "attacker@example.com", victim.owner)
	ok("inbound cannot set `docstatus`", victim.docstatus == 0, victim.docstatus)
	frappe.delete_doc("ToDo", victim.name, ignore_permissions=True, force=True)


# -- the loop guard ---------------------------------------------------


def _loop_guard():
	"""An inbound write must not fire the outbound hook.

	The control comes first and is the point of it. "No outbound event
	was queued" is trivially true when nothing would have queued one
	anyway - which is exactly the state this script used to be in, with
	the mapping disabled and no store configured. So: prove an ordinary
	save DOES queue one, then prove the identical write arriving from
	Medusa does not.
	"""
	mapping = frappe.get_doc(fx.MAPPING_DOCTYPE, MAPPING)

	before = fx.outbound_count()
	todo = frappe.get_doc({"doctype": "ToDo", "description": "loop guard control", "status": "Open"})
	todo.insert(ignore_permissions=True)
	queued = fx.outbound_count() - before
	# Exactly one: Frappe runs on_update inside insert(), and a mapping
	# listening to both triggers must not emit the same state twice.
	ok("an ordinary save queues exactly one outbound event", queued == 1, queued)

	row = frappe.get_all(
		"Medusync Log",
		filters={"direction": "Outbound", "document_name": todo.name},
		fields=["status", "error", "event", "site"],
		order_by="creation desc", limit=1,
	)
	ok("the event names the store it was meant for", bool(row) and row[0].site == SITE_ID, row)
	ok("the derived event name is on the row", bool(row) and row[0].event == "todo.created", row)
	# The store has a URL but no outbound secret, so delivery stops in
	# `deliver` before opening a socket. A half-connected store has to
	# say so, not fail silently.
	ok("a store with no secret fails loudly instead of sending",
	   bool(row) and row[0].status == "Failed" and "outbound secret" in (row[0].error or ""),
	   dict(row[0]) if row else None)
	frappe.delete_doc("ToDo", todo.name, ignore_permissions=True, force=True)

	before = fx.outbound_count()
	res = fx.receive_inbound(mapping, {"event": "todo.created", "data": {"title": "loop guard"}})
	after = fx.outbound_count()
	ok("an inbound write queues no outbound event", before == after, f"{before} -> {after}")
	ok("the flag is cleared afterwards", not frappe.flags.get("medusync_inbound"))
	frappe.delete_doc("ToDo", res["name"], ignore_permissions=True, force=True)
