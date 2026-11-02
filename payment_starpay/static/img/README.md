# Star-Pay logo

`starpay.png` is the **official Star-Pay logo**, taken from Star-Pay's own
site.

| | |
|---|---|
| Source | `https://www.starpayethiopia.com/_next/static/media/logo-white.16wgr2_c09-f5.webp` |
| Fetched | 10 September 2026 |
| Original | 1264 x 756, RGBA, white artwork on a transparent background |
| Processing | Scaled proportionally and centred on a 128 x 128 rounded tile filled with `#183153`. The artwork itself is unmodified - not recoloured, not redrawn. |

## Why this one has a coloured backing

The only high-resolution logo Star-Pay publish is the white-on-transparent
version intended for dark backgrounds. Dropped straight onto Odoo's white
checkout it would be invisible, so it sits on `#183153` - the dominant navy on
starpayethiopia.com, which is how Star-Pay themselves present this artwork.

The alternative was their 48 x 48 `favicon.ico`, which is the green-and-navy
lockup meant for light backgrounds and needs no tile. It was rejected because
upscaling 48px to 128px is visibly soft, and legibility at checkout matters
more than avoiding a background.

If Star-Pay supply a high-resolution light-background logo, prefer it: replace
`starpay.png` with a 128 x 128 render on a transparent background and copy the
same file to `../description/icon.png`. No code or XML changes are needed -
both `payment.method.image` and `payment.provider.image_128` already point
here.

## Trade mark

The Star-Pay name and logo belong to Star-Pay Ethiopia. They are used here
only to identify Star-Pay as a payment option to members choosing how to pay,
which is the same nominative use Odoo makes of the two hundred provider logos
it ships.
