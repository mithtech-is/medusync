>**Retired (2026-09-27).** This Frappe app has been replaced by the Medusa plugin
> **`@mithtech-medusa/plugin-erpnext`** (repo
> [mithtech-is/medusa-erpnextsync](https://github.com/mithtech-is/medusa-erpnextsync)), which
> does ERPNext↔Medusa sync over Frappe core Webhooks and plain REST with nothing installed on
> ERPNext. This repository is archived and read-only; do not deploy it to new sites. To remove it
> from a site: `bench --site <site> uninstall-app medusync --yes && bench --site <site> migrate`.

# Medusync

Two-way sync between a Frappe/ERPNext site and a [Medusa v2](https://medusajs.com) backend.

Site-agnostic: no assumptions about which doctypes you sync, no
customer-specific fields, nothing to edit in Python to add a doctype.
Install it on any ERPNext site, point it at a Medusa backend, and
configure the mappings from the Desk UI.

## Do I actually need this?

Not necessarily — and that's worth being honest about.

Medusa can drive a stock Frappe site on its own: it writes through
`/api/resource/<Doctype>` with an API key, and reads either by polling
or via Frappe's built-in **Webhook** doctype. If that covers you, use it.

Medusync earns its place when you want:

| | Stock Frappe | With Medusync |
|---|---|---|
| Add a synced doctype | Hand-write a Jinja payload template per doctype, on the Medusa side | Pick it from a dropdown in Desk |
| Outbound field selection | Whatever the template hard-codes | A field map, or "send all" |
| Deletes | No inbound delete handling | `on_trash` → `<doctype>.deleted` |
| Retries | 3 fixed inline attempts | Configurable, backed off, on the queue |
| Idempotency | None — a retry re-applies | Deduped on `event_id` |
| Audit | Webhook Request Log (outbound only) | Both directions, with payloads |
| Backfill existing rows | Touch every record by hand | One `bench execute` |

## Install

```bash
bench get-app medusync <repo-url>
bench --site <site> install-app medusync
bench --site <site> migrate
sudo supervisorctl restart all      # or: bench restart
```

**That last line is not optional on a running site.** Adding an app to
`sites/apps.txt` makes every request import it, but gunicorn workers
started before the install cannot see a newly pip-installed package.
Skip the restart and the entire site returns HTTP 500 —
`ModuleNotFoundError: No module named 'medusync'` — until you do it.
That is the whole site, not just the sync.

If you installed by pointing bench at a local directory rather than
`get-app`, install the package into the bench env first:

```bash
./env/bin/pip install -e apps/medusync
```

If `install-app` fails partway with *"Module import failed for Medusync
Settings"*, the Module Def existed but the doctype loader had a stale
module map. Recover with:

```bash
bench --site <site> clear-cache && bench --site <site> migrate
```

Then open **Medusync Settings** and set:

- **Medusa URL** — e.g. `https://admin.example.com` (no trailing slash)
- **Inbound Secret** — must equal the Medusa plugin's `webhook_secret`
- **Outbound Secret** — must equal the Medusa plugin's `frappe_to_medusa_secret`

Leave **Enable Sync** off until the mappings are in place. Installing
the app never starts moving data on its own.

## Configure a mapping

**Medusync Mapping** → New:

| Field | Meaning |
|---|---|
| Document Type | The doctype to sync |
| Direction | `To Medusa`, `From Medusa`, or `Two-way` |
| Trigger On | Which Frappe events push outbound (one per line) |
| Condition | Optional Python expression over `doc` |
| Key Field | The field that identifies this record to Medusa |
| Field Map | Frappe field ↔ Medusa path, with a per-field direction |
| Send All Fields | Skip the field map and send everything |

Per-field direction is how you say "Frappe owns the address, Medusa
owns the email" without splitting the mapping in two.

### Fixed values

A field-map row can send a constant instead of reading a Medusa path — for
a field this side requires that the store has no equivalent for. Use `=` on
the row in the field mapper to switch it over, `↩` to switch it back.

The value is chosen from this site rather than typed: a Link offers this
site's records, a Select its options, anything else free text
(`portal.field_options`). A value the site no longer has stays selected and
says so rather than vanishing from a mapping that has been running on it.

A row in fixed-value mode with nothing filled in is not a source. It does
not count towards required coverage, and the mapper refuses to save it —
"Some rows have no source". The in-progress marker is a single space in
`constant_value`, which is trimmed away on save and never persists, so only
a real value is ever stored or put on the wire.

The Medusa plugin expresses the same rule differently: it carries `constant`
as a key whose presence means "fixed value", so *its* in-progress marker is
an empty string rather than a space. Worth knowing before changing either —
see `pending_work/2026-09-07-mapping-studio-parity.md`.

## Endpoints

| Path | Auth | Purpose |
|---|---|---|
| `/api/method/medusync.api.receive` | HMAC | Apply one inbound event |
| `/api/method/medusync.api.health` | none | Liveness probe |

Both directions sign the **raw request body** with HMAC-SHA256 and send
it in `X-Medusa-Signature`. Hex is emitted; hex or base64 is accepted,
so a site part-way through migrating off Frappe's native Webhook rows
(which sign base64) keeps working.

Inbound envelope:

```json
{
  "event": "customer.updated",
  "event_id": "medusa:cus_01H...:1723459200",
  "doctype": "Customer",
  "key_field": "email_id",
  "key_value": "someone@example.com",
  "data": { "customer_name": "Alex Fern", "mobile_no": "+91..." }
}
```

`event_id` is the idempotency key: a repeat is answered `skipped`, not
applied twice.

## Backfill

Hooks only fire on change, so a new mapping syncs nothing until each
record is touched. To replay what already exists:

```bash
bench --site <site> execute medusync.backfill.run --kwargs "{'mapping': 'Customers to Medusa', 'limit': 50, 'dry_run': True}"
```

Drop `dry_run` once the counts look right.

## Handler packs

medusync's core is domain-neutral. Domain behaviour ships as *handler
packs* under `medusync/handlers/<pack>/`. One is shipped: `commerce` —
stock levels, prices, delivery notes, invoices and order metadata, which
is what every store has. A site chooses its packs in `site_config.json`:

```json
"medusync_handler_packs": ["commerce"]
```

When the key is absent, `commerce` loads, which is what a site without an
opinion wants. An empty list loads nothing: inbound events then go through
Medusync Mapping rows only, and a mapped push is skipped until a pack that
provides an upsert is configured.

The switch works in **both** directions. `hooks.py` names no business
doctype at all — it binds one wildcard handler for the six document
events, and that handler asks the configured packs which of their outbound
hooks apply. A site running no pack runs no domain code either way.

A deployment with behaviour of its own adds a pack rather than editing
this one: a module under `medusync/handlers/<name>/` exposing `register()`,
`MAPPED_UPSERT` and `OUTBOUND_HOOKS`, named in that site's config. Nothing
client-specific is shipped here.

## Retries

A delivery that fails is parked with a **Next Attempt At** on its Medusync
Log row (30 s, then 120 s, 270 s …) and picked up by a once-a-minute
scheduler sweep (`medusync.tasks.retry_due`), so the scheduler must be
running. **Max Attempts** in the settings bounds the total.

## Sites

One ERPNext can serve several Medusa stores. Each is a **Medusync Site**
record holding that store's URL and its own pair of shared secrets, so a
leak or an outage at one store cannot touch another. A mapping with no
site applies to every enabled site; one pinned to a site applies only
there.

An inbound request is attributed to a site **by its signature**, not by
anything the caller claims, so a site cannot impersonate another by
setting a header.

Upgrading from the single-connection setup needs no work: the migration
copies the Single's URL and secrets onto a site called `default`.

## Wire contract (v2)

Every signed body carries a version, a timestamp and an origin:

```json
{
  "v": 2,
  "kind": "event",
  "event": "customer.updated",
  "event_id": "frappe:Customer:CUST-0001:2026-09-04 10:00:00:default",
  "ts": 1788474641,
  "origin": {
    "system": "erpnext",
    "site_id": "default",
    "correlation_id": "1722607afc9a4e6b95ae85bf1c0746ce"
  },
  "data": { "email_id": "someone@example.com" }
}
```

`kind` says what the body holds: `event` (a business event), `mapped` (an
already-mapped upsert with doctype, key and payload) or `mapping` (the
mapping configuration itself). A body with no `v` is read as v1 and still
applies, so the two apps can be upgraded one at a time.
`medusync/envelope.py` and the plugin's `envelope.ts` are mirrors — change
them together.

## Mapping synchronisation

A mapping is one configuration living in two systems. Both copies share a
`mapping_uid` and carry a `version`; saving on either side sends
`mapping.upserted` to the other. The higher version wins, and on a tie
**ERPNext wins**, because ERPNext owns which documents may sync at all and
the two decisions must not disagree. A deleted mapping disables the far
copy rather than destroying it, so records already correlated by it stay
traceable.

A mapping arriving under a **uid this site has never seen** is created
switched **off**, whatever the sender says: first contact between two
systems that each already had mappings must not enable a rule nobody
reviewed here. Updates to a uid already held apply as sent.

The canonical form carries what both sides understand: identity, version,
direction, the key pair and the field pairs. Options that exist on one
side only — **Send All Fields** here, the Medusa event list there — do not
travel, so a mapping that relies on them needs that part set again on the
far side.

## Sync selection

ERPNext decides which documents are allowed to sync. A mapping says a
DocType *can*; the selector on the document says whether this one *does*,
and to which stores:

| Stores connected | What the document shows |
|---|---|
| one | **Sync with Medusa**, a single checkbox |
| several | **Medusa Sites**, a checkbox per store |

The field that does not apply is hidden rather than removed, so going from
one store to several never loses what was already chosen.

Which DocTypes carry the selector is **Medusync Settings → Sync
Selection**. The **Catalogue DocType** (Item on a stock ERPNext) always
does; list any others alongside it. Changing the catalogue moves the
selector onto the new DocType and tells every connected store, so the
plugin's "link to an existing product" search looks in the right place.

Everything excluded is also written to **Medusync Exclusion**, the central
Don't Sync list, and an entry added there by hand updates the document.
Two views of one decision: an operator can never be shown a ticked box for
a document the system refuses to sync. Entries the checkbox created are
cleaned up by the checkbox; entries a person added are theirs to remove.

Defaults are deliberate. Turning selection on must not silently stop a
store that was syncing perfectly well, so a document nobody has touched is
allowed, and an empty store list falls back to the checkbox.

## Stock across warehouses

One ERPNext site holds stock in several warehouses; each store keeps its
own stock locations. The pairing is per store, on **Medusync Site →
Warehouses**:

| Column | Means |
|---|---|
| Warehouse | the ERPNext warehouse whose sellable stock this store sees |
| Medusa Stock Location ID | where it lands over there; blank lets the store choose |
| Enabled | untick to stop sending, without forgetting the pairing |

The same warehouse can appear on several stores under different location
ids, and one store can draw on several warehouses. What travels is
**sellable** stock, not raw quantity:

    sellable = max(0, actual - reserved - safety_stock)

ERPNext stays the single reservation authority, so a Medusa store is
never told about stock that a Sales Order has already promised.

A store with no rows falls back to **Medusync Settings → Inventory Source
Warehouse**, exactly as before the map existed. Rows that exist but are
switched off are still a map: a store that unticked its only warehouse is
saying "send me nothing", not "go back to the global default".

## Price lists

Prices are two-way by default, but each Price List decides for itself, per
store, on **Medusync Site → Price Lists**:

| Column | Means |
|---|---|
| Price List | the ERPNext list |
| Direction | Two-way, To Medusa, From Medusa, or Don't Sync |
| Role | Base Price (the price on the shelf) or Tier Price (a B2B tier) |
| Medusa Tier Code | which tier, for a Tier Price |

The role is per store because the same list means different things in
different places: a wholesale list can be the shelf price on a trade
store and a customer tier on the retail one. A cost list marked **Don't
Sync** stays documented and moves nothing.

Only the outbound half is wired today: `To Medusa` and `Two-way` send,
`From Medusa` and `Don't Sync` send nothing. Medusa writing a price back
into an Item Price is not built yet, so `From Medusa` currently means
"ERPNext keeps out of it" rather than "Medusa drives it".

A store with no rows keeps what it had: the Settings selling price list
as its base price, and the `medusa_customer_tier` field on Price List for
its tiers.

## Orders: where they came from and what was paid

Two things a storefront cannot work out for itself.

**Source.** Once a web order and a phone order are both Sales Orders they
are indistinguishable. Medusa sends its sales channel, it lands in `Sales
Order.medusa_order_source`, and submitting the order reports it back as
`order.source.set` so the store can say which it was. An order raised in
ERPNext reports `erpnext`.

**Payments.** Money that arrives by transfer, cheque or UPI never touches
Medusa. A submitted Payment Entry sends `order.payment.set` for each
order it is allocated against, carrying what was allocated to *that*
order rather than the size of the whole receipt. The store files each one
under the Payment Entry that produced it, so three transfers against one
order are three receipts and a re-send overwrites only its own. Cancelling
the Payment Entry marks its receipt cancelled and drops it from the total;
the row stays, because somebody will ask where the money went.

Both land in the Medusa order's **metadata**, not in Medusa payment
records. ERPNext is the accounting authority here, and a payment record no
captured transaction backs would put a figure in the storefront ledger
that nothing reconciles.

## The catalogue is ERPNext's

Two rules that no mapping can sign away:

- **An update to a record that already exists here is skipped**, unless
  **Medusync Settings → Medusa May Update Catalogue Fields** is on.
  Carrying `title` in both directions is a reasonable mapping to
  configure; the person who configured it was thinking about new
  products, not about the description someone in purchasing wrote.
- **A Medusa delete never deletes the Item.** It unlinks: the record keeps
  its stock, its purchase history and its ledger entries, and simply stops
  claiming a Medusa product. There is no setting for this. Disabling would
  be nearly as bad — ERPNext would stop letting anyone transact against it.

Creating is not affected: whether a Medusa-origin product may become a
record at all is the plugin's product policy (off / link / create).

A refusal comes back as a **skip**, not a failure, so the sender records
"nothing to do" and stops. Returning an error would put the event into a
retry loop that could never succeed.

## Trying a mapping before you trust it

A mapping is a small program somebody types into a grid, and until now the
only feedback it gave was a log row after a real document was saved. That
is a poor place to discover a wrong field name and a worse one to discover
a condition that excludes everything.

Open a **Medusync Mapping** and use **Test**:

| Button | Does |
|---|---|
| Show a Sample Record | a real record of that DocType, or one made up from the form's own definition when the site has none |
| Rehearse | both directions, reported, nothing written |
| Rehearse and Enable | the same, and switches the mapping on if it held up |
| Send a Test Event | a real signed request to every store, which checks it and answers without writing |

**Leaving here** reports the payload that would travel, the events that
would fire, which stores would receive it, and whether the condition
passes. A mapping that is enabled, valid and silent is the hardest fault
to see from outside, so "no document events selected", "the condition
excludes this record" and "every store is excluded" are named rather than
left for somebody to notice later.

**Arriving here** reports whether the payload would create or update, the
field-by-field difference, and anything ERPNext itself would object to.
It runs the doctype's own validation inside a savepoint that is always
rolled back, so a mandatory field or a bad Select shows up here instead of
as a queue of failures.

Both halves ask the same code the real paths ask: outbound uses the same
payload builder and the same condition and selection checks, inbound uses
the same plan `apply_inbound` executes. A studio that reasons
independently is worse than none, because it is believed.

### Enable after a rehearsal

Switching a mapping **on** requires a rehearsal that matches it. What
counts as "matches" is a signature over what the mapping *does* — DocType,
direction, key, field map — so a pass survives ticking Enabled and does
not survive somebody adding a field afterwards. Renaming it costs nothing.

Only the transition is gated. A mapping already running keeps running
whatever is edited on it; retro-fitting the rule would stop a working site
on the next save of anything.

The far side cannot switch on a mapping this site has not rehearsed. It
arrives, its fields are applied, and `Enabled` stays off — the same rule
as first contact. It is refused quietly rather than with an error, because
an error would put the sender into a retry loop.

### Test rows

A test event is real traffic with `dry_run` in the envelope: signed,
inside the replay window, checked by the far side, and stopped before the
write. That is the only check that can prove the shared secret, the
network and the far side's own verdict, which between them are most of the
reasons a sync fails.

The rows it leaves are marked **Test Run**, and everything that reads the
log skips them: the retry sweep will not retry one, a rehearsed success
never suppresses a genuine event as a duplicate, and a rehearsal does not
count as having reached a store. They are pruned after a day whatever the
retention setting says.

## The mappings this app ships with

Three, because three is what a store needs before anything else is worth
configuring: **Customers**, **Catalogue** and **Orders**. They carry fixed
identifiers (`default:customer` and friends) so both systems agree which
mapping is which without asking each other, and the catalogue one follows
whatever **Medusync Settings → Catalogue DocType** says.

They arrive switched off, always. A mapping goes live only after a
rehearsal that matches it, and that rule is exactly what makes restoring
them safe: the configuration comes back without changing what the site
does until somebody has looked at each one.

`Medusync Settings → Default Mappings Version` records which set is
installed. Applying a *newer* set to a site that has edited the old one is
a harder problem — it has to decide what "edited" means — and is not what
restoring does.

## Hard reset

A reset throws away configuration somebody spent a week getting right, so
the question worth answering is not what it does but who may ask. Nobody,
alone.

Open the **Medusync Site** and use **Danger Zone → Hard Reset**. Each
system generates a secret and shows it once; each has to be handed the
other's. A secret is 32 random bytes, lives three minutes, works once, and
is stored only as a hash — the receiver redacts the body of a verify
before it writes its audit row, whatever the payload-logging setting says.
A secret that reaches a log has a much longer life than three minutes.

Only a side holding both proofs resets: the far side proved it holds ours,
and we proved we hold theirs.

| | |
|---|---|
| **Keeps** | every business document, and every `medusa_*` id on it |
| | `Medusync Site` records and both their secrets |
| | the Don't Sync list, and the warehouse and price-list maps |
| **Restores** | the three shipped mappings, switched off |
| **Switches off** | every other mapping — kept, not deleted |
| **Clears** | `Medusync Log`, and the loop-prevention breadcrumbs |

Keeping the cross-system ids is the important one. A reset that took
`medusa_customer_id` with it would leave both systems holding the same
customers and no longer knowing it, which is worse than the configuration
mistake anyone was trying to fix. Keeping the store's secrets means
recovering from a bad configuration does not also mean re-pairing. Keeping
exclusions means the reset cannot quietly resume syncing a document
somebody deliberately stopped.

Afterwards nothing is running. Rehearse and enable what you want back.

## Loop prevention

An inbound write is an ordinary document save, so it would fire the
outbound hook and push the change straight back. Medusync sets
`frappe.flags.medusync_inbound` for the duration of an inbound apply
and the outbound hook checks it.

The flag is per-request, so on its own it cannot stop the echo a
background worker sends a moment later, after the request is gone. An
inbound write therefore also leaves a short-lived breadcrumb on each
document it touched: "this was last changed by correlation C, which came
from medusa:site-a". Anything sent about that document within the window
is stamped `echo_of`, and the far side drops what it recognises as its
own. The breadcrumb expires, so a person editing the same document a
minute later is never mistaken for an echo.

## Operational notes

- **Log retention.** `Medusync Log` stores whatever your mapping
  carries — on a Customer mapping, that is personal data. Retention
  defaults to 180 days; set **Log Payload Bodies** off on sites where a
  second copy of that data is not acceptable.
- **Delivery is queued** by default so a slow Medusa never blocks a
  user's save. Turn it off only while debugging.
- **Deletes are opt-in** per mapping, and off by default.
- The outbound hook is a wildcard across every doctype. It returns
  immediately when no mapping matches, from a request-local cache.

## Licence

MIT
