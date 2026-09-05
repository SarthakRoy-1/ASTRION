import styles from "./Button.module.css";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

const VARIANT_CLASS: Record<ButtonVariant, string | undefined> = {
  primary: styles.primary,
  secondary: styles.secondary,
  ghost: styles.ghost,
  danger: styles.danger,
};

const SIZE_CLASS: Record<ButtonSize, string | undefined> = {
  sm: styles.sm,
  md: styles.md,
};

/** Compose the class list for a control, so links can look like buttons
 *  without a second copy of the styles. */
export function buttonClass(
  variant: ButtonVariant = "secondary",
  size: ButtonSize = "md",
  extra?: { block?: boolean; className?: string },
): string {
  return [
    styles.button,
    VARIANT_CLASS[variant],
    SIZE_CLASS[size],
    extra?.block ? styles.block : null,
    extra?.className,
  ]
    .filter(Boolean)
    .join(" ");
}

/**
 * The product's only button.
 *
 * `type` defaults to `"button"` rather than to the HTML default of `"submit"`.
 * Every accidental form submission this codebase has had came from a control
 * inside a `<form>` that meant to do something else, and defaulting the safe
 * way costs one explicit `type="submit"` on the handful that really submit.
 *
 * There is no `loading` prop. A button that swaps its own label for a spinner
 * changes width mid-click and drops the word the user was reading; callers
 * pass the busy label as children and set `disabled` themselves, which keeps
 * the wording specific ("Confirming…", "Creating…") instead of generic.
 */
export function Button({
  variant = "secondary",
  size = "md",
  block = false,
  className,
  type = "button",
  ...rest
}: {
  variant?: ButtonVariant;
  size?: ButtonSize;
  block?: boolean;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...rest}
      type={type}
      className={buttonClass(variant, size, { block, className })}
    />
  );
}
