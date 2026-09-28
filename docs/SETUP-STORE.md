# Setting up a store

Code travels in git; settings and data do not. This is the other half — the
list of everything that has to exist on a site before the sync behaves, in the
order it has to happen, with the parts a script can do marked as such.

Nothing here is specific to one deployment. Put your own values in a copy of
`store.example.json`, keep that copy out of git, and keep secrets in the
environment.

## 1. Before anything

| | |
|---|---|
| ERPNext | v15 or v16, this app installed (`bench get-app`, `bench --site <site> install-app medusync`) |
| Medusa | v2 with `@mithtech-medusa/plugin-erpnext` installed and its ERPNext URL + API key/secret in its env |
| Clocks | both hosts agree to within a minute |

**The clocks matter.** Every message is signed with a timestamp and rejected
outside a replay window. A host that has been asleep is usually minutes or
hours out, and every push then fails with "Stale timestamp", which reads like a
secrets problem and is not. On the ERPNext host:

```bash
sudo chronyc makestep
```

## 2. The one command

```bash
cp docs/store.example.json ~/store.json    # then edit it
export MEDUSYNC_INBOUND_SECRET=...         # Medusa -> here
export MEDUSYNC_OUTBOUND_SECRET=...        # here -> Medusa

bench --site <site> execute medusync.setup_store.run \
  --kwargs "{'config': '/home/frappe/store.json', 'dry_run': True}"   # look first
bench --site <site> execute medusync.setup_store.run \
  --kwargs "{'config': '/home/frappe/store.json'}"
```

It is idempotent: run it again after editing the file and it writes only what
differs. It prints three lists — what it changed, what it ignored, and what is
left for a person.

What it does:

* creates the default mappings when the site has none (`install-app` alone does
  not create them)
* Medusync Settings: enable sync, Medusa URL, timeouts, catalogue doctype
* the shared secrets, from the two environment variables
* **Sync Selection**: which doctypes are restricted, and to which mode
* the **Medusync Site** row: URL, secrets, handler pack, default account
  manager, what an arriving order becomes, whether it is submitted, whether
  payments are recorded, the warehouses and price lists the store draws on
* **fixed values** in a field map — the values an arriving document must carry
  that the store has no idea about (see §5)

What it will not do: choose documents, switch a mapping on, or touch Medusa.

## 3. Keep the catalogue on "Only chosen documents"

`Medusync Settings -> Sync Selection`. Two modes:

| Mode | Meaning |
|---|---|
| Only chosen documents | nothing moves until someone picks it |
| Every document unless excluded | everything moves except what is excluded |

On a real ERP the item table is tens of thousands of rows. Switched to "Every
document unless excluded", the catalogue mapping pushes all of them the moment
it is enabled. Leave `Item` on **Only chosen documents**.

Choosing items:

* one at a time: Item form -> **Medusa sync** -> tick the store -> Save
* in bulk: Item list -> filter -> set the page size to 500 -> tick the rows ->
  **Actions -> Medusa sync...**
* by rule: `medusync.selection.choose_all` with filters

Choosing an item sends nothing by itself. To send what is already chosen:
open the catalogue mapping -> **Run -> Push Everything Now** (limit 0). Items
already in the store are updated, not duplicated.

When the store confirms it has the product, this app follows up with that
item's price in force today (for each price list the store draws on) and its
sellable stock (for each warehouse linked on the store). That is why the
warehouse and price-list rows in §2 matter: with none, a product arrives with
no price and no stock and the storefront will not sell it.

## 4. Rehearse, then switch on — in that order

Neither side enables a mapping it has not rehearsed **in its current shape**,
and it declines silently rather than loudly. Editing a field map invalidates
the rehearsal on **both** sides.

1. edit the mapping
2. here: **Test -> Rehearse**
3. in Medusa: open the same mapping -> **Test push**
4. switch it on in Medusa; this side follows if its own rehearsal still matches

Start with Customers, then the catalogue, then Orders. Rehearse against a real
record, not a sample one.

## 5. Fixed values: what an order cannot know

A store order knows what was bought, by whom, for how much. It does not know
the things ERPNext insists on for its own books, and a draft that stops on a
missing mandatory field is a person's evening. Each of these is one row in the
field map with a **Fixed Value** and no Medusa path (`mapping_constants` in the
config file):

| Document | Field | Why |
|---|---|---|
| Sales Order | `company_address_name` | the company address the sale is made from. On an Indian site this is where the **company GSTIN** and the place of supply are read from, so leaving it blank lets ERPNext pick whichever address it finds first — often the wrong one. Pin the branch that actually sells. |
| Sales Order | `order_type` | mandatory; the store has no equivalent |
| Sales Order | site-specific mandatory fields | e.g. a Sales Type / Sub Type a site has made mandatory |
| Customer | `customer_type`, `customer_group`, `territory` | a first-time shopper's Customer is created by the order itself; without these it is created wrong (a person filed as a company, say) |

Tax arrives as **one line** on the order ("Medusa Tax") carrying the amount the
store charged, plus one line for delivery. The store is the authority on both:
its total and the ERPNext total then agree to the paisa. Splitting that one
line into CGST/SGST/IGST postings is a separate decision and is not done here.

## 6. The Medusa half

Done in Medusa, once, and easy to miss:

* a **region** for the country, with the store's currency
* a **tax region** for it with a **tax provider** selected — with none, the
  store charges no tax at all and the order arrives with tax 0
* a **sales channel**, and the products on it (the plugin puts arriving
  products on the store's default channel; a product on no channel is invisible
  to the storefront even when it exists)
* a **shipping option**, and a **shipping profile** on every product — a cart
  whose items have no profile cannot be completed
* a **publishable API key** on the storefront
* on each mapping: the **push events** it listens for (e.g.
  `customer.created,customer.updated`) — a mapping with an empty event list
  pushes nothing

## 7. Prove it, in this order

1. **A product.** Choose one item, Push Everything Now, and check it in Medusa
   with a price and stock. Then change its price in ERPNext and watch the new
   price arrive.
2. **A customer.** Sign up on the storefront; the Customer appears here.
3. **An order.** Buy that product on the storefront. Then check: the Sales
   Order exists, is submitted if the store is set to submit, carries the
   company address you pinned, and its grand total equals the store's total,
   with the tax and delivery lines matching.
4. **The log.** `Medusync Log` — every message both ways, with its payload.
   A row stuck in `Queued` with no error is worth a second look; a failed one
   retries on its own (`medusync.tasks.retry_due`, every minute), or
   `medusync.manual.resync_failed()`.

## 8. Things that look like bugs and are not

* **Saving an unchanged form sends nothing** ("No changes in document"), so a
  save cannot be used to re-send a price.
* **`medusa_*_id` fields are supposed to be absent** from the doctype. They are
  lifted off the payload into `Medusync Link` rows, which is where the
  cross-system id lives. Do not delete those rows.
* **`Customer.email_id` is not a stored field** — it is fetched from the
  primary contact. A mapping keyed on it works, but rests on a derived column;
  keying on the link id is safer.
* **A mapping that silently refuses to switch on** has an out-of-date rehearsal.
  See §4.
