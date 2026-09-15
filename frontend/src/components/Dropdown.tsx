import { useEffect, useId, useRef, useState } from 'react'
import { Check, ChevronDown } from 'lucide-react'

/**
 * A select whose options can say more on hover than their label does.
 *
 * WHY NOT A NATIVE <select>. Its open list is drawn by the operating system outside the page,
 * so nothing in the page can decorate an option: a `title` on an <option> is ignored on Windows
 * and the question "what is the full path of this project" had no answer at the moment it was
 * asked. The Population list names a project by its folder alone, which is only usable if the
 * rest of the path is one hover away, so the list is drawn here, as rows.
 *
 * THE SELECT-ONLY COMBOBOX, as the ARIA pattern spells it: the trigger is the combobox, the
 * popup a listbox, the active row named through aria-activedescendant so focus never leaves the
 * trigger. Arrows move, Enter and Space commit, Escape closes, Tab leaves, a click outside
 * closes. Escape stops here: the Adopt drawer listens on the window for the same key, and one
 * press closing both would surprise the person who pressed it once.
 */
export interface DropdownOption {
  value: string
  label: string
  /** What the hover says: for a project, its full working directory. */
  title?: string
}

export function Dropdown({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: DropdownOption[]
  onChange: (value: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [cursor, setCursor] = useState(0)
  const box = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLUListElement>(null)
  const id = useId()
  const labelId = `${id}-label`
  const listId = `${id}-list`
  const optionId = (index: number) => `${id}-option-${index}`
  const selected = options.findIndex((option) => option.value === value)
  // A value the options do not name shows ITSELF and marks no row. The native select rendered
  // the first option and fired no change for such a value, which is how the header once read
  // "No restriction" while a deleted project was still set.
  const current = selected >= 0 ? options[selected] : undefined

  const show = () => {
    setCursor(selected >= 0 ? selected : 0)
    setOpen(true)
  }
  const hide = () => setOpen(false)
  const commit = (index: number) => {
    const option = options[index]
    if (option && option.value !== value) onChange(option.value)
    hide()
  }

  useEffect(() => {
    if (!open) return
    const away = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) hide()
    }
    document.addEventListener('mousedown', away)
    return () => document.removeEventListener('mousedown', away)
  }, [open])

  useEffect(() => {
    if (!open) return
    const active = list.current?.querySelector('[data-active="true"]')
    if (active instanceof HTMLElement) active.scrollIntoView?.({ block: 'nearest' })
  }, [cursor, open])

  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    const last = options.length - 1
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        if (open) setCursor((was) => Math.min(was + 1, last))
        else show()
        break
      case 'ArrowUp':
        event.preventDefault()
        if (open) setCursor((was) => Math.max(was - 1, 0))
        else show()
        break
      case 'Home':
        if (open) {
          event.preventDefault()
          setCursor(0)
        }
        break
      case 'End':
        if (open) {
          event.preventDefault()
          setCursor(last)
        }
        break
      case 'Enter':
      case ' ':
        event.preventDefault()
        if (open) commit(cursor)
        else show()
        break
      case 'Escape':
        if (open) {
          event.preventDefault()
          event.stopPropagation()
          hide()
        }
        break
      case 'Tab':
        if (open) hide()
        break
      default:
        break
    }
  }

  return (
    <div ref={box} className="relative flex items-center gap-1.5 text-xs text-ink-faint">
      <span id={labelId}>{label}</span>
      <button
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-labelledby={labelId}
        aria-activedescendant={open ? optionId(cursor) : undefined}
        title={current?.title}
        onClick={() => (open ? hide() : show())}
        onKeyDown={onKeyDown}
        className="flex max-w-[22rem] items-center gap-2 rounded-md border border-edge bg-panel
                   px-2 py-1.5 text-sm text-ink outline-none focus:border-accent"
      >
        <span className="truncate">{current?.label ?? value}</span>
        <ChevronDown size={12} aria-hidden="true" className="shrink-0 text-ink-faint" />
      </button>
      {open ? (
        <ul
          ref={list}
          id={listId}
          role="listbox"
          aria-labelledby={labelId}
          className="absolute left-0 top-full z-30 mt-1 max-h-[52vh] min-w-full max-w-[36rem]
                     overflow-auto rounded-md bg-panel-raised p-1 shadow-float"
        >
          {options.map((option, index) => (
            <li
              key={option.value}
              id={optionId(index)}
              role="option"
              aria-selected={option.value === value}
              data-active={index === cursor ? 'true' : 'false'}
              title={option.title}
              onMouseEnter={() => setCursor(index)}
              // The trigger keeps focus: a mousedown on a row would move it and blur the combobox
              // before the click that commits.
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => commit(index)}
              className={`flex cursor-pointer items-center gap-2 rounded px-3 py-1.5 text-sm
                          transition-colors duration-150 ${
                            index === cursor ? 'bg-accent/12 text-ink' : 'text-ink-dim'
                          }`}
            >
              <span className="w-3.5 shrink-0 text-accent">
                {option.value === value ? <Check size={13} aria-hidden="true" /> : null}
              </span>
              <span className="truncate">{option.label}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
