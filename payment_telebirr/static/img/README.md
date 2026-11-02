# telebirr logo

`telebirr.png` and `telebirr.svg` are the **official telebirr logo**, taken
from Ethio Telecom's own site.

| | |
|---|---|
| Source | `https://www.ethiotelecom.et/wp-content/uploads/2021/04/TeleBirr-Logo.svg` (and the `.png` sibling) |
| Fetched | 10 September 2026 |
| Original | 348 x 159, RGBA, transparent background |
| Processing | Scaled proportionally and centred on a 128 x 128 transparent canvas. Colours untouched. |

The vector is kept alongside the raster because it is the better master: if a
larger or differently-shaped icon is ever needed, re-render from
`telebirr.svg` rather than upscaling the PNG.

## A note on fetching this

`www.ethiotelecom.et` serves only its leaf certificate and omits the GlobalSign
intermediate, so an ordinary `curl` or browser fetch fails with *unable to get
local issuer certificate*. That is a misconfiguration on their server, not a
sign of a bad certificate: the leaf is a genuine GlobalSign OV certificate
issued to `ETHIO TELECOM S C`, and it verifies once the intermediate from the
certificate's own AIA extension is supplied. If you need to re-download this
asset, fetch
`http://secure.globalsign.com/cacert/gsrsaovsslca2018.crt`, append it to your
CA bundle, and pass that with `--cacert`. Do not reach for `curl -k`.

## Trade mark

The telebirr name and logo belong to Ethio Telecom. They are used here only to
identify telebirr as a payment option to members choosing how to pay, which is
the same nominative use Odoo makes of the two hundred provider logos it ships.
