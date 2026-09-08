# Copyright (c) 2026, Mithtech Innovative Solutions PVT LTD and contributors
"""A Medusa-shaped receiver, in this process, for the delivery smoke test.

`selftest_delivery` used to expect somebody to have started a stub on
port 8791 by hand and to have pointed it at a file in /tmp. No such stub
was ever shipped, so the script could not be run from a checkout at all
- it failed on its own fixtures long before it reached an assertion.

The stub lives here now. It binds a port the OS picks, runs on a daemon
thread inside the same process as the test, and keeps what it saw in
memory, so

    bench --site <site> execute medusync.selftest_delivery.run

needs nothing set up first and cannot collide with whatever else is
listening on this machine.

It verifies the signature itself rather than trusting the sender. A stub
that answered 200 to anything would let a broken signer pass the whole
delivery test, which is the one thing that test exists to catch.

Nothing here may touch frappe: the handler runs on another thread, and
frappe's connection and request-local state are not shared across
threads. Only `medusync.signing` is imported, which is pure hmac.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from medusync.signing import EVENT_ID_HEADER, SIGNATURE_HEADER, verify


class Receiver:
	"""One stub store. Start it, point a Medusync Site at `url`, then read
	`records` - each entry is what arrived on one request.

	Usable as a context manager, which is how the smoke test uses it, so
	the port is always given back even when an assertion raises.
	"""

	def __init__(self, secret: str):
		self.secret = secret
		self.records: list[dict] = []
		self._server = None
		self._thread = None

	# -- lifecycle ----------------------------------------------------

	def start(self) -> "Receiver":
		receiver = self

		class Handler(BaseHTTPRequestHandler):
			protocol_version = "HTTP/1.1"

			def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
				length = int(self.headers.get("Content-Length") or 0)
				raw = self.rfile.read(length)

				# The signature covers the bytes on the wire. Parsing the
				# body first and re-serialising it would change key order
				# and whitespace, and so the digest - the exact mistake
				# this stub exists to detect on the sending side.
				good = verify(raw, receiver.secret, self.headers.get(SIGNATURE_HEADER))

				try:
					envelope = json.loads(raw.decode("utf-8"))
				except Exception:
					envelope = {}

				receiver.records.append(
					{
						"ok": good,
						"path": self.path,
						"raw": raw,
						"content_type": self.headers.get("Content-Type"),
						"signature": self.headers.get(SIGNATURE_HEADER),
						"event": envelope.get("event"),
						"event_id": envelope.get("event_id"),
						"event_id_header": self.headers.get(EVENT_ID_HEADER),
						"kind": envelope.get("kind"),
						"dry_run": bool(envelope.get("dry_run")),
						"origin": envelope.get("origin") or {},
						"data": envelope.get("data") or {},
						"envelope": envelope,
					}
				)

				if good:
					# Medusa reports what it did with the record, and
					# `deliver` stores that on the log row. Answering in
					# the same shape keeps the `action` column honest.
					body = json.dumps({"result": {"action": "created"}}).encode("utf-8")
					status = 200
				else:
					body = json.dumps({"error": "invalid signature"}).encode("utf-8")
					status = 401

				self.send_response(status)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(body)))
				self.end_headers()
				self.wfile.write(body)

			def log_message(self, *args):
				"""Quiet. The smoke test prints its own report and a line
				per request on stderr would bury it."""

		class Server(ThreadingHTTPServer):
			daemon_threads = True
			# The test restarts the rig on a rerun; without this the
			# second run can trip over a socket still in TIME_WAIT.
			allow_reuse_address = True

		# Port 0: the OS picks a free one. Nothing has to be reserved,
		# and two benches can run this at the same time.
		self._server = Server(("127.0.0.1", 0), Handler)
		self._thread = threading.Thread(
			target=self._server.serve_forever, name="medusync-selftest-receiver", daemon=True
		)
		self._thread.start()
		return self

	def stop(self) -> None:
		if self._server is not None:
			self._server.shutdown()
			self._server.server_close()
			self._server = None
		if self._thread is not None:
			self._thread.join(timeout=5)
			self._thread = None

	def __enter__(self) -> "Receiver":
		return self.start()

	def __exit__(self, *exc) -> None:
		self.stop()

	# -- what the test asks it ----------------------------------------

	@property
	def port(self) -> int:
		return self._server.server_address[1]

	@property
	def url(self) -> str:
		"""What to put in `Medusync Site.medusa_url`."""
		return "http://127.0.0.1:%d" % self.port

	def for_document(self, docname: str) -> list[dict]:
		"""Every event about one document, oldest first.

		Filters out the mapping-definition pushes that a Medusync Site
		save sends to its store, which carry no `data`.
		"""
		return [r for r in self.records if (r.get("data") or {}).get("name") == docname]
