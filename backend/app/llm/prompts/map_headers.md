You are reading the header row of a vendor's product spreadsheet and deciding
what each column means.

Map every column to exactly one canonical field from this list:

- title: the product name
- description: long text describing the product
- brand: manufacturer or brand name
- source_category: the vendor's own category or product type
- price: the selling price
- compare_at_price: the pre-discount or list price
- currency: a currency code or symbol column
- sku: a stock keeping unit code
- handle: a slug or url-safe identifier
- source_url: a link to the product page
- image: an image URL
- tags: a list of keywords, usually comma separated
- stock: a quantity on hand
- option_name: the name of a variant dimension, for example a column literally
  holding the word "Size"
- option_value: the value of a variant dimension, for example S, M, L
- attribute: any other factual product property, such as material, weight,
  colour or dimensions
- ignore: internal columns with no product meaning, such as row ids, cost price,
  margin, supplier notes, timestamps or warehouse codes

Rules you must not break:

1. Use canonical_field "attribute" for anything factual that is not in the list
   above, and set attribute_name to the human readable name for it. For a column
   headed "Net weight (g)" that is attribute_name "Net weight" with unit "g".
2. Set unit only when the header states one, usually in brackets. Take the unit
   out of the name: "Weight (kg)" becomes attribute_name "Weight", unit "kg".
3. Use "ignore" freely. A vendor's export is full of internal columns, and
   wrongly mapping a cost price to price would publish the wrong number.
4. Judge by both the header and the sample values. A column headed "Size" whose
   values are "12.5 cm" is an attribute, not a variant option.
5. Set confidence per column. Below 0.5 means you are guessing from the header
   alone and a human should confirm it.
6. Every column in the input must appear exactly once, either in mappings or in
   unmapped_columns. Do not invent columns that are not there.

Columns, with sample values:

$columns
