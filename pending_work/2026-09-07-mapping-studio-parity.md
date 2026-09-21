# A real two-panel mapper, and parity between the two sides

**Raised:** 2026-09-07, from looking at both mapping pages side by side
**Belongs to:** the next block of operator-facing work
**Side:** both, and they are in very different places.

## Where each side actually is

**Medusa** has a purpose-built page — `/app/erpnext` → **Mappings**. Entity
picker, DocType search, a field-pair grid, autofill suggestions with
confidence scores, transforms, templates, a trigger builder, and since
Phase 7: Sample, Test push, Test pull, Pull now. It is a real editor.

**ERPNext** has a DocType form. `Medusync Mapping` with a child-table grid
where you type `frappe_field` and `medusa_path` into two columns. It has
the studio bolted on as toolbar buttons — Rehearse, Rehearse and Enable,
Send a Test Event, Show a Sample Record, Push Everything Now, Re-send What
Gave Up — and a headline that says where the mapping stands. Useful, but it
is still a form where you type field names.

## What neither side has

Neither is n8n's mapper, and the difference is not cosmetic:

- **No two-panel drag.** n8n puts source fields on the left, target on the
  right, and you drag one onto the other. Both sides here are a grid of
  text pairs. The Sample button shows you the source record, but in a
  separate dialog you then read from and type back into the grid.
- **No live preview per row.** You can rehearse the whole mapping and see
  the resulting payload; you cannot see, on the row you are editing, what
  that one field currently resolves to.
- **No item-by-item walk.** There is no "show me record 1 of 40, map it,
  next" flow. Rehearsal is one sample at a time and the sample is chosen
  for you.
- **No drag to reorder or split.** Reordering is deliberately not a
  behaviour change (the signature ignores it), but there is no way to do it
  by dragging either.

## What would close it

In the order that gets the most from the least:

1. **A per-row resolved value.** The Sample already returns the record and
   the mapping engine already walks a dotted path. Showing the current
   value beside each row is a small change and removes most of the
   typing-blind problem.
2. **A field picker on each side of the row.** ERPNext's fields come from
   live meta already (`/admin/erpnext/doctypes/:name`, `studio.fields_of`).
   Medusa's come from the registry — see
   `2026-09-07-medusa-field-discovery.md`, which is the blocker for making
   the Medusa half a picker rather than a text box.
3. **The two-panel layout**, once both sides have a real field list to
   render.
4. **ERPNext parity**: a Frappe Page rather than a DocType form. A DocType
   form cannot host a two-panel mapper; it can host the buttons, which is
   what it does today.

## What is already true and worth not rebuilding

- **The mapping list does synchronise both ways.** Saving on either side
  sends the whole mapping to the other, paired by `mapping_uid`, ordered by
  `version`, ERPNext winning a tie. It fires on save, not as you type, and
  first contact always arrives switched off.
- **Both sides log it.** A mapping-config message is an ordinary logged
  message: `Medusync Log` on one side, `erpnext_sync_event` on the other,
  with the same retry, the same idempotency and the same rehearsal marking.
- **What is not logged** is the editing itself. Frappe keeps document
  version history for `Medusync Mapping` by default; Medusa keeps only
  `version` and `updated_by_user_id`. Nobody can currently answer "who
  changed this field, and to what, on the Medusa side".

## Questions

Both were answered and built — see **Built** below. What is still open
here is the edit-log question, **Q9**.

---

## Built — 2026-09-07

Both questions here were answered and built: both sides now have a real editor,
and ERPNext got its own page rather than a DocType form.

**ERPNext**
- `Mappings` page (`/app/medusync-mappings`) — the same columns as the
  Medusa admin, in the same order: name, entity, doctype, direction, pairs,
  last run, an enable switch and delete. Computed in `portal.py` so the two
  lists agree on what a mapping is instead of each deciding for itself.
- A field mapper dialog: both sides are dropdowns with **Required** first
  above a separator, direction as four arrows, a coverage banner checking
  *both* sides, `Suggest matches`, and fixed values that offer the real
  accepted values (a Link lists this site's records, a Select its options).
- Lives in `public/js/mapper.bundle.js` via `app_include_js`, so the page
  and the form share one implementation.

**Medusa**
- The field-pair row was six always-visible controls; it is now store field
  · arrows · Frappe field, with transforms, combined `{a} {b}` sources and
  unlisted fieldnames behind a `⋯` that opens itself when a row already
  uses one.
- The picker was still reading the **curated** paths. It now loads the
  discovered list per entity, falling back to curated only if the store
  cannot be read.
- The pull filter is rows of dropdowns rather than hand-written JSON, and
  the raw condition expression is hidden unless the preset is Custom.

**Not done:** neither side searches a long dropdown — fine for Item at 84
fields, tedious for Sales Order at 117. And the mapper does not yet order
suggestions by the dictionary's confidence, because the dictionary does not
exist yet.

---

## Built — 2026-09-21

Parity on fixed values, closing the gap the other way round: ERPNext has
offered the real accepted values since 2026-09-07, the Medusa editor showed
a bare text box.

**Medusa**
- A fixed value in the editor is chosen the way the wizard chooses one: a
  Link offers that site's records, a Select its options, anything else free
  text. A pair can be turned into a fixed value and back with `=` / `↩`, so
  a constant can be added to a mapping that already exists. A saved value
  the site no longer has stays selected and says so.
- The wizard and the editor now share one control and one options reader,
  rather than the two near-copies that let them drift.

**Both sides, and the reason this needed doing**

A row switched to "fixed value" and left blank named a Frappe field, so it
counted as covering it. On the Medusa side that silenced the mandatory-field
banner, satisfied `unmetRequired`, passed the rehearsal that gates switching
a mapping on, and then wrote an empty string over whatever ERPNext held on
the first real record. It is now flagged in the editor, dropped on save,
left out of the payload, and reported in the skipped fields.

ERPNext never had the bug — `coveredHere` has always required a trimmed
value, the mapper refuses to save a row with neither a store field nor a
fixed value, and `mapping_sync` only puts a truthy constant on the wire. The
sentinel it uses for "constant mode, nothing chosen yet" is a single space,
which never survives the save. That difference is worth knowing before
touching either: the two sides express the same rule in different shapes.

**Not done:** the dropdown-search and dictionary-ordering gaps above are
unchanged.

