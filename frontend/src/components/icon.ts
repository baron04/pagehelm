/** The panel's icon ramp.
 *
 * Size follows the type an icon sits beside: 14 against body prose and card headings,
 * 12 against meta prose and chips. Weight is stated in CSS pixels of ink -- one, as
 * everywhere, 1.2 on the send control, which carries the heaviest click in the panel, and
 * 1.25 on the title bar's window controls -- and converted here, because lucide measures
 * `strokeWidth` against its own 24px grid rather than in pixels. That conversion is why the
 * call sites used to carry five different strokeWidth numbers: each one was
 * hand-compensating for its size, which is how an icon system drifts.
 */
export function iconProps(size: 12 | 14 | 16, ink = 1): {
  size: number
  strokeWidth: number
  'aria-hidden': true
} {
  return {size, strokeWidth: (24 * ink) / size, 'aria-hidden': true}
}
