# v33 — Vehicle documents, deal status trail, status dropdowns

Builds on v32 (documents on Add Vehicle). Drop-in update: with the default
settings no manual migration is needed (see "Database").

## What changed

### 1. Vehicle Details
- The **Condition Details** card is removed. (The "Condition: Used/New" row in Specifications stays.)
- **Documents & Registration** lists every document saved for the vehicle and for its customer:
  type, name, number, issue and expiry dates (with *Expired* / *Expires soon*), notes, file name,
  upload date, an image thumbnail, and **View** / **Download**.
- **Manage Documents** opens Edit Vehicle at the documents section.
- The vehicle status in the header is a dropdown that saves immediately.

### 2. Documents are managed with the vehicle; the Documents page is gone
- Removed: the Documents page, its sidebar link, its upload / edit / delete routes,
  `templates/dealer/documents.html`, and `db.document_create` / `db.document_delete` (nothing else used them).
- Kept: `/dealer/documents/<id>/file`, the owner-only route that serves document files.
- **Add Vehicle** (as in v32): several vehicle and customer documents, each with a type, details and a
  file, previewed and removable before saving.
- **Edit Vehicle**: every saved document has **Edit** (details and/or a replacement file) and
  **Remove** (with Undo). Changes are saved with the vehicle, checked first, all or nothing. Only
  documents of that vehicle and its customer can be changed.
- **Inventory → Other Documents** lists documents that no vehicle page shows (uploaded without a
  vehicle on the old page, or whose vehicle was deleted) and admin-assigned files, with
  **Attach to vehicle** and **Delete**. Everything the old page showed is still reachable.
- Fix: the vehicle form submitted with `form.submit()`, which skipped the browser-side document
  checks. It now uses `requestSubmit()` (with `novalidate`, so browser number checks that never
  applied to this form still don't).

### 3. Purchase → Sale Trail (Deals & Sales)
- New table `deal_status_history`, one row per step: deal, vehicle, step number, event, previous and
  new status, date and time, user, optional remark.
- A new deal records step 1 ("Deal created with Negotiation status"); every real Car Status change
  adds the next step ("Deal status changed from Negotiation to Finalized"), in the same transaction
  as the change.
- No duplicates: saving without a change adds nothing, the deal row is locked while its status
  changes, and (deal, step) is unique, so a simultaneous save is refused with a message instead of
  being recorded twice.
- Append-only: the model refuses to update a recorded step.
- Deals that existed before this release get one starting step at startup, labelled as the status
  they had when tracking began (their earlier changes were never recorded).
- The trail keeps the existing purchase, sale and profit details and shows each deal's steps as
  # / Stage / Description / Date & Time / Changed By / Remarks. On phones each step is a small card.
- The deal form has an optional **Status Remark**, saved with the step.

### 4. Car Status and Document Status
- Column **Status** is now **Car Status**, with **Document Status** (Pending / Delivered) right
  after it. Both are dropdowns in the table that save on change, and both are on the deal form.
- New column `deals.document_status`, default Pending. The two are independent: neither ever
  changes the other, and Document Status changes are not Car Status steps.

### 5. Status dropdowns
- Audit: every status field was already a dropdown. The real problem was that each page hard-coded
  its own options and the server stored whatever was posted, so editing a record whose status was
  not in that page's list silently replaced it.
- `utils/statuses.py` holds one option list per status field (vehicle, Car Status, Document
  Status), used by every form, filter, inline dropdown and badge. A saved status that is not listed
  stays selectable and is kept on save; known spellings ("Delivered", "trade in", "finalised") map
  to the listed value; anything else is rejected.
- Inline dropdowns on the Inventory cards, the Vehicle Details header and the Deals table.
- Dashboard, Finance and Reports use the same status badge (their colours disagreed before).
  Finalized is navy, so every stage has its own colour.
- Left as they were: CRM lead stages, agent and admin statuses (already dropdowns, separate flows).

### Also fixed
- New Deal accepted any vehicle id from the form, including another dealer's vehicle. It now checks ownership.
- After a validation error, Edit Vehicle shows the saved photos again (all photo slots used to look empty).

## Database
- `deal_status_history` is created by `db.create_all()` at startup.
- `deals.document_status` is added to existing databases by the schema auto-sync at startup and
  existing deals are set to `pending`. With `SCHEMA_AUTO_SYNC=0`, run `python migrate_schema_sync.py`
  once, or:

  ```sql
  ALTER TABLE deals ADD COLUMN document_status VARCHAR(20) DEFAULT 'pending';
  UPDATE deals SET document_status = 'pending' WHERE document_status IS NULL;
  ```
- No existing row is changed or deleted; the startup step only adds a first step to deals that have none.

## Routes
| Method | Path | |
|---|---|---|
| POST | `/dealer/inventory/<id>/status` | new: vehicle status dropdown |
| POST | `/dealer/inventory/documents/<id>/attach` | new: Other Documents |
| POST | `/dealer/inventory/documents/<id>/delete` | new: Other Documents |
| POST | `/dealer/deals/<id>/status` | new: Car Status dropdown |
| POST | `/dealer/deals/<id>/document-status` | new: Document Status dropdown |
| GET / POST | `/dealer/documents`, `/dealer/documents/upload`, `/dealer/documents/edit/<id>`, `/dealer/documents/delete/<id>` | removed |
| GET | `/dealer/documents/<id>/file` | kept |

`db.deal_create()` and `db.deal_update()` take optional `changed_by` and `status_note` arguments
(existing calls work unchanged); `deal_update()` can raise `DealStatusConflict`.

## Files
- New: `utils/statuses.py`, `utils/deal_history.py`, `templates/dealer/_status.html`, `test_deal_status.py`, this file
- Changed: `app.py`, `models.py`, `db.py`, `dealer/routes.py`, `utils/vehicle_documents.py`,
  `utils/visitor_tracker.py`, `static/css/dms.css`, `test_vehicle_documents.py`, and the dealer
  templates `base`, `deals`, `deal_form`, `inventory`, `inventory_detail`, `vehicle_form`,
  `_vehicle_documents`, `dashboard`, `finance`, `reports`
- Deleted: `templates/dealer/documents.html`

## Testing
- `pytest`: 101 passed (whole suite). The four tests of the old Documents page were replaced;
  `test_deal_status.py` is new.
- Browser walkthrough in Chromium at desktop, tablet and phone sizes: Add Vehicle with documents
  (including the browser-side document check), Inventory, Vehicle Details (documents, status
  dropdown, no Condition Details), editing and removing saved documents, a deal taken through
  Negotiation → Finalized → Delivered with its Document Status changed separately, the trail, and
  Other Documents. No JavaScript or server errors, no horizontal overflow on phone or tablet.

## Notes
- No screenshot of the Deals table came with the request; the existing styles were followed.
- "Final" in the example history is the app's existing "Finalized".
- The dealer app has no separate purchase, payment or delivery status fields; Car Status and
  Document Status cover them.
- Each step stores its deal and vehicle. Vehicle and customer details are shown once at the top of
  each deal's trail rather than repeated on every row.

## Noticed, not changed
- Forms have no CSRF protection.
- The `fmtdate` filter adds 5:30 to times that `_now_ist()` already stores in IST, so times shown
  through it are 5 h 30 min ahead. The new pages format their own times and are not affected.
- `responsive.css` hides columns 3, 5 and 6 of every `.table-wrap` table on phones (on Deals:
  Vehicle, Payment and GST, as before). The new tables avoid that class.
