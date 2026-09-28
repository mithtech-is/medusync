# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
# For license information, please see license.txt

"""Registry of domain behaviour, loaded from opt-in packs.

medusync's core is site-agnostic. Domain behaviour lives in *handler
packs* (`medusync.handlers.<pack>`), each exposing any of:

    register()        calls `register_handler(event, fn)` for the inbound
                      events it owns
    MAPPED_UPSERT     dotted path of the doctype-aware upsert that
                      `medusync.api.receive` uses for a mapped push
    OUTBOUND_HOOKS    {doctype: {docevent: dotted path or [paths]}} — the
                      document events this pack wants to act on
    DELIVERED_HOOKS   {doctype: dotted path or [paths]} — called once a
                      store has confirmed a delivery about a document
                      of that doctype
    REHEARSAL_BUILDERS {doctype: dotted path} — builds the document a
                      rehearsal should validate, for a doctype this pack
                      assembles from more than the field map

Which packs a site loads is that site's decision, in `site_config.json`:

    "medusync_handler_packs": ["commerce"]

Packs are opt-in in BOTH directions. `hooks.py` names no business doctype
at all: it binds one wildcard handler for the six document events, and
that handler asks this registry which of the configured packs care. A
site running no pack runs no domain code either way.

Nothing is registered at import time — a bench CLI process has no site
context yet — so the registry is (re)built lazily on first use, per site.
When the key is absent the commerce pack loads, which is what a site
without an opinion wants: stock, prices and the post-order path.
"""

import importlib
import os
from typing import Any, Callable

import frappe

from medusync.handlers import outbound_guard

HANDLERS: dict[str, Callable] = {}

CONF_KEY = "medusync_handler_packs"
DEFAULT_PACKS = ("commerce",)

# site -> the packs tuple the registry currently reflects. One process may
# serve several sites; a different pack list rebuilds the registry rather
# than merging into it.
_loaded_for: dict[str, tuple] = {}
_outbound_for: dict[str, tuple] = {}
_OUTBOUND: dict[str, dict[str, list]] = {}
# site -> (packs, {doctype: [callable, ...]}). Kept per site, so one
# process serving sites with different packs never answers one site
# from another's list.
_DELIVERED: dict[str, tuple] = {}
# site -> (packs, {doctype: callable}). Same shape, same reason.
_REHEARSAL: dict[str, tuple] = {}


def configured_packs() -> list[str]:
	"""Pack names for the current site, in load order."""
	try:
		raw = frappe.conf.get(CONF_KEY)
	except Exception:
		raw = None
	if raw is None:
		packs = list(DEFAULT_PACKS)
	elif isinstance(raw, str):
		packs = [p.strip() for p in raw.split(",") if p.strip()]
	else:
		packs = [str(p).strip() for p in raw if str(p).strip()]
	return packs


def _pack_module(name: str):
	return importlib.import_module(f"medusync.handlers.{name}")


def _log_pack_failure(name: str) -> None:
	try:
		frappe.log_error(
			title=f"medusync: failed to load handler pack '{name}'",
			message=frappe.get_traceback(),
		)
	except Exception:
		pass


def _resolve(path):
	module_path, _, fn_name = str(path).rpartition(".")
	return getattr(importlib.import_module(module_path), fn_name)


def _site_key() -> str:
	return getattr(frappe.local, "site", None) or ""


def ensure_packs_loaded(force: bool = False) -> list[str]:
	"""Make HANDLERS reflect the current site's configured packs."""
	site = _site_key()
	packs = tuple(configured_packs())
	if not force and _loaded_for.get(site) == packs:
		return list(packs)
	HANDLERS.clear()
	for name in packs:
		try:
			_pack_module(name).register()
		except Exception:
			# A broken or missing pack must not take the whole registry
			# down; the other packs still load and the failure is logged.
			_log_pack_failure(name)
	_loaded_for[site] = packs
	return list(packs)


def get_mapped_upsert() -> Callable | None:
	"""The doctype-aware upsert a mapped push should use: the first
	configured pack that declares `MAPPED_UPSERT`, else None."""
	for name in configured_packs():
		try:
			mod = _pack_module(name)
		except Exception:
			_log_pack_failure(name)
			continue
		path = getattr(mod, "MAPPED_UPSERT", None)
		if not path:
			continue
		return _resolve(path)
	return None


# ── Inbound events ───────────────────────────────────────────────────


def register_handler(event: str, fn: Callable, *, replace: bool = False) -> None:
	"""Register one event handler. No-op on duplicate unless `replace=True`."""
	if not replace and event in HANDLERS:
		return
	HANDLERS[event] = fn


def unregister_handler(event: str) -> None:
	HANDLERS.pop(event, None)


def dispatch(event: str, payload: Any, *, event_id: str = "") -> dict:
	"""Call the registered handler for `event`. Returns a dict envelope.

	On no handler: `{"status": "skipped", "reason": "no_handler_for_event", "event": event}`
	On handler error: the exception propagates so `medusync.api.receive`
	can write a Failed `Medusync Log` row and return 5xx (the Medusa
	plugin's retry logic will then back off and retry).
	"""
	ensure_packs_loaded()
	fn = HANDLERS.get(event)
	if fn is None:
		return {"status": "skipped", "reason": "no_handler_for_event", "event": event}
	return fn(payload, event_id=event_id)


def list_registered() -> list[str]:
	"""Sorted list of registered event names — used by `health` and tests."""
	ensure_packs_loaded()
	return sorted(HANDLERS.keys())


# ── Outbound document events ─────────────────────────────────────────


def outbound_hook_map() -> dict[str, dict[str, list]]:
	"""{doctype: {docevent: [callable, ...]}} for the configured packs."""
	site = _site_key()
	packs = tuple(configured_packs())
	if _outbound_for.get(site) != packs:
		_OUTBOUND.clear()
		for name in packs:
			try:
				declared = getattr(_pack_module(name), "OUTBOUND_HOOKS", None) or {}
			except Exception:
				_log_pack_failure(name)
				continue
			for doctype, events in declared.items():
				for event, paths in (events or {}).items():
					if isinstance(paths, str):
						paths = [paths]
					for path in paths:
						try:
							fn = _resolve(path)
						except Exception:
							_log_pack_failure(name)
							continue
						_OUTBOUND.setdefault(doctype, {}).setdefault(event, []).append(fn)
		_outbound_for[site] = packs
	return _OUTBOUND


def outbound_hooks_for(doctype: str, docevent: str) -> list:
	return list(outbound_hook_map().get(doctype, {}).get(docevent, ()))


def run_outbound_hooks(doc, method: str | None) -> None:
	"""Run the configured packs' hooks for this document event.

	Called from the one wildcard hook. Must never raise: an exception
	here would abort the user's save, and a domain pack failing is not a
	reason to refuse a business document.

	It must also never re-enter itself. Reporting a failure writes an
	Error Log document, which fires this same wildcard hook — so a hook
	that raises would log, insert, re-enter, raise, and loop forever. The
	guard refuses re-entry while the dispatcher is already running, and
	skips Frappe's own bookkeeping doctypes outright.
	"""
	doctype = getattr(doc, "doctype", None)
	if not method or outbound_guard.is_internal(doctype) or outbound_guard.already_running():
		return
	fns = outbound_hooks_for(doctype, method)
	if not fns:
		return
	with outbound_guard.running():
		for fn in fns:
			try:
				fn(doc, method)
			except Exception:
				try:
					frappe.log_error(
						title=f"Medusync outbound pack hook failed on {doctype or '?'}",
						message=frappe.get_traceback(),
					)
				except Exception:
					pass


# ── After a store confirms a delivery ────────────────────────────────


def delivered_hook_map() -> dict[str, list]:
	"""{doctype: [callable, ...]} for the configured packs."""
	site = _site_key()
	packs = tuple(configured_packs())
	cached = _DELIVERED.get(site)
	if cached and cached[0] == packs:
		return cached[1]
	built: dict[str, list] = {}
	for name in packs:
		try:
			declared = getattr(_pack_module(name), "DELIVERED_HOOKS", None) or {}
		except Exception:
			_log_pack_failure(name)
			continue
		for doctype, paths in declared.items():
			if isinstance(paths, str):
				paths = [paths]
			for path in paths:
				try:
					fn = _resolve(path)
				except Exception:
					_log_pack_failure(name)
					continue
				built.setdefault(doctype, []).append(fn)
	_DELIVERED[site] = (packs, built)
	return built


def run_delivered_hooks(doctype: str, docname: str, *, site_id: str, event_name: str, response: dict) -> None:
	"""Tell the configured packs a store has confirmed a delivery.

	Called by the delivery job once the log row says Success. Must never
	raise: the delivery has already succeeded, and a pack failing
	afterwards must not turn it into a retry of something that landed.
	"""
	if not doctype or not docname:
		return
	for fn in list(delivered_hook_map().get(doctype, ())):
		try:
			fn(doctype, docname, site_id=site_id, event_name=event_name, response=response)
		except Exception:
			try:
				frappe.log_error(
					title=f"Medusync delivered hook failed on {doctype}",
					message=frappe.get_traceback(),
				)
			except Exception:
				pass


def rehearsal_builder(doctype: str):
	"""How a rehearsal should stand up a document of this doctype, or None.

	A pack that assembles a document from more than the field map — a sales
	document's customer and lines are built from what the store sends, not
	from field pairs — says so here. Without it a rehearsal would validate
	an order with no customer and no lines and report that, which is true
	of nothing that ever arrives and hides the site's real problems.
	"""
	site = _site_key()
	packs = tuple(configured_packs())
	cached = _REHEARSAL.get(site)
	if not cached or cached[0] != packs:
		built: dict = {}
		for name in packs:
			try:
				declared = getattr(_pack_module(name), "REHEARSAL_BUILDERS", None) or {}
			except Exception:
				_log_pack_failure(name)
				continue
			for dt, path in declared.items():
				try:
					built[dt] = _resolve(path)
				except Exception:
					_log_pack_failure(name)
		_REHEARSAL[site] = (packs, built)
		cached = _REHEARSAL[site]
	return cached[1].get(doctype)


def clear() -> None:
	"""Test-only — drop every registered handler and forget what was loaded."""
	HANDLERS.clear()
	_loaded_for.clear()
	_OUTBOUND.clear()
	_outbound_for.clear()
	_DELIVERED.clear()
	_REHEARSAL.clear()
