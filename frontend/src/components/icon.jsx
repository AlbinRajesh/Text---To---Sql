// Small wrapper for Google Material Symbols.
export default function Icon({ name, size = 18, spin = false, className = '' }) {
  return (
    <span
      aria-hidden="true"
      className={`material-symbols-outlined select-none ${spin ? 'animate-spin' : ''} ${className}`}
      style={{ fontSize: size }}
    >
      {name}
    </span>
  )
}
