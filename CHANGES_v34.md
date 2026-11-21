# v34 — Deal history in a popup

The Purchase → Sale Trail is no longer a section at the bottom of Deals & Sales.
Each deal row has a **History** button next to **Edit** that opens that deal's
trail in a popup:

- Vehicle, bought from / for, sold to / for, days held, profit, and the current
  Car and Document Status, laid out so the purchase and the sale line up.
- Every Car Status step: # / Stage / Description / Date & Time / Changed By / Remarks.
- **Edit Deal** and **Close** at the bottom.

## Behaviour
- Closes with ×, Close, Escape or a click outside. Keyboard focus moves into the
  popup, stays there while it is open, and returns to the History button.
- `/dealer/deals#history-deal-<id>` opens a deal's history directly; the
  "View history" link under Status Remark on Edit Deal uses it.
- Phones: the popup takes the screen width, the summary is two columns and each
  step is a compact card.
- History is recorded exactly as before: no data, route or database change.

## Files
`templates/dealer/deals.html`, `templates/dealer/deal_form.html`, `test_deal_status.py`

## Testing
- `pytest`: whole suite passes (new test: History button placement, popup content,
  Edit Deal link).
- Chromium: opening, every way of closing, focus handling, the popup covering the
  sidebar, the Edit Deal link, and the phone layout. No JavaScript or server errors.

## Note
With the extra button the Deals table fits from 1536 px wide. At 1440 px it scrolls
18 px inside its card and at 1366 px about 90 px (the table already scrolls inside
its card on narrower screens).
