"""
car_colors.py
-------------
The colour list behind the vehicle Colour dropdown, in one place so the
dealer form and both admin forms can never drift apart.

Nothing here touches the database. Vehicle.color stays a free-text
VARCHAR(50), which is what makes the dropdown safe to add: any colour
already saved — typed before this list existed, or entered by an admin —
still loads and still saves. See color_options() below.
"""

# Common paint names across the Indian market. Order is roughly by how
# often they turn up on a used lot, not alphabetical, so the usual picks
# sit near the top of the list.
CAR_COLORS = [
    'White',
    'Pearl White',
    'Silver',
    'Grey',
    'Black',
    'Red',
    'Blue',
    'Brown',
    'Beige',
    'Bronze',
    'Gold',
    'Green',
    'Maroon',
    'Orange',
    'Yellow',
    'Purple',
    'Copper',
    'Sky Blue',
    'Navy Blue',
    'Dark Grey',
    'Champagne',
    'Dual Tone',
    'Other',
]


def color_options(current=None):
    """
    The dropdown options for a vehicle, preserving whatever is already saved.

    If a vehicle's stored colour is not in CAR_COLORS — an older record, or
    something typed in free text before this dropdown existed — it is added
    to the front of the list so opening and saving that vehicle does not
    wipe the value. Matching ignores case and surrounding spaces, so
    "  white " loads as the existing "White" option rather than duplicating it.
    """
    options = list(CAR_COLORS)

    if not current:
        return options

    current = str(current).strip()
    if not current:
        return options

    if current.casefold() not in {c.casefold() for c in options}:
        options.insert(0, current)

    return options


def normalise_color(value):
    """
    Map a stored value onto its canonical list entry when one matches by
    case, so 'WHITE' and 'white' both select the 'White' option. Returns the
    value unchanged when there is no match.
    """
    if not value:
        return ''
    value = str(value).strip()
    for c in CAR_COLORS:
        if c.casefold() == value.casefold():
            return c
    return value
