# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
# For license information, please see license.txt

"""This site → Medusa.

One wildcard `doc_events` hook covers every doctype (see hooks.py); the
per-doctype decision is made here, at runtime, from Medusync Mapping
rows. That is what lets an operator add a new synced doctype from the
Desk UI without an app release. The same hook asks the configured handler
packs whether they want the event, so no business doctype is named in
hooks.py at all.

Multi-site
----------
A mapping with no site is sent to every enabled Medusync Site; one pinned
to a site is sent only there. Each site gets its own log row, its own
secret and its own retry schedule, so an outage at one store cannot stall
another.

Loop prevention
---------------
An inbound write from Medusa lands as an ordinary document save, which
would fire this hook and push the change straight back.
`frappe.flags.medusync_inbound` covers the part of that which happens
inside the request. What it cannot cover is the part a background worker
sends moments later, so an inbound write also leaves a breadcrumb on the
document (see medusync.echo); anything we send about that document within
the window is stamped `echo_of` and the far side drops it as its own.

Retries
-------
`frappe.enqueue` cannot delay a job, so a failed delivery is *parked*:
the log row keeps `status = Queued` and gets a `next_attempt_at`. The
once-a-minute sweep `medusync.tasks.retry_due` re-enqueues rows whose
time has come. Attempts exhausted → `Poison`, which is never retried
automatically.
"""

import hashlib
import json

import frappe
from frappe.utils import add_to_date, now_datetime

from medusync import breaker, config, echo, envelope, selection, sites
from medusync.signing import EVENT_ID_HEADER, SIGNATURE_HEADER, sign

QUEUE = "short"

#: Our own doctypes never sync — configuring the sync must not emit sync.
MEDUSYNC_DOCTYPES = (
	"Medusync Log",
	"Medusync Mapping",
	"Medusync Settings",
	"Medusync Site",
	"Medusync Exclusion",
	"Medusync Reset Request",
)


def on_doc_event(doc, method=None):
	"""Wildcard entry point — called for every doctype on every event.

	Must be cheap and must never raise: an exception here would abort
	the user's save.
	"""
	try:
		if frappe.flags.get("medusync_inbound"):
			return
		if doc.doctype in MEDUSYNC_DOCTYPES:
			return
		if not config.is_enabled():
			return

		for row in config.mappings_for(doc.doctype):
			if row["direction"] == "From Medusa":
				continue
			_maybe_dispatch(row["name"], doc, method)

		# Domain packs the site opted into. Nothing here names a doctype;
		# the registry answers from what the configured packs declare.
		from medusync import handlers

		handlers.run_outbound_hooks(doc, method)
	except Exception:
		frappe.log_error(
			title="Medusync outbound hook failed",
			message=frappe.get_traceback(),
		)


def _maybe_dispatch(mapping_name: str, doc, method: str | None):
	mapping = frappe.get_cached_doc(config.MAPPING_DOCTYPE, mapping_name)
	events = mapping.docevent_list()
	if method not in events:
		return

	# Frappe runs `on_update` as part of `insert()`, so a mapping that
	# listens to BOTH after_insert and on_update emits two events for one
	# create — same state, twice, doubling traffic and relying on the
	# receiver being idempotent. Drop the redundant one.
	#
	# When on_update is the ONLY configured trigger this must still fire:
	# the operator has asked for "any change", and a create is a change.
	if method == "on_update" and doc.flags.get("in_insert") and "after_insert" in events:
		return

	if not _condition_passes(mapping, doc):
		return
	dispatch(mapping, doc, method)


def dispatch(mapping, doc, docevent: str):
	"""Queue one event per site this mapping applies to.

	Split out from the hook path so backfill can replay a record without
	pretending a document event just happened — the caller has already
	decided this record qualifies.
	"""
	event = mapping.resolved_event_name(docevent)
	payload = build_payload(mapping, doc)

	# "Only send when something actually changed."
	#
	# Frappe fires on_update for any save, including ones that touched
	# nothing this mapping cares about. Hashing the PAYLOAD (not the
	# doc) is the point: an unrelated field changing must not count as a
	# change for a mapping that doesn't sync that field.
	payload_hash = hashlib.sha256(
		json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
	).hexdigest()

	# Was this document just written BY one of our sites? Then anything we
	# send about it is that site's own change coming home; tag it so the
	# far side can drop it instead of applying it again.
	mark = echo.origin_of(doc.doctype, doc.name) or {}
	echo_of = mark.get("origin")
	correlation_id = mark.get("correlation_id")

	# ERPNext decides which documents may reach which store. A mapping
	# says the doctype can sync; the selector on the document says whether
	# this one does, and where.
	for site in selection.sites_allowed(
		doc.doctype, doc.name, sites.sites_for_mapping(mapping), doc=doc
	):
		site_id = site["site_id"]
		# `modified` is part of the key so a repeated save produces a new
		# event, but a retry of the SAME save does not apply twice on the
		# Medusa side. The site id keeps two sites' rows distinct.
		event_id = f"frappe:{doc.doctype}:{doc.name}:{doc.get('modified') or now_datetime()}:{site_id}"

		if mapping.get("skip_unchanged") and already_delivered(
			doctype=doc.doctype, docname=doc.name, payload_hash=payload_hash, site_id=site_id
		):
			continue

		log = _create_log(
			direction="Outbound",
			status="Queued",
			event=event,
			event_id=event_id,
			document_type=doc.doctype,
			document_name=doc.name,
			payload_hash=payload_hash,
			site=site_id,
			request_body=payload,
		)
		send(
			log.name,
			event,
			event_id,
			payload,
			site_id=site_id,
			correlation_id=correlation_id,
			echo_of=echo_of,
		)


def already_delivered(*, doctype, docname, payload_hash: str, site_id: str) -> bool:
	"""Has this exact payload already reached this store?

	Rehearsals are excluded on purpose. A test run must never be the
	reason a genuine change is dropped as a duplicate — that failure is
	invisible from both ends, which is the worst kind.
	"""
	filters = {
		"direction": "Outbound",
		"payload_hash": payload_hash,
		"status": "Success",
		"site": site_id,
		"is_test": 0,
	}
	if doctype:
		filters["document_type"] = doctype
	if docname:
		filters["document_name"] = docname
	return bool(frappe.db.exists("Medusync Log", filters))


def send(
	log_name: str,
	event_name: str,
	event_id: str,
	payload: dict,
	*,
	site_id: str | None = None,
	kind: str = envelope.KIND_EVENT,
	correlation_id: str | None = None,
	echo_of: str | None = None,
	is_test: bool = False,
):
	"""Hand one logged event to the delivery channel.

	Background by default (`Send in Background`), so a slow or down Medusa
	never blocks the user's save; inline only when the operator turned the
	queue off for debugging. Handler packs call this instead of `deliver`
	directly so they inherit the same behaviour.
	"""
	cfg = config.settings()
	kwargs = dict(
		log_name=log_name,
		event_name=event_name,
		event_id=event_id,
		payload=payload,
		attempt=1,
		site_id=site_id,
		kind=kind,
		correlation_id=correlation_id,
		echo_of=echo_of,
		is_test=is_test,
	)
	if cfg.use_background_jobs:
		frappe.enqueue("medusync.outbound.deliver", queue=QUEUE, enqueue_after_commit=True, **kwargs)
	else:
		deliver(**kwargs)


def emit(
	event: str,
	payload: dict,
	*,
	ref: str,
	doctype: str | None = None,
	docname: str | None = None,
	per_site=None,
	is_test: bool = False,
):
	"""Log and send one event to every enabled site.

	The single entry point for handler packs. It does what `dispatch`
	does for mapping-driven events — one log row per site, the site's own
	secret and retry schedule, and the echo tag when this change was
	itself caused by an inbound write — so a pack cannot accidentally
	reach only one of several stores.

	Stores do not always want the same body. `per_site(site_id, payload)`
	returns the body that store should get, or None to leave it out
	entirely. Stock is the clearest case: two stores can draw on the same
	warehouse under different stock-location ids, so each has to be told
	its own. Doing that in the handler instead would mean re-implementing
	the log row, the echo tag and the selection filter once per handler.

	`ref` is what keeps two events apart. It ends up in the event id, and
	Medusa treats a repeated event id as a duplicate and drops it — so a
	caller sending about the same document twice for different reasons
	(two warehouses, say) must put the difference in `ref`.
	"""
	mark = echo.origin_of(doctype, docname) if doctype and docname else None
	mark = mark or {}
	targets = sites.all_sites()
	if doctype and docname:
		targets = selection.sites_allowed(doctype, docname, targets)
	for site in targets:
		site_id = site["site_id"]
		body = per_site(site_id, payload) if per_site else payload
		if body is None:
			# This store did not ask for this one.
			continue
		payload_hash = hashlib.sha256(
			json.dumps(body, sort_keys=True, default=str).encode("utf-8")
		).hexdigest()
		event_id = "frappe:%s:%s:%s" % (event, ref, site_id)
		if is_test:
			# Marked in the id as well as the column so it is obvious in a
			# log listing, in a retry queue and on the far side at once.
			event_id = "test:" + event_id
		log = _create_log(
			direction="Outbound",
			status="Queued",
			event=event,
			event_id=event_id,
			document_type=doctype,
			document_name=docname,
			payload_hash=payload_hash,
			site=site_id,
			is_test=1 if is_test else 0,
			request_body=body,
		)
		send(
			log.name,
			event,
			event_id,
			body,
			site_id=site_id,
			correlation_id=mark.get("correlation_id"),
			echo_of=mark.get("origin"),
			is_test=is_test,
		)


def _condition_passes(mapping, doc) -> bool:
	if not mapping.condition:
		return True
	try:
		return bool(
			frappe.safe_eval(
				mapping.condition.strip(),
				None,
				{"doc": doc.as_dict(), "frappe": frappe._dict(utils=frappe.utils)},
			)
		)
	except Exception:
		frappe.log_error(
			title=f"Medusync condition failed on {mapping.name}",
			message=frappe.get_traceback(),
		)
		# A broken condition must not silently sync everything.
		return False


#: Field-map directions whose data does NOT leave this site. "Don't Sync"
#: keeps the pair documented in the mapping but moves nothing either way.
_NOT_OUTBOUND = ("From Medusa", "Don't Sync")


def build_payload(mapping, doc) -> dict:
	"""The `data` object Medusa receives, keyed by OUR fieldnames.

	An event describes the system it came from, so the keys are Frappe
	fieldnames and Medusa applies the field map on receipt — the same way
	it applies it to the raw rows its pull cron reads. Renaming here as
	well made the receiver's lookup miss, and every pair whose two names
	differed was dropped without an error (see
	pending_work/2026-09-06-payload-key-convention-mismatch.md).

	Child tables are included as lists of plain dicts when `Send All
	Fields` is on; an explicit field map can also name a Table field and
	get the same treatment.
	"""
	source = doc.as_dict(convert_dates_to_str=True)

	if mapping.include_all_fields:
		data = {k: v for k, v in source.items() if not k.startswith("_")}
	else:
		data = {}
		for row in mapping.field_map:
			if row.direction in _NOT_OUTBOUND:
				continue
			# A fixed value is our answer to ERPNext's requirement, not a
			# fact about the store. Sending it out would invent a field on
			# the Medusa record that nobody asked for.
			if row.get("constant_value"):
				continue
			data[row.frappe_field] = source.get(row.frappe_field)

	# The key is always present regardless of the field map — Medusa
	# cannot correlate the record without it.
	key_field = mapping.key_field or "name"
	data.setdefault(key_field, source.get(key_field))
	data.setdefault("name", doc.name)
	data.setdefault("doctype", doc.doctype)
	return data


def deliver(
	log_name: str,
	event_name: str,
	event_id: str,
	payload: dict,
	attempt: int = 1,
	site_id: str | None = None,
	kind: str = envelope.KIND_EVENT,
	correlation_id: str | None = None,
	echo_of: str | None = None,
	is_test: bool = False,
):
	"""POST one event to one site and record the outcome.

	NB the parameter is `event_name`, not `event`. `frappe.enqueue`
	reserves `event` for its own scheduler-event argument and consumes
	it, so a kwarg by that name never reaches this function — the job
	then dies with "missing 1 required positional argument". Inline
	delivery hides the bug completely, which is how it survived a
	green test run.

	Runs on the background queue by default. A failed attempt parks the
	row for the retry sweep rather than looping in-process, so a long
	outage doesn't pin a worker.
	"""
	import requests

	site = sites.get_site(site_id) if site_id else sites.default_site()
	if not site:
		_finish_log(
			log_name,
			status="Failed",
			error=f"no Medusync Site configured for '{site_id or 'default'}'",
			next_attempt_at=None,
		)
		return

	# A store that has failed ten times running will fail the eleventh,
	# and every attempt holds a worker for the length of the timeout. With
	# several stores connected, one that is down would starve the queue
	# for the ones that are up.
	if not breaker.allows(site["site_id"], is_test=is_test):
		_finish_log(
			log_name,
			status="Skipped",
			error="this store is not answering, so deliveries to it are paused (see Stopped Trying At on the store)",
			attempt=attempt,
			next_attempt_at=None,
		)
		return

	endpoint = sites.endpoint(site)
	secret = sites.secret(site, "outbound_secret")
	if not endpoint or not secret:
		_finish_log(
			log_name,
			status="Failed",
			error=f"site '{site['site_id']}' has no Medusa URL or outbound secret",
			next_attempt_at=None,
		)
		return

	body_kwargs = {"data": payload} if kind == envelope.KIND_EVENT else {}
	if kind == envelope.KIND_MAPPING:
		body_kwargs = {"mapping": (payload or {}).get("mapping")}
	env = envelope.build(
		event_name,
		event_id,
		site_id=site["site_id"],
		kind=kind,
		correlation_id=correlation_id,
		echo_of=echo_of,
		# A rehearsal is always a dry run on the wire. The far side does
		# every check it would normally do and stops before the write.
		dry_run=is_test,
		**body_kwargs,
	)
	body = json.dumps(env, separators=(",", ":"), default=str).encode("utf-8")

	cfg = config.settings()
	try:
		response = requests.post(
			endpoint,
			data=body,
			headers={
				"Content-Type": "application/json",
				SIGNATURE_HEADER: sign(body, secret),
				EVENT_ID_HEADER: event_id,
			},
			timeout=sites.timeout(site),
			verify=sites.verify_ssl(site),
		)
	except Exception as exc:
		_retry_or_fail(log_name, event_name, event_id, payload, attempt, str(exc), status_code=0, site_id=site["site_id"], kind=kind)
		return

	text = (response.text or "")[:4000]
	if 200 <= response.status_code < 300:
		# Medusa reports what it did with the record; store it so
		# "why is this missing over there" is answerable from the log
		# alone. Older receivers say nothing — fall back to "updated".
		action = "updated"
		body_json = {}
		try:
			body_json = json.loads(text or "{}")
			action = (
				body_json.get("result", {}).get("action")
				or body_json.get("action")
				or "updated"
			)
		except Exception:
			pass
		fields = dict(
			status="Success",
			status_code=response.status_code,
			response_body=text,
			attempt=attempt,
			action=str(action)[:40],
			next_attempt_at=None,
		)
		if not cfg.log_payloads:
			# A retry may have parked the payload on the row; the row is
			# terminal now, so honour "don't log bodies".
			fields["request_body"] = None
		# The store row first, the result second. Every delivery to this
		# store writes "last seen" on the same row, and on MariaDB 11.6+
		# (innodb_snapshot_isolation, on by default) a job that finishes
		# second has that write refused and its whole transaction undone.
		# In this order only the "last seen" note can be lost that way,
		# never the result: the row would otherwise stay Queued forever.
		_mark_site_seen(site["site_id"], is_test=is_test)
		_finish_log(log_name, **fields)
		if not is_test and kind == envelope.KIND_EVENT:
			_after_delivered(log_name, site["site_id"], event_name, body_json)
		return

	_retry_or_fail(
		log_name,
		event_name,
		event_id,
		payload,
		attempt,
		f"HTTP {response.status_code}: {text[:500]}",
		status_code=response.status_code,
		site_id=site["site_id"],
		kind=kind,
	)


def _after_delivered(log_name: str, site_id: str, event_name: str, response) -> None:
	"""Let the configured packs follow up on what a store just confirmed.

	The delivery job is handed the payload, not the document; the log row
	knows which document this was. Never raises: this delivery has already
	succeeded, whatever a pack does next.
	"""
	try:
		row = frappe.db.get_value(
			"Medusync Log", log_name, ["document_type", "document_name"], as_dict=True
		)
		if not row or not row.document_type or not row.document_name:
			return
		from medusync import handlers

		handlers.run_delivered_hooks(
			row.document_type,
			row.document_name,
			site_id=site_id,
			event_name=event_name,
			response=response if isinstance(response, dict) else {},
		)
	except Exception:
		frappe.log_error(title="Medusync follow-up after delivery failed", message=frappe.get_traceback())


def retry_delay_seconds(attempt: int) -> int:
	"""Backoff before the next attempt: 30 s after the first failure,
	120 s after the second, 270 s after the third … — long enough to ride
	out a deploy, short enough that a blip clears within minutes."""
	return 30 * max(int(attempt or 1), 1) ** 2


def _retry_or_fail(
	log_name,
	event_name,
	event_id,
	payload,
	attempt,
	error,
	status_code=0,
	site_id=None,
	kind: str = envelope.KIND_EVENT,
):
	cfg = config.settings()
	max_attempts = cfg.max_attempts or 3

	if attempt >= max_attempts:
		# Terminal. `Poison` rather than `Failed` so the sweep leaves it
		# alone and an operator can see, in one filter, what has actually
		# given up rather than what merely failed once.
		fields = dict(status="Poison", status_code=status_code, error=error, attempt=attempt, next_attempt_at=None)
		if not cfg.log_payloads:
			fields["request_body"] = None
		# Store row first, result second: see the success path in deliver().
		_mark_site_error(site_id, error)
		breaker.record_failure(site_id)
		_finish_log(log_name, **fields)
		return

	# Park the row; `medusync.tasks.retry_due` re-enqueues it once
	# `next_attempt_at` has passed. No immediate re-enqueue: frappe.enqueue
	# has no delay, and retrying within milliseconds burns every attempt
	# during a single outage.
	fields = dict(
		status="Queued",
		status_code=status_code,
		error=error,
		attempt=attempt,
		next_attempt_at=add_to_date(now_datetime(), seconds=retry_delay_seconds(attempt)),
	)
	if not cfg.log_payloads:
		# The sweep re-reads the payload from the row. Keep it until the
		# row is terminal, then it is cleared again (see deliver()).
		fields["request_body"] = json.dumps(payload, indent=2, default=str)
	_finish_log(log_name, **fields)


# ── Site health ──────────────────────────────────────────────────────


def _mark_site_seen(site_id, is_test: bool = False):
	"""Record that this store answered us.

	A rehearsal does not count. "Last seen" is what an operator reads to
	decide whether a store is reachable, and a green test run says only
	that the studio is working.
	"""
	if not site_id or is_test:
		return
	try:
		frappe.db.set_value(
			sites.SITE_DOCTYPE,
			site_id,
			{"last_seen_at": now_datetime(), "last_error": None},
			update_modified=False,
		)
	except Exception:
		pass
	# It answered, so start counting again from nothing.
	breaker.record_success(site_id)


def _mark_site_error(site_id, error):
	if not site_id:
		return
	try:
		frappe.db.set_value(
			sites.SITE_DOCTYPE, site_id, {"last_error": (error or "")[:1000]}, update_modified=False
		)
	except Exception:
		pass


# ── Logging ──────────────────────────────────────────────────────────


def _create_log(**kwargs):
	cfg = config.settings()
	if not cfg.log_payloads:
		kwargs.pop("request_body", None)
	doc = frappe.new_doc("Medusync Log")
	for key, value in kwargs.items():
		if key in ("request_body",) and value is not None:
			value = json.dumps(value, indent=2, default=str)
		doc.set(key, value)
	doc.insert(ignore_permissions=True)
	# Deliberately NOT committing here. This runs inside a document save,
	# where Frappe refuses a commit outright ("Commit/rollback are
	# disabled during certain events") because it would break the
	# atomicity of the user's own save. Ordering is already guaranteed by
	# `enqueue_after_commit=True` on the job: the worker cannot start
	# until this transaction lands, log row included.
	return doc


def _finish_log(log_name: str, **kwargs):
	cfg = config.settings()
	if not cfg.log_payloads:
		kwargs.pop("response_body", None)
	try:
		frappe.db.set_value("Medusync Log", log_name, kwargs, update_modified=False)
		# No explicit commit: in the worker, Frappe's job runner commits
		# on success; inline, the surrounding request transaction does.
		# Committing here would hit the same doc-event guard as above.
	except Exception:
		frappe.log_error(title="Medusync could not update its log", message=frappe.get_traceback())
