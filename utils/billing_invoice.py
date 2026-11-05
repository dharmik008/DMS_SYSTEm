"""
billing_invoice.py
------------------
Builds the tax invoice for a SUBSCRIPTION payment — Caryanams billing a
dealer for their plan.

Not to be confused with templates/dealer/invoice.html, which is the dealer
billing a customer for a car. The roles are reversed here: Caryanams is the
supplier, the dealer is the recipient.

>>> READ THIS BEFORE GOING LIVE <<<
Two things below are placeholders that only your accountant can settle:

  1. SUPPLIER — your legal name, address, GSTIN and state. A tax invoice
     without a correct supplier GSTIN is not a valid tax invoice.
  2. GST_INCLUSIVE — whether the ₹999 / ₹2999 on the pricing page already
     includes GST (assumed True here, which is normal for a price shown to
     the customer) or is exclusive and tax is added on top.

Get either wrong and you will be issuing incorrect tax documents.
"""

from datetime import datetime

# ── Supplier: replace every value here with your real registered details ────
SUPPLIER = {
    'legal_name': 'Caryanams',
    'address_lines': [
        'REPLACE WITH REGISTERED ADDRESS',
        'City, State - PIN',
    ],
    'gstin': '',            # e.g. '24AABCU9603R1ZX' — leave '' to hide the row
    'state': '',            # e.g. 'Gujarat'
    'state_code': '',       # e.g. '24'
    'email': '',
    'phone': '',
    'pan': '',
}

# Is the advertised plan price GST-inclusive?
#   True  -> ₹999 is the total; base = 999 / 1.18, tax backed out of it
#   False -> ₹999 is before tax; ₹1,178.82 is charged
GST_INCLUSIVE = True
GST_RATE = 0.18

# Services default to an intra-state split. A dealer in a different state to
# the supplier should be billed IGST instead — set this False and the template
# will show a single IGST line.
INTRA_STATE = True

# SAC code for "Information technology software services". Confirm with your
# accountant; the wrong code on an invoice is a compliance problem.
SAC_CODE = '998314'

PLAN_TITLES = {
    'starter': 'Starter Plan — Dealer Management Subscription',
    'growth': 'Growth Plan — Dealer Management Subscription',
    'pro': 'Pro Plan — Dealer Management Subscription',
}


def fiscal_year(when):
    """Indian financial year label for a date: April to March."""
    if when.month >= 4:
        start, end = when.year, when.year + 1
    else:
        start, end = when.year - 1, when.year
    return '{}-{}'.format(start, str(end)[-2:])


def invoice_number(source, record_id, when):
    """
    Deterministic invoice number, so reprinting the same payment always gives
    the same number. Never generate these from a counter that can restart.

        CY/2026-27/SUB-0042
        CY/2026-27/RZP-0117
    """
    prefix = 'SUB' if source == 'subscription' else 'RZP'
    return 'CY/{}/{}-{:04d}'.format(fiscal_year(when), prefix, record_id)


def split_tax(amount):
    """
    Break a charged amount into base and tax.

    Returns (base, tax_total, total). With GST_INCLUSIVE the tax is backed out
    of the amount charged, so `total` equals what the dealer actually paid.
    """
    amount = float(amount or 0)
    if amount <= 0:
        return 0.0, 0.0, 0.0

    if GST_INCLUSIVE:
        base = amount / (1 + GST_RATE)
        tax = amount - base
        total = amount
    else:
        base = amount
        tax = amount * GST_RATE
        total = base + tax

    return round(base, 2), round(tax, 2), round(total, 2)


def build_invoice(payment):
    """
    Assemble everything the invoice template needs from one payment row
    (the dicts the admin payments page already produces).
    """
    when = payment.get('when') or datetime.now()
    amount = float(payment.get('amount') or 0)
    base, tax, total = split_tax(amount)

    half = round(tax / 2, 2)
    # rounding can leave a paisa adrift; put it on the second line
    tax_lines = ([{'label': 'CGST @ 9%', 'amount': half},
                  {'label': 'SGST @ 9%', 'amount': round(tax - half, 2)}]
                 if INTRA_STATE else
                 [{'label': 'IGST @ 18%', 'amount': tax}])

    plan = (payment.get('plan') or '').lower()
    description = PLAN_TITLES.get(plan, 'Dealer Management Subscription')
    if plan and plan not in PLAN_TITLES:
        description += ' ({})'.format(payment['plan'])

    dealer = payment.get('dealer')

    return {
        'number': invoice_number(payment.get('source_key', 'subscription'),
                                 payment.get('id', 0), when),
        'date': when,
        'fiscal_year': fiscal_year(when),
        'supplier': SUPPLIER,
        'sac_code': SAC_CODE,
        'dealer': dealer,
        'description': description,
        'plan': payment.get('plan'),
        'base': base,
        'tax_lines': tax_lines,
        'tax_total': tax,
        'total': total,
        'gst_inclusive': GST_INCLUSIVE,
        'method': payment.get('method'),
        'reference': payment.get('reference'),
        'status': payment.get('status'),
        'amount_words': rupees_in_words(total),
        'supplier_incomplete': not SUPPLIER.get('gstin'),
    }


_ONES = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight',
         'Nine', 'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen',
         'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen']
_TENS = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy',
         'Eighty', 'Ninety']


def _under_thousand(n):
    if n == 0:
        return ''
    if n < 20:
        return _ONES[n]
    if n < 100:
        return (_TENS[n // 10] + (' ' + _ONES[n % 10] if n % 10 else '')).strip()
    return (_ONES[n // 100] + ' Hundred' +
            (' ' + _under_thousand(n % 100) if n % 100 else '')).strip()


def rupees_in_words(amount):
    """
    Indian numbering: lakh and crore, not million. An invoice that says
    'One Million' where it should say 'Ten Lakh' looks wrong to every Indian
    accountant who reads it.
    """
    rupees = int(amount)
    paise = int(round((amount - rupees) * 100))

    if rupees == 0:
        words = 'Zero'
    else:
        parts = []
        crore, rupees_left = divmod(rupees, 10000000)
        lakh, rupees_left = divmod(rupees_left, 100000)
        thousand, rupees_left = divmod(rupees_left, 1000)

        if crore:
            parts.append(_under_thousand(crore) + ' Crore')
        if lakh:
            parts.append(_under_thousand(lakh) + ' Lakh')
        if thousand:
            parts.append(_under_thousand(thousand) + ' Thousand')
        if rupees_left:
            parts.append(_under_thousand(rupees_left))
        words = ' '.join(parts)

    out = words + ' Rupees'
    if paise:
        out += ' and ' + _under_thousand(paise) + ' Paise'
    return out + ' Only'
