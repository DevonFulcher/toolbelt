# toolbelt-usage

A Claude Code mod that draws the account's usage limits above the prompt: one
bar per limit window (`five_hour` as "5 hours", `seven_day` as "7 days",
`spend_limit` as "Spend", any other kind title-cased), with a vertical **pace
line** where an even spread of the allowance across the window would be now.

- Desktop: one `Svg` (500 px wide, 20 px per row): label, bar, then
  `38%  ·  9% under pace  ·  resets in 2h 10m`. Hover a row for a tooltip.
- Terminal: a text row per window, `█` used, `░` free, `│` pace.
- Colour: green/blue while used is at or under pace, amber over pace, red at
  100% or more. Fill is clamped to the bar; the text shows the real number.
- No limits (off a subscription, or before the first API response): nothing is
  drawn.

Pace needs the window's start: `resetsAt` minus 5 h / 7 d, or one calendar
month for `spend_limit` (and any kind containing "month"). An unknown kind or a
missing `resetsAt` gets no pace line, and the tooltip says why.

Data comes from `$.session.usage()`, refreshed on `session.measure` and every
60 s (the pace moves with time alone). Nothing is spawned or sent anywhere.

## Option

`statusLine` (default off): also keep a one-line text summary under the prompt.

## Install

`../install.sh` installs this and `toolbelt-claims`. Start a new session
afterwards. To disable only this one: `claude plugin disable toolbelt-usage@toolbelt`.

## Development

```sh
claude plugin validate claude-plugin/mods/usage-limits
claude plugin test claude-plugin/mods/usage-limits   # Claude Code 2.1.295 or newer
```

`hooks/window-pace.ts` holds the pure label/pace/colour/format functions,
`hooks/bar-geometry.ts` the SVG and terminal layout, `hooks/register.tsx` the
hooks. Tests are in `tests/`.
