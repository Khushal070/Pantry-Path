import type { Option } from "../types";
import { CheckIcon, LockIcon } from "../icons";

/** Toggle chips, e.g. allergens. Locked chips are on and can't be turned off here. */
export function ChipGroup({
  options,
  selected,
  locked = [],
  disabled = false,
  label,
  onToggle,
}: {
  options: Option[];
  selected: string[];
  locked?: string[];
  disabled?: boolean;
  label: string;
  onToggle: (id: string) => void;
}) {
  return (
    <div className="chips" role="group" aria-label={label}>
      {options.map((o) => {
        const on = selected.includes(o.id);
        const isLocked = locked.includes(o.id);
        return (
          <button
            type="button"
            key={o.id}
            className={`chip${on ? " on" : ""}${isLocked ? " locked" : ""}`}
            aria-pressed={on}
            disabled={disabled || isLocked}
            title={isLocked ? "From your preferences. Change it in Preferences." : undefined}
            onClick={() => onToggle(o.id)}
          >
            {isLocked ? <LockIcon size={12} /> : on ? <CheckIcon size={12} /> : null}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

/** One choice out of a few, e.g. the diet. */
export function Segmented({
  options,
  value,
  disabledIds = [],
  disabled = false,
  label,
  onChange,
}: {
  options: Option[];
  value: string;
  disabledIds?: string[];
  disabled?: boolean;
  label: string;
  onChange: (id: string) => void;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          type="button"
          role="radio"
          key={o.id}
          aria-checked={value === o.id}
          className={value === o.id ? "on" : ""}
          disabled={disabled || disabledIds.includes(o.id)}
          onClick={() => onChange(o.id)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function toggle(list: string[], id: string): string[] {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
}
