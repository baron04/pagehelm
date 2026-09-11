/**
 * 闸 — a heavy ring cut once, with a solid mass sitting in the cut.
 *
 * One closed silhouette so it still reads at 16px, one colour so it follows the theme, and
 * one idea: this thing can act on your application, but only through the one opening.
 */
export function Mark({size = 20}: {size?: number}): React.JSX.Element {
  return (
    <svg className="woa-mark" viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
      <path d="M14.50 18.86A7.3 7.3 0 1 1 18.86 14.50" fill="none" stroke="currentColor"
            strokeWidth="5" strokeLinecap="butt" />
      <circle cx="17.16" cy="17.16" r="2.05" fill="currentColor" />
    </svg>
  )
}
