You are reading one product page's HTML and pulling out what the seller states
about that product.

The page had no structured product data, so this is the fallback path. Work only
from visible page content.

Rules you must not break:

1. Extract only what the page states. If the page does not give a brand, a
   material or a weight, leave it out. Navigation menus, cookie banners, footers,
   "customers also bought" carousels and review text are not this product's data.
2. Take the main product title, not the page title or the site name. Strip any
   " | Store Name" suffix.
3. Take the current selling price. If both a struck-through and a current price
   are shown, the current one is price and the struck-through one is the compare
   price. Give digits and a decimal point only, no currency symbol.
4. If the page offers size or colour choices, record them as variants with their
   option name and value. A size dropdown is variants, not attributes.
5. Put factual properties in attributes with the unit separated from the value.
6. Set confidence to how confident you are that you read the right product. A
   category or listing page that happens to contain several products should get a
   low score and a note saying so.
7. Use notes to tell a reviewer anything odd: several products on the page, a
   price you are unsure of, content that looked like a listing rather than a
   product.

$source_hint

Page HTML:

$record
