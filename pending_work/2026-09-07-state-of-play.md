# Where this stands — 2026-09-07

Read this first, then `00-QUESTIONS-ANSWER-THESE-FIRST.md`. Everything below
is **uncommitted** in both repos.

## The one thing that blocked the rest — now settled

**Q12 is decided and built (2026-09-07).** The envelope kind fixes the key
convention: an `event` body is keyed by the sender's fieldnames and the
receiver maps; a `mapped` body is keyed by the receiver's fieldnames and
is applied as it is. `outbound.build_payload` was the one sender breaking
that rule and now keys by `frappe_field`, so the ERPNext → Medusa webhook
carries the same shape the pull cron always read. The Medusa → ERPNext
push was never on the broken path. Details and the reasoning under
**Decided** in `2026-09-06-payload-key-convention-mismatch.md`; the open
follow-on is **Q21**.

Verified live the same day: an Item created in ERPNext with the Catalogue
mapping enabled went out as `item.created` keyed `item_code / item_name /
description`, Medusa's inbound event recorded `success`, and the product
appeared with its title six seconds later — the same path that had logged
"Product title is required" the day before. The Medusa → ERPNext `mapped`
path was exercised with the product-link action, which surfaced **Q22**.

## Also settled on 2026-09-07: a sync is its pair

The two mapping lists had drifted (Medusa held one mapping, ERPNext four,
two of them for Orders). A mapping's identity is now derived from what it
pairs — `pair:<entity>:<doctype>` — on both sides, one mapping per pair is
enforced, twins are folded by a patch and a migration, the guided setup
folds into an existing sync, deletes propagate both ways, and the whole
list travels on demand ("Send all…" on both Mappings pages) and whenever
a connection is saved. See `2026-09-07-one-sync-per-pair.md`. The store
field picker on both sides now groups by record and folds bookkeeping
columns, foreign keys and second names away behind "show internal".
The off state follows too: a mapping switched off anywhere is off
everywhere, with the reason, and each editor shows a "Required in ERPNext"
/ "Required in Medusa" box for the side data flows to.
Rehearsals now ask the receiving side's question: Medusa's dry run
rehearses the pull half for a pull or two-way mapping and fails on a
store-required field nobody sends; ERPNext's outbound rehearsal asks the
store what it requires and fails the same way. That was item 3 below.

## Settled on 2026-09-12: neither app adds a field on its own

Installing medusync put a row of Medusa fields on Item, Customer, the
sales doctypes, Address and Price List, and every Settings save put a
"Sync with Medusa" selector on the catalogue doctype. Now nothing is
added unless somebody presses the button: `medusync.reference_fields`
holds the catalogue of what the connector can add, Medusync Settings →
"Reference fields…" adds or removes one at a time (removal refuses a
field that holds values), and the mapper offers to create a field a
mapping names that the doctype lacks. The selector goes only on
doctypes listed under Sync Selection and comes off when unlisted. A
shipped default that names a missing field is written without that pair
and flagged "Field Missing" with the way to complete it. On the Medusa
side the editor no longer fills the pair list when a doctype or entity
is picked; "Auto-map fields" does that on request.

## Built this week, verified against both running stacks

| Piece | Where |
|---|---|
| Medusa field discovery from mikro-orm metadata | `discovery.ts`, `discovery-runtime.ts` |
| `GET /admin/erpnext/medusa-entities/:entity/fields` | plugin |
| Fixed-value field pairs, both sides | `mapping-engine.ts`, `constant_value` on `Medusync Field Map` |
| Required-coverage check, gating the rehearsal | `unmetRequired` in `mapping-engine.ts` |
| Signed read channel ERPNext → Medusa | `POST /webhooks/erpnext-describe`, `medusa_fields.py` |
| Automap on the ERPNext side | `medusa_fields.suggest` |
| Field mapper dialog | `public/js/mapper.bundle.js` |
| Mappings page | `page/medusync_mappings/`, `portal.py` |
| Value pickers for fixed values | `portal.field_options` |
| Guided wizard fixed (it could never finish) | plugin `page.tsx` |
| Pull filter as rows, not JSON | plugin `page.tsx` |
| Delete propagates instead of disabling | `mapping_sync.apply_deleted` |
| Fixed values in the Medusa editor, not only the wizard (2026-09-21) | plugin `page.tsx`, `useConstantOptions` |
| A blank fixed value is not a source (2026-09-21) | `constantHasValue` in `mapping-engine.ts` |

Counts: 126 curated Medusa paths → **417 discovered**. 177 vitest, 322 bench
tests, all green.

## Next, in order

1. ~~Answer Q12 and fix it.~~ Done — see above. Both sides now have a
   test whose every pair has a different name on each side.
2. **The shared dictionary** — `2026-09-07-field-equivalence-dictionary.md`.
   Its four design questions are answered (see that file's **Decided**
   section). The matcher is reachable from both sides now and
   its quality is the weak link: for `product → Item` it returns
   `has_variants ← variants` (a boolean against an array) and misses
   `item_name ← title` entirely. The dictionary is what fixes that, and it
   needs the per-row lock described in that file so a learned correction is
   not thrown away by the next Automap.
3. ~~Pull-direction required coverage.~~ Done — see above.
4. **Q10 / Q11** — the Medusa singleton, and Customer needing Contact and
   Address. Both are decisions, not defects.

## Things that will waste an hour if not known

- **After `yalc update` touching admin UI:** check `.yalc/`, not just
  `node_modules`. `yalc publish` runs its own build, so a chained
  `build && publish && update` can race and leave `.yalc/` older. Then force
  the browser to refetch — Vite serves the plugin admin as a pre-bundled dep
  under a `?v=` hash that does not change, so Chrome keeps the old copy.
  Clearing `node_modules/.vite` and restarting is not enough on its own.
- **Never run the bench suite against the sandbox site.** `test_reset`
  performs a real hard reset on whatever site it runs on: it restores the
  defaults (switched off, rehearsal cleared), disables every other mapping,
  deletes every Medusync Log row and bumps every mapping's version. That
  is what wiped the sandbox's log this morning and what pushed its
  Catalogue version far ahead of Medusa's, so Medusa's edits were refused
  as stale. Run the suite on a dedicated site (`medusync-test.localhost`)
  and keep `medusync-vanilla.localhost` for hand testing.
- **`bench serve` runs no scheduler or workers.** Outbound events queue.
  This bench's Procfile already runs one `bench worker` for every site, so
  check `pgrep -f "frappe worker"` before starting another. A bare
  `bench worker` on macOS dies with SIGABRT on the first job unless it is
  started with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`, as the Procfile
  does. With two workers running, one delivery's log row once stayed
  `Queued` after a successful POST (2026-09-07, not reproduced with one).
- **Piping a script into `bench console`** prints the first line of output
  on the same line as the `In [n]:` prompt; strip the prompt with
  `sed -E 's/^In \[[0-9]+\]: //'` rather than filtering those lines out.
- **Two backends on one port masquerade as each other.** On 2026-09-07
  another session held 48010 with a smoke-test database, and its instance
  answered the sandbox's admin login with "invalid" and every webhook with
  "secret not configured" — which looked like a broken pairing. The paired
  backend ran on 48011 for the afternoon and is back on 48010 as
  `PORTS-AND-HOSTS.md` describes; check `lsof -iTCP:48010` and the
  process's `DATABASE_URL` before assuming the pairing is wrong.
- **`bench --site X console` mangles multi-line blocks** (IPython
  autoindent). Write single statements, or a file and `bench console < file`.
- **The ERPNext sandbox needed System Settings country/timezone/currency set
  before its setup wizard could complete** — see `PORTS-AND-HOSTS.md`.

## Ground rules still in force

- Dev only. Nothing runs against or deploys to a live server.
- **Pushing or committing to either repo needs explicit go-ahead each time.**
  Commit as `suparikoli <manoj311093@gmail.com>` for medusync, and as
  `Mithtech Innovative Solutions <mithtech.is@gmail.com>` for the plugin.
- TDD both sides; verify live against both running stacks, not only tests.
- Nothing client-specific in either application, and **nothing that varies
  per deployment may be hard-coded** — read it from the connected system at
  runtime and let the operator pick.
