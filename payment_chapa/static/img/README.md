# Chapa logo

`chapa.png` is the **official Chapa mark**, taken from Chapa's own site.

| | |
|---|---|
| Source | `https://d14e7ksvv6cssc.cloudfront.net/dcc3bbd7-ba64-4f7c-b020-6264d7af236f/assets/images/favicon_1615196644.png` (the favicon referenced by `https://chapa.co`) |
| Fetched | 10 September 2026 |
| Original | 68 x 68, RGBA, transparent background |
| Processing | Scaled proportionally and centred on a 128 x 128 transparent canvas. Colours untouched. |

## Known limitation

68 x 68 is the largest Chapa publish on their marketing site, so this icon is
upscaled and will look slightly soft next to the telebirr logo, which came
from a vector. It is sharp enough at the 32-48px Odoo actually renders it at.

If Chapa's brand pack or merchant dashboard offers a larger PNG or an SVG,
replace `chapa.png` with it and copy the same file to
`../description/icon.png`. No code or XML changes are needed - both
`payment.method.image` and `payment.provider.image_128` already point here.

## Trade mark

The Chapa name and logo belong to Chapa Financial Technologies. They are used
here only to identify Chapa as a payment option to members choosing how to
pay, which is the same nominative use Odoo makes of the two hundred provider
logos it ships.
