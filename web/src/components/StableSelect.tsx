// A `<select>` that only touches the DOM's selection when the chosen value
// changes. React sets `option.selected` on a controlled select every time it
// renders, and the page renders on every update from the server (every few
// seconds), which makes an open dropdown flicker, close or lose its
// highlighted option. Here the select is uncontrolled and follows `value` in
// an effect, only when the DOM disagrees with it.
import { useLayoutEffect, useRef } from "react";

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

interface Props {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  name?: string;
  disabled?: boolean;
  ariaLabel?: string;
}

export function StableSelect({ value, options, onChange, name, disabled, ariaLabel }: Props) {
  const element = useRef<HTMLSelectElement>(null);
  useLayoutEffect(() => {
    // After the options are in place, so a value that was just added is found.
    if (element.current && element.current.value !== value) element.current.value = value;
  });
  return (
    <select
      ref={element}
      name={name}
      aria-label={ariaLabel}
      defaultValue={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value} disabled={option.disabled}>
          {option.label}
        </option>
      ))}
    </select>
  );
}
